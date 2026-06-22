#!/bin/sh
# NymeriaOS installer (front door).
#
#   curl -fsSL https://get.nymeriaos.com/install.sh | sh
#
# Lets you choose between three install tracks:
#   Slim   - simpler, best for a few users. Single process on SQLite, installed
#            with uv (which can fetch a matching Python for you). No Docker.
#   Full   - more robust, better multi-user support. Runs in Docker (the script
#            can install Docker for you on Linux).
#   Source - hackable: git clone plus an editable uv install, so code edits
#            (yours or the agent's own) apply on the next restart, with git
#            for diff/branch/revert safety.
#
# Cautious users: download and read this script before running it, e.g.
#   curl -fsSL https://get.nymeriaos.com/install.sh -o install.sh
#   less install.sh && sh install.sh
#
# Overridable via environment variables:
#   NYMERIA_BASE_URL              Where compose + .env template are served
#                                 (default: https://get.nymeriaos.com)
#   NYMERIA_IMAGE_NAMESPACE       GHCR namespace for the Full images
#                                 (default baked into the compose file)
#   NYMERIA_PYPI_SIMPLE_INDEX_URL Private index URL for the Slim/beta install
#   NYMERIA_INSTALL_MODE          "slim", "full", or "source" (same as the flags)
#   NYMERIA_REPO_URL              Git repo for the Source track (default:
#                                 https://github.com/ManningAskew7/NymeriaOS.git)
#   NYMERIA_SOURCE_DIR            Checkout dir for the Source track
#                                 (default: ~/NymeriaOS)
#   NYMERIA_UV_INSTALLER_SHA256   Pin+verify the uv installer (astral.sh) by hash
#   NYMERIA_DOCKER_INSTALLER_SHA256  Pin+verify the get.docker.com installer by hash
#
# The whole script is wrapped in functions and only invoked on the final line,
# so a truncated download cannot execute a partial command.

set -eu

# Restrict permissions on everything this installer writes. The Full track
# writes NYMERIA_SECRETS_KEY (the vault master key that decrypts every stored
# API key and OAuth token) into .env.docker, so it must not be world-readable
# on a shared host. 0600/0700 for created files/dirs; .env is also chmod'd
# explicitly below as belt-and-suspenders.
umask 077

NYMERIA_BASE_URL="${NYMERIA_BASE_URL:-https://get.nymeriaos.com}"
NYMERIA_REPO_URL="${NYMERIA_REPO_URL:-https://github.com/ManningAskew7/NymeriaOS.git}"
COMPOSE_FILE="docker-compose.single.published.yml"
ENV_FILE=".env.docker"
ENV_EXAMPLE=".env.docker.example"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    C_BOLD="$(printf '\033[1m')"; C_DIM="$(printf '\033[2m')"
    C_GREEN="$(printf '\033[32m')"; C_YELLOW="$(printf '\033[33m')"
    C_RED="$(printf '\033[31m')"; C_RESET="$(printf '\033[0m')"
else
    C_BOLD=""; C_DIM=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""
fi

info()  { printf '%s\n' "${C_GREEN}==>${C_RESET} $*"; }
warn()  { printf '%s\n' "${C_YELLOW}warning:${C_RESET} $*" >&2; }
err()   { printf '%s\n' "${C_RED}error:${C_RESET} $*" >&2; }
die()   { err "$@"; exit 1; }

have()  { command -v "$1" >/dev/null 2>&1; }

# Read a line from the controlling terminal so prompts work even when the
# script itself arrives on stdin (curl ... | sh).
ask() {
    _prompt="$1"; _default="$2"; _reply=""
    if [ -r /dev/tty ]; then
        printf '%s' "$_prompt" > /dev/tty
        IFS= read -r _reply < /dev/tty || _reply=""
    fi
    [ -n "$_reply" ] && printf '%s' "$_reply" || printf '%s' "$_default"
}

INTERACTIVE=1
[ -r /dev/tty ] || INTERACTIVE=0

# Offer to launch the guided setup wizard; shared by the slim and source tracks.
# `exec`s into `nymeria init` so the wizard replaces this process and owns the
# terminal. Reattach stdin to the terminal: under `curl ... | sh` the shell's
# stdin is the pipe, and the wizard refuses non-tty stdin.
maybe_run_wizard() {
    [ "$INTERACTIVE" -eq 1 ] || return 0
    _go="$(ask "Run the setup wizard now (nymeria init)? [Y/n]: " "y")"
    case "$_go" in n|N|no|No) ;; *) exec nymeria init < /dev/tty ;; esac
}

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
detect_os() {
    case "$(uname -s 2>/dev/null)" in
        Linux*)  OS="linux" ;;
        Darwin*) OS="macos" ;;
        *)       OS="other" ;;
    esac
}

fetch() {  # fetch <url> <dest>
    if have curl; then
        curl -fsSL "$1" -o "$2"
    elif have wget; then
        wget -qO "$2" "$1"
    else
        die "need curl or wget to download $1"
    fi
}

verify_sha256() {  # verify_sha256 <file> <expected_hex> -> 0 match, 1 mismatch, 2 no-tool
    _vf="$1"; _vexp="$2"; _vact=""
    if have sha256sum; then
        _vact="$(sha256sum "$_vf" | awk '{print $1}')"
    elif have shasum; then
        _vact="$(shasum -a 256 "$_vf" | awk '{print $1}')"
    elif have openssl; then
        _vact="$(openssl dgst -sha256 "$_vf" | awk '{print $NF}')"
    else
        return 2
    fi
    [ "$_vact" = "$_vexp" ]
}

# Fetch a remote installer to a temp file and run it. Avoids piping straight
# into a shell (a truncated download cannot execute a partial command) and
# supports opt-in pinning: when the matching *_SHA256 env var is set, the
# download is verified before it runs and aborts on mismatch.
run_remote_installer() {  # run_remote_installer <url> <label> <expected_sha256>
    _ri_url="$1"; _ri_label="$2"; _ri_exp="$3"
    _ri_tmp="$(mktemp 2>/dev/null || printf '%s' "/tmp/nymeria-installer.$$")"
    fetch "$_ri_url" "$_ri_tmp"
    if [ -n "$_ri_exp" ]; then
        if verify_sha256 "$_ri_tmp" "$_ri_exp"; then
            info "$_ri_label installer checksum verified."
        else
            _rc=$?
            rm -f "$_ri_tmp"
            if [ "$_rc" = "2" ]; then
                die "Cannot verify $_ri_label installer: no sha256sum/shasum/openssl available."
            fi
            die "$_ri_label installer SHA-256 mismatch (expected $_ri_exp). Aborting."
        fi
    else
        warn "Running the $_ri_label installer from $_ri_url without checksum verification."
        warn "To pin it, set its *_SHA256 env var before running this script."
    fi
    sh "$_ri_tmp"
    rm -f "$_ri_tmp"
}

# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------
choose_mode() {
    [ -n "${MODE:-}" ] && return 0
    if [ "$INTERACTIVE" -eq 0 ]; then
        MODE="slim"
        info "Non-interactive: defaulting to the Slim install. Pass --full for Docker, or --source for a hackable checkout."
        return 0
    fi
    printf '%s\n' "" > /dev/tty
    printf '%s\n' "${C_BOLD}Choose how to install NymeriaOS:${C_RESET}" > /dev/tty
    printf '%s\n' "  ${C_BOLD}1) Slim${C_RESET}   - simpler, best for a few users. Single process on SQLite," > /dev/tty
    printf '%s\n' "              installed with uv. No Docker required." > /dev/tty
    printf '%s\n' "  ${C_BOLD}2) Full${C_RESET}   - more robust, better multi-user support. Runs in Docker" > /dev/tty
    printf '%s\n' "              (Docker required; this script can install it for you on Linux)." > /dev/tty
    printf '%s\n' "  ${C_BOLD}3) Source${C_RESET} - hackable: a git checkout with an editable install, so" > /dev/tty
    printf '%s\n' "              code edits (yours or the agent's own) apply on restart." > /dev/tty
    _choice="$(ask "Enter 1, 2 or 3 [1]: " "1")"
    case "$_choice" in
        2|full|Full|FULL)         MODE="full" ;;
        3|source|Source|SOURCE)   MODE="source" ;;
        *)                        MODE="slim" ;;
    esac
}

# ---------------------------------------------------------------------------
# Slim track (uv)
# ---------------------------------------------------------------------------
ensure_uv() {
    if have uv; then return 0; fi
    info "Installing uv (https://astral.sh/uv) ..."
    run_remote_installer "https://astral.sh/uv/install.sh" "uv" "${NYMERIA_UV_INSTALLER_SHA256:-}"
    # uv installs to ~/.local/bin (or $XDG_BIN_HOME); make it visible now.
    [ -d "$HOME/.local/bin" ] && PATH="$HOME/.local/bin:$PATH"
    export PATH
    have uv || die "uv was installed but is not on PATH; open a new shell and re-run."
}

install_slim() {
    ensure_uv
    info "Installing the nymeriaos package with uv ..."
    if [ -n "${NYMERIA_PYPI_SIMPLE_INDEX_URL:-}" ]; then
        uv tool install nymeriaos --index "$NYMERIA_PYPI_SIMPLE_INDEX_URL"
    else
        uv tool install nymeriaos
    fi
    info "Installed. ${C_BOLD}nymeria${C_RESET} is on your PATH."
    maybe_run_wizard
    cat <<EOF

Next steps:
  nymeria init      # guided setup (provider, model, API key)
  nymeria doctor    # verify the install
  nymeria slim      # start the single-process backend
EOF
}

# ---------------------------------------------------------------------------
# Source track (git clone + editable uv install)
# ---------------------------------------------------------------------------
install_source() {
    have git || die "git is required for the Source install. Install git and re-run."
    ensure_uv
    SRCDIR="${NYMERIA_SOURCE_DIR:-$HOME/NymeriaOS}"
    # The global umask 077 exists to protect secret-bearing env files; a code
    # checkout must stay world-readable (normal 755/644) or the bind-mount
    # Docker shapes cannot read the source from inside the container (it runs
    # as its own non-root user). Scope the relaxed umask to the git commands.
    if [ -e "$SRCDIR/.git" ]; then
        info "Existing checkout at $SRCDIR; fast-forwarding ..."
        (umask 022; git -C "$SRCDIR" pull --ff-only) \
            || warn "Could not fast-forward $SRCDIR (local changes or a diverged branch); continuing with the current tree."
    else
        info "Cloning $NYMERIA_REPO_URL into $SRCDIR ..."
        (umask 022; git clone "$NYMERIA_REPO_URL" "$SRCDIR")
    fi
    info "Installing the backend as an editable uv tool ..."
    # --force replaces a previous PyPI (Slim) install of the same tool; the
    # editable install means edits under the checkout apply on the next
    # restart, no reinstall needed (reinstall only when dependencies change).
    uv tool install --force --editable "$SRCDIR/Nymeria"
    info "Installed. ${C_BOLD}nymeria${C_RESET} on your PATH runs the live code in $SRCDIR."
    maybe_run_wizard
    cat <<EOF

Next steps:
  nymeria init      # guided setup (provider, model, API key)
  nymeria doctor    # verify the install
  nymeria slim      # start the single-process backend (live code from $SRCDIR)

This track is for hacking on Nymeria, including letting the agent modify its
own source: the install is editable, so edits under $SRCDIR apply on the next
restart, and the git checkout gives you diff/branch/revert safety.

Update later with:
  git -C "$SRCDIR" pull --ff-only
  uv tool install --force --editable "$SRCDIR/Nymeria"   # only if dependencies changed
EOF
}

# ---------------------------------------------------------------------------
# Full track (Docker, single-container)
# ---------------------------------------------------------------------------
ensure_docker() {
    if have docker && docker info >/dev/null 2>&1; then return 0; fi
    if have docker; then
        die "Docker is installed but not running. Start Docker and re-run."
    fi
    case "$OS" in
        linux)
            warn "Docker is not installed."
            _go="$(ask "Install Docker now via get.docker.com (needs sudo)? [Y/n]: " "y")"
            case "$_go" in
                n|N|no|No) docker_fallback ;;
                *)
                    info "Installing Docker ..."
                    run_remote_installer "https://get.docker.com" "Docker" "${NYMERIA_DOCKER_INSTALLER_SHA256:-}"
                    have docker || die "Docker install did not complete."
                    docker info >/dev/null 2>&1 || die "Docker installed but the daemon is not running (you may need to log out/in for group changes, or 'sudo systemctl start docker')."
                    ;;
            esac
            ;;
        macos)
            err "Docker Desktop is required for the Full install on macOS."
            printf '%s\n' "  Install it with: brew install --cask docker   (then open Docker.app once)"
            printf '%s\n' "  or download from https://www.docker.com/products/docker-desktop/"
            docker_fallback
            ;;
        *)
            err "Docker is required for the Full install."
            printf '%s\n' "  Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
            docker_fallback
            ;;
    esac
}

docker_fallback() {
    if [ "$INTERACTIVE" -eq 1 ]; then
        _go="$(ask "Install the Slim (no-Docker) track instead? [Y/n]: " "y")"
        case "$_go" in n|N|no|No) die "Docker is required for the Full install." ;; esac
        MODE="slim"; install_slim; exit 0
    fi
    die "Docker is required for the Full install."
}

compose() {  # docker compose vs docker-compose shim
    if docker compose version >/dev/null 2>&1; then
        docker compose "$@"
    elif have docker-compose; then
        docker-compose "$@"
    else
        die "docker compose plugin not found."
    fi
}

gen_secret() {  # 44-char urlsafe-base64 Fernet key, no cryptography dep
    if have python3; then
        python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
    elif have openssl; then
        openssl rand 32 | openssl base64 | tr '+/' '-_' | tr -d '\n'; echo
    else
        echo ""
    fi
}

install_full() {
    ensure_docker
    WORKDIR="${NYMERIA_FULL_DIR:-$HOME/nymeria}"
    info "Setting up the Full (Docker) stack in $WORKDIR ..."
    mkdir -p "$WORKDIR"
    cd "$WORKDIR"

    info "Fetching compose file and env template from $NYMERIA_BASE_URL ..."
    fetch "$NYMERIA_BASE_URL/$COMPOSE_FILE" "$COMPOSE_FILE"

    if [ ! -f "$ENV_FILE" ]; then
        fetch "$NYMERIA_BASE_URL/$ENV_EXAMPLE" "$ENV_FILE"
        # Lock down the secret-bearing env file before writing the master key
        # into it (umask should already give 0600, but be explicit).
        chmod 600 "$ENV_FILE" 2>/dev/null || true
        _key="$(gen_secret)"
        if [ -n "$_key" ]; then
            printf '\nNYMERIA_SECRETS_KEY=%s\n' "$_key" >> "$ENV_FILE"
            info "Generated NYMERIA_SECRETS_KEY (at-rest credential encryption)."
        else
            warn "Could not generate NYMERIA_SECRETS_KEY (no python3/openssl); set it later in $ENV_FILE."
        fi
    else
        info "Reusing existing $ENV_FILE."
        chmod 600 "$ENV_FILE" 2>/dev/null || true
    fi

    [ -n "${NYMERIA_IMAGE_NAMESPACE:-}" ] && export NYMERIA_IMAGE_NAMESPACE

    info "Pulling and starting the container ..."
    compose -f "$COMPOSE_FILE" pull
    compose -f "$COMPOSE_FILE" up -d

    info "Waiting for the API to become healthy ..."
    _ok=0
    _i=0
    while [ "$_i" -lt 60 ]; do
        if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then _ok=1; break; fi
        _i=$((_i + 1)); sleep 2
    done
    if [ "$_ok" -eq 1 ]; then
        info "${C_BOLD}NymeriaOS is running${C_RESET} at http://localhost:8000"
    else
        warn "API did not report healthy yet. Check: cd $WORKDIR && $(compose_label) -f $COMPOSE_FILE logs -f"
    fi

    cat <<EOF

Next steps (in $WORKDIR):
  1. Add your LLM provider key to $ENV_FILE (LLM_PROVIDER, LLM_MODEL, and the
     matching key, e.g. ANTHROPIC_API_KEY), then restart:
       docker compose -f $COMPOSE_FILE restart
  2. Grab the first-run admin sign-in token:
       docker compose -f $COMPOSE_FILE exec nymeria-single cat /data/BOOTSTRAP_TOKEN.txt
  3. Manage the stack:
       docker compose -f $COMPOSE_FILE logs -f
       docker compose -f $COMPOSE_FILE down
EOF
}

compose_label() {
    if docker compose version >/dev/null 2>&1; then printf 'docker compose'; else printf 'docker-compose'; fi
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
NymeriaOS installer

Usage: install.sh [--slim | --full | --source] [--non-interactive] [-h|--help]

  --slim             Install the single-process (uv/SQLite) track.
  --full             Install the Docker (single-container) track.
  --source           Install from a git checkout (editable; for hacking on
                     Nymeria or letting the agent modify its own source).
  --non-interactive  Do not prompt; defaults to --slim unless another track
                     flag is given.

Environment overrides: NYMERIA_BASE_URL, NYMERIA_IMAGE_NAMESPACE,
NYMERIA_PYPI_SIMPLE_INDEX_URL, NYMERIA_INSTALL_MODE, NYMERIA_REPO_URL,
NYMERIA_SOURCE_DIR.
EOF
}

main() {
    MODE="${NYMERIA_INSTALL_MODE:-}"
    while [ $# -gt 0 ]; do
        case "$1" in
            --slim) MODE="slim" ;;
            --full) MODE="full" ;;
            --source) MODE="source" ;;
            --non-interactive|-y) INTERACTIVE=0 ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
        shift
    done

    detect_os
    info "NymeriaOS installer (${OS})"
    choose_mode

    case "$MODE" in
        slim)   install_slim ;;
        full)   install_full ;;
        source) install_source ;;
        *)      die "unknown install mode: $MODE" ;;
    esac
}

main "$@"

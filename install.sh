#!/bin/sh
# NymeriaOS installer (clone-free front door).
#
#   curl -fsSL https://get.nymeriaos.com/install.sh | sh
#
# Lets you choose between two install tracks:
#   Slim  - simpler, best for a few users. Single process on SQLite, installed
#           with uv (which can fetch a matching Python for you). No Docker.
#   Full  - more robust, better multi-user support. Runs in Docker (the script
#           can install Docker for you on Linux).
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
#   NYMERIA_INSTALL_MODE          "slim" or "full" (same as --slim/--full)
#
# The whole script is wrapped in functions and only invoked on the final line,
# so a truncated download cannot execute a partial command.

set -eu

NYMERIA_BASE_URL="${NYMERIA_BASE_URL:-https://get.nymeriaos.com}"
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

# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------
choose_mode() {
    [ -n "${MODE:-}" ] && return 0
    if [ "$INTERACTIVE" -eq 0 ]; then
        MODE="slim"
        info "Non-interactive: defaulting to the Slim install. Pass --full for Docker."
        return 0
    fi
    printf '%s\n' "" > /dev/tty
    printf '%s\n' "${C_BOLD}Choose how to install NymeriaOS:${C_RESET}" > /dev/tty
    printf '%s\n' "  ${C_BOLD}1) Slim${C_RESET} - simpler, best for a few users. Single process on SQLite," > /dev/tty
    printf '%s\n' "           installed with uv. No Docker required." > /dev/tty
    printf '%s\n' "  ${C_BOLD}2) Full${C_RESET} - more robust, better multi-user support. Runs in Docker" > /dev/tty
    printf '%s\n' "           (Docker required; this script can install it for you on Linux)." > /dev/tty
    _choice="$(ask "Enter 1 or 2 [1]: " "1")"
    case "$_choice" in
        2|full|Full|FULL) MODE="full" ;;
        *)                MODE="slim" ;;
    esac
}

# ---------------------------------------------------------------------------
# Slim track (uv)
# ---------------------------------------------------------------------------
ensure_uv() {
    if have uv; then return 0; fi
    info "Installing uv (https://astral.sh/uv) ..."
    if have curl; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif have wget; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        die "need curl or wget to install uv"
    fi
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
    if [ "$INTERACTIVE" -eq 1 ]; then
        _go="$(ask "Run the setup wizard now (nymeria init)? [Y/n]: " "y")"
        case "$_go" in n|N|no|No) ;; *) exec nymeria init ;; esac
    fi
    cat <<EOF

Next steps:
  nymeria init      # guided setup (provider, model, API key)
  nymeria doctor    # verify the install
  nymeria slim      # start the single-process backend
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
                    fetch "https://get.docker.com" /tmp/get-docker.sh
                    sh /tmp/get-docker.sh
                    rm -f /tmp/get-docker.sh
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
        _key="$(gen_secret)"
        if [ -n "$_key" ]; then
            printf '\nNYMERIA_SECRETS_KEY=%s\n' "$_key" >> "$ENV_FILE"
            info "Generated NYMERIA_SECRETS_KEY (at-rest credential encryption)."
        else
            warn "Could not generate NYMERIA_SECRETS_KEY (no python3/openssl); set it later in $ENV_FILE."
        fi
    else
        info "Reusing existing $ENV_FILE."
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

Usage: install.sh [--slim | --full] [--non-interactive] [-h|--help]

  --slim             Install the single-process (uv/SQLite) track.
  --full             Install the Docker (single-container) track.
  --non-interactive  Do not prompt; defaults to --slim unless --full is given.

Environment overrides: NYMERIA_BASE_URL, NYMERIA_IMAGE_NAMESPACE,
NYMERIA_PYPI_SIMPLE_INDEX_URL, NYMERIA_INSTALL_MODE.
EOF
}

main() {
    MODE="${NYMERIA_INSTALL_MODE:-}"
    while [ $# -gt 0 ]; do
        case "$1" in
            --slim) MODE="slim" ;;
            --full) MODE="full" ;;
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
        slim) install_slim ;;
        full) install_full ;;
        *)    die "unknown install mode: $MODE" ;;
    esac
}

main "$@"

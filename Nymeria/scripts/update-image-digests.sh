#!/usr/bin/env bash
# Refresh the digest pins for third-party images in docker-compose.yml.
#
# Pinning by `@sha256:<digest>` ensures we always pull the exact bytes we
# audited, immune to image-author tag-rewrite or registry compromise. This
# script pulls each tag, reads the registry's reported digest, and prints
# the lines you should paste into docker-compose.yml.
#
# Usage:
#   ./scripts/update-image-digests.sh
#
# Then update the `image:` lines in Nymeria/docker-compose.yml and commit
# with a note about why you bumped (CVE patch, feature, etc.). The searxng
# digest also appears in docker-compose.single.yml,
# docker-compose.single.published.yml, and the wheel asset copy of the latter
# (nymeria/setup/assets/); a test asserts all stay in lockstep.

set -euo pipefail

IMAGES=(
    "postgres:15-alpine"
    "redis:7-alpine"
    "caddy:2-alpine"
    "searxng/searxng:latest"
)

printf 'Pulling images to resolve registry digests…\n\n'
for img in "${IMAGES[@]}"; do
    docker pull --quiet "$img" > /dev/null
    # `docker buildx imagetools inspect` works for multi-arch refs, but
    # `docker images --digests` is sufficient for our single-arch use.
    digest="$(docker images --digests --no-trunc \
        --format '{{.Repository}}:{{.Tag}} {{.Digest}}' \
        | awk -v tag="$img" '$1 == tag { print $2; exit }')"
    if [[ -z "$digest" || "$digest" == "<none>" ]]; then
        printf 'ERROR: could not resolve digest for %s\n' "$img" >&2
        exit 1
    fi
    printf '  %-25s %s\n' "$img" "$digest"
done

printf '\nDate to record in compose comments: %s\n' "$(date -u +%Y-%m-%d)"

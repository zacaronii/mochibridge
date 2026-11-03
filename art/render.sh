#!/usr/bin/env bash
# Renders the icon SVGs into the PNG sizes the web UI and the extension use (needs rsvg-convert, or Docker).
set -euo pipefail
cd "$(dirname "$0")/.."
run() {
  if command -v rsvg-convert >/dev/null; then rsvg-convert "$@"; else
    # KB_HOST_PREFIX="<container prefix>=<host prefix>" when this runs inside a container (Docker-outside-of-Docker)
    local here; here=$(pwd); [ -n "${KB_HOST_PREFIX:-}" ] && here="${KB_HOST_PREFIX#*=}${here#"${KB_HOST_PREFIX%%=*}"}"
    docker run --rm -v "$here:/w" -w /w alpine:latest sh -c "apk add -q --no-cache rsvg-convert >/dev/null && rsvg-convert $* && chown $(id -u):$(id -g) ${*: -1}"
  fi
}
cp art/keeper.svg src/kbridge/web/static/favicon.svg
run -w 32 -h 32 art/keeper.svg -o src/kbridge/web/static/favicon-32.png
run -w 180 -h 180 art/keeper.svg -o src/kbridge/web/static/apple-touch-icon.png
mkdir -p extension/icons
for s in 16 32 48 128; do run -w $s -h $s art/keeper-extension.svg -o extension/icons/icon-$s.png; done
cp art/keeper-extension.svg extension/icons/icon.svg

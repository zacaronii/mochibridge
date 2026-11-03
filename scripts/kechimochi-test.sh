#!/usr/bin/env bash
# Throwaway kechimochi for integration tests and migration rehearsals: same image as your kechimochi container
# (or KB_KECHIMOCHI_IMAGE), private network, data on tmpfs unless DATA_DIR is given.
#   scripts/kechimochi-test.sh up [DATA_DIR]   -> prints the base URL
#       without DATA_DIR: empty tmpfs data (profile initialised); with DATA_DIR: that folder is mounted as
#       /data (e.g. a copy of your kechimochi data).
#   scripts/kechimochi-test.sh down
# NAME (default kechimochi-test) allows a second instance, e.g. NAME=kechimochi-restore.
#
# On a normal host the instance is published on 127.0.0.1 (a random port). When this runs inside a container
# (Docker-outside-of-Docker), set KB_SELF_CONTAINER to that container's name: it joins the private network and
# reaches the instance by name instead; KB_HOST_PREFIX ("<container prefix>=<host prefix>") translates DATA_DIR
# into the host path the Docker daemon sees.
set -euo pipefail
NAME=${NAME:-kechimochi-test}
NET=kb-test
SELF=${KB_SELF_CONTAINER:-}
PREFIX=${KB_HOST_PREFIX:-}

case "${1:-}" in
  up)
    image=${KB_KECHIMOCHI_IMAGE:-$(docker inspect kechimochi --format '{{.Image}}')}
    docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
    [ -n "$SELF" ] && { docker network connect "$NET" "$SELF" 2>/dev/null || true; }
    publish=(); [ -z "$SELF" ] && publish=(-p 127.0.0.1::3000)
    user=$(docker image inspect "$image" --format '{{.Config.User}}')
    uid=$(docker run --rm --entrypoint id "$image" -u "$user")
    if [ -n "${2:-}" ]; then
      dir=$(cd "$2" && pwd)
      host_dir=$dir
      [ -n "$PREFIX" ] && host_dir="${PREFIX#*=}${dir#"${PREFIX%%=*}"}"
      # run as your kechimochi container's user, which owns the copied data
      run_user=$(docker inspect kechimochi --format '{{.Config.User}}' 2>/dev/null || true)
      docker run --rm -d --name "$NAME" --network "$NET" "${publish[@]}" ${run_user:+--user "$run_user"} \
        -v "$host_dir:/data" "$image" >/dev/null
    else
      docker run --rm -d --name "$NAME" --network "$NET" "${publish[@]}" --tmpfs "/data:uid=$uid" "$image" >/dev/null
    fi
    if [ -n "$SELF" ]; then url="http://$NAME:3000"; else url="http://$(docker port "$NAME" 3000 | head -1)"; fi
    for _ in $(seq 1 30); do
      curl -sf "$url/api/version" >/dev/null && break
      sleep 1
    done
    if [ -z "${2:-}" ]; then
      curl -sf -X POST -H 'Content-Type: application/json' -d '{"username":"kbtest"}' \
        "$url/api/profiles/initialize" >/dev/null
    fi
    echo "$url"
    ;;
  down)
    docker stop "$NAME" >/dev/null 2>&1 || true
    if ! docker ps --format '{{.Names}}' | grep -q '^kechimochi-\(test\|restore\|rehearsal\)$'; then
      [ -n "$SELF" ] && { docker network disconnect "$NET" "$SELF" 2>/dev/null || true; }
      docker network rm "$NET" >/dev/null 2>&1 || true
    fi
    ;;
  *) echo "usage: $0 up [DATA_DIR] | down" >&2; exit 2 ;;
esac

#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077
docker compose version >/dev/null
mkdir -p data
if [[ ! -e .env ]]; then
    port=${ONDIER_PORT:-8788}
    origin=${ONDIER_ORIGIN:-http://localhost:$port}
    [[ "$port" =~ ^[0-9]{1,5}$ ]] && (( 10#$port >= 1 && 10#$port <= 65535 )) || {
        printf 'Choose a valid ONDIER_PORT.\n' >&2
        exit 1
    }
    [[ "$origin" != *$'\n'* && "$origin" != *$'\r'* ]] || exit 1
    installation_key=$(openssl rand -hex 32)
    (
        set -o noclobber
        printf 'ONDIER_PORT=%s\nONDIER_ORIGIN=%s\nONDIER_ACCESS_KEY=%s\n' "$port" "$origin" "$installation_key" > .env
    )
fi
if [[ ! -e data/setup-key ]]; then
    setup_key=$(openssl rand -hex 32)
    (
        set -o noclobber
        printf '%s\n' "$setup_key" > data/setup-key
    )
fi
docker compose -f compose.yaml --env-file .env up --build -d --wait
printf 'Open the Ondier address in .env. For the first account, use the key in data/setup-key.\n'

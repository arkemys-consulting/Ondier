#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
docker run --rm --network none \
  -e PYTHONPATH=/app -e ONDIER_WORKERS=0 -e ONDIER_DATA=/data \
  -e QUIRE_PODCAST_DATA=/data -e ONDIER_EMPTY_LIBRARY=1 \
  --tmpfs /data --entrypoint sh \
  --mount "type=bind,src=$PWD/tests,dst=/tests,readonly" \
  ondier-server:local -c 'set -e; for test in /tests/test-*.py; do python -B "$test"; done'

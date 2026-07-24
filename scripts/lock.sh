#!/usr/bin/env bash
#
# Regenerate (or verify) the hashed dependency lockfiles.
#
# Each service has a requirements.in (direct deps / intent) plus the shared
# constraints.txt; this script compiles those into a fully hashed, transitively
# pinned requirements.txt using `uv`, run inside a python:3.11-slim container so
# the resolved wheels and hashes match the build images (linux/amd64). No uv (or
# Docker-image change) is required on the host.
#
# Usage:
#   scripts/lock.sh                  # recompile every */requirements.in
#   scripts/lock.sh api-service ...  # recompile only the named service(s)
#   scripts/lock.sh --check          # CI mode: fail if any committed lock is stale
#
# A --check failure means the committed requirements.txt is no longer compatible
# with its requirements.in / constraints.txt inputs. Check mode preserves existing
# locked versions; dependency upgrades happen only in explicit generation mode.
set -euo pipefail

cd "$(dirname "$0")/.."

UV_VERSION="0.11.32"

mode="generate"
if [ "${1:-}" = "--check" ]; then
  mode="check"
  shift
fi

if [ "$#" -gt 0 ]; then
  targets=("$@")
else
  targets=()
  for f in */requirements.in; do
    targets+=("$(dirname "$f")")
  done
fi

echo "Mode: $mode | services: ${targets[*]}"

docker run --rm -v "$PWD:/work" -w /work \
  -e MODE="$mode" \
  -e TARGETS="${targets[*]}" \
  -e OWNER="$(id -u):$(id -g)" \
  -e UV_VERSION="$UV_VERSION" \
  python:3.11-slim bash -lc '
    set -euo pipefail
    pip install -q "uv==$UV_VERSION" 1>&2
    fail=0
    for s in $TARGETS; do
      if [ "$MODE" = "check" ]; then
        tmp="$(mktemp)"
        # uv prefers versions from an existing output file unless --upgrade is
        # supplied. Seed the temporary output from the committed lock so CI
        # validates input compatibility without performing surprise upgrades.
        cp "$s/requirements.txt" "$tmp"
        uv pip compile --quiet --no-cache "$s/requirements.in" --generate-hashes -o "$tmp" 1>&2
        # Compare ignoring the header comment (it records the output path/command).
        out="$(diff -u <(grep -v "^#" "$s/requirements.txt") <(grep -v "^#" "$tmp") || true)"
        if [ -n "$out" ]; then
          echo "STALE: $s/requirements.txt — run scripts/lock.sh" 1>&2
          echo "$out" 1>&2
          fail=1
        else
          echo "ok: $s" 1>&2
        fi
      else
        echo ">>> compiling $s" 1>&2
        uv pip compile --upgrade --no-cache "$s/requirements.in" --generate-hashes -o "$s/requirements.txt" 1>&2
      fi
    done
    if [ "$MODE" = "generate" ]; then
      chown "$OWNER" */requirements.txt
    fi
    exit $fail
  '

if [ "$mode" = "generate" ]; then
  echo "Done. Review the diff, then rebuild: docker compose up -d --build"
else
  echo "All lockfiles are up to date."
fi

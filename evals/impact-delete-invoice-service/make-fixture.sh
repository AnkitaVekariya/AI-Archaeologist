#!/usr/bin/env bash
# Builds the planted "shop" repo (with git history) in the empty eval workspace.
set -eu
FIXTURE="$(cd "$(dirname "$0")/../.." && pwd)/tests/fixture_shop.py"
[ -f "$FIXTURE" ] || { echo "fixture builder not found at $FIXTURE" >&2; exit 1; }
python3 "$FIXTURE" .

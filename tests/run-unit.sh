#!/usr/bin/env bash
# Portal unit/integration tests, run inside the SAME image that ships (python:3.12.14-slim +
# hash-locked requirements), so the results mean something for production. Before building,
# the locked dependency set is audited against the PyPI advisory database (pip-audit via
# uvx; needs network) so a known CVE in a pin fails the run instead of shipping.
#   bash tests/run-unit.sh            # pip-audit, build image, run pytest
#   bash tests/run-unit.sh -k kobo    # extra args go to pytest
#   SKIP_AUDIT=1 bash tests/run-unit.sh   # offline
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -z "${SKIP_AUDIT:-}" ]]; then
  command -v uvx >/dev/null || { echo "uvx not found (install uv) or set SKIP_AUDIT=1"; exit 1; }
  uvx pip-audit --require-hashes --strict -r librarian/requirements.lock
fi
docker build -q -t bookstack/librarian:test librarian/ >/dev/null
# Parallel (pytest-xdist): each worker is its own process, and conftest gives every process its own
# temporary databases and folders, so workers never share state. UNIT_WORKERS=0 runs them one at a
# time (to compare, or to chase a test that leans on another's leftovers); default: one per core.
workers="${UNIT_WORKERS:-auto}"
par=""; [ "$workers" = 0 ] || par="-n $workers"
# scripts/ too, read-only: test_canary loads scripts/synthetic.py (host-side, stdlib only)
docker run --rm -e LIBRARIAN_TEST=1 -e PAR="$par" -v "$PWD/librarian/tests:/app/tests:ro" -v "$PWD/scripts:/scripts:ro" bookstack/librarian:test \
  sh -c 'pip install -q pytest pytest-xdist >/dev/null 2>&1 && python -m pytest -q -p no:cacheprovider $PAR tests "$@"' -- "$@"

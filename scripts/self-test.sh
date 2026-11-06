#!/usr/bin/env bash
# Builds and tests everything in this repo. Used as the gate's test-command in the
# self-test workflow, so it covers both the testbed (npm) and the action itself (pip).
set -euo pipefail

echo "== Reviewer unit tests =="
python -m pip install --quiet --disable-pip-version-check -r requirements.txt pytest
python -m pytest -q

echo "== Testbed =="
cd testbed
npm ci --no-audit --no-fund
npm test

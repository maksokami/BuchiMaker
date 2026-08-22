#!/usr/bin/env bash
# Regenerates requirements.txt / requirements-dev.txt (hash-pinned lockfiles)
# from requirements.in / requirements-dev.in (the human-edited, loosely
# pinned dependency lists). Run this after changing a .in file, and commit
# the resulting lockfile diff in the same change — see "Dependency
# lockfile" in docs/backend_architecture.md for why these are two separate
# files rather than one.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python -m pip install --quiet pip-tools

pip-compile --generate-hashes --output-file=requirements.txt requirements.in
pip-compile --generate-hashes --constraint=requirements.txt --output-file=requirements-dev.txt requirements-dev.in

echo "Lockfiles regenerated. Review the diff, then install with:"
echo "  pip install --require-hashes -r requirements.txt -r requirements-dev.txt"

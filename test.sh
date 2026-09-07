#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
python3 -m unittest discover -s tests -v
python3 -m py_compile git_logic.py

echo "All tests passed."

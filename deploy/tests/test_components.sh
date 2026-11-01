#!/usr/bin/env bash
set -euo pipefail
python3 "$(dirname "$0")/test_components.py"
echo 'PASS components'

#!/usr/bin/env bash
# Run all tests in the project using pytest in the .venv

# Exit immediately if a command exits with a non-zero status
set -e

# Navigate to the project root directory
cd "$(dirname "$0")/.."

echo "======================================"
echo "      Running Social Sentiment Tests  "
echo "======================================"

.venv/bin/pytest "$@"

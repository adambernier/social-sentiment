#!/usr/bin/env bash
# Watch for Python file changes and run pytest automatically

# Navigate to the project root directory
cd "$(dirname "$0")/.."

echo "======================================"
echo "  Watching Python files for changes   "
echo "  Auto-running tests via run_tests.sh"
echo "  Press Ctrl+C to stop                "
echo "======================================"

# Run watchmedo to monitor changes and run tests
.venv/bin/watchmedo shell-command \
    --patterns="*.py" \
    --ignore-directories \
    --ignore-patterns="*/.venv/*;*/node_modules/*;*/.git/*" \
    --recursive \
    --command="scripts/run_tests.sh" \
    .

#!/bin/bash

# Vigil GUI Launch Script

# Navigate to the project root
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

# Add project root to PYTHONPATH so 'import vigil' works correctly
export PYTHONPATH="$PYTHONPATH:$PROJECT_ROOT"

echo "------------------------------------------------"
echo "Starting Vigil (Combined Mode) on http://localhost:8080"
echo "------------------------------------------------"

python3 vigil/core/engine.py --config config.yaml --port 8080
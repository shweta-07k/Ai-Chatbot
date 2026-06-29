#!/usr/bin/env bash
# Render.com build — staged installs to avoid pip ResolutionImpossible conflicts.
set -euo pipefail

python -m pip install --upgrade "pip>=24,<26" "setuptools>=70,<82" wheel

echo "Installing CPU-only PyTorch..."
python -m pip install --no-cache-dir "torch==2.11.0" --index-url https://download.pytorch.org/whl/cpu

echo "Installing application dependencies..."
python -m pip install --no-cache-dir -r requirements-render.txt

echo "Render build complete."

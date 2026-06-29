#!/usr/bin/env bash
# Render.com build script — installs CPU PyTorch first to avoid OOM/timeouts.
set -euo pipefail

pip install --upgrade pip setuptools wheel

echo "Installing CPU-only PyTorch..."
pip install --no-cache-dir "torch==2.11.0" --index-url https://download.pytorch.org/whl/cpu

echo "Installing application dependencies..."
pip install --no-cache-dir -r requirements-render.txt

echo "Render build complete."

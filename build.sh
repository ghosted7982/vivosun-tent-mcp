#!/usr/bin/env bash
# Builds the Lambda bundle into ./build for Linux arm64 (no Docker needed).
set -euo pipefail
cd "$(dirname "$0")"
rm -rf build && mkdir build
python3 -m pip install -q -r requirements.txt -t build \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.12 --only-binary=:all: --upgrade
cp -r src/* build/
find build -name "__pycache__" -prune -exec rm -rf {} +
echo "built ./build"

#!/usr/bin/env bash
# Build script for Render.com

set -o errexit

echo "Installing Python dependencies..."
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

echo "Build completed successfully!"

#!/usr/bin/env bash
# Render.com build script

set -o errexit

# Install system dependencies for asyncpg
apt-get update
apt-get install -y gcc python3-dev libpq-dev

# Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt

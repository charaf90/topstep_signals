#!/usr/bin/env bash
# Wrapper bash pour la régénération des golden masters.
set -e
cd "$(dirname "$0")/../.."
python tests/golden/regenerate.py

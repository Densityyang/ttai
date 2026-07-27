#!/usr/bin/env bash
set -euo pipefail

uv run python -m compileall src main.py

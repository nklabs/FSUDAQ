#!/bin/bash
# Create the Python environment the Online Dashboard button uses (online/venv).
cd "$(dirname "$0")" || exit 1
python3 -m venv venv && venv/bin/pip install --disable-pip-version-check -r requirements.txt && echo "online/venv ready"

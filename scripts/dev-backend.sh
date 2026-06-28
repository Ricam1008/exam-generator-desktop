#!/bin/sh
set -e
cd "$(dirname "$0")/../backend"
python3 -m exam_backend.cli serve --port 8766

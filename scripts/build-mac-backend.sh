#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
PYINSTALLER_CONFIG_DIR=/private/tmp/exam-pyinstaller "${EXAM_BUILD_PYTHON:-.venv-package/bin/python}" -m PyInstaller --noconfirm --clean --onedir --name exam-backend-v2 \
  --distpath backend/sidecar-bin --workpath backend/build/v2 \
  --paths backend --add-data backend/exam_backend/templates:exam_backend/templates \
  --collect-all docling --collect-all docling_core \
  --collect-all latex2mathml --collect-all docx --collect-all pptx \
  --collect-all pypdfium2 --collect-all ocrmac \
  --recursive-copy-metadata docling-slim --recursive-copy-metadata ocrmac \
  --copy-metadata python-docx --copy-metadata python-pptx \
  --copy-metadata pypdf --copy-metadata pdfplumber --copy-metadata Pillow \
  --copy-metadata httpx --copy-metadata keyring --copy-metadata PyJWT \
  --hidden-import keyring.backends.macOS --hidden-import jwt.algorithms \
  --hidden-import Vision --hidden-import Quartz --hidden-import Foundation \
  scripts/backend_entry.py

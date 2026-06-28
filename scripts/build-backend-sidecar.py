#!/usr/bin/env python3
"""Build the packaged Python backend sidecar."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
ENTRY = BACKEND / "exam_backend" / "cli.py"
TEMPLATES = BACKEND / "exam_backend" / "templates"
SIDECAR_DIR = BACKEND / "sidecar-bin"
NAME = "exam-generator-backend"


def main() -> int:
    sep = ";" if os.name == "nt" else ":"
    shutil.rmtree(SIDECAR_DIR, ignore_errors=True)
    SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--name",
        NAME,
        "--distpath",
        str(SIDECAR_DIR),
        "--workpath",
        str(BACKEND / "build" / "pyinstaller"),
        "--specpath",
        str(BACKEND / "build"),
        "--paths",
        str(BACKEND / "exam_backend"),
        "--add-data",
        f"{TEMPLATES}{sep}templates",
        str(ENTRY),
    ]
    try:
        subprocess.run(cmd, cwd=ROOT, check=True)
    except subprocess.CalledProcessError as exc:
        print("Backend sidecar build failed. Install PyInstaller with: python -m pip install pyinstaller", file=sys.stderr)
        return exc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

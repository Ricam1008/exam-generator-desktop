"""PyInstaller entry point; package imports work in developer and bundled runs."""
from exam_backend.cli import main

if __name__ == '__main__':
    raise SystemExit(main())

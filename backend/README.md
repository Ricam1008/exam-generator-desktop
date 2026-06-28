# Backend

Run locally:

```bash
python3 -m pip install marker-pdf pypdf
python3 -m exam_backend.cli serve --port 8766
```

Marker is the required primary PDF parser. The legacy parsers, including `pypdf`, remain as per-PDF fallbacks if Marker is installed but fails on a specific file.

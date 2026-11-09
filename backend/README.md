# Backend

Run from the repository root with the isolated development interpreter:

```sh
.venv-package/bin/python -m exam_backend.cli serve --port 0
```

The first stdout line is the private startup handshake containing the actual loopback URL and session token. The desktop launcher consumes it; do not share or log it. Control endpoints require `Authorization: Bearer <session token>`. Browser previews use separate, narrowly scoped capabilities.

Dependencies and reproducible Mac build commands are documented in the main README and `requirements-mac.lock`. Word/PowerPoint use Docling Slim; scanned pages/images use macOS Vision. No model download is performed by normal extraction.

The legacy standalone generator CLI expects an output workspace as `--root`; the desktop service is responsible for protecting original material and staging updates.

# Exam Generator Desktop

A simple local desktop app for generating study exams from university PDF folders.

This repository is separate from the production exam workspace. Do not commit PDFs or generated exams.

## MVP

- Select an input folder containing course subfolders and PDFs.
- Select a separate output folder.
- Check Ollama, model availability, and Marker PDF parser availability.
- Generate one example exam, all exams, or final exams.
- Preview generated results locally.

## Required local tools

- Python 3.10+
- Ollama with at least one usable model
- Marker for PDF parsing

Install Marker:

```bash
python3 -m pip install marker-pdf
```

If Marker/PyTorch installation fails, install PyTorch for your platform using the selector at https://pytorch.org/get-started/locally/ and then install `marker-pdf` again.

Notes:

- macOS: Apple Silicon may use PyTorch/MPS when available. The first Marker run can be slow while models load.
- Windows: make sure Python scripts are on `PATH` so `marker_single.exe` can be found, then restart the app.
- Marker is intentionally not bundled into the Tauri app because PyTorch and model files are large and platform-sensitive.
- The old PDF parsers remain as a per-PDF fallback if Marker is installed but fails on a specific PDF.

## Development

Start the Python backend first:

```bash
cd backend
python3 -m pip install --user pypdf marker-pdf
python3 -m exam_backend.cli serve --port 8766
```

Then start the desktop UI:

```bash
cd apps/desktop
npm install
npm run dev
```

Full Tauri packaging requires Rust/Cargo. The current milestone validates the backend and React/Vite UI first; bundling the Python backend as a sidecar is a later packaging phase.

## Checks

```bash
python3 -m unittest tests.backend.test_backend_safety -v
cd apps/desktop && npm run build
```

## Safety

Input folders are read-only. Generation materializes PDFs into a separate output project folder and writes exams there.

# Exam Generator Desktop

A simple local desktop app for generating study exams from university PDF folders.

This repository is separate from the production exam workspace. Do not commit PDFs or generated exams.

## MVP

- Select an input folder containing course subfolders and PDFs.
- Select a separate output folder.
- Check Ollama and model availability.
- Generate one example exam, all exams, or final exams.
- Preview generated results locally.

## Development

Start the Python backend first:

```bash
cd backend
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

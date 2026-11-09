# Contributing

The app targets Apple Silicon macOS with Python 3.12, Node 22 and Tauri 2. Follow the [development and build instructions](README.md#entwicklung-und-build) to create the isolated document runtime and install the locked frontend dependencies.

Before submitting a change, run the [relevant checks](README.md#prüfen). GitHub runs source backend tests and frontend tests/builds on Linux with simulated providers. Four native document tests require macOS Vision, the document dependencies and `EXAM_DOCUMENT_RUNTIME_TESTS=1`; they are skipped in Linux CI. Packaged generation and OCR smoke tests remain separate Mac checks.

Use `EXAM_GENERATOR_DATA_DIR` to isolate application state in tests. Keep the real `HOME` intact for macOS Keychain tests. The packaged generation smoke creates and removes its own temporary dummy credential; never read or overwrite an existing credential for a test.

Keep original material, generated exams, personal run logs, credentials, model weights, virtual environments and installers out of commits. Use synthetic fixtures, preserve the [safety boundaries](docs/SAFETY.md), and keep model quality claims separate from mocked transport or synthetic OCR results.

Describe the observable behavior, relevant checks and remaining limitations in a pull request. New provider options must reach generation, calibration, result metadata and reuse/estimate identities where applicable. Do not run paid inference, download models or switch providers automatically as part of a test or analysis.

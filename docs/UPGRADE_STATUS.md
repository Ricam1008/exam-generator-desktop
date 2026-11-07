# Mac app upgrade — checkpoint 1

The upgrade is implemented in source and remains under integration verification.

Implemented: German four-area interface, provider profiles, DeepSeek/custom APIs, official ChatGPT sign-in implementation, protected credential storage, local document extraction/OCR, per-run generation context, transactional outputs, source identity/fingerprints, scoped previews, and saved study attempts.

Fresh checks: 97 backend tests ran, 93 passed and four optional native-document tests skipped in the restricted unit run; those four native runtime tests passed separately with normal macOS access. Frontend build and two frontend tests passed. Rust check passed. The dependency audit reported zero findings.

The bundled backend was built. Its initial launch check was blocked by the restricted process permission to bind a loopback socket; repeat with normal macOS access. No final app/DMG or release-readiness claim yet.

Remaining verification and completion: integrated HTTP security and preview/attempt tests; default/fresh finals and partial-failure behavior; complete browser/native workflow and bounded visual review; packaged document tools and credential store; app/DMG build and smoke test. Enhanced Granite OCR and cloud-image processing are not enabled. Real ChatGPT eligibility and live paid inference were not tested.

Original material and existing generated outputs are preserved. Model weights have not been downloaded. Build files and credentials are excluded from Git.

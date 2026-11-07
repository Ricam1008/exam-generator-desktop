# Mac app upgrade — checkpoint 2

Implemented in source: German four-area workflow; Ollama, DeepSeek, compatible APIs and official ChatGPT sign-in; Keychain credentials; PDF/Office/text/image extraction; native OCR; source IDs/fingerprints; atomic output replacement; cancellation; scoped previews; and saved attempts.

Fresh verification: all 120 backend tests passed with normal macOS permissions, including native document tests and 18 real HTTP/subprocess integration tests. Frontend build and all four frontend tests passed. Rust check passed before the latest shutdown-deadline-only edit. Dependency audit reported zero findings.

The standalone document backend passed 18/18 smoke checks after bundling missing latex2mathml resources. Native Mac app startup, source scan and extraction review were visually checked. Browser workflow with a local simulated AI verified custom counts, generation, none-correct scoring, grading, restored attempts and invalidation after answer edits. This was not a live paid-provider test.

The backend package was reduced from about 1.5 GB to 165 MB using Docling's model-free Office dependencies. Native OCR and Granite-Docling 258M were compared on the same six synthetic pages; native OCR was faster and had better text/order scores. Granite is not enabled or bundled. The measurement and model revision are in docs/verification/ocr-comparison.json. These simple fixtures do not establish performance on difficult lecture scans.

Remaining delivery checks: rebuild the latest source into the app/DMG, rerun standalone generation/Keychain smoke, validate final installer and reopen behavior, then refresh these notes. Real ChatGPT eligibility and real paid inference remain unverified. Cloud-image extraction is not offered.

Original material and existing outputs are preserved. Credentials, model weights, local state and build artifacts are excluded from Git.

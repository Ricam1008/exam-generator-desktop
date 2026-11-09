# Safety boundaries

- Original material remains unchanged. Validate the real output workspace after resolving links before any output/probe write.
- Identify workspaces by input and output location; identify sources by stable relative-path IDs and content fingerprints.
- Generate into a separate staged workspace. Check cancellation before commit, preserve the previous workspace, and restore it if replacement fails.
- One analysis, calibration or generation job at a time. Generation allows at most two exams and four active provider requests through a shared, cancellation-aware limit. Stopping remains a state until all workers finish cleanup. Remote providers may still charge accepted work.
- Local control requests need an unpredictable per-launch bearer token and allowed origin/host. Preview cookies never authorize control requests.
- Previews can access only registered workspace artifacts. Attempts and grading are bound to the current exam fingerprint. Reject traversal, hidden backups and external symlinks.
- Credentials belong in the system protected store. Never serialize them into profiles, manifests, exams or logs. Bind saved credentials to their configured provider endpoint.
- Source text is untrusted evidence; model output must pass question/boolean/reference validation before publication.
- Normal logs omit source bodies and raw responses. Explicit diagnostic logs may include them; redact credentials.
- No silent provider fallback, automatic cloud OCR or model download. Never commit study documents, generated exams, credentials, local state or build output.

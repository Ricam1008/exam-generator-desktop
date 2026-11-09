# Material analysis and generation planning

The preparation flow describes the selected workload before inference: planned exams, readable material, reusable outputs, a measured time range and suggested concurrency. Exam counts follow the same grouping rules as generation. Runtime and model suitability remain estimates with visible evidence.

Implemented and verified in the Mac app on 2026-11-09. See the [verification status](UPGRADE_STATUS.md) and [packaged app checks](verification/app-update-2026-11-09.json).

## Preparation flow

1. **Material prüfen** discovers supported files and lets the user select sources and edit course assignments. Discovery makes no AI requests.
2. **Material analysieren** starts a cancelable local extraction job and reads available model metadata. Cached extraction is reused when the fingerprint and extractor settings match.
3. The preparation summary lists planned, valid, new, reusable and unavailable exams; source paths; question targets; nominal request counts; document descriptions; and reading warnings.
4. An optional **Modell für diese Aufgabe testen** action uses synthetic German facts with known answer keys. A separate opt-in compares equal work at one and two concurrent requests. These actions may incur provider costs.
5. Generation validates the preparation signature and source fingerprints again. It reports completed, active and queued exams and refreshes the remaining-time estimate as exams complete and measurements become available.

Changes to selection, course assignments, OCR, model, language, difficulty or question targets invalidate the relevant preparation. Concurrency changes recalculate scheduling estimates without changing the exam-content reuse signature. DeepSeek thinking effort changes content, estimate and calibration identities.

## Exam grouping and reuse

Example mode uses the first selected source in discovery order. Per-document mode produces one exam per selected source. Finals mode produces one exam per distinct effective course assignment. Groups such as `Module 2` and `Module 2/Archiv` remain separate unless the user changes their assignments.

Unchanged exams are reusable only when content fingerprints, generation settings and the published exam file match. The summary distinguishes planned groups from groups with usable extraction. Files that fail extraction remain visible as material warnings; groups without usable sources are unavailable.

`planning.task_groups`, `exam_record`, `is_reused`, `source_entries` and `describe_tasks` are shared by preparation and generation. Target planning reuses the existing per-document planner and finals adaptive-target rules.

## Local material descriptions

| Description | Meaning |
| --- | --- |
| Length | File bytes, extracted characters and words, actual PDF pages and all PPTX slides, including empty final slides. Word uses paragraphs/sections rather than invented page numbers. |
| Images | Placed embedded raster images and unique assets where supported. Repeated logos can produce multiple placements. PDF vector diagrams are not reliably counted as raster images. |
| Tables | Structural tables where exposed by the extractor. Counts remain unknown when they cannot be established reliably. |
| Readability | Native text versus OCR, extraction failures, image/readability warnings and cached/fresh extraction. Word count is not an OCR accuracy score. |
| Alternatives | Identical file fingerprints and matching names in different formats are flagged for review. Files are not removed or merged automatically. |

Unknown values appear as “nicht ermittelt”, not zero. Aggregates expose how many documents lack a measurement. Image presence and OCR text do not establish understanding of a diagram: generation currently receives extracted text.

Metadata collection uses bounded Office archives/XML and existing extraction safeguards. A failed description probe does not discard otherwise useful text. Source fingerprints are rechecked before caching changed extraction.

## Execution location and context

The model probe distinguishes local, cloud, remote and unknown execution. An Ollama connection may relay to a cloud model, and a compatible API may run on another machine. A loopback address or model name alone is not proof of local inference.

Ollama discovery reads supported model, running-model, tag and server-version metadata without loading or downloading a model. Identity includes available digest, quantization, context, runtime and relevant hardware information. Local Mac hardware is not attributed to a remote server. Missing metadata remains unknown; the UI permits an explicit location choice without overriding evidence of a cloud relay.

Prompt fit includes instructions, source text and output reserve. The current token approximation uses three characters per token; it is not an exact tokenizer. Preparation cannot know the final blueprint or previously generated stems, so context fit remains provisional. The runtime checks the actual prompt before each request. Oversized known-context requests stop with guidance rather than silently dropping required input.

Small models are not accepted or rejected by parameter count. The original MiniCPM-2B configuration, for example, declares a 4,096-position context; different MiniCPM variants must be identified separately. A model that fits a prompt still needs a task sample to assess its output.

## Model samples

The bounded synthetic sample requests a blueprint, six MC questions and four open questions. Known facts cover calculation, multi-correct answers, a contradictory claim and information that was not collected. Parsing is checked without repair, and answer keys are checked against independent fixture facts.

Results distinguish `Nicht geprüft`, `Stichprobe bestanden`, `Mit Einschränkungen` and `Für diese Konfiguration ungeeignet`. A connection test or valid JSON alone cannot establish task quality. Questions and model-marked answers remain available for human review of German fluency, explanation fidelity, ambiguity and difficulty.

The sample is German and uses a limited factual task. It cannot confirm another language or demanding exam quality. Results identify that scope, and timing evidence is not reused across languages. A model's first sample may reveal its configured context after loading; the stored identity accounts for that without treating changed model weights as equivalent evidence.

## Runtime estimates

The estimator uses numerical observations from comparable requests rather than PDF file size or a universal speed assigned to a model name. History is separated by model/runtime identity, language, difficulty, mode, concurrency and DeepSeek thinking effort. Records omit prompts, source bodies, credentials and raw model responses.

Finals have sequential blueprint chunks followed by independent MC/open chains; batches within each chain remain ordered. Nominal question calls are `ceil(target_mc / mc_batch_size) + ceil(target_open / open_batch_size)`, plus blueprint calls. Retries, invalid/duplicate output and reduced batches add work, so nominal counts are lower bounds.

The estimator schedules dependencies through exam slots and the shared request limit. It does not simply divide sequential duration by four. Ranges include a retry reserve and are labelled heuristic; sparse or synthetic evidence receives a wider range. Without matching observations or a usable calibration, the UI says “noch nicht kalibriert”. Extraction duration is reported separately.

A successful synthetic sample can bootstrap a provisional estimate. Real phase observations replace synthetic observations as they become available. Reused outputs and failed/canceled exams do not masquerade as complete successful generation measurements. Missing provider timing/token fields remain missing.

## Concurrency and thinking controls

| Execution | Initial recommendation |
| --- | --- |
| Cloud | Two exams and up to four requests; actual account limits may be unknown. |
| Local | One exam and one request. A valid equal-work sample that is at least 15% faster with two requests can recommend two, with a warning to observe memory and quality on larger tasks. |
| Remote or unknown | One exam and one request until metadata or measurements support another choice. |

The UI caps generation at two exam workers and four active provider requests. One cancellation-aware request semaphore is shared through the runtime context, including nested MC/open/chunk calls. Only provider calls acquire it; parent tasks waiting for children do not hold permits. Cancellation and exceptions release them.

DeepSeek defaults to Low thinking. Off, Low, High and Max are explicit choices; Medium maps to High at the provider and is not shown as a separate level. The app remembers the choice, uses it in generation and calibration, and records it on exams for subsequent grading through the same connection. Lower effort's cost and quality advantages require a representative comparison; they are not guaranteed by the selector.

## Implementation map

| Module | Responsibility |
| --- | --- |
| `planning.py` | Selection, shared grouping/reuse, request limits, workload descriptions, numerical history and dependency-aware estimates |
| `documents.py` | Cancelable local extraction, structural metadata and content-based cache |
| `model_checks.py` | Read-only model discovery, context estimates and explicitly requested synthetic samples |
| `runtime.py` | Shared request budget, cancellation, phase observations, token summaries and atomic output helpers |
| `cli.py` | One active analysis/calibration/generation job, frozen settings, result publication and bounded state |
| `service.py` | Authenticated `/api/preflight`, `/api/calibrate`, `/api/generate` and existing polling/cancel routes |
| `App.tsx` | German preparation screen, controls, sample review, stale-result guards and progress |

See [safety boundaries](SAFETY.md) for credential, preview and original-file protection, and [contribution checks](../CONTRIBUTING.md) for source versus native package verification.

## Primary references

- [Ollama timing fields](https://docs.ollama.com/api/usage)
- [Ollama concurrency and memory](https://docs.ollama.com/faq)
- [Ollama running-model metadata](https://docs.ollama.com/api/ps)
- [Ollama cloud execution](https://docs.ollama.com/cloud)
- [Original MiniCPM-2B context configuration](https://huggingface.co/openbmb/MiniCPM-2B-sft-bf16/blob/main/config.json)
- [DeepSeek thinking controls](https://api-docs.deepseek.com/guides/thinking_mode/)

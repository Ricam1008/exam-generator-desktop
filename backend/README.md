# Backend

Run from the repository root with the isolated development interpreter:

```sh
.venv-package/bin/python -m exam_backend.cli serve --port 0
```

The first stdout line is the private startup handshake containing the actual loopback URL and session token. The desktop launcher consumes it; do not share or log it. Control endpoints require `Authorization: Bearer <session token>`. Browser previews use separate, narrowly scoped capabilities.

Dependencies and reproducible Mac build commands are documented in the main README and `requirements-mac.lock`. Word/PowerPoint use Docling Slim; scanned pages/images use macOS Vision. No model download is performed by normal extraction.

The legacy standalone generator CLI expects an output workspace as `--root`; the desktop service is responsible for protecting original material and staging updates.

## Question quality and source evidence

New exams use `learning-objectives-evidence-v1`. Generators select central learning
objectives and may return fewer questions than requested. Incidental empirical
numbers, study/sample trivia, slide layout and example thresholds are excluded;
calculation and interpretation tasks supply their required data in the question.
A separate model request reviews each batch of up to six candidates for relevance,
answer-key correctness, self-contained solvability, ambiguity, duplication and
support for the entire solution. Missing references can be repaired in that review.
The application validates every returned block ID and verbatim quote against a
local source catalogue (only Unicode/whitespace normalization is permitted).
A missing, malformed or negative review excludes the question. A review transport
failure aborts generation instead of publishing unchecked drafts. Refill candidates
pass the same gate; an old exam remains recoverable if regeneration fails.

The HTML shows document names, page/slide/paragraph/section locators and every
supporting passage. Existing saved exams are not rewritten. The policy version is
part of the content signature, so new runs do not reuse pre-policy exams. Review
requests add inference time/cost and are represented in the workload plan; the
semantic judgment remains model-dependent, unlike deterministic quote/ID checking.

Regression cases: `tests/backend/test_question_quality.py` includes the BGF feedback
examples and transport/publication checks. Simulated reviewers establish the gate's
behavior, not real model quality. No private lecture source is stored in this repo.

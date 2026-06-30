#!/usr/bin/env python3
"""Generate one brutal final exam per course from existing extracted source text."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import generate_exams


DEFAULT_ENDPOINT = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "gemma4:31b-cloud"
BLUEPRINT_CHARS = 30000
QUESTION_SOURCE_CHARS = 18000
DebugLogger = Callable[[str, str], None]


FINAL_SYSTEM_PROMPT = """You are an expert university final-exam writer.

Create very hard but fair final-exam material from university lecture source text.
Use only the provided source material and course blueprint. Do not invent facts.
Write all generated final-exam content in German, even when the source text is partly or fully English.
Keep established technical terms, theory names, study names, author names, formulas, and quoted source phrases in their original language when that is clearer or source-faithful.
Questions must test deep understanding, distinctions, mechanisms, applications, and transfer.
Avoid trivial recognition questions unless the concept is foundational.
Distractors must be plausible and reflect nearby concepts or common misconceptions.

Return valid JSON only. No markdown. No prose outside JSON."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate one hard final exam per course.")
    parser.add_argument("--root", required=True, help="Root folder containing course folders.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing final-exam folders.")
    parser.add_argument("--only-folder", help="Generate only one course folder by exact name.")
    parser.add_argument("--target-mc", type=int, default=120)
    parser.add_argument("--target-open", type=int, default=30)
    parser.add_argument("--mc-batch-size", type=int, default=15)
    parser.add_argument("--open-batch-size", type=int, default=5)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--retries", type=int, default=2)
    return parser.parse_args()


def count_words(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE))


def course_dirs(root: Path, only_folder: str | None) -> list[Path]:
    dirs = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if not child.is_dir() or not (child / "exams").is_dir():
            continue
        if only_folder and child.name.casefold() != only_folder.casefold():
            continue
        dirs.append(child)
    return dirs


def source_entries(course_dir: Path) -> list[dict[str, Any]]:
    entries = []
    for exam_json in sorted((course_dir / "exams").glob("*/exam.json"), key=lambda p: p.parent.name.casefold()):
        if exam_json.parent.name == "final-exam":
            continue
        source_path = exam_json.parent / "source.txt"
        if not source_path.exists():
            continue
        data = json.loads(exam_json.read_text(encoding="utf-8"))
        metadata = data.get("metadata", {})
        audit = data.get("audit") if isinstance(data.get("audit"), dict) else {}
        text = source_path.read_text(encoding="utf-8", errors="replace").strip()
        words = count_words(text)
        entries.append(
            {
                "exam_folder": exam_json.parent.name,
                "source_pdf": metadata.get("source_pdf") or exam_json.parent.name,
                "text": text,
                "words": words,
                "warning": metadata.get("text_extraction_warning"),
                "pages_total": int(audit.get("pages_total") or 0),
                "visuals_detected": int(audit.get("visuals_detected") or 0),
            }
        )
    return entries


def aggregate_source(entries: list[dict[str, Any]]) -> str:
    parts = []
    for index, entry in enumerate(entries, start=1):
        parts.append(
            "\n".join(
                [
                    f"===== SOURCE {index}: {entry['source_pdf']} =====",
                    f"Exam folder: {entry['exam_folder']}",
                    f"Extracted words: {entry['words']}",
                    f"Warning: {entry['warning'] or 'None'}",
                    "",
                    entry["text"],
                ]
            )
        )
    return "\n\n".join(parts).strip() + "\n"


def chunk_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks = []
    current: list[str] = []
    current_len = 0
    for block in text.split("\n\n"):
        block_len = len(block) + 2
        if current and current_len + block_len > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        if block_len > max_chars:
            for i in range(0, len(block), max_chars):
                chunks.append(block[i : i + max_chars])
            continue
        current.append(block)
        current_len += block_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def selected_source_for_batch(chunks: list[str], batch_index: int, window: int = 2) -> str:
    if not chunks:
        return ""
    selected = [chunks[(batch_index + offset) % len(chunks)] for offset in range(min(window, len(chunks)))]
    combined = "\n\n".join(selected)
    return combined[:QUESTION_SOURCE_CHARS]


def post_ollama(endpoint: str, model: str, system_prompt: str, user_prompt: str, timeout: int) -> str:
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"temperature": 0.18},
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(endpoint, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response_data = json.loads(response.read().decode("utf-8"))
    if isinstance(response_data.get("message"), dict):
        return response_data["message"].get("content", "")
    return response_data.get("response", "")


def parse_json(
    raw: str,
    debug: DebugLogger | None = None,
    context: str = "model response",
    audit_warnings: list[str] | None = None,
) -> dict[str, Any]:
    parsed = generate_exams.load_json_from_model(raw, debug=debug, context=context, audit_warnings=audit_warnings)
    if not isinstance(parsed, dict):
        raise ValueError("Model JSON response is not an object.")
    return parsed


def call_json(
    args: argparse.Namespace,
    user_prompt: str,
    context: str,
    debug: DebugLogger | None = None,
    audit_warnings: list[str] | None = None,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(max(0, args.retries) + 1):
        prompt = user_prompt
        if attempt:
            prompt += (
                "\n\nSTRICT RETRY INSTRUCTION:\n"
                "The previous response was invalid or incomplete. Return one complete valid JSON object only. "
                "No markdown, no trailing commas, no comments, no prose outside JSON."
            )
        raw = ""
        try:
            if debug:
                debug(
                    f"Ollama request: {context} attempt {attempt + 1}",
                    f"endpoint={args.endpoint}\nmodel={args.model}\ntimeout={args.timeout}\n\nSYSTEM PROMPT:\n{FINAL_SYSTEM_PROMPT}\n\nUSER PROMPT:\n{prompt}",
                )
            raw = post_ollama(args.endpoint, args.model, FINAL_SYSTEM_PROMPT, prompt, args.timeout)
            if debug:
                debug(f"Ollama raw response: {context} attempt {attempt + 1}", raw)
            return parse_json(raw, debug=debug, context=f"{context} attempt {attempt + 1}", audit_warnings=audit_warnings)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as exc:
            last_error = exc
            if debug:
                debug(
                    f"Ollama/JSON error: {context} attempt {attempt + 1}",
                    f"{type(exc).__name__}: {exc}" + (f"\n\nRAW RESPONSE:\n{raw}" if raw else ""),
                )
            if attempt < max(0, args.retries):
                print(f"RETRY {attempt + 1}/{args.retries} for {context}: {exc}", flush=True)
                continue
    raise RuntimeError(f"Could not get valid JSON for {context}: {last_error}") from last_error


def build_blueprint(
    args: argparse.Namespace,
    course: str,
    entries: list[dict[str, Any]],
    aggregate: str,
    debug: DebugLogger | None = None,
    audit_warnings: list[str] | None = None,
) -> dict[str, Any]:
    chunks = chunk_text(aggregate, BLUEPRINT_CHARS)
    chunk_blueprints = []
    for index, chunk in enumerate(chunks, start=1):
        prompt = f"""Create a compact final-exam coverage blueprint for this chunk of course source material.

Course: {course}
Chunk: {index} of {len(chunks)}

Return JSON with this shape:
{{
  "chunk": {index},
  "major_topics": [
    {{
      "topic": "string",
      "exam_targets": ["string"],
      "common_traps": ["string"],
      "applications_or_transfer": ["string"],
      "source_area": "string"
    }}
  ]
}}

Make the blueprint dense and useful for a very hard final exam.

SOURCE CHUNK:
<<<SOURCE
{chunk}
SOURCE>>>"""
        data = call_json(args, prompt, f"{course} blueprint chunk {index}", debug=debug, audit_warnings=audit_warnings)
        chunk_blueprints.append(data)
        print(f"BLUEPRINT {course}: chunk {index}/{len(chunks)}", flush=True)

    weak_sources = [entry for entry in entries if entry["warning"] or entry["words"] < 800]
    return {
        "course": course,
        "source_count": len(entries),
        "weak_source_count": len(weak_sources),
        "chunk_blueprints": chunk_blueprints,
    }


def blueprint_text(blueprint: dict[str, Any], max_chars: int = 26000) -> str:
    text = json.dumps(blueprint, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [blueprint truncated for prompt length]"


def question_signature(text: str) -> str:
    text = re.sub(r"\s+", " ", text.casefold()).strip()
    text = re.sub(r"[^a-z0-9äöüß ]", "", text)
    return text[:180]


def normalize_mc(raw_questions: Any, existing: set[str]) -> list[dict[str, Any]]:
    normalized = []
    if not isinstance(raw_questions, list):
        return normalized
    for raw in raw_questions:
        if not isinstance(raw, dict):
            continue
        question_text = str(raw.get("question", "")).strip()
        explanation = str(raw.get("explanation", "")).strip()
        if not question_text or not explanation:
            continue
        signature = question_signature(question_text)
        if signature in existing:
            continue
        options = []
        for option in raw.get("options", []):
            if not isinstance(option, dict):
                continue
            option_text = str(option.get("text", "")).strip()
            if not option_text:
                continue
            options.append({"text": option_text, "is_correct": bool(option.get("is_correct", False))})
        if len(options) < 4:
            continue
        normalized.append(
            {
                "id": "",
                "topic": str(raw.get("topic", "")).strip(),
                "question": question_text,
                "options": options[:6],
                "explanation": explanation,
            }
        )
        existing.add(signature)
    return normalized


def normalize_open(raw_questions: Any, existing: set[str]) -> list[dict[str, Any]]:
    normalized = []
    if not isinstance(raw_questions, list):
        return normalized
    for raw in raw_questions:
        if not isinstance(raw, dict):
            continue
        question_text = str(raw.get("question", "")).strip()
        expected = str(raw.get("expected_answer", "")).strip()
        if not question_text or not expected:
            continue
        signature = question_signature(question_text)
        if signature in existing:
            continue
        rubric = raw.get("grading_rubric")
        if not isinstance(rubric, dict):
            rubric = {
                "90-100": "Excellent, precise, complete, and well structured.",
                "76-89": "Good answer with minor gaps.",
                "61-75": "Solid answer with noticeable gaps.",
                "41-60": "On topic but incomplete or imprecise.",
                "21-40": "Partially relevant with major conceptual gaps.",
                "0-20": "Mostly wrong, vague, or empty.",
            }
        concepts = raw.get("key_concepts")
        if not isinstance(concepts, list):
            concepts = []
        normalized.append(
            {
                "id": "",
                "question": question_text,
                "expected_answer": expected,
                "key_concepts": [str(item) for item in concepts if str(item).strip()],
                "grading_rubric": rubric,
                "max_score": 100,
            }
        )
        existing.add(signature)
    return normalized


def mc_prompt(course: str, blueprint: dict[str, Any], source_excerpt: str, count: int, batch_index: int, existing_stems: list[str]) -> str:
    return f"""Generate {count} very hard final-exam multiple-choice questions.

Course: {course}
Batch: {batch_index}

Language requirements:
- Write topics, question text, options, and explanations in German.
- Preserve established technical terms, model names, formulas, author names, and source quotations in the original language where appropriate.

These are true multiple-choice questions:
- 4 to 6 options per question.
- Every option must have "is_correct": true or false.
- One, several, or all options may be correct if justified by the source.
- Avoid single-obvious-answer patterns across the whole batch.
- Explanations must explain why the correct and incorrect choices matter.

Difficulty requirements:
- Integrate concepts across lectures where possible.
- Prefer mechanisms, distinctions, applications, and transfer.
- Use plausible distractors based on neighboring concepts or misconceptions.
- Do not ask shallow recognition questions unless foundational.
- If course is economics/Mikro-related, include calculations, curve shifts, welfare, model assumptions, comparative statics, and strategic reasoning where supported.

Avoid duplicating these already generated stems:
{json.dumps(existing_stems[-60:], ensure_ascii=False, indent=2)}

Return JSON only:
{{
  "multiple_choice": [
    {{
      "topic": "string",
      "question": "string",
      "options": [
        {{"text": "string", "is_correct": true}},
        {{"text": "string", "is_correct": false}},
        {{"text": "string", "is_correct": false}},
        {{"text": "string", "is_correct": true}}
      ],
      "explanation": "string"
    }}
  ]
}}

COURSE BLUEPRINT:
<<<BLUEPRINT
{blueprint_text(blueprint)}
BLUEPRINT>>>

SOURCE EXCERPT FOR THIS BATCH:
<<<SOURCE
{source_excerpt}
SOURCE>>>"""


def open_prompt(course: str, blueprint: dict[str, Any], source_excerpt: str, count: int, batch_index: int, existing_stems: list[str]) -> str:
    return f"""Generate {count} very hard final-exam open-ended questions.

Course: {course}
Batch: {batch_index}

Language requirements:
- Write questions, expected answers, key concepts, and grading rubrics in German.
- Preserve established technical terms, model names, formulas, author names, and source quotations in the original language where appropriate.

Requirements:
- Questions should demand precise explanation, transfer, comparison, mechanism, interpretation, or application.
- Avoid vague essay prompts; each question must be gradeable from the provided expected answer.
- Include strict expected answers, key concepts, grading rubric, and max_score 100.
- Rubrics must reward conceptual precision and penalize buzzwords, vagueness, contradictions, and unsupported claims.
- If course is economics/Mikro-related, include calculation/model/graph/comparative-static reasoning where supported.

Avoid duplicating these already generated stems:
{json.dumps(existing_stems[-40:], ensure_ascii=False, indent=2)}

Return JSON only:
{{
  "open_ended": [
    {{
      "question": "string",
      "expected_answer": "string",
      "key_concepts": ["string"],
      "grading_rubric": {{
        "90-100": "string",
        "76-89": "string",
        "61-75": "string",
        "41-60": "string",
        "21-40": "string",
        "0-20": "string"
      }},
      "max_score": 100
    }}
  ]
}}

COURSE BLUEPRINT:
<<<BLUEPRINT
{blueprint_text(blueprint)}
BLUEPRINT>>>

SOURCE EXCERPT FOR THIS BATCH:
<<<SOURCE
{source_excerpt}
SOURCE>>>"""


def generate_mc(
    args: argparse.Namespace,
    course: str,
    blueprint: dict[str, Any],
    source_chunks: list[str],
    target: int,
    debug: DebugLogger | None = None,
    audit_warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    batch_index = 0
    empty_batches = 0
    while len(questions) < target and batch_index < 36:
        batch_index += 1
        remaining = target - len(questions)
        count = min(args.mc_batch_size, remaining)
        data = None
        while count >= 5:
            excerpt = selected_source_for_batch(source_chunks, batch_index - 1)
            stems = [question["question"] for question in questions]
            try:
                data = call_json(
                    args,
                    mc_prompt(course, blueprint, excerpt, count, batch_index, stems),
                    f"{course} MC batch {batch_index} ({count})",
                    debug=debug,
                    audit_warnings=audit_warnings,
                )
                break
            except RuntimeError as exc:
                smaller = max(5, count // 2)
                if smaller == count:
                    print(f"MC {course}: abandoning batch {batch_index} after JSON failures: {exc}", flush=True)
                    if audit_warnings is not None:
                        audit_warnings.append(f"Final MC batch {batch_index} failed: {exc}")
                    break
                print(f"MC {course}: reducing batch {batch_index} from {count} to {smaller} after JSON failure", flush=True)
                if audit_warnings is not None:
                    audit_warnings.append(f"Final MC batch {batch_index} reduced from {count} to {smaller} after JSON failure: {exc}")
                count = smaller
        if data is None:
            empty_batches += 1
            if empty_batches >= 3:
                break
            continue
        raw_questions = data.get("multiple_choice")
        new_questions = normalize_mc(raw_questions, seen)
        generate_exams.attach_question_metadata(new_questions, excerpt, f"final-mc-batch-{batch_index:03d}")
        questions.extend(new_questions)
        raw_count = len(raw_questions) if isinstance(raw_questions, list) else 0
        if audit_warnings is not None and raw_count > len(new_questions):
            audit_warnings.append(f"Discarded {raw_count - len(new_questions)} duplicate or invalid final MC generation(s) in batch {batch_index}.")
        print(f"MC {course}: {len(questions)}/{target}", flush=True)
        if not new_questions:
            empty_batches += 1
        else:
            empty_batches = 0
        if empty_batches >= 3:
            break
    for index, question in enumerate(questions[:target], start=1):
        question["id"] = f"final-mc-{index:03d}"
    return questions[:target]


def generate_open(
    args: argparse.Namespace,
    course: str,
    blueprint: dict[str, Any],
    source_chunks: list[str],
    target: int,
    debug: DebugLogger | None = None,
    audit_warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    batch_index = 0
    empty_batches = 0
    while len(questions) < target and batch_index < 24:
        batch_index += 1
        remaining = target - len(questions)
        count = min(args.open_batch_size, remaining)
        data = None
        while count >= 2:
            excerpt = selected_source_for_batch(source_chunks, batch_index + 3)
            stems = [question["question"] for question in questions]
            try:
                data = call_json(
                    args,
                    open_prompt(course, blueprint, excerpt, count, batch_index, stems),
                    f"{course} open batch {batch_index} ({count})",
                    debug=debug,
                    audit_warnings=audit_warnings,
                )
                break
            except RuntimeError as exc:
                smaller = max(2, count // 2)
                if smaller == count:
                    print(f"OPEN {course}: abandoning batch {batch_index} after JSON failures: {exc}", flush=True)
                    if audit_warnings is not None:
                        audit_warnings.append(f"Final open batch {batch_index} failed: {exc}")
                    break
                print(f"OPEN {course}: reducing batch {batch_index} from {count} to {smaller} after JSON failure", flush=True)
                if audit_warnings is not None:
                    audit_warnings.append(f"Final open batch {batch_index} reduced from {count} to {smaller} after JSON failure: {exc}")
                count = smaller
        if data is None:
            empty_batches += 1
            if empty_batches >= 3:
                break
            continue
        raw_questions = data.get("open_ended")
        new_questions = normalize_open(raw_questions, seen)
        generate_exams.attach_question_metadata(new_questions, excerpt, f"final-open-batch-{batch_index:03d}")
        questions.extend(new_questions)
        raw_count = len(raw_questions) if isinstance(raw_questions, list) else 0
        if audit_warnings is not None and raw_count > len(new_questions):
            audit_warnings.append(f"Discarded {raw_count - len(new_questions)} duplicate or invalid final open generation(s) in batch {batch_index}.")
        print(f"OPEN {course}: {len(questions)}/{target}", flush=True)
        if not new_questions:
            empty_batches += 1
        else:
            empty_batches = 0
        if empty_batches >= 3:
            break
    for index, question in enumerate(questions[:target], start=1):
        question["id"] = f"final-open-{index:03d}"
    return questions[:target]


def adaptive_targets(args: argparse.Namespace, entries: list[dict[str, Any]], total_words: int, weak_count: int) -> tuple[int, int, str | None]:
    warning = None
    if total_words < 9000:
        mc = min(args.target_mc, 60)
        open_count = min(args.target_open, 15)
        warning = f"Final exam reduced because only {total_words} words were extracted across the course."
    elif weak_count >= max(3, len(entries) // 2):
        mc = min(args.target_mc, 80)
        open_count = min(args.target_open, 20)
        warning = f"Final exam reduced because {weak_count} of {len(entries)} source decks had weak text extraction."
    else:
        mc = args.target_mc
        open_count = args.target_open
    return mc, open_count, warning


def write_final_exam(args: argparse.Namespace, course_dir: Path, debug: DebugLogger | None = None) -> dict[str, Any] | None:
    final_dir = course_dir / "exams" / "final-exam"
    if final_dir.exists() and not args.overwrite:
        print(f"SKIP existing final: {final_dir}", flush=True)
        return None

    entries = source_entries(course_dir)
    if not entries:
        print(f"SKIP no source entries: {course_dir}", flush=True)
        return None

    course = course_dir.name
    aggregate = aggregate_source(entries)
    total_words = count_words(aggregate)
    weak_count = sum(1 for entry in entries if entry["warning"] or entry["words"] < 800)
    target_mc, target_open, target_warning = adaptive_targets(args, entries, total_words, weak_count)
    source_chunks = chunk_text(aggregate, QUESTION_SOURCE_CHARS)
    audit_warnings = [
        f"Included source warning for {entry['source_pdf']}: {entry['warning']}"
        for entry in entries
        if entry.get("warning")
    ]
    audit_warnings.append("Final exam combines multiple source decks; page coverage is approximate and per-source audits remain the detailed reference.")
    if debug:
        debug(
            f"Final source summary: {course}",
            json.dumps(
                {
                    "course": course,
                    "source_count": len(entries),
                    "total_words": total_words,
                    "weak_source_count": weak_count,
                    "target_mc": target_mc,
                    "target_open": target_open,
                    "source_chunks": len(source_chunks),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    blueprint = build_blueprint(args, course, entries, aggregate, debug=debug, audit_warnings=audit_warnings)

    mc_questions = generate_mc(args, course, blueprint, source_chunks, target_mc, debug=debug, audit_warnings=audit_warnings)
    open_questions = generate_open(args, course, blueprint, source_chunks, target_open, debug=debug, audit_warnings=audit_warnings)
    if not mc_questions or not open_questions:
        raise RuntimeError(f"Final exam for {course} did not produce usable questions.")

    warnings = [target_warning] if target_warning else []
    if len(mc_questions) < args.target_mc or len(open_questions) < args.target_open:
        warnings.append(
            f"Generated {len(mc_questions)} MC and {len(open_questions)} open questions instead of target {args.target_mc}/{args.target_open}; source quality or model output limited the final."
        )
    audit_warnings.extend(warnings)

    exam = {
        "metadata": {
            "title": f"Final Exam - {course}",
            "course": course,
            "source_pdf": "Final exam from all course source decks",
            "generated_date": dt.date.today().isoformat(),
            "text_extraction_warning": " ".join(warnings) if warnings else None,
            "generator": "generate_final_exams.py",
            "is_final_exam": True,
            "included_sources": [
                {
                    "exam_folder": entry["exam_folder"],
                    "source_pdf": entry["source_pdf"],
                    "word_count": entry["words"],
                    "warning": entry["warning"],
                }
                for entry in entries
            ],
            "source_word_count": total_words,
            "weak_extraction_source_count": weak_count,
            "question_count": {
                "multiple_choice": len(mc_questions),
                "open_ended": len(open_questions),
            },
        },
        "multiple_choice": mc_questions,
        "open_ended": open_questions,
    }
    pages_total = sum(int(entry.get("pages_total") or 0) for entry in entries)
    visuals_detected = sum(int(entry.get("visuals_detected") or 0) for entry in entries)
    exam = generate_exams.apply_exam_audit(
        exam,
        "Final exam from all course source decks",
        aggregate,
        pages_total or None,
        len(source_chunks),
        len(source_chunks),
        0,
        audit_warnings,
        visuals_detected=visuals_detected or None,
    )
    if debug:
        debug(f"AUDIT SUMMARY: final exam {course}", generate_exams.format_audit_summary(exam["audit"]))

    if final_dir.exists() and args.overwrite:
        shutil.rmtree(final_dir)
    final_dir.mkdir(parents=True, exist_ok=True)
    template_dir = Path(__file__).resolve().parent / "templates"
    (final_dir / "index.html").write_text(generate_exams.render_exam_html(template_dir, exam), encoding="utf-8")
    (final_dir / "exam.json").write_text(json.dumps(exam, ensure_ascii=False, indent=2), encoding="utf-8")
    (final_dir / "source.txt").write_text(aggregate, encoding="utf-8")
    print(f"WROTE FINAL {final_dir}", flush=True)
    return {
        "course": course,
        "mc": len(mc_questions),
        "open": len(open_questions),
        "weak": weak_count,
        "words": total_words,
    }


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"Root folder does not exist: {root}", flush=True)
        return 2

    generated = []
    for course_dir in course_dirs(root, args.only_folder):
        result = write_final_exam(args, course_dir)
        if result:
            generated.append(result)

    generate_exams.write_index_pages(root)
    print(f"Done. Generated {len(generated)} final exam(s). Index: {root / 'exam_index.html'}", flush=True)
    for item in generated:
        print(f"{item['course']}: {item['mc']} MC, {item['open']} open, {item['words']} source words, {item['weak']} weak sources", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

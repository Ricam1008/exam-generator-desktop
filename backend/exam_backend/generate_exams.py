#!/usr/bin/env python3
"""Generate local static exam apps from university lecture PDFs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_ENDPOINT = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "gemma4:31b-cloud"
MIN_WORDS_FOR_FULL_EXAM = 3000
LOW_TEXT_WORD_THRESHOLD = 800
MAX_PROMPT_TEXT_CHARS = 52000


EXAM_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["metadata", "multiple_choice", "open_ended"],
    "properties": {
        "metadata": {
            "type": "object",
            "required": [
                "course",
                "source_pdf",
                "generated_date",
                "text_extraction_warning",
            ],
            "properties": {
                "title": {"type": "string"},
                "course": {"type": "string"},
                "source_pdf": {"type": "string"},
                "generated_date": {"type": "string"},
                "text_extraction_warning": {"type": ["string", "null"]},
                "generator": {"type": "string"},
                "question_count": {"type": "object"},
            },
        },
        "multiple_choice": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "question", "options", "explanation"],
                "properties": {
                    "id": {"type": "string"},
                    "topic": {"type": "string"},
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "minItems": 4,
                        "maxItems": 6,
                        "items": {
                            "type": "object",
                            "required": ["text", "is_correct"],
                            "properties": {
                                "text": {"type": "string"},
                                "is_correct": {"type": "boolean"},
                            },
                        },
                    },
                    "explanation": {"type": "string"},
                },
            },
        },
        "open_ended": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "question",
                    "expected_answer",
                    "key_concepts",
                    "grading_rubric",
                    "max_score",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "question": {"type": "string"},
                    "expected_answer": {"type": "string"},
                    "key_concepts": {"type": "array", "items": {"type": "string"}},
                    "grading_rubric": {"type": "object"},
                    "max_score": {"type": "integer", "const": 100},
                },
            },
        },
    },
}


GENERATION_SYSTEM_PROMPT = """You are an expert university exam writer.

Create difficult but fair exam-preparation questions from lecture slide text.
Use only the source material provided by the user. Do not invent facts.
Write all generated exam content in German, even when the source text is partly or fully English.
Keep established technical terms, theory names, study names, author names, formulas, and quoted source phrases in their original language when that is clearer or source-faithful.
Focus on definitions, models, theories, central findings, distinctions between similar concepts, examples, applications, and likely exam-relevant lecture material.
Avoid duplicate, trivial, or overly easy questions.
Distractors must be plausible and based on common misconceptions or nearby concepts from the source material.

Return valid JSON only. No markdown. No prose outside JSON."""


def build_generation_prompt(
    course: str,
    source_pdf: str,
    text: str,
    target_mc: int,
    target_open: int,
    extraction_warning: str | None,
) -> str:
    schema = json.dumps(EXAM_JSON_SCHEMA, ensure_ascii=False, indent=2)
    warning_note = extraction_warning or "None"
    return f"""Create one standalone exam JSON object for this lecture PDF.

Course: {course}
Source PDF: {source_pdf}
Text extraction warning: {warning_note}

Question counts:
- Create exactly {target_mc} multiple-choice questions if the source text supports it.
- Create exactly {target_open} open-ended questions if the source text supports it.
- If the source text is too thin or repetitive, create fewer high-quality questions and rely on the metadata warning.

Multiple-choice requirements:
- Write question text, options, explanations, topics, expected answers, key concepts, and rubrics in German.
- Preserve established technical terms, model names, formulas, and source quotations in the original language where appropriate.
- True multiple-choice, not single-choice.
- Each question has 4 to 6 options.
- Each option has a hardcoded boolean is_correct.
- There may be one correct answer, several correct answers, all correct, or none correct, but only when justified by the source.
- Include a short explanation for each MC question.
- Do not make every question have the same number of correct options.

Open-ended requirements:
- Each open question has max_score 100.
- Include expected_answer, key_concepts, and a grading_rubric object.
- Rubrics must be strict and useful for grading from 0 to 100 points.

Required JSON schema:
{schema}

Source slide text:
<<<SOURCE_TEXT
{text}
SOURCE_TEXT>>>"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate local static exam apps from PDF slide decks.")
    parser.add_argument("--root", required=True, help="Root folder containing course folders.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing exam folders.")
    parser.add_argument("--only-folder", help="Generate only PDFs inside a folder with this name.")
    parser.add_argument("--limit", type=int, help="Generate at most this many exams. Useful for the first inspection pass.")
    parser.add_argument("--min-mc", type=int, default=40)
    parser.add_argument("--max-mc", type=int, default=60)
    parser.add_argument("--min-open", type=int, default=10)
    parser.add_argument("--max-open", type=int, default=20)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=600, help="LLM request timeout in seconds.")
    parser.add_argument("--retries", type=int, default=2, help="Retries per PDF when the model returns malformed JSON.")
    parser.add_argument(
        "--allow-heuristic-fallback",
        action="store_true",
        help="Create a small inspection-only exam if the LLM is unavailable.",
    )
    return parser.parse_args()


def slugify(value: str, max_length: int = 80) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    lowered = normalized.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug[:max_length].strip("-") or "exam")


def is_inside_exams(path: Path) -> bool:
    return any(part.lower() == "exams" for part in path.parts)


def matches_only_folder(pdf_path: Path, root: Path, only_folder: str | None) -> bool:
    if not only_folder:
        return True
    wanted = only_folder.casefold()
    try:
        relative_parts = pdf_path.relative_to(root).parts
    except ValueError:
        relative_parts = pdf_path.parts
    return any(part.casefold() == wanted for part in relative_parts[:-1])


def find_pdfs(root: Path, only_folder: str | None) -> list[Path]:
    pdfs = []
    for pdf in sorted(root.rglob("*.pdf")):
        if is_inside_exams(pdf):
            continue
        if matches_only_folder(pdf, root, only_folder):
            pdfs.append(pdf)
    return pdfs


def run_command(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)


def extract_with_pypdf(pdf_path: Path) -> tuple[str, int | None] | None:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return None

    reader = PdfReader(str(pdf_path))
    page_text = []
    for page in reader.pages:
        page_text.append(page.extract_text() or "")
    return "\n\n".join(page_text), len(reader.pages)


def extract_with_pdfplumber(pdf_path: Path) -> tuple[str, int | None] | None:
    try:
        import pdfplumber  # type: ignore
    except Exception:
        return None

    page_text = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            page_text.append(page.extract_text() or "")
        return "\n\n".join(page_text), len(pdf.pages)


def extract_with_pdftotext(pdf_path: Path) -> tuple[str, int | None] | None:
    if not shutil.which("pdftotext"):
        return None
    result = run_command(["pdftotext", "-layout", str(pdf_path), "-"], timeout=180)
    if result.returncode != 0:
        return None
    return result.stdout, None


def extract_with_strings(pdf_path: Path) -> tuple[str, int | None] | None:
    if not shutil.which("strings"):
        return None
    result = run_command(["strings", str(pdf_path)], timeout=180)
    if result.returncode != 0:
        return None
    lines = []
    for line in result.stdout.splitlines():
        clean = line.strip()
        if len(clean) >= 4 and re.search(r"[A-Za-z0-9]", clean):
            lines.append(clean)
    return "\n".join(lines), None


def clean_extracted_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def extract_pdf_text(pdf_path: Path) -> tuple[str, str | None, int | None]:
    attempts = [
        ("pypdf", extract_with_pypdf),
        ("pdfplumber", extract_with_pdfplumber),
        ("pdftotext", extract_with_pdftotext),
        ("strings fallback", extract_with_strings),
    ]
    last_error = None
    for name, extractor in attempts:
        try:
            result = extractor(pdf_path)
        except Exception as exc:
            last_error = f"{name} failed: {exc}"
            continue
        if not result:
            continue
        text, page_count = result
        text = clean_extracted_text(text)
        if text:
            words = count_words(text)
            warning = None
            if name == "strings fallback":
                warning = "Text extraction used a rough fallback; question quality may be weak. Install pypdf or Poppler/pdftotext for better extraction."
            elif words < LOW_TEXT_WORD_THRESHOLD:
                warning = f"Only {words} words were extracted; this PDF may contain scanned slides, images, or little text."
            return text, warning, page_count

    message = last_error or "No PDF text extractor was available."
    return "", f"Could not extract usable text. {message}", None


def count_words(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE))


def chunk_for_prompt(text: str, max_chars: int = MAX_PROMPT_TEXT_CHARS) -> str:
    if len(text) <= max_chars:
        return text

    chunk_size = 6500
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
    keep_count = max(3, max_chars // chunk_size)
    if len(chunks) <= keep_count:
        selected = chunks
    else:
        selected_indexes = sorted({round(i * (len(chunks) - 1) / (keep_count - 1)) for i in range(keep_count)})
        selected = [chunks[index] for index in selected_indexes]
    labeled = [f"[Excerpt {i + 1} of {len(selected)}]\n{chunk}" for i, chunk in enumerate(selected)]
    return "\n\n".join(labeled)


def target_counts(words: int, args: argparse.Namespace) -> tuple[int, int]:
    if words < LOW_TEXT_WORD_THRESHOLD:
        return min(12, args.min_mc), min(4, args.min_open)
    if words < MIN_WORDS_FOR_FULL_EXAM:
        return min(25, args.min_mc), min(8, args.min_open)
    if words < 6000:
        mc = args.min_mc
        open_count = args.min_open
    elif words < 12000:
        mc = round((args.min_mc + args.max_mc) / 2)
        open_count = round((args.min_open + args.max_open) / 2)
    else:
        mc = args.max_mc
        open_count = args.max_open
    mc = max(args.min_mc, min(args.max_mc, mc))
    open_count = max(args.min_open, min(args.max_open, open_count))
    return mc, open_count


def scaled_retry_counts(mc: int, open_count: int, attempt: int, args: argparse.Namespace) -> tuple[int, int]:
    if attempt <= 0:
        return mc, open_count
    factor = 0.85 if attempt == 1 else 0.7
    retry_mc = max(args.min_mc, int(round(mc * factor)))
    retry_open = max(args.min_open, int(round(open_count * factor)))
    return min(args.max_mc, retry_mc), min(args.max_open, retry_open)


def post_ollama(endpoint: str, model: str, prompt: str, timeout: int) -> str:
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": GENERATION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": 0.25},
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(endpoint, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    parsed = json.loads(body)
    if "message" in parsed and isinstance(parsed["message"], dict):
        return parsed["message"].get("content", "")
    return parsed.get("response", "")


def load_json_from_model(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(raw[start : end + 1])


def ensure_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def normalize_exam(
    model_exam: dict[str, Any],
    course: str,
    source_pdf: str,
    extraction_warning: str | None,
    source_word_count: int,
) -> dict[str, Any]:
    mc_questions = []
    for index, question in enumerate(ensure_list(model_exam.get("multiple_choice")), start=1):
        options = []
        for option in ensure_list(question.get("options"))[:6]:
            if not isinstance(option, dict):
                continue
            text = str(option.get("text", "")).strip()
            if not text:
                continue
            options.append({"text": text, "is_correct": bool(option.get("is_correct", False))})
        if len(options) < 4:
            continue
        mc_questions.append(
            {
                "id": str(question.get("id") or f"mc-{index:03d}"),
                "topic": str(question.get("topic") or ""),
                "question": str(question.get("question") or "").strip(),
                "options": options,
                "explanation": str(question.get("explanation") or "").strip(),
            }
        )

    open_questions = []
    for index, question in enumerate(ensure_list(model_exam.get("open_ended")), start=1):
        rubric = question.get("grading_rubric")
        if not isinstance(rubric, dict):
            rubric = {
                "90-100": "Precise, complete answer covering all key concepts.",
                "61-89": "Mostly correct answer with minor to noticeable gaps.",
                "41-60": "On topic but incomplete or imprecise.",
                "21-40": "Partially relevant with major conceptual gaps.",
                "0-20": "Mostly wrong, vague, or empty.",
            }
        open_questions.append(
            {
                "id": str(question.get("id") or f"open-{index:03d}"),
                "question": str(question.get("question") or "").strip(),
                "expected_answer": str(question.get("expected_answer") or "").strip(),
                "key_concepts": [str(item) for item in ensure_list(question.get("key_concepts")) if str(item).strip()],
                "grading_rubric": rubric,
                "max_score": 100,
            }
        )

    today = dt.date.today().isoformat()
    title = Path(source_pdf).stem
    if not mc_questions or not open_questions:
        raise ValueError("The model response did not contain usable MC and open-ended questions.")
    for question in mc_questions:
        if not question["question"] or not question["explanation"]:
            raise ValueError("At least one MC question is missing a question text or explanation.")
    for question in open_questions:
        if not question["question"] or not question["expected_answer"]:
            raise ValueError("At least one open-ended question is missing a question text or expected answer.")

    return {
        "metadata": {
            "title": title,
            "course": course,
            "source_pdf": source_pdf,
            "generated_date": today,
            "text_extraction_warning": extraction_warning,
            "generator": "generate_exams.py",
            "source_word_count": source_word_count,
            "question_count": {
                "multiple_choice": len(mc_questions),
                "open_ended": len(open_questions),
            },
        },
        "multiple_choice": mc_questions,
        "open_ended": open_questions,
    }


def heuristic_exam(
    course: str,
    source_pdf: str,
    text: str,
    extraction_warning: str | None,
    source_word_count: int,
) -> dict[str, Any]:
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) > 55]
    seeds = sentences[:16] or [Path(source_pdf).stem]
    mc = []
    for index, sentence in enumerate(seeds[:8], start=1):
        topic = sentence[:70]
        mc.append(
            {
                "id": f"mc-{index:03d}",
                "topic": topic,
                "question": f"Which statements are supported by the slide text about: {topic}?",
                "options": [
                    {"text": sentence[:220], "is_correct": True},
                    {"text": "The source presents this point as unrelated to the lecture topic.", "is_correct": False},
                    {"text": "The source treats the concept as a purely historical detail with no exam relevance.", "is_correct": False},
                    {"text": "The point should be understood in relation to the surrounding lecture concepts.", "is_correct": True},
                ],
                "explanation": "This inspection-only fallback uses extracted text directly because the LLM generation endpoint was unavailable.",
            }
        )
    open_questions = []
    for index, sentence in enumerate(seeds[8:12] or seeds[:4], start=1):
        open_questions.append(
            {
                "id": f"open-{index:03d}",
                "question": f"Explain the exam relevance of this slide point: {sentence[:180]}",
                "expected_answer": sentence,
                "key_concepts": [word for word in re.findall(r"\b[A-Za-zÄÖÜäöüß][\wÄÖÜäöüß-]{5,}\b", sentence)[:6]],
                "grading_rubric": {
                    "90-100": "Precise, complete explanation grounded in the quoted source point.",
                    "61-89": "Mostly correct explanation with minor gaps.",
                    "41-60": "Relevant but incomplete or imprecise.",
                    "21-40": "Only loosely related to the source point.",
                    "0-20": "Missing, wrong, or generic answer.",
                },
                "max_score": 100,
            }
        )
    warning = extraction_warning or ""
    warning = (warning + " " if warning else "") + "LLM generation was unavailable; this is a small heuristic inspection exam, not a final study set."
    return normalize_exam(
        {"multiple_choice": mc, "open_ended": open_questions},
        course,
        source_pdf,
        warning.strip(),
        source_word_count,
    )


def escape_script_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2).replace("</", "<\\/")


def render_exam_html(template_dir: Path, exam: dict[str, Any]) -> str:
    template = (template_dir / "index_template.html").read_text(encoding="utf-8")
    styles = textwrap.indent((template_dir / "styles.css").read_text(encoding="utf-8"), "    ")
    script = textwrap.indent((template_dir / "app.js").read_text(encoding="utf-8"), "    ")
    exam_json = textwrap.indent(escape_script_json(exam), "    ")
    return template.replace("{{ styles }}", styles).replace("{{ script }}", script).replace("{{ exam_json }}", exam_json)


def write_exam_folder(
    pdf_path: Path,
    root: Path,
    args: argparse.Namespace,
    template_dir: Path,
) -> dict[str, Any] | None:
    course_dir = pdf_path.parent
    course = course_dir.name
    exams_dir = course_dir / "exams"
    exam_dir = exams_dir / slugify(pdf_path.stem)

    if exam_dir.exists() and not args.overwrite:
        print(f"SKIP existing: {exam_dir}", flush=True)
        return None

    text, extraction_warning, page_count = extract_pdf_text(pdf_path)
    word_count = count_words(text)
    prompt_text = chunk_for_prompt(text)
    if len(text) > len(prompt_text):
        chunk_note = "Long PDF text was chunked into representative excerpts for LLM generation."
        extraction_warning = f"{extraction_warning} {chunk_note}".strip() if extraction_warning else chunk_note

    base_mc, base_open = target_counts(word_count, args)
    last_error: Exception | None = None
    exam: dict[str, Any] | None = None

    for attempt in range(max(0, args.retries) + 1):
        target_mc, target_open = scaled_retry_counts(base_mc, base_open, attempt, args)
        prompt = build_generation_prompt(course, pdf_path.name, prompt_text, target_mc, target_open, extraction_warning)
        if attempt:
            prompt += (
                "\n\nIMPORTANT RETRY INSTRUCTION:\n"
                "Your previous response for this PDF was not valid JSON. Return one complete, syntactically valid JSON object only. "
                "Do not include markdown, comments, trailing commas, undefined values, or text outside the JSON object. "
                "Use shorter but still substantive question and explanation text if needed."
            )
        try:
            raw = post_ollama(args.endpoint, args.model, prompt, args.timeout)
            model_exam = load_json_from_model(raw)
            exam = normalize_exam(model_exam, course, pdf_path.name, extraction_warning, word_count)
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as exc:
            last_error = exc
            if attempt < max(0, args.retries):
                print(f"RETRY {attempt + 1}/{args.retries} for {pdf_path.name}: {exc}", flush=True)
                continue

    if exam is None:
        if not args.allow_heuristic_fallback:
            raise RuntimeError(f"Could not generate questions for {pdf_path.name}: {last_error}") from last_error
        print(f"LLM unavailable for {pdf_path.name}; writing heuristic inspection exam.", flush=True)
        exam = heuristic_exam(course, pdf_path.name, text, extraction_warning, word_count)

    if exam_dir.exists() and args.overwrite:
        shutil.rmtree(exam_dir)
    exam_dir.mkdir(parents=True, exist_ok=True)
    (exam_dir / "index.html").write_text(render_exam_html(template_dir, exam), encoding="utf-8")
    (exam_dir / "exam.json").write_text(json.dumps(exam, ensure_ascii=False, indent=2), encoding="utf-8")
    (exam_dir / "source.txt").write_text(text, encoding="utf-8")
    print(f"WROTE {exam_dir}", flush=True)

    return {
        "course": course,
        "course_dir": str(course_dir),
        "exam_dir": str(exam_dir),
        "source_pdf": pdf_path.name,
        "warning": exam["metadata"].get("text_extraction_warning"),
        "mc_count": len(exam["multiple_choice"]),
        "open_count": len(exam["open_ended"]),
        "page_count": page_count,
        "word_count": word_count,
    }


def discover_generated_exams(root: Path) -> dict[Path, list[dict[str, Any]]]:
    courses: dict[Path, list[dict[str, Any]]] = {}
    for exam_json in sorted(root.rglob("exams/*/exam.json")):
        try:
            data = json.loads(exam_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        metadata = data.get("metadata", {})
        course_dir = exam_json.parent.parent.parent
        courses.setdefault(course_dir, []).append(
            {
                "exam_dir": exam_json.parent,
                "title": metadata.get("title") or exam_json.parent.name,
                "course": metadata.get("course") or course_dir.name,
                "source_pdf": metadata.get("source_pdf") or "",
                "warning": metadata.get("text_extraction_warning"),
                "mc_count": len(data.get("multiple_choice", [])),
                "open_count": len(data.get("open_ended", [])),
            }
        )
    return courses


def html_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape_html(title)}</title>
  <style>
    body {{ margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f5f0; color: #17211d; }}
    main {{ width: min(1000px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0 56px; }}
    h1 {{ margin: 0 0 8px; font-size: clamp(2rem, 5vw, 3.2rem); }}
    p {{ color: #65726d; }}
    ul {{ list-style: none; padding: 0; display: grid; gap: 10px; }}
    li {{ background: white; border: 1px solid #d9ded8; border-radius: 8px; padding: 14px 16px; }}
    a {{ color: #0f766e; font-weight: 750; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .warning {{ display: inline-block; margin-top: 8px; color: #995c00; }}
    .meta {{ display: block; margin-top: 4px; color: #65726d; }}
  </style>
</head>
<body>
  <main>
{body}
  </main>
</body>
</html>
"""


def escape_html(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def rel_href(from_dir: Path, target: Path) -> str:
    return Path(os.path.relpath(target, start=from_dir)).as_posix()


def write_index_pages(root: Path) -> None:
    courses = discover_generated_exams(root)
    course_links = []
    for course_dir, exams in sorted(courses.items(), key=lambda item: item[0].name.casefold()):
        exams_dir = course_dir / "exams"
        items = []
        for exam in sorted(exams, key=lambda item: item["source_pdf"].casefold()):
            href = rel_href(exams_dir, exam["exam_dir"] / "index.html")
            warning = f'<span class="warning">Warning: {escape_html(exam["warning"])}</span>' if exam.get("warning") else ""
            items.append(
                f'<li><a href="{escape_html(href)}">{escape_html(exam["title"])}</a>'
                f'<span class="meta">Source: {escape_html(exam["source_pdf"])} · '
                f'{exam["mc_count"]} MC · {exam["open_count"]} open</span>{warning}</li>'
            )
        body = (
            f"    <h1>{escape_html(course_dir.name)}</h1>\n"
            f"    <p>{len(exams)} generated exam{'s' if len(exams) != 1 else ''}</p>\n"
            f"    <ul>\n      " + "\n      ".join(items) + "\n    </ul>\n"
        )
        exams_dir.mkdir(exist_ok=True)
        (exams_dir / "index.html").write_text(html_page(f"{course_dir.name} exams", body), encoding="utf-8")
        root_href = rel_href(root, exams_dir / "index.html")
        course_links.append(
            f'<li><a href="{escape_html(root_href)}">{escape_html(course_dir.name)}</a>'
            f'<span class="meta">{len(exams)} generated exam{"s" if len(exams) != 1 else ""}</span></li>'
        )

    root_body = (
        "    <h1>Generated exam index</h1>\n"
        f"    <p>{sum(len(exams) for exams in courses.values())} generated exams across {len(courses)} course folders.</p>\n"
        "    <ul>\n      " + "\n      ".join(course_links) + "\n    </ul>\n"
    )
    (root / "exam_index.html").write_text(html_page("Generated exam index", root_body), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    template_dir = Path(__file__).resolve().parent / "templates"
    if not root.exists():
        print(f"Root does not exist: {root}", file=sys.stderr, flush=True)
        return 2
    for required in ["index_template.html", "styles.css", "app.js"]:
        if not (template_dir / required).exists():
            print(f"Missing template file: {template_dir / required}", file=sys.stderr, flush=True)
            return 2

    pdfs = find_pdfs(root, args.only_folder)
    if args.limit is not None:
        pdfs = pdfs[: max(0, args.limit)]

    if not pdfs:
        print("No PDFs found.", flush=True)
        write_index_pages(root)
        return 0

    generated = []
    for pdf in pdfs:
        result = write_exam_folder(pdf, root, args, template_dir)
        if result:
            generated.append(result)

    write_index_pages(root)
    print(f"Done. Generated {len(generated)} exam(s). Index: {root / 'exam_index.html'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

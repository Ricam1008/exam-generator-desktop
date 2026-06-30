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
from typing import Any, Callable


DEFAULT_ENDPOINT = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "gemma4:31b-cloud"
GENERATOR_VERSION = "0.1.0"
MIN_WORDS_FOR_FULL_EXAM = 3000
LOW_TEXT_WORD_THRESHOLD = 800
MAX_PROMPT_TEXT_CHARS = 52000
FULL_COVERAGE_TEXT_CHARS = 22000
DebugLogger = Callable[[str, str], None]


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
                "source_word_count": {"type": "integer"},
                "coverage_mode": {"type": "string"},
                "source_chunk_count": {"type": "integer"},
                "processed_chunk_count": {"type": "integer"},
                "failed_chunk_count": {"type": "integer"},
                "coverage_warning": {"type": ["string", "null"]},
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
Formula and JSON safety: prefer plain-text formula notation over LaTeX commands. If a JSON string must contain a backslash, emit it as a doubled JSON backslash.

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

Source handling:
- Treat [TABLE ...] blocks as structured source evidence; preserve row/column relationships when creating questions.
- If a chart, curve, or graphic is only visible visually and not described in the extracted text or table blocks, do not invent its details.

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
        "--coverage-mode",
        choices=["representative", "full_coverage", "auto"],
        default="auto",
        help="Use representative excerpts, full multi-chunk coverage, or automatic full coverage for long PDFs.",
    )
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
    for page_number, page in enumerate(reader.pages, start=1):
        extracted_text = page.extract_text() or ""
        if extracted_text.strip():
            page_text.append(f"--- Page {page_number} ---\n[PAGE {page_number} TEXT]\n{extracted_text}")
    return "\n\n".join(page_text), len(reader.pages)


def clean_table_cell(cell: Any, max_chars: int = 240) -> str:
    clean = re.sub(r"\s+", " ", str(cell or "")).strip()
    if len(clean) > max_chars:
        return f"{clean[:max_chars].rstrip()} ..."
    return clean


def format_pdf_table(page_number: int, table_index: int, table: list[list[Any]]) -> str:
    rows = []
    for row in table:
        clean_row = [clean_table_cell(cell) for cell in row]
        if any(clean_row):
            rows.append(clean_row)

    if len(rows) < 2:
        return ""

    width = min(max(len(row) for row in rows), 8)
    normalized = [(row + [""] * width)[:width] for row in rows]
    header = normalized[0]
    if not any(header):
        header = [f"Column {index + 1}" for index in range(width)]

    lines = [f"[TABLE page={page_number} index={table_index}]"]
    lines.append("Columns: " + " | ".join(cell or f"Column {index + 1}" for index, cell in enumerate(header)))
    for row_number, row in enumerate(normalized[1:61], start=1):
        values = []
        for cell_index, value in enumerate(row):
            if value:
                column = header[cell_index] or f"Column {cell_index + 1}"
                values.append(f"{column}={value}")
        if values:
            lines.append(f"Row {row_number}: " + "; ".join(values))
    if len(normalized) > 61:
        lines.append(f"... {len(normalized) - 61} more rows")
    lines.append("[/TABLE]")
    return "\n".join(lines)


def extract_with_pdfplumber(pdf_path: Path) -> tuple[str, int | None] | None:
    try:
        import pdfplumber  # type: ignore
    except Exception:
        return None

    page_text = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            sections = []
            extracted_text = page.extract_text() or ""
            if extracted_text.strip():
                sections.append(f"[PAGE {page_number} TEXT]\n{extracted_text}")

            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []
            formatted_tables = [
                formatted
                for table_index, table in enumerate(tables, start=1)
                if (formatted := format_pdf_table(page_number, table_index, table))
            ]
            if formatted_tables:
                sections.append(f"[PAGE {page_number} TABLES]\n" + "\n\n".join(formatted_tables))

            if sections:
                page_text.append(f"--- Page {page_number} ---\n" + "\n\n".join(sections))
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


def extract_pdf_text(pdf_path: Path, debug: DebugLogger | None = None) -> tuple[str, str | None, int | None]:
    attempts = [
        ("pdfplumber", extract_with_pdfplumber),
        ("pypdf", extract_with_pypdf),
        ("pdftotext", extract_with_pdftotext),
        ("strings fallback", extract_with_strings),
    ]
    last_error = None
    last_page_count: int | None = None
    for name, extractor in attempts:
        try:
            result = extractor(pdf_path)
        except Exception as exc:
            last_error = f"{name} failed: {exc}"
            continue
        if not result:
            continue
        text, page_count = result
        if page_count is not None:
            last_page_count = page_count
        text = clean_extracted_text(text)
        if text:
            words = count_words(text)
            warning = None
            if name == "strings fallback":
                warning = "Text extraction used a rough fallback; question quality may be weak. Install pypdf or Poppler/pdftotext for better extraction."
            elif name == "pypdf":
                warning = "PDF layout/table extraction was unavailable; table structure and visual chart details may be underrepresented. Install pdfplumber for better table extraction."
            elif words < LOW_TEXT_WORD_THRESHOLD:
                warning = f"Only {words} words were extracted; this PDF may contain scanned slides, images, or little text."
            if debug:
                debug(
                    f"PDF extraction: {pdf_path.name}",
                    json.dumps(
                        {
                            "extractor": name,
                            "page_count": page_count,
                            "characters": len(text),
                            "words": words,
                            "warning": warning,
                            "contains_table_blocks": "[TABLE " in text,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            return text, warning, page_count

    message = last_error or "No PDF text extractor was available."
    if debug:
        debug(f"PDF extraction failed: {pdf_path.name}", message)
    return "", f"Could not extract usable text. {message}", last_page_count


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


def chunk_text(text: str, max_chars: int = FULL_COVERAGE_TEXT_CHARS) -> list[str]:
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
            for index in range(0, len(block), max_chars):
                chunks.append(block[index : index + max_chars])
            continue
        current.append(block)
        current_len += block_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def resolve_coverage_mode(text: str, args: argparse.Namespace) -> str:
    mode = getattr(args, "coverage_mode", "representative")
    if mode == "auto":
        return "full_coverage" if len(text) > MAX_PROMPT_TEXT_CHARS else "representative"
    return mode


def distribute_counts(total: int, buckets: int) -> list[int]:
    if buckets <= 0:
        return []
    base, remainder = divmod(total, buckets)
    return [base + (1 if index < remainder else 0) for index in range(buckets)]


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
    retry_mc = max(8, int(round(mc * factor)))
    retry_open = max(3, int(round(open_count * factor)))
    return min(args.max_mc, retry_mc), min(args.max_open, retry_open)


def source_pages_from_text(text: str) -> list[int]:
    pages: set[int] = set()
    patterns = [
        r"---\s*Page\s+(\d+)\s*---",
        r"\[PAGE\s+(\d+)\s+(?:TEXT|TABLES)\]",
        r"\[TABLE\s+page=(\d+)\b",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            try:
                page = int(match)
            except (TypeError, ValueError):
                continue
            if page > 0:
                pages.add(page)
    return sorted(pages)


def source_type_from_text(text: str) -> str:
    lower = text.casefold()
    kinds = set()
    if re.search(r"\[table\s+page=|\[page\s+\d+\s+tables\]|\btabelle\b|\btable\b", lower):
        kinds.add("table")
    if re.search(r"\b(abbildung|diagramm|grafik|graph|kurve|chart|figure|plot|axis|achsen|verschiebung)\b", lower):
        kinds.add("diagram")
    if re.search(r"(?:[a-zA-ZÄÖÜäöüß]\s*=\s*[^,\n;.]{1,40})|[πΠΔ∂Σ√∫≤≥→←]|\\(?:Delta|pi|sum|frac)|\bformel\b", text):
        kinds.add("formula")
    if len(kinds) > 1:
        return "mixed"
    if kinds:
        return next(iter(kinds))
    if text.strip():
        return "text"
    return "unknown"


def question_source_meta(source_text: str, chunk_id: str) -> dict[str, Any]:
    source_type = source_type_from_text(source_text)
    return {
        "source_pages": source_pages_from_text(source_text),
        "source_type": source_type,
        "chunk_id": chunk_id,
        "visual_required": source_type in {"diagram", "table", "mixed"},
    }


def attach_question_metadata(questions: list[dict[str, Any]], source_text: str, chunk_id: str) -> None:
    meta = question_source_meta(source_text, chunk_id)
    for question in questions:
        question.setdefault("_meta", dict(meta))


def exam_questions(exam: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        question
        for question in ensure_list(exam.get("multiple_choice")) + ensure_list(exam.get("open_ended"))
        if isinstance(question, dict)
    ]


def detected_visual_count(text: str) -> int:
    table_blocks = len(re.findall(r"\[TABLE\s+page=", text, flags=re.IGNORECASE))
    visual_terms = re.findall(
        r"\b(abbildung|diagramm|grafik|graph|kurve|chart|figure|plot|axis|achsen)\b",
        text,
        flags=re.IGNORECASE,
    )
    return table_blocks + len(visual_terms)


def question_text_blob(question: dict[str, Any]) -> str:
    parts = [
        str(question.get("topic") or ""),
        str(question.get("question") or ""),
        str(question.get("explanation") or ""),
        str(question.get("expected_answer") or ""),
        " ".join(str(item) for item in ensure_list(question.get("key_concepts"))),
    ]
    for option in ensure_list(question.get("options")):
        if isinstance(option, dict):
            parts.append(str(option.get("text") or ""))
    return " ".join(parts)


def is_calculation_question(question: dict[str, Any]) -> bool:
    blob = question_text_blob(question)
    has_number_or_formula = bool(re.search(r"\d|[=+\-*/%πΠΔ∂Σ√∫≤≥]", blob))
    has_calculation_word = bool(
        re.search(
            r"\b(berechne|berechnen|rechnung|calculation|calculate|preis|menge|kosten|erlös|gewinn|elastizität|welfare|überschuss|gleichgewicht)\b",
            blob,
            flags=re.IGNORECASE,
        )
    )
    return has_number_or_formula and has_calculation_word


def is_diagram_question(question: dict[str, Any]) -> bool:
    meta = question.get("_meta") if isinstance(question.get("_meta"), dict) else {}
    if meta.get("source_type") in {"diagram", "table", "mixed"}:
        return True
    return bool(
        re.search(
            r"\b(abbildung|diagramm|grafik|graph|kurve|chart|figure|achsen|interpretier|verschiebung)\b",
            question_text_blob(question),
            flags=re.IGNORECASE,
        )
    )


def question_distribution(exam: dict[str, Any]) -> dict[str, int]:
    mc = ensure_list(exam.get("multiple_choice"))
    open_ended = ensure_list(exam.get("open_ended"))
    questions = [question for question in mc + open_ended if isinstance(question, dict)]
    distribution = {
        "multiple_choice": len(mc),
        "open_ended": len(open_ended),
    }
    diagram_count = sum(1 for question in questions if is_diagram_question(question))
    calculation_count = sum(1 for question in questions if is_calculation_question(question))
    if diagram_count:
        distribution["diagram_interpretation"] = diagram_count
    if calculation_count:
        distribution["calculation"] = calculation_count
    return distribution


def topic_distribution(exam: dict[str, Any]) -> dict[str, int]:
    topics: dict[str, int] = {}
    for question in ensure_list(exam.get("multiple_choice")):
        if not isinstance(question, dict):
            continue
        topic = str(question.get("topic") or "").strip()
        if topic:
            topics[topic] = topics.get(topic, 0) + 1
    for question in ensure_list(exam.get("open_ended")):
        if not isinstance(question, dict):
            continue
        concepts = [str(item).strip() for item in ensure_list(question.get("key_concepts")) if str(item).strip()]
        if concepts:
            topics[concepts[0]] = topics.get(concepts[0], 0) + 1
    return dict(sorted(topics.items(), key=lambda item: (-item[1], item[0]))[:40])


def format_number_list(values: list[int]) -> str:
    return ",".join(str(value) for value in values) if values else "None"


def apply_exam_audit(
    exam: dict[str, Any],
    source_pdf: str,
    source_text: str,
    pages_total: int | None,
    chunks_total: int,
    chunks_processed: int,
    chunks_failed: int,
    warnings: list[str] | None = None,
    visuals_detected: int | None = None,
) -> dict[str, Any]:
    inferred_pages = source_pages_from_text(source_text)
    total_pages = int(pages_total or 0)
    if total_pages <= 0 and inferred_pages:
        total_pages = max(inferred_pages)

    pages_used = sorted(
        {
            int(page)
            for question in exam_questions(exam)
            for page in ensure_list((question.get("_meta") or {}).get("source_pages") if isinstance(question.get("_meta"), dict) else [])
            if isinstance(page, int) or (isinstance(page, str) and page.isdigit())
        }
    )
    if total_pages > 0:
        pages_used = [page for page in pages_used if 1 <= page <= total_pages]
        pages_without_questions = [page for page in range(1, total_pages + 1) if page not in set(pages_used)]
        coverage_ratio = round(len(pages_used) / total_pages, 4)
    else:
        pages_without_questions = []
        coverage_ratio = 0.0

    detected = detected_visual_count(source_text) if visuals_detected is None else max(0, int(visuals_detected))
    visual_questions = sum(
        1
        for question in exam_questions(exam)
        if isinstance(question.get("_meta"), dict)
        and question["_meta"].get("source_type") in {"diagram", "table", "mixed"}
    )
    visuals_used = min(detected, visual_questions) if detected else 0

    audit_warnings = [warning for warning in (warnings or []) if warning]
    if pages_without_questions:
        audit_warnings.append(f"Pages without generated questions: {format_number_list(pages_without_questions)}")
    if chunks_failed:
        audit_warnings.append(f"{chunks_failed} chunk(s) failed during generation.")
    if detected and visuals_used < detected:
        audit_warnings.append(f"Only {visuals_used}/{detected} detected visual/table source marker(s) were reflected in question metadata.")

    exam["audit"] = {
        "generator_version": GENERATOR_VERSION,
        "source_file": source_pdf,
        "pages_total": total_pages,
        "pages_used": pages_used,
        "pages_without_questions": pages_without_questions,
        "coverage_ratio": coverage_ratio,
        "visuals_detected": detected,
        "visuals_used": visuals_used,
        "chunks_total": max(0, int(chunks_total)),
        "chunks_processed": max(0, int(chunks_processed)),
        "chunks_failed": max(0, int(chunks_failed)),
        "question_distribution": question_distribution(exam),
        "topic_distribution": topic_distribution(exam),
        "warnings": audit_warnings,
    }
    return exam


def format_audit_summary(audit: dict[str, Any]) -> str:
    def dotted(label: str, value: Any) -> str:
        return f"{label:.<24}{value}"

    lines = [
        "=" * 50,
        "AUDIT SUMMARY",
        "=" * 50,
        "",
        f"Generator version: {audit.get('generator_version') or 'unknown'}",
        f"Source file: {audit.get('source_file') or 'unknown'}",
        "",
        f"Pages total: {audit.get('pages_total', 0)}",
        f"Pages used: {len(ensure_list(audit.get('pages_used')))}",
        "",
        "Pages without questions:",
        format_number_list([int(page) for page in ensure_list(audit.get("pages_without_questions")) if isinstance(page, int)]),
        "",
        "Coverage ratio:",
        f"{float(audit.get('coverage_ratio') or 0.0):.2f}",
        "",
        "Chunks:",
        f"{audit.get('chunks_processed', 0)} / {audit.get('chunks_total', 0)} processed",
        "",
        "Visuals:",
        f"{audit.get('visuals_used', 0)} / {audit.get('visuals_detected', 0)} used",
        "",
        "Question distribution:",
    ]
    question_counts = audit.get("question_distribution")
    if isinstance(question_counts, dict) and question_counts:
        lines.extend(dotted(str(key), value) for key, value in question_counts.items())
    else:
        lines.append("None")

    topics = audit.get("topic_distribution")
    lines.extend(["", "Topic distribution:"])
    if isinstance(topics, dict) and topics:
        lines.extend(dotted(str(key), value) for key, value in list(topics.items())[:20])
    else:
        lines.append("None")

    warnings = [str(warning) for warning in ensure_list(audit.get("warnings")) if str(warning).strip()]
    lines.extend(["", "Warnings:"])
    lines.extend(warnings or ["None"])
    lines.extend(["", "=" * 50])
    return "\n".join(lines)


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


def escape_invalid_json_backslashes(raw: str) -> str:
    repaired: list[str] = []
    index = 0
    simple_escapes = {'"', "\\", "/", "b", "f", "n", "r", "t"}
    hex_digits = set("0123456789abcdefABCDEF")

    while index < len(raw):
        char = raw[index]
        if char != "\\":
            repaired.append(char)
            index += 1
            continue

        next_char = raw[index + 1] if index + 1 < len(raw) else ""
        if next_char in simple_escapes:
            repaired.append(raw[index : index + 2])
            index += 2
            continue
        if next_char == "u" and index + 5 < len(raw) and all(item in hex_digits for item in raw[index + 2 : index + 6]):
            repaired.append(raw[index : index + 6])
            index += 6
            continue

        repaired.append("\\\\")
        index += 1

    return "".join(repaired)


def load_model_json_candidate(
    raw: str,
    debug: DebugLogger | None = None,
    context: str = "model response",
    audit_warnings: list[str] | None = None,
) -> dict[str, Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        repaired = escape_invalid_json_backslashes(raw)
        if repaired == raw:
            raise
        try:
            parsed = json.loads(repaired)
        except json.JSONDecodeError:
            if debug:
                debug(f"JSON repair failed: {context}", f"{type(exc).__name__}: {exc}")
            raise
        if debug:
            debug(f"JSON repair applied: {context}", f"{type(exc).__name__}: {exc}\nEscaped invalid single-backslash sequences.")
        if audit_warnings is not None:
            audit_warnings.append(f"Parser repair applied for {context}: escaped invalid single-backslash sequences.")
        return parsed


def load_json_from_model(
    raw: str,
    debug: DebugLogger | None = None,
    context: str = "model response",
    audit_warnings: list[str] | None = None,
) -> dict[str, Any]:
    raw = raw.strip()
    try:
        return load_model_json_candidate(raw, debug=debug, context=context, audit_warnings=audit_warnings)
    except json.JSONDecodeError as exc:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        if debug:
            debug(f"JSON object extracted: {context}", f"{type(exc).__name__}: {exc}\nUsing substring {start}:{end + 1}.")
        if audit_warnings is not None:
            audit_warnings.append(f"Parser repair applied for {context}: extracted JSON object from surrounding text.")
        return load_model_json_candidate(raw[start : end + 1], debug=debug, context=context, audit_warnings=audit_warnings)


def call_json_with_retries(
    args: argparse.Namespace,
    prompt: str,
    context: str,
    progress: Callable[[str], None] | None = None,
    debug: DebugLogger | None = None,
    audit_warnings: list[str] | None = None,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(max(0, args.retries) + 1):
        retry_prompt = prompt
        if attempt:
            retry_prompt += (
                "\n\nSTRICT RETRY INSTRUCTION:\n"
                "Return one complete valid JSON object only. No markdown, no comments, no prose outside JSON. "
                "Do not use single-backslash LaTeX commands inside JSON strings; use plain text or doubled JSON backslashes."
            )
        raw = ""
        try:
            if progress:
                progress(f"Waiting for Ollama: {context} (attempt {attempt + 1}/{max(0, args.retries) + 1})")
            if debug:
                debug(
                    f"Ollama request: {context} attempt {attempt + 1}",
                    f"endpoint={args.endpoint}\nmodel={args.model}\ntimeout={args.timeout}\n\nPROMPT:\n{retry_prompt}",
                )
            raw = post_ollama(args.endpoint, args.model, retry_prompt, args.timeout)
            if debug:
                debug(f"Ollama raw response: {context} attempt {attempt + 1}", raw)
            return load_json_from_model(raw, debug=debug, context=f"{context} attempt {attempt + 1}", audit_warnings=audit_warnings)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as exc:
            last_error = exc
            if debug:
                debug(
                    f"Ollama/JSON error: {context} attempt {attempt + 1}",
                    f"{type(exc).__name__}: {exc}" + (f"\n\nRAW RESPONSE:\n{raw}" if raw else ""),
                )
            if attempt < max(0, args.retries):
                if progress:
                    progress(f"RETRY {attempt + 1}/{args.retries} for {context}: {exc}")
                continue
    raise RuntimeError(f"Could not get valid JSON for {context}: {last_error}") from last_error


def ensure_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def first_list(model_exam: dict[str, Any], keys: list[str]) -> list[Any]:
    for key in keys:
        value = model_exam.get(key)
        if isinstance(value, list):
            return value
    questions = model_exam.get("questions")
    if isinstance(questions, dict):
        for key in keys:
            value = questions.get(key)
            if isinstance(value, list):
                return value
    return []


def question_signature(text: str) -> str:
    text = re.sub(r"\s+", " ", text.casefold()).strip()
    text = re.sub(r"[^a-z0-9äöüß ]", "", text)
    return text[:180]


def normalize_mc_items(raw_questions: Any, existing: set[str]) -> list[dict[str, Any]]:
    questions = []
    if not isinstance(raw_questions, list):
        return questions
    for raw in raw_questions:
        if not isinstance(raw, dict):
            continue
        question_text = str(raw.get("question") or "").strip()
        explanation = str(raw.get("explanation") or "").strip()
        if not question_text or not explanation:
            continue
        signature = question_signature(question_text)
        if signature in existing:
            continue
        options = []
        for option in ensure_list(raw.get("options"))[:6]:
            if not isinstance(option, dict):
                continue
            option_text = str(option.get("text") or "").strip()
            if option_text:
                options.append({"text": option_text, "is_correct": bool(option.get("is_correct", False))})
        if len(options) < 4:
            continue
        questions.append(
            {
                "id": "",
                "topic": str(raw.get("topic") or "").strip(),
                "question": question_text,
                "options": options,
                "explanation": explanation,
            }
        )
        existing.add(signature)
    return questions


def normalize_open_items(raw_questions: Any, existing: set[str]) -> list[dict[str, Any]]:
    questions = []
    if not isinstance(raw_questions, list):
        return questions
    for raw in raw_questions:
        if not isinstance(raw, dict):
            continue
        question_text = str(raw.get("question") or "").strip()
        expected = str(raw.get("expected_answer") or "").strip()
        if not question_text or not expected:
            continue
        signature = question_signature(question_text)
        if signature in existing:
            continue
        rubric = raw.get("grading_rubric")
        if not isinstance(rubric, dict):
            rubric = {
                "90-100": "Präzise, vollständige Antwort.",
                "76-89": "Gute Antwort mit kleineren Lücken.",
                "61-75": "Solide Antwort mit merklichen Lücken.",
                "41-60": "Grundsätzlich thematisch, aber unvollständig.",
                "21-40": "Teilweise relevant mit großen Lücken.",
                "0-20": "Falsch, leer oder sehr allgemein.",
            }
        questions.append(
            {
                "id": "",
                "question": question_text,
                "expected_answer": expected,
                "key_concepts": [str(item) for item in ensure_list(raw.get("key_concepts")) if str(item).strip()],
                "grading_rubric": rubric,
                "max_score": 100,
            }
        )
        existing.add(signature)
    return questions


def normalize_exam(
    model_exam: dict[str, Any],
    course: str,
    source_pdf: str,
    extraction_warning: str | None,
    source_word_count: int,
) -> dict[str, Any]:
    mc_questions = []
    mc_source = first_list(model_exam, ["multiple_choice", "multiple_choice_questions", "mc_questions", "mcq", "mc"])
    for index, question in enumerate(mc_source, start=1):
        if not isinstance(question, dict):
            continue
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
        normalized_mc = {
            "id": str(question.get("id") or f"mc-{index:03d}"),
            "topic": str(question.get("topic") or ""),
            "question": str(question.get("question") or "").strip(),
            "options": options,
            "explanation": str(question.get("explanation") or "").strip(),
        }
        if isinstance(question.get("_meta"), dict):
            normalized_mc["_meta"] = question["_meta"]
        mc_questions.append(normalized_mc)

    open_questions = []
    open_source = first_list(model_exam, ["open_ended", "open_ended_questions", "open_questions", "short_answer", "essay_questions"])
    for index, question in enumerate(open_source, start=1):
        if not isinstance(question, dict):
            continue
        rubric = question.get("grading_rubric")
        if not isinstance(rubric, dict):
            rubric = {
                "90-100": "Precise, complete answer covering all key concepts.",
                "61-89": "Mostly correct answer with minor to noticeable gaps.",
                "41-60": "On topic but incomplete or imprecise.",
                "21-40": "Partially relevant with major conceptual gaps.",
                "0-20": "Mostly wrong, vague, or empty.",
            }
        normalized_open = {
            "id": str(question.get("id") or f"open-{index:03d}"),
            "question": str(question.get("question") or "").strip(),
            "expected_answer": str(question.get("expected_answer") or "").strip(),
            "key_concepts": [str(item) for item in ensure_list(question.get("key_concepts")) if str(item).strip()],
            "grading_rubric": rubric,
            "max_score": 100,
        }
        if isinstance(question.get("_meta"), dict):
            normalized_open["_meta"] = question["_meta"]
        open_questions.append(normalized_open)

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


def apply_coverage_metadata(
    exam: dict[str, Any],
    coverage_mode: str,
    source_chunk_count: int,
    processed_chunk_count: int,
    failed_chunk_count: int,
    coverage_warning: str | None,
) -> dict[str, Any]:
    metadata = exam.setdefault("metadata", {})
    metadata["coverage_mode"] = coverage_mode
    metadata["source_chunk_count"] = source_chunk_count
    metadata["processed_chunk_count"] = processed_chunk_count
    metadata["failed_chunk_count"] = failed_chunk_count
    metadata["coverage_warning"] = coverage_warning
    if coverage_warning:
        existing = metadata.get("text_extraction_warning")
        metadata["text_extraction_warning"] = f"{existing} {coverage_warning}".strip() if existing else coverage_warning
    return exam


def build_chunk_coverage_prompt(course: str, source_pdf: str, chunk: str, chunk_index: int, chunk_count: int) -> str:
    return f"""Create compact coverage notes for one source chunk from a university lecture PDF.

Course: {course}
Source PDF: {source_pdf}
Chunk: {chunk_index} of {chunk_count}

Write notes in German, preserving established technical terms, study names, formulas, and author names where useful.
Focus on exam-relevant definitions, theories, models, findings, distinctions, examples, applications, and conceptual traps.
Treat [TABLE ...] blocks as structured source evidence. Do not invent details from charts or graphics that are not present in the extracted text or table blocks.

Return JSON only:
{{
  "coverage_notes": [
    {{
      "topic": "string",
      "exam_targets": ["string"],
      "common_traps": ["string"],
      "source_area": "string"
    }}
  ]
}}

SOURCE CHUNK:
<<<SOURCE
{chunk}
SOURCE>>>"""


def build_chunk_questions_prompt(
    course: str,
    source_pdf: str,
    chunk: str,
    coverage_notes: dict[str, Any],
    target_mc: int,
    target_open: int,
    existing_stems: list[str],
    chunk_index: int,
    chunk_count: int,
) -> str:
    return f"""Generate exam questions from this source chunk.

Course: {course}
Source PDF: {source_pdf}
Chunk: {chunk_index} of {chunk_count}

Question counts:
- Generate exactly {target_mc} multiple-choice questions.
- Generate exactly {target_open} open-ended questions.
- If a count is 0, return an empty array for that section.

Language requirements:
- Write questions, options, explanations, expected answers, key concepts, and rubrics in German.
- Preserve established technical terms, model names, formulas, author names, and source quotations in the original language where appropriate.

Source handling:
- Treat [TABLE ...] blocks as structured source evidence; preserve row/column relationships when creating questions.
- If a chart, curve, or graphic is only visible visually and not described in the extracted text or table blocks, do not invent its details.

Multiple-choice requirements:
- True multiple-choice, not single-choice.
- Each question has 4 to 6 options.
- Every option has a hardcoded boolean is_correct.
- Distractors must be plausible and source-adjacent.

Open-ended requirements:
- Each open question has expected_answer, key_concepts, grading_rubric, and max_score 100.
- Make questions precise, difficult, and gradeable.

Avoid duplicating these already generated stems:
{json.dumps(existing_stems[-80:], ensure_ascii=False, indent=2)}

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
  ],
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

COVERAGE NOTES:
<<<COVERAGE
{json.dumps(coverage_notes, ensure_ascii=False, indent=2)[:18000]}
COVERAGE>>>

SOURCE CHUNK:
<<<SOURCE
{chunk}
SOURCE>>>"""


def generate_full_coverage_exam(
    course: str,
    source_pdf: str,
    text: str,
    extraction_warning: str | None,
    word_count: int,
    args: argparse.Namespace,
    progress: Callable[[str], None] | None = None,
    debug: DebugLogger | None = None,
    pages_total: int | None = None,
) -> dict[str, Any]:
    chunks = chunk_text(text)
    target_mc, target_open = target_counts(word_count, args)
    mc_targets = distribute_counts(target_mc, len(chunks))
    open_targets = distribute_counts(target_open, len(chunks))
    mc_questions: list[dict[str, Any]] = []
    open_questions: list[dict[str, Any]] = []
    seen_mc: set[str] = set()
    seen_open: set[str] = set()
    failed_chunks = 0
    processed_chunks = 0
    audit_warnings: list[str] = []
    if extraction_warning:
        audit_warnings.append(f"Extraction warning: {extraction_warning}")

    for index, chunk in enumerate(chunks, start=1):
        chunk_id = f"chunk-{index:03d}"
        if progress:
            progress(f"Processing chunk {index}/{len(chunks)}")
        try:
            coverage = call_json_with_retries(
                args,
                build_chunk_coverage_prompt(course, source_pdf, chunk, index, len(chunks)),
                f"{source_pdf} coverage chunk {index}",
                progress,
                debug,
                audit_warnings,
            )
            if progress:
                progress(f"Generating questions batch {index}/{len(chunks)}")
            data = call_json_with_retries(
                args,
                build_chunk_questions_prompt(
                    course,
                    source_pdf,
                    chunk,
                    coverage,
                    mc_targets[index - 1],
                    open_targets[index - 1],
                    [question["question"] for question in mc_questions + open_questions],
                    index,
                    len(chunks),
                ),
                f"{source_pdf} questions chunk {index}",
                progress,
                debug,
                audit_warnings,
            )
            raw_mc = first_list(data, ["multiple_choice", "multiple_choice_questions", "mc_questions", "mcq", "mc"])
            raw_open = first_list(data, ["open_ended", "open_ended_questions", "open_questions", "short_answer", "essay_questions"])
            new_mc = normalize_mc_items(raw_mc, seen_mc)
            new_open = normalize_open_items(raw_open, seen_open)
            attach_question_metadata(new_mc, chunk, chunk_id)
            attach_question_metadata(new_open, chunk, chunk_id)
            mc_questions.extend(new_mc)
            open_questions.extend(new_open)
            processed_chunks += 1
            discarded = max(0, len(raw_mc) - len(new_mc)) + max(0, len(raw_open) - len(new_open))
            if discarded:
                audit_warnings.append(f"Discarded {discarded} duplicate or invalid generation(s) in {chunk_id}.")
            if not new_mc and not new_open:
                audit_warnings.append(f"{chunk_id} produced no usable questions.")
            if progress:
                progress(f"Merged {len(mc_questions)} MC / {len(open_questions)} open")
        except RuntimeError as exc:
            failed_chunks += 1
            audit_warnings.append(f"{chunk_id} failed: {exc}")
            if progress:
                progress(f"Chunk {index}/{len(chunks)} failed: {exc}")

    too_many_failed = failed_chunks > len(chunks) // 2
    if processed_chunks == 0 or too_many_failed or len(mc_questions) < args.min_mc or len(open_questions) < args.min_open:
        raise RuntimeError(
            f"Full-coverage generation failed for {source_pdf}: processed {processed_chunks}/{len(chunks)} chunks, "
            f"generated {len(mc_questions)} MC and {len(open_questions)} open questions."
        )

    warnings = []
    if failed_chunks:
        warnings.append(f"Full coverage processed {processed_chunks}/{len(chunks)} chunks; {failed_chunks} chunk(s) failed.")
    if len(mc_questions) < target_mc or len(open_questions) < target_open:
        warnings.append(f"Generated {len(mc_questions)} MC and {len(open_questions)} open questions instead of target {target_mc}/{target_open}.")
    audit_warnings.extend(warnings)

    exam = normalize_exam(
        {"multiple_choice": mc_questions[:target_mc], "open_ended": open_questions[:target_open]},
        course,
        source_pdf,
        extraction_warning,
        word_count,
    )
    exam = apply_coverage_metadata(exam, "full_coverage", len(chunks), processed_chunks, failed_chunks, " ".join(warnings) if warnings else None)
    return apply_exam_audit(exam, source_pdf, text, pages_total, len(chunks), processed_chunks, failed_chunks, audit_warnings)


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
                "question": f"Welche Aussagen werden durch den Quellentext zu folgendem Punkt gestützt: {topic}?",
                "options": [
                    {"text": sentence[:220], "is_correct": True},
                    {"text": "Die Quelle stellt diesen Punkt als unabhängig vom Thema der Lehrveranstaltung dar.", "is_correct": False},
                    {"text": "Die Quelle behandelt dieses Konzept nur als historische Randnotiz ohne Prüfungsrelevanz.", "is_correct": False},
                    {"text": "Der Punkt sollte im Zusammenhang mit den umliegenden Konzepten aus der Quelle verstanden werden.", "is_correct": True},
                ],
                "explanation": "Diese Notfallfrage nutzt direkt extrahierten Quellentext, weil die KI kein vollständig verwertbares Prüfungs-JSON geliefert hat.",
            }
        )
    open_questions = []
    for index, sentence in enumerate(seeds[8:12] or seeds[:4], start=1):
        open_questions.append(
            {
                "id": f"open-{index:03d}",
                "question": f"Erkläre die Prüfungsrelevanz dieses Quellpunkts: {sentence[:180]}",
                "expected_answer": sentence,
                "key_concepts": [word for word in re.findall(r"\b[A-Za-zÄÖÜäöüß][\wÄÖÜäöüß-]{5,}\b", sentence)[:6]],
                "grading_rubric": {
                    "90-100": "Präzise, vollständige Erklärung, die klar am zitierten Quellpunkt verankert ist.",
                    "61-89": "Überwiegend korrekte Erklärung mit kleineren Lücken.",
                    "41-60": "Relevant, aber unvollständig oder unpräzise.",
                    "21-40": "Nur locker mit dem Quellpunkt verbunden.",
                    "0-20": "Fehlende, falsche oder sehr allgemeine Antwort.",
                },
                "max_score": 100,
            }
        )
    warning = extraction_warning or ""
    warning = (warning + " " if warning else "") + "Die KI konnte kein vollständig verwertbares Prüfungs-JSON liefern; diese Datei ist eine kleinere Notfall-Inspektionsprüfung aus direkt extrahiertem Quellentext."
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
    progress: Callable[[str], None] | None = None,
    debug: DebugLogger | None = None,
) -> dict[str, Any] | None:
    course_dir = pdf_path.parent
    course = course_dir.name
    exams_dir = course_dir / "exams"
    exam_dir = exams_dir / slugify(pdf_path.stem)

    if exam_dir.exists() and not args.overwrite:
        print(f"SKIP existing: {exam_dir}", flush=True)
        return None

    text, extraction_warning, page_count = extract_pdf_text(pdf_path, debug=debug)
    word_count = count_words(text)
    if progress:
        pages = page_count if page_count is not None else "unknown"
        progress(f"Extracted {word_count:,} words from {pages} pages")

    audit_warnings: list[str] = []
    if extraction_warning:
        audit_warnings.append(f"Extraction warning: {extraction_warning}")
    base_mc, base_open = target_counts(word_count, args)
    last_error: Exception | None = None
    exam: dict[str, Any] | None = None
    max_attempts = max(0, args.retries) + 1
    coverage_mode = resolve_coverage_mode(text, args)

    if coverage_mode == "full_coverage":
        if progress:
            progress("Using full-coverage generation")
        exam = generate_full_coverage_exam(course, pdf_path.name, text, extraction_warning, word_count, args, progress, debug, page_count)
    else:
        if progress:
            progress("Using representative generation")
        prompt_text = chunk_for_prompt(text)
        if len(text) > len(prompt_text):
            chunk_note = "Long PDF text was chunked into representative excerpts for LLM generation."
            extraction_warning = f"{extraction_warning} {chunk_note}".strip() if extraction_warning else chunk_note
            audit_warnings.append(chunk_note)

        for attempt in range(max_attempts):
            target_mc, target_open = scaled_retry_counts(base_mc, base_open, attempt, args)
            prompt = build_generation_prompt(course, pdf_path.name, prompt_text, target_mc, target_open, extraction_warning)
            if attempt:
                prompt += (
                    "\n\nIMPORTANT RETRY INSTRUCTION:\n"
                    "Your previous response for this PDF was invalid, incomplete, or failed schema validation. Return one complete, syntactically valid JSON object only. "
                    "Do not include markdown, comments, trailing commas, undefined values, or text outside the JSON object. "
                    f"Include both a multiple_choice array with {target_mc} usable questions and an open_ended array with {target_open} usable questions. "
                    "Use shorter but still substantive question and explanation text if needed."
                )
            try:
                if progress:
                    progress(f"Waiting for Ollama: {pdf_path.name} (attempt {attempt + 1}/{max_attempts}, {target_mc} MC / {target_open} open)")
                if debug:
                    debug(
                        f"Ollama request: {pdf_path.name} representative attempt {attempt + 1}",
                        f"endpoint={args.endpoint}\nmodel={args.model}\ntimeout={args.timeout}\n\nPROMPT:\n{prompt}",
                    )
                raw = post_ollama(args.endpoint, args.model, prompt, args.timeout)
                if debug:
                    debug(f"Ollama raw response: {pdf_path.name} representative attempt {attempt + 1}", raw)
                model_exam = load_json_from_model(
                    raw,
                    debug=debug,
                    context=f"{pdf_path.name} representative attempt {attempt + 1}",
                    audit_warnings=audit_warnings,
                )
                exam = normalize_exam(model_exam, course, pdf_path.name, extraction_warning, word_count)
                attach_question_metadata(exam["multiple_choice"], prompt_text, "representative-001")
                attach_question_metadata(exam["open_ended"], prompt_text, "representative-001")
                exam = apply_coverage_metadata(exam, "representative", 1, 1, 0, None)
                exam = apply_exam_audit(exam, pdf_path.name, text, page_count, 1, 1, 0, audit_warnings)
                if progress:
                    progress(f"Ollama returned usable exam JSON for {pdf_path.name}")
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as exc:
                last_error = exc
                audit_warnings.append(f"Representative attempt {attempt + 1} failed validation or parsing: {type(exc).__name__}: {exc}")
                if debug:
                    debug(f"Ollama/JSON error: {pdf_path.name} representative attempt {attempt + 1}", f"{type(exc).__name__}: {exc}")
                if attempt < max_attempts - 1:
                    message = f"RETRY {attempt + 1}/{args.retries} for {pdf_path.name}: {exc}"
                    print(message, flush=True)
                    if progress:
                        progress(message)
                    continue

    if exam is None:
        if not args.allow_heuristic_fallback:
            raise RuntimeError(f"Could not generate questions for {pdf_path.name}: {last_error}") from last_error
        print(f"LLM unavailable for {pdf_path.name}; writing heuristic inspection exam.", flush=True)
        if progress:
            progress(f"Using fallback inspection exam for {pdf_path.name}")
        exam = heuristic_exam(course, pdf_path.name, text, extraction_warning, word_count)
        attach_question_metadata(exam["multiple_choice"], text, "heuristic-001")
        attach_question_metadata(exam["open_ended"], text, "heuristic-001")
        audit_warnings.append("Heuristic fallback exam generated after model output could not be used.")
        exam = apply_coverage_metadata(exam, "heuristic", 1, 1, 0, None)
        exam = apply_exam_audit(exam, pdf_path.name, text, page_count, 1, 1, 0, audit_warnings)

    if "audit" not in exam:
        metadata = exam.get("metadata", {})
        exam = apply_exam_audit(
            exam,
            pdf_path.name,
            text,
            page_count,
            int(metadata.get("source_chunk_count") or 1),
            int(metadata.get("processed_chunk_count") or 1),
            int(metadata.get("failed_chunk_count") or 0),
            audit_warnings,
        )
    if debug:
        debug(f"AUDIT SUMMARY: {pdf_path.name}", format_audit_summary(exam["audit"]))

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
    failures = []
    for pdf in pdfs:
        try:
            result = write_exam_folder(pdf, root, args, template_dir)
            if result:
                generated.append(result)
        except Exception as exc:
            failures.append((pdf.name, str(exc)))
            print(f"FAILED {pdf.name}: {exc}", file=sys.stderr, flush=True)
            continue

    write_index_pages(root)
    if failures:
        print(f"Completed with {len(failures)} failed PDF(s).", file=sys.stderr, flush=True)
        if len(failures) == len(pdfs):
            return 1
    print(f"Done. Generated {len(generated)} exam(s). Index: {root / 'exam_index.html'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Generate local static exam apps from university lecture PDFs."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


DEFAULT_ENDPOINT = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "gemma4:31b-cloud"
GENERATOR_VERSION = "0.1.0"
PROMPT_VERSION = "merged-chunk-v1"
MIN_WORDS_FOR_FULL_EXAM = 3000
LOW_TEXT_WORD_THRESHOLD = 800
MAX_PROMPT_TEXT_CHARS = 52000
FULL_COVERAGE_TEXT_CHARS = 22000
DEFAULT_MAX_PARALLEL_CHUNKS = 2
ALLOWED_MAX_PARALLEL_CHUNKS = {1, 2, 3, 5}
DebugLogger = Callable[[str, str], None]

RUBRIC_TEMPLATES: dict[str, dict[str, Any]] = {
    "standard_concept_explanation": {
        "description": "Use for conceptual explanation questions.",
        "criteria": [
            "Correctly explains the main concept",
            "Uses relevant terminology",
            "Mentions key implications or common traps",
            "Provides a coherent answer structure",
        ],
    },
    "calculation_with_reasoning": {
        "description": "Use for calculation questions with explanation.",
        "criteria": [
            "Uses the correct formula",
            "Shows the correct calculation path",
            "Interprets the result correctly",
            "Avoids common formula or sign errors",
        ],
    },
    "compare_and_contrast": {
        "description": "Use for comparison questions.",
        "criteria": [
            "Defines both concepts",
            "Explains the key difference",
            "Gives an example or implication",
            "Avoids mixing up similar terms",
        ],
    },
    "diagram_interpretation": {
        "description": "Use for questions requiring graph or diagram interpretation.",
        "criteria": [
            "Identifies axes or relevant elements",
            "Interprets the visual relationship correctly",
            "Connects the diagram to the underlying concept",
            "Avoids common graph-reading mistakes",
        ],
    },
}

DEFAULT_RUBRIC_TEMPLATE = "standard_concept_explanation"
DEFAULT_GRADING_RUBRIC = {
    "90-100": "Precise, complete answer covering all key concepts.",
    "61-89": "Mostly correct answer with minor to noticeable gaps.",
    "41-60": "On topic but incomplete or imprecise.",
    "21-40": "Partially relevant with major conceptual gaps.",
    "0-20": "Mostly wrong, vague, or empty.",
}


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
                    "answer_mode": {"type": "string", "enum": ["single_correct", "multiple_correct"]},
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
                    "_meta": {"type": "object"},
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
                    "max_score",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "topic": {"type": "string"},
                    "question_type": {"type": "string"},
                    "question": {"type": "string"},
                    "expected_answer": {"type": "string"},
                    "key_concepts": {"type": "array", "items": {"type": "string"}},
                    "grading_rubric": {"type": "object"},
                    "rubric_template": {"type": "string"},
                    "max_score": {"type": "integer", "const": 100},
                    "_meta": {"type": "object"},
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

Return only valid JSON. Do not use markdown. Do not include commentary outside JSON.
The first character must be {. The final character must be }. Do not wrap JSON in code fences."""


def build_generation_prompt(
    course: str,
    source_pdf: str,
    text: str,
    target_mc: int,
    target_open: int,
    extraction_warning: str | None,
) -> str:
    schema = json.dumps(EXAM_JSON_SCHEMA, ensure_ascii=False, indent=2)
    rubric_templates = json.dumps(RUBRIC_TEMPLATES, ensure_ascii=False, indent=2)
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
- Return per-question _meta where possible. evidence_pages must only contain pages that directly support the question. If exact pages are unknown, use [].
- source_excerpt should be a short source-backed phrase, formula, table row summary, or sentence where possible.
- source_type must be one of text, formula, table, diagram, mixed, unknown. Use unknown when unsure.

Multiple-choice requirements:
- Write question text, options, explanations, topics, expected answers, key concepts, and rubrics in German.
- Preserve established technical terms, model names, formulas, and source quotations in the original language where appropriate.
- Use the source to decide whether each MC question is single_correct or multiple_correct. Do not enforce a global ratio.
- Each question has 4 to 6 options.
- Each option has a hardcoded boolean is_correct.
- There may be one correct answer, several correct answers, all correct, or none correct, but only when justified by the source.
- Include answer_mode: "single_correct" when exactly one option is correct, otherwise "multiple_correct".
- Include a short explanation for each MC question.
- Do not make every question have the same number of correct options.
- Prefer a balanced exam-like mix: basic recall, conceptual traps, application questions, calculation/formula questions, and graph/table/diagram interpretation where the source supports it.
- Include plausible near-miss distractors and common formula, sign, graph-reading, or concept-swap mistakes when source-supported.
- Do not make every question hard and do not make every question a trap.

Open-ended requirements:
- Each open question has max_score 100.
- Include expected_answer, key_concepts, and rubric_template.
- Use one of these reusable rubric templates instead of generating per-question score bands:
{rubric_templates}

Strict JSON output:
- Return only valid JSON.
- Do not use markdown.
- Do not include commentary outside JSON.
- The first character must be {{.
- The final character must be }}.
- Do not wrap JSON in code fences.

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
    parser.add_argument("--target-mode", choices=["auto", "manual"], default="auto")
    parser.add_argument("--target-mc", type=int, help="Manual multiple-choice target. Requires --target-mode manual.")
    parser.add_argument("--target-open", type=int, help="Manual open-ended target. Requires --target-mode manual.")
    parser.add_argument("--min-mc", type=int, default=None, help="Lower safety bound for auto MC targets.")
    parser.add_argument("--max-mc", type=int, default=None, help="Upper safety bound for auto MC targets.")
    parser.add_argument("--min-open", type=int, default=None, help="Lower safety bound for auto open-ended targets.")
    parser.add_argument("--max-open", type=int, default=None, help="Upper safety bound for auto open-ended targets.")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=600, help="LLM request timeout in seconds.")
    parser.add_argument("--retries", type=int, default=2, help="Retries per PDF when the model returns malformed JSON.")
    parser.add_argument(
        "--max-parallel-chunks",
        type=int,
        choices=sorted(ALLOWED_MAX_PARALLEL_CHUNKS),
        default=DEFAULT_MAX_PARALLEL_CHUNKS,
        help="Internal full-coverage chunk parallelism. Not shown in the student UI.",
    )
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


def normalize_max_parallel_chunks(args: argparse.Namespace) -> int:
    value = int(getattr(args, "max_parallel_chunks", DEFAULT_MAX_PARALLEL_CHUNKS) or DEFAULT_MAX_PARALLEL_CHUNKS)
    return value if value in ALLOWED_MAX_PARALLEL_CHUNKS else DEFAULT_MAX_PARALLEL_CHUNKS


def new_timing() -> dict[str, Any]:
    return {
        "total_seconds": 0.0,
        "extraction_seconds": 0.0,
        "chunking_seconds": 0.0,
        "model_seconds": 0.0,
        "validation_seconds": 0.0,
        "repair_seconds": 0.0,
        "write_seconds": 0.0,
        "model_calls": 0,
        "parallelism_used": 1,
    }


def add_seconds(timing: dict[str, Any] | None, key: str, started: float) -> None:
    if timing is not None:
        timing[key] = round(float(timing.get(key) or 0.0) + (time.perf_counter() - started), 4)


def format_seconds(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds >= 10:
        return f"{seconds:.0f}s"
    return f"{seconds:.2f}s"


def format_timing_summary(timing: dict[str, Any]) -> str:
    return "\n".join(
        [
            "=" * 50,
            "TIMING SUMMARY",
            "=" * 50,
            "",
            f"Total duration: {format_seconds(timing.get('total_seconds'))}",
            f"Extraction: {format_seconds(timing.get('extraction_seconds'))}",
            f"Chunking: {format_seconds(timing.get('chunking_seconds'))}",
            f"Model calls: {format_seconds(timing.get('model_seconds'))}",
            f"Validation: {format_seconds(timing.get('validation_seconds'))}",
            f"Repair: {format_seconds(timing.get('repair_seconds'))}",
            f"Writing: {format_seconds(timing.get('write_seconds'))}",
            f"Model calls made: {int(timing.get('model_calls') or 0)}",
            f"Parallelism used: {int(timing.get('parallelism_used') or 1)}",
            "",
            "=" * 50,
        ]
    )


def pdf_file_hash(pdf_path: Path) -> str:
    digest = hashlib.sha256()
    with pdf_path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def preprocessing_cache_key(pdf_path: Path, args: argparse.Namespace, source_hash: str) -> dict[str, str]:
    return {
        "source_file_hash": source_hash,
        "generator_version": GENERATOR_VERSION,
        "extraction_mode": str(getattr(args, "extraction_mode", "auto")),
        "model_name": str(getattr(args, "model", DEFAULT_MODEL)),
        "prompt_version": PROMPT_VERSION,
    }


def preprocessing_cache_path(root: Path, cache_key: dict[str, str]) -> Path:
    encoded = json.dumps(cache_key, sort_keys=True).encode("utf-8")
    name = hashlib.sha256(encoded).hexdigest() + ".json"
    return root / ".exam-generator-cache" / name


def visual_markers_from_text(text: str) -> dict[str, Any]:
    table_pages = sorted(
        {
            int(page)
            for page in re.findall(r"\[TABLE\s+page=(\d+)\b", text, flags=re.IGNORECASE)
            if str(page).isdigit()
        }
    )
    visual_terms = re.findall(
        r"\b(abbildung|diagramm|grafik|graph|kurve|chart|figure|plot|axis|achsen)\b",
        text,
        flags=re.IGNORECASE,
    )
    return {
        "tables": len(table_pages),
        "table_pages": table_pages,
        "visual_term_count": len(visual_terms),
    }


def chunk_map_from_chunks(chunks: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": f"chunk-{index:03d}",
            "chunk_page_range": source_pages_from_text(chunk),
            "characters": len(chunk),
        }
        for index, chunk in enumerate(chunks, start=1)
    ]


def load_preprocessing_cache(
    root: Path,
    pdf_path: Path,
    args: argparse.Namespace,
    debug: DebugLogger | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, str], Path]:
    source_hash = pdf_file_hash(pdf_path)
    cache_key = preprocessing_cache_key(pdf_path, args, source_hash)
    path = preprocessing_cache_path(root, cache_key)
    if not path.exists():
        if progress:
            progress(f"Preprocessing cache miss: {pdf_path.name}")
        if debug:
            debug("Preprocessing cache miss", json.dumps({"source_pdf": pdf_path.name, "path": str(path)}, ensure_ascii=False, indent=2))
        return None, cache_key, path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if debug:
            debug("Preprocessing cache invalid", f"{pdf_path.name}: {type(exc).__name__}: {exc}")
        return None, cache_key, path
    if payload.get("cache_key") != cache_key:
        if debug:
            debug("Preprocessing cache invalid", f"{pdf_path.name}: cache key mismatch")
        return None, cache_key, path
    if "coverage_notes" in payload:
        if debug:
            debug("Preprocessing cache invalid", f"{pdf_path.name}: model-generated coverage_notes are not cached in phase 1")
        return None, cache_key, path
    if progress:
        progress(f"Preprocessing cache hit: {pdf_path.name}")
    if debug:
        debug("Preprocessing cache hit", json.dumps({"source_pdf": pdf_path.name, "path": str(path)}, ensure_ascii=False, indent=2))
    return payload, cache_key, path


def write_preprocessing_cache(
    path: Path,
    cache_key: dict[str, str],
    text: str,
    extraction_warning: str | None,
    page_count: int | None,
    chunks: list[str],
    debug: DebugLogger | None = None,
) -> None:
    payload = {
        "cache_key": cache_key,
        "extracted_text": text,
        "extraction_warning": extraction_warning,
        "page_count": page_count,
        "page_map": source_pages_from_text(text),
        "chunk_map": chunk_map_from_chunks(chunks),
        "visual_markers": visual_markers_from_text(text),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        if debug:
            debug("Preprocessing cache write failed", f"{path}: {type(exc).__name__}: {exc}")
        return
    if debug:
        debug("Preprocessing cache written", json.dumps({"path": str(path), "source_pages": payload["page_map"]}, ensure_ascii=False, indent=2))


def distribute_counts(total: int, buckets: int) -> list[int]:
    if buckets <= 0:
        return []
    base, remainder = divmod(total, buckets)
    return [base + (1 if index < remainder else 0) for index in range(buckets)]


def positive_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def clamp_optional(value: int, minimum: int | None, maximum: int | None) -> int:
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return max(0, value)


def formula_marker_count(text: str) -> int:
    return len(
        re.findall(
            r"(?:[a-zA-ZÄÖÜäöüß]\s*=\s*[^,\n;.]{1,40})|[πΠΔ∂Σ√∫≤≥→←]|\\(?:Delta|pi|sum|frac)|\bformel\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def repetition_ratio(text: str) -> float:
    words = [word.casefold() for word in re.findall(r"\b[\wÄÖÜäöüß-]{3,}\b", text)]
    if not words:
        return 1.0
    return len(set(words)) / len(words)


def resolve_target_plan(
    text: str,
    word_count: int,
    page_count: int | None,
    chunk_count: int,
    coverage_mode: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    inferred_pages = source_pages_from_text(text)
    pages = int(page_count or 0)
    if pages <= 0 and inferred_pages:
        pages = max(inferred_pages)
    chunks = max(1, int(chunk_count or 1))
    markers = visual_markers_from_text(text)
    visuals = max(0, detected_visual_count(text))
    tables = max(0, int(markers.get("tables") or 0))
    formulas = formula_marker_count(text)
    formula_density = round(formulas / max(1, word_count) * 1000, 4)
    target_mode = str(getattr(args, "target_mode", "auto") or "auto").strip()
    target_mc = positive_int(getattr(args, "target_mc", None))
    target_open = positive_int(getattr(args, "target_open", None))
    min_mc = positive_int(getattr(args, "min_mc", None))
    max_mc = positive_int(getattr(args, "max_mc", None))
    min_open = positive_int(getattr(args, "min_open", None))
    max_open = positive_int(getattr(args, "max_open", None))

    exact_pair_manual = (
        min_mc is not None
        and max_mc is not None
        and min_open is not None
        and max_open is not None
        and min_mc == max_mc
        and min_open == max_open
    )
    explicit_manual = target_mode == "manual" and target_mc is not None and target_open is not None
    if explicit_manual or exact_pair_manual:
        mc = target_mc if explicit_manual else min_mc
        open_count = target_open if explicit_manual else min_open
        assert mc is not None and open_count is not None
        return {
            "mode": "manual",
            "page_count": pages,
            "word_count": max(0, int(word_count)),
            "chunk_count": chunks,
            "visuals_detected": visuals,
            "tables_detected": tables,
            "formula_density": formula_density,
            "target_multiple_choice": max(0, int(mc)),
            "target_open_ended": max(0, int(open_count)),
            "reason": "manual targets from explicit target values" if explicit_manual else "manual targets from exact min/max pairs",
        }
    if target_mode == "manual":
        raise ValueError("target_mode=manual requires --target-mc and --target-open, or exact min/max pairs.")

    size_basis = pages if pages > 0 else max(1, round(word_count / 450))
    if pages > 0:
        tiny_source = size_basis <= 2
        small_source = size_basis <= 8
        medium_source = size_basis <= 25
        large_source = size_basis <= 60
    else:
        tiny_source = word_count < LOW_TEXT_WORD_THRESHOLD
        small_source = word_count < MIN_WORDS_FOR_FULL_EXAM
        medium_source = word_count < 9000
        large_source = word_count < 22000
    if tiny_source:
        bucket = "tiny"
        mc, open_count = 10, 4
        bucket_range = "1-2 pages or very low word count"
    elif small_source:
        bucket = "small"
        mc, open_count = 20, 6
        bucket_range = "3-8 pages or short source"
    elif medium_source:
        bucket = "medium"
        mc, open_count = 38, 13
        bucket_range = "9-25 pages or medium source"
    elif large_source:
        bucket = "large"
        mc, open_count = 58, 21
        bucket_range = "26-60 pages or large source"
    else:
        bucket = "very_large"
        mc, open_count = 68, 24
        bucket_range = "very large capped source"

    adjustment = 0
    density_notes: list[str] = []
    if tables or visuals >= 4:
        adjustment += 2
        density_notes.append("visual/table evidence")
    if formula_density >= 2.0:
        adjustment += 2
        density_notes.append("formula-dense source")
    if chunks >= 4 and bucket in {"medium", "large", "very_large"}:
        adjustment += min(4, chunks // 2)
        density_notes.append("many source chunks")
    if repetition_ratio(text) < 0.18:
        adjustment -= 3
        density_notes.append("high repetition")
    if word_count < 350:
        adjustment -= 2
        density_notes.append("very sparse text")

    mc = clamp_optional(mc + adjustment, None, 70)
    open_count = clamp_optional(open_count + round(adjustment / 3), None, 25)
    mc = max(5, mc)
    open_count = max(2, open_count)

    had_bounds = any(value is not None for value in [min_mc, max_mc, min_open, max_open])
    bounded_mc = clamp_optional(mc, min_mc, max_mc)
    bounded_open = clamp_optional(open_count, min_open, max_open)
    mode = "auto_with_bounds" if had_bounds and (bounded_mc != mc or bounded_open != open_count) else "auto"
    if had_bounds and mode == "auto":
        mode = "auto_with_bounds"
    reason = f"{bucket} planner bucket from {bucket_range}; coverage_mode={coverage_mode}"
    if density_notes:
        reason += "; adjusted for " + ", ".join(density_notes)
    if mode == "auto_with_bounds":
        reason += "; clamped by configured bounds"
    return {
        "mode": mode,
        "page_count": pages,
        "word_count": max(0, int(word_count)),
        "chunk_count": chunks,
        "visuals_detected": visuals,
        "tables_detected": tables,
        "formula_density": formula_density,
        "target_multiple_choice": bounded_mc,
        "target_open_ended": bounded_open,
        "reason": reason,
    }


def target_counts(words: int, args: argparse.Namespace) -> tuple[int, int]:
    plan = resolve_target_plan("", words, None, 1, str(getattr(args, "coverage_mode", "auto") or "auto"), args)
    return int(plan["target_multiple_choice"]), int(plan["target_open_ended"])


def scaled_retry_counts(mc: int, open_count: int, attempt: int, args: argparse.Namespace) -> tuple[int, int]:
    if attempt <= 0:
        return mc, open_count
    factor = 0.85 if attempt == 1 else 0.7
    retry_mc = max(1, int(round(mc * factor)))
    retry_open = max(1, int(round(open_count * factor)))
    return (
        clamp_optional(retry_mc, None, positive_int(getattr(args, "max_mc", None))),
        clamp_optional(retry_open, None, positive_int(getattr(args, "max_open", None))),
    )


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
    pages = source_pages_from_text(source_text)
    return {
        "chunk_id": chunk_id,
        "chunk_page_range": pages,
        "evidence_pages": [],
        "source_excerpt": None,
        "source_type": "unknown",
        "visual_required": False,
        "source_pages": [],
    }


def attach_question_metadata(questions: list[dict[str, Any]], source_text: str, chunk_id: str) -> None:
    meta = question_source_meta(source_text, chunk_id)
    for question in questions:
        existing = question.get("_meta") if isinstance(question.get("_meta"), dict) else {}
        merged = dict(meta)
        if existing:
            evidence_pages = [
                int(page)
                for page in ensure_list(existing.get("evidence_pages", existing.get("source_pages")))
                if isinstance(page, int) or (isinstance(page, str) and page.isdigit())
            ]
            merged["evidence_pages"] = sorted({page for page in evidence_pages if page > 0})
            merged["source_pages"] = list(merged["evidence_pages"])
            excerpt = existing.get("source_excerpt")
            merged["source_excerpt"] = str(excerpt).strip()[:500] if excerpt else None
            source_type = str(existing.get("source_type") or "unknown").strip()
            if source_type in {"text", "formula", "table", "diagram", "mixed", "unknown"}:
                merged["source_type"] = source_type
            merged["visual_required"] = bool(existing.get("visual_required", False))
        question["_meta"] = merged


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
    requires_calculation = bool(
        re.search(
            r"\b(berechne|berechnen|berechnet|bestimme|ermittle|rechne|calculation|calculate|derive|solve|algebraisch|numerisch)\b",
            blob,
            flags=re.IGNORECASE,
        )
    )
    return has_number_or_formula and requires_calculation


def is_diagram_question(question: dict[str, Any]) -> bool:
    meta = question.get("_meta") if isinstance(question.get("_meta"), dict) else {}
    return meta.get("source_type") == "diagram" or bool(meta.get("visual_required"))


def is_formula_question(question: dict[str, Any]) -> bool:
    meta = question.get("_meta") if isinstance(question.get("_meta"), dict) else {}
    blob = question_text_blob(question)
    if meta.get("source_type") == "formula":
        return True
    has_symbolic_formula = bool(
        re.search(r"[a-zA-ZÄÖÜäöüß]\s*=\s*[^,\n;.]{1,40}|[πΠΔ∂Σ√∫≤≥]|\\(?:Delta|pi|sum|frac)", blob, flags=re.IGNORECASE)
    )
    centers_formula = bool(
        re.search(
            r"\b(formel|gleichung|funktion|ableitung|transform|anwenden|erkläre|wähle)\b",
            blob,
            flags=re.IGNORECASE,
        )
    )
    return has_symbolic_formula and centers_formula


def is_definition_question(question: dict[str, Any]) -> bool:
    blob = question_text_blob(question)
    return bool(
        re.search(
            r"\b(definiere|definition|was ist|was versteht man|begriff|bezeichnet|nenne)\b",
            blob,
            flags=re.IGNORECASE,
        )
    )


def is_application_question(question: dict[str, Any]) -> bool:
    blob = question_text_blob(question)
    return bool(
        re.search(
            r"\b(anwend|szenario|fall|beispiel|unternehmen|markt|situation|gegeben|use-case|praxis|folge für)\b",
            blob,
            flags=re.IGNORECASE,
        )
    )


def question_counts(exam: dict[str, Any]) -> dict[str, int]:
    return {
        "multiple_choice": len(ensure_list(exam.get("multiple_choice"))),
        "open_ended": len(ensure_list(exam.get("open_ended"))),
    }


def skill_tags(exam: dict[str, Any]) -> dict[str, int]:
    tags = {
        "conceptual": 0,
        "definition": 0,
        "application": 0,
        "formula": 0,
        "calculation": 0,
        "diagram_interpretation": 0,
    }
    for question in exam_questions(exam):
        tagged = False
        if is_definition_question(question):
            tags["definition"] += 1
            tagged = True
        if is_application_question(question):
            tags["application"] += 1
            tagged = True
        if is_formula_question(question):
            tags["formula"] += 1
            tagged = True
        if is_calculation_question(question):
            tags["calculation"] += 1
            tagged = True
        if is_diagram_question(question):
            tags["diagram_interpretation"] += 1
            tagged = True
        if not tagged or re.search(r"\b(warum|erkläre|beziehung|unterschied|wirkung|konzept|theorie)\b", question_text_blob(question), flags=re.IGNORECASE):
            tags["conceptual"] += 1
    return tags


def answer_mode_for_mc(question: dict[str, Any]) -> str:
    explicit = str(question.get("answer_mode") or "").strip()
    if explicit in {"single_correct", "multiple_correct"}:
        return explicit
    correct_count = sum(1 for option in ensure_list(question.get("options")) if isinstance(option, dict) and bool(option.get("is_correct")))
    return "single_correct" if correct_count == 1 else "multiple_correct"


def answer_modes(exam: dict[str, Any]) -> dict[str, int]:
    counts = {"single_correct": 0, "multiple_correct": 0, "free_text": 0}
    for question in ensure_list(exam.get("multiple_choice")):
        if isinstance(question, dict):
            counts[answer_mode_for_mc(question)] += 1
    counts["free_text"] = len(ensure_list(exam.get("open_ended")))
    return counts


def source_traceability(exam: dict[str, Any]) -> dict[str, int]:
    questions = exam_questions(exam)
    with_evidence = 0
    with_excerpt = 0
    mc_without = 0
    open_without = 0
    for collection_name, collection in [("mc", ensure_list(exam.get("multiple_choice"))), ("open", ensure_list(exam.get("open_ended")))]:
        for question in collection:
            if not isinstance(question, dict):
                continue
            meta = question.get("_meta") if isinstance(question.get("_meta"), dict) else {}
            has_evidence = bool(ensure_list(meta.get("evidence_pages")))
            if has_evidence:
                with_evidence += 1
            elif collection_name == "mc":
                mc_without += 1
            else:
                open_without += 1
            if str(meta.get("source_excerpt") or "").strip():
                with_excerpt += 1
    return {
        "questions_total": len(questions),
        "questions_with_evidence_pages": with_evidence,
        "questions_without_evidence_pages": len(questions) - with_evidence,
        "mc_without_evidence_pages": mc_without,
        "open_without_evidence_pages": open_without,
        "questions_with_source_excerpt": with_excerpt,
    }


def question_distribution(exam: dict[str, Any]) -> dict[str, int]:
    distribution = question_counts(exam)
    tags = skill_tags(exam)
    if tags["diagram_interpretation"]:
        distribution["diagram_interpretation"] = tags["diagram_interpretation"]
    if tags["calculation"]:
        distribution["calculation"] = tags["calculation"]
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
    timing: dict[str, Any] | None = None,
    repair_generation: dict[str, Any] | None = None,
    pages_covered_by_chunks: list[int] | None = None,
    target_planning: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inferred_pages = source_pages_from_text(source_text)
    total_pages = int(pages_total or 0)
    if total_pages <= 0 and inferred_pages:
        total_pages = max(inferred_pages)

    derived_chunk_pages = sorted(
        {
            int(page)
            for question in exam_questions(exam)
            for page in ensure_list((question.get("_meta") or {}).get("chunk_page_range") if isinstance(question.get("_meta"), dict) else [])
            if isinstance(page, int) or (isinstance(page, str) and page.isdigit())
        }
    )
    pages_covered = sorted(set(pages_covered_by_chunks or derived_chunk_pages))
    pages_with_evidence = sorted(
        {
            int(page)
            for question in exam_questions(exam)
            for page in ensure_list((question.get("_meta") or {}).get("evidence_pages") if isinstance(question.get("_meta"), dict) else [])
            if isinstance(page, int) or (isinstance(page, str) and page.isdigit())
        }
    )
    if total_pages > 0:
        pages_covered = [page for page in pages_covered if 1 <= page <= total_pages]
        pages_with_evidence = [page for page in pages_with_evidence if 1 <= page <= total_pages]
        pages_without_questions = [page for page in range(1, total_pages + 1) if page not in set(pages_with_evidence)]
        coverage_ratio_chunks = round(len(pages_covered) / total_pages, 4)
        coverage_ratio_evidence = round(len(pages_with_evidence) / total_pages, 4)
    else:
        pages_without_questions = []
        coverage_ratio_chunks = 0.0
        coverage_ratio_evidence = 0.0

    detected = detected_visual_count(source_text) if visuals_detected is None else max(0, int(visuals_detected))
    visual_questions = sum(
        1
        for question in exam_questions(exam)
        if isinstance(question.get("_meta"), dict)
        and question["_meta"].get("visual_required")
    )
    visuals_used = min(detected, visual_questions) if detected else 0

    audit_warnings = [warning for warning in (warnings or []) if warning]
    if pages_without_questions:
        audit_warnings.append(f"Pages without exact question evidence: {format_number_list(pages_without_questions)}")
    if chunks_failed:
        audit_warnings.append(f"{chunks_failed} chunk(s) failed during generation.")
    if detected and visuals_used < detected:
        audit_warnings.append(f"Only {visuals_used}/{detected} detected visual/table source marker(s) were reflected in question metadata.")

    exam["audit"] = {
        "generator_version": GENERATOR_VERSION,
        "source_file": source_pdf,
        "pages_total": total_pages,
        "pages_used": pages_with_evidence,
        "pages_without_questions": pages_without_questions,
        "coverage_ratio": coverage_ratio_evidence,
        "pages_covered_by_chunks": pages_covered,
        "pages_with_evidence": pages_with_evidence,
        "coverage_ratio_chunks": coverage_ratio_chunks,
        "coverage_ratio_evidence": coverage_ratio_evidence,
        "visuals_detected": detected,
        "visuals_used": visuals_used,
        "chunks_total": max(0, int(chunks_total)),
        "chunks_processed": max(0, int(chunks_processed)),
        "chunks_failed": max(0, int(chunks_failed)),
        "target_planning": target_planning
        or {
            "mode": "unknown",
            "page_count": total_pages,
            "word_count": count_words(source_text),
            "chunk_count": max(0, int(chunks_total)),
            "visuals_detected": detected,
            "tables_detected": int(visual_markers_from_text(source_text).get("tables") or 0),
            "formula_density": round(formula_marker_count(source_text) / max(1, count_words(source_text)) * 1000, 4),
            "target_multiple_choice": len(ensure_list(exam.get("multiple_choice"))),
            "target_open_ended": len(ensure_list(exam.get("open_ended"))),
            "reason": "target planning metadata was not supplied",
        },
        "question_counts": question_counts(exam),
        "skill_tags": skill_tags(exam),
        "answer_modes": answer_modes(exam),
        "source_traceability": source_traceability(exam),
        "question_distribution": question_distribution(exam),
        "topic_distribution": topic_distribution(exam),
        "repair_generation": repair_generation
        or {
            "attempted": False,
            "missing_multiple_choice": 0,
            "missing_open_ended": 0,
            "successful": False,
            "attempts": 0,
        },
        "timing": timing or new_timing(),
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
        f"Pages covered by chunks: {len(ensure_list(audit.get('pages_covered_by_chunks')))}",
        f"Pages with exact evidence: {len(ensure_list(audit.get('pages_with_evidence')))}",
        "",
        "Pages without exact evidence:",
        format_number_list([int(page) for page in ensure_list(audit.get("pages_without_questions")) if isinstance(page, int)]),
        "",
        "Coverage ratios:",
        f"chunks={float(audit.get('coverage_ratio_chunks') or 0.0):.2f} evidence={float(audit.get('coverage_ratio_evidence') or 0.0):.2f}",
        "",
        "Chunks:",
        f"{audit.get('chunks_processed', 0)} / {audit.get('chunks_total', 0)} processed",
        "",
        "Visuals:",
        f"{audit.get('visuals_used', 0)} / {audit.get('visuals_detected', 0)} used",
        "",
        "Target planning:",
    ]
    target_plan = audit.get("target_planning")
    if isinstance(target_plan, dict) and target_plan:
        lines.extend(
            [
                dotted("mode", target_plan.get("mode", "unknown")),
                dotted("target MC", target_plan.get("target_multiple_choice", 0)),
                dotted("target open", target_plan.get("target_open_ended", 0)),
            ]
        )
    else:
        lines.append("None")

    lines.extend(["", "Question counts:"])
    counts = audit.get("question_counts") or audit.get("question_distribution")
    if isinstance(counts, dict) and counts:
        lines.extend(dotted(str(key), value) for key, value in counts.items())
    else:
        lines.append("None")

    tags = audit.get("skill_tags")
    lines.extend(["", "Skill tags:"])
    if isinstance(tags, dict) and tags:
        lines.extend(dotted(str(key), value) for key, value in tags.items())
    else:
        lines.append("None")

    modes = audit.get("answer_modes")
    lines.extend(["", "Answer modes:"])
    if isinstance(modes, dict) and modes:
        lines.extend(dotted(str(key), value) for key, value in modes.items())
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
    timing: dict[str, Any] | None = None,
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
            model_started = time.perf_counter()
            if timing is not None:
                timing["model_calls"] = int(timing.get("model_calls") or 0) + 1
            raw = post_ollama(args.endpoint, args.model, retry_prompt, args.timeout)
            model_elapsed = time.perf_counter() - model_started
            if timing is not None:
                timing["model_seconds"] = round(float(timing.get("model_seconds") or 0.0) + model_elapsed, 4)
            if debug:
                debug(
                    f"Ollama call finished: {context} attempt {attempt + 1}",
                    f"duration_seconds={model_elapsed:.4f}",
                )
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
        normalized = {
            "id": "",
            "topic": str(raw.get("topic") or "").strip(),
            "question": question_text,
            "options": options,
            "explanation": explanation,
        }
        answer_mode = str(raw.get("answer_mode") or "").strip()
        if answer_mode in {"single_correct", "multiple_correct"}:
            normalized["answer_mode"] = answer_mode
        if isinstance(raw.get("_meta"), dict):
            normalized["_meta"] = raw["_meta"]
        questions.append(normalized)
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
        rubric_template = str(raw.get("rubric_template") or DEFAULT_RUBRIC_TEMPLATE).strip()
        if rubric_template not in RUBRIC_TEMPLATES:
            rubric_template = DEFAULT_RUBRIC_TEMPLATE
        normalized = {
            "id": "",
            "topic": str(raw.get("topic") or "").strip(),
            "question_type": "open_ended",
            "question": question_text,
            "expected_answer": expected,
            "key_concepts": [str(item) for item in ensure_list(raw.get("key_concepts")) if str(item).strip()],
            "rubric_template": rubric_template,
            "max_score": 100,
        }
        if isinstance(rubric, dict):
            normalized["grading_rubric"] = rubric
        if isinstance(raw.get("_meta"), dict):
            normalized["_meta"] = raw["_meta"]
        questions.append(
            normalized
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
        answer_mode = str(question.get("answer_mode") or "").strip()
        if answer_mode in {"single_correct", "multiple_correct"}:
            normalized_mc["answer_mode"] = answer_mode
        if isinstance(question.get("_meta"), dict):
            normalized_mc["_meta"] = question["_meta"]
        mc_questions.append(normalized_mc)

    open_questions = []
    open_source = first_list(model_exam, ["open_ended", "open_ended_questions", "open_questions", "short_answer", "essay_questions"])
    for index, question in enumerate(open_source, start=1):
        if not isinstance(question, dict):
            continue
        rubric = question.get("grading_rubric")
        rubric_template = str(question.get("rubric_template") or DEFAULT_RUBRIC_TEMPLATE).strip()
        if rubric_template not in RUBRIC_TEMPLATES:
            rubric_template = DEFAULT_RUBRIC_TEMPLATE
        normalized_open = {
            "id": str(question.get("id") or f"open-{index:03d}"),
            "topic": str(question.get("topic") or ""),
            "question_type": "open_ended",
            "question": str(question.get("question") or "").strip(),
            "expected_answer": str(question.get("expected_answer") or "").strip(),
            "key_concepts": [str(item) for item in ensure_list(question.get("key_concepts")) if str(item).strip()],
            "rubric_template": rubric_template,
            "max_score": 100,
        }
        if isinstance(rubric, dict):
            normalized_open["grading_rubric"] = rubric
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
        "rubric_templates": dict(RUBRIC_TEMPLATES),
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


def build_merged_chunk_prompt(
    course: str,
    source_pdf: str,
    chunk: str,
    target_mc: int,
    target_open: int,
    chunk_index: int,
    chunk_count: int,
) -> str:
    rubric_templates = json.dumps(RUBRIC_TEMPLATES, ensure_ascii=False, indent=2)
    return f"""Generate coverage notes and exam questions from this source chunk in one JSON object.

Course: {course}
Source PDF: {source_pdf}
Chunk: {chunk_index} of {chunk_count}

Internal analysis:
- First analyze the chunk internally for exam-relevant concepts, formulas, traps, diagrams, tables, examples, and likely exam targets.
- Do not output your analysis process.
- Return only the final JSON object.

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
- coverage_notes must summarize the exam-relevant coverage behind the generated questions.
- If you generate any question, coverage_notes must contain at least one note.
- evidence_pages in per-question _meta should include only pages that directly support the question. If exact evidence pages are unknown, use an empty array.
- source_excerpt should be a short source-backed phrase, formula, table row summary, or sentence where possible.
- source_type must be one of text, formula, table, diagram, mixed, unknown. Use unknown when unsure.
- chunk_page_range is broad provenance, not exact evidence.

Multiple-choice requirements:
- Use the source to decide whether each MC question is single_correct or multiple_correct. Do not enforce a global ratio.
- Each question has 4 to 6 options.
- Every option has a hardcoded boolean is_correct.
- Include answer_mode: "single_correct" when exactly one option is correct, otherwise "multiple_correct".
- Distractors must be plausible and source-adjacent.
- Prefer a balanced exam-like mix: basic recall, conceptual traps, application questions, calculation/formula questions, and graph/table/diagram interpretation where the source supports it.
- Useful traps include near-miss distractors, common formula mistakes, sign mistakes, confusing similar concepts, MC/TC swaps, fixed-cost/marginal-cost mistakes, Pmax via P=0 instead of Q=0, Cournot/Bertrand swaps, substitution/income-effect swaps, normal/inferior/Giffen confusion, elastic/inelastic revenue direction mistakes, monopoly P/MR/MC confusion, and graph-reading traps when supported.
- Do not make every question hard and do not make every question a trap.

Open-ended requirements:
- Each open question has topic, question_type "open_ended", expected_answer, key_concepts, rubric_template, and max_score 100.
- Use exactly one rubric_template key from this object:
{rubric_templates}
- Do not generate full score-band grading_rubric objects for open-ended questions.
- Make questions precise, difficult, and gradeable.

Strict JSON output:
- Return only valid JSON.
- Do not use markdown.
- Do not include commentary outside JSON.
- The first character must be {{.
- The final character must be }}.
- Do not wrap JSON in code fences.

Return exactly this JSON shape:
{{
  "coverage_notes": [
    {{
      "topic": "string",
      "exam_targets": ["string"],
      "common_traps": ["string"],
      "source_area": "string"
    }}
  ],
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
      "answer_mode": "multiple_correct",
      "explanation": "string",
      "_meta": {{
        "evidence_pages": [],
        "source_excerpt": null,
        "source_type": "text",
        "visual_required": false
      }}
    }}
  ],
  "open_ended": [
    {{
      "topic": "string",
      "question_type": "open_ended",
      "question": "string",
      "expected_answer": "string",
      "key_concepts": ["string"],
      "rubric_template": "standard_concept_explanation",
      "max_score": 100,
      "_meta": {{
        "evidence_pages": [],
        "source_excerpt": null,
        "source_type": "text",
        "visual_required": false
      }}
    }}
  ],
  "audit_hints": {{
    "source_pages_used": [],
    "visuals_used": [],
    "topics": []
  }}
}}

SOURCE CHUNK:
<<<SOURCE
{chunk}
SOURCE>>>"""


def build_refill_prompt(
    course: str,
    source_pdf: str,
    source_text: str,
    coverage_notes: list[dict[str, Any]],
    missing_mc: int,
    missing_open: int,
    existing_stems: list[str],
    underrepresented_topics: list[str],
) -> str:
    rubric_templates = json.dumps(RUBRIC_TEMPLATES, ensure_ascii=False, indent=2)
    return f"""Generate only the missing exam questions needed to refill an exam.

Course: {course}
Source PDF: {source_pdf}

Missing counts:
- Generate exactly {missing_mc} multiple-choice questions.
- Generate exactly {missing_open} open-ended questions.
- If a count is 0, return an empty array for that section.

Prefer these underrepresented topics when the source supports them:
{json.dumps(underrepresented_topics[:20], ensure_ascii=False, indent=2)}

Avoid duplicating these already generated stems:
{json.dumps(existing_stems[-120:], ensure_ascii=False, indent=2)}

Use the coverage notes as planning context, but rely only on the source text for facts:
{json.dumps(coverage_notes[:80], ensure_ascii=False, indent=2)[:18000]}

Open-ended questions must use one rubric_template key from:
{rubric_templates}

Question quality:
- Prefer a balanced exam-like MC mix: recall, conceptual traps, application, formula/calculation, and graph/table/diagram interpretation where the source supports it.
- Use plausible near-miss distractors and common source-supported mistakes, but do not make every question hard or trap-based.
- Use answer_mode "single_correct" or "multiple_correct" per MC question based on the actual correct options. Do not enforce a fixed ratio.
- Return per-question _meta where possible: evidence_pages, source_excerpt, source_type, visual_required.
- evidence_pages must only contain pages that directly support the question; use [] when exact pages are unknown.
- source_type must be one of text, formula, table, diagram, mixed, unknown; use unknown when unsure.

Strict JSON output:
- Return only valid JSON.
- Do not use markdown.
- Do not include commentary outside JSON.
- The first character must be {{.
- The final character must be }}.
- Do not wrap JSON in code fences.

Return exactly this JSON shape:
{{
  "multiple_choice": [],
  "open_ended": [],
  "audit_hints": {{
    "source_pages_used": [],
    "visuals_used": [],
    "topics": []
  }}
}}

SOURCE TEXT:
<<<SOURCE
{source_text[:MAX_PROMPT_TEXT_CHARS]}
SOURCE>>>"""


def validate_merged_chunk_response(data: dict[str, Any], chunk_id: str) -> None:
    if not isinstance(data, dict):
        raise ValueError(f"{chunk_id} did not return a JSON object.")
    for key in ["coverage_notes", "multiple_choice", "open_ended", "audit_hints"]:
        if key not in data:
            raise ValueError(f"{chunk_id} response is missing {key}.")
    if not isinstance(data.get("coverage_notes"), list):
        raise ValueError(f"{chunk_id} coverage_notes must be an array.")
    if not isinstance(data.get("multiple_choice"), list):
        raise ValueError(f"{chunk_id} multiple_choice must be an array.")
    if not isinstance(data.get("open_ended"), list):
        raise ValueError(f"{chunk_id} open_ended must be an array.")
    if not isinstance(data.get("audit_hints"), dict):
        raise ValueError(f"{chunk_id} audit_hints must be an object.")


def normalized_coverage_notes(data: dict[str, Any]) -> list[dict[str, Any]]:
    notes = []
    for raw in ensure_list(data.get("coverage_notes")):
        if not isinstance(raw, dict):
            continue
        topic = str(raw.get("topic") or "").strip()
        if not topic:
            continue
        notes.append(
            {
                "topic": topic,
                "exam_targets": [str(item) for item in ensure_list(raw.get("exam_targets")) if str(item).strip()],
                "common_traps": [str(item) for item in ensure_list(raw.get("common_traps")) if str(item).strip()],
                "source_area": str(raw.get("source_area") or "").strip(),
            }
        )
    return notes


def merge_model_timing(timing: dict[str, Any], partial: dict[str, Any] | None) -> None:
    if not partial:
        return
    timing["model_calls"] = int(timing.get("model_calls") or 0) + int(partial.get("model_calls") or 0)
    timing["model_seconds"] = round(float(timing.get("model_seconds") or 0.0) + float(partial.get("model_seconds") or 0.0), 4)


class ChunkGenerationError(RuntimeError):
    def __init__(self, message: str, timing: dict[str, Any], warnings: list[str]) -> None:
        super().__init__(message)
        self.timing = timing
        self.warnings = warnings


def generate_chunk_data(
    course: str,
    source_pdf: str,
    chunk: str,
    target_mc: int,
    target_open: int,
    index: int,
    chunk_count: int,
    args: argparse.Namespace,
    progress: Callable[[str], None] | None = None,
    debug: DebugLogger | None = None,
    fallback: bool = False,
) -> dict[str, Any]:
    chunk_id = f"chunk-{index:03d}"
    local_warnings: list[str] = []
    local_timing = new_timing()
    label = "sequential fallback" if fallback else "merged"
    if progress:
        progress(f"Processing chunk {index}/{chunk_count}" + (" fallback" if fallback else ""))
    if debug:
        debug(
            f"Chunk generation start: {chunk_id}",
            f"mode={label}\nmc_target={target_mc}\nopen_target={target_open}",
        )
    try:
        data = call_json_with_retries(
            args,
            build_merged_chunk_prompt(course, source_pdf, chunk, target_mc, target_open, index, chunk_count),
            f"{source_pdf} {label} chunk {index}",
            progress,
            debug,
            local_warnings,
            local_timing,
        )
        validate_merged_chunk_response(data, chunk_id)
    except Exception as exc:
        raise ChunkGenerationError(f"{type(exc).__name__}: {exc}", local_timing, local_warnings) from exc
    if debug:
        debug(
            f"Chunk generation end: {chunk_id}",
            json.dumps(
                {
                    "coverage_notes": len(ensure_list(data.get("coverage_notes"))),
                    "multiple_choice": len(ensure_list(data.get("multiple_choice"))),
                    "open_ended": len(ensure_list(data.get("open_ended"))),
                    "warnings": local_warnings,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return {
        "index": index,
        "chunk_id": chunk_id,
        "chunk": chunk,
        "data": data,
        "warnings": local_warnings,
        "timing": local_timing,
    }


def chunk_results_parallel(
    course: str,
    source_pdf: str,
    chunks: list[str],
    mc_targets: list[int],
    open_targets: list[int],
    args: argparse.Namespace,
    max_parallel_chunks: int,
    progress: Callable[[str], None] | None,
    debug: DebugLogger | None,
    timing: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    results: list[dict[str, Any]] = []
    failures: list[tuple[int, str, Exception]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel_chunks) as executor:
        futures = {
            executor.submit(
                generate_chunk_data,
                course,
                source_pdf,
                chunk,
                mc_targets[index - 1],
                open_targets[index - 1],
                index,
                len(chunks),
                args,
                progress,
                debug,
            ): (index, chunk)
            for index, chunk in enumerate(chunks, start=1)
        }
        for future in concurrent.futures.as_completed(futures):
            index, chunk = futures[future]
            chunk_id = f"chunk-{index:03d}"
            try:
                results.append(future.result())
            except Exception as exc:
                if isinstance(exc, ChunkGenerationError):
                    merge_model_timing(timing, exc.timing)
                    warnings.extend(str(warning) for warning in exc.warnings if str(warning).strip())
                message = f"{chunk_id} parallel generation failed: {type(exc).__name__}: {exc}"
                warnings.append(message)
                if progress:
                    progress(message)
                if debug:
                    debug(f"Parallel chunk failed: {chunk_id}", f"{type(exc).__name__}: {exc}")
                failures.append((index, chunk, exc))

    for index, chunk, original_error in failures:
        chunk_id = f"chunk-{index:03d}"
        if progress:
            progress(f"Retrying {chunk_id} sequentially after parallel failure")
        if debug:
            debug(
                f"Sequential fallback start: {chunk_id}",
                f"Original parallel failure: {type(original_error).__name__}: {original_error}",
            )
        try:
            results.append(
                generate_chunk_data(
                    course,
                    source_pdf,
                    chunk,
                    mc_targets[index - 1],
                    open_targets[index - 1],
                    index,
                    len(chunks),
                    args,
                    progress,
                    debug,
                    fallback=True,
                )
            )
        except Exception as exc:
            if isinstance(exc, ChunkGenerationError):
                merge_model_timing(timing, exc.timing)
                warnings.extend(str(warning) for warning in exc.warnings if str(warning).strip())
            message = f"{chunk_id} failed after sequential fallback: {type(exc).__name__}: {exc}"
            warnings.append(message)
            if progress:
                progress(message)
            if debug:
                debug(f"Sequential fallback failed: {chunk_id}", f"{type(exc).__name__}: {exc}")

    return sorted(results, key=lambda item: int(item["index"])), warnings


def chunk_results_sequential(
    course: str,
    source_pdf: str,
    chunks: list[str],
    mc_targets: list[int],
    open_targets: list[int],
    args: argparse.Namespace,
    progress: Callable[[str], None] | None,
    debug: DebugLogger | None,
    timing: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    results: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        chunk_id = f"chunk-{index:03d}"
        try:
            results.append(
                generate_chunk_data(
                    course,
                    source_pdf,
                    chunk,
                    mc_targets[index - 1],
                    open_targets[index - 1],
                    index,
                    len(chunks),
                    args,
                    progress,
                    debug,
                )
            )
        except Exception as exc:
            if isinstance(exc, ChunkGenerationError):
                merge_model_timing(timing, exc.timing)
                warnings.extend(str(warning) for warning in exc.warnings if str(warning).strip())
            message = f"{chunk_id} failed: {type(exc).__name__}: {exc}"
            warnings.append(message)
            if progress:
                progress(f"Chunk {index}/{len(chunks)} failed: {exc}")
            if debug:
                debug(f"Chunk failed: {chunk_id}", f"{type(exc).__name__}: {exc}")
    return results, warnings


def assign_question_ids(mc_questions: list[dict[str, Any]], open_questions: list[dict[str, Any]]) -> None:
    for index, question in enumerate(mc_questions, start=1):
        question["id"] = f"mc-{index:03d}"
    for index, question in enumerate(open_questions, start=1):
        question["id"] = f"open-{index:03d}"


def underrepresented_topics(coverage_notes: list[dict[str, Any]], exam: dict[str, Any]) -> list[str]:
    existing = topic_distribution(exam)
    topics = [str(note.get("topic") or "").strip() for note in coverage_notes if str(note.get("topic") or "").strip()]
    return sorted(set(topics), key=lambda topic: (existing.get(topic, 0), topic))[:20]


def refill_missing_questions(
    course: str,
    source_pdf: str,
    text: str,
    coverage_notes: list[dict[str, Any]],
    mc_questions: list[dict[str, Any]],
    open_questions: list[dict[str, Any]],
    target_mc: int,
    target_open: int,
    args: argparse.Namespace,
    progress: Callable[[str], None] | None,
    debug: DebugLogger | None,
    audit_warnings: list[str],
    timing: dict[str, Any],
) -> dict[str, Any]:
    missing_mc = max(0, target_mc - len(mc_questions))
    missing_open = max(0, target_open - len(open_questions))
    repair = {
        "attempted": bool(missing_mc or missing_open),
        "missing_multiple_choice": missing_mc,
        "missing_open_ended": missing_open,
        "successful": False,
        "attempts": 0,
    }
    if not missing_mc and not missing_open:
        return repair

    repair_started = time.perf_counter()
    repair["attempts"] = 1
    if progress:
        progress(f"Refilling missing questions: {missing_mc} MC / {missing_open} open")
    try:
        seed_exam = {"multiple_choice": mc_questions, "open_ended": open_questions}
        data = call_json_with_retries(
            args,
            build_refill_prompt(
                course,
                source_pdf,
                text,
                coverage_notes,
                missing_mc,
                missing_open,
                [question["question"] for question in mc_questions + open_questions],
                underrepresented_topics(coverage_notes, seed_exam),
            ),
            f"{source_pdf} refill",
            progress,
            debug,
            audit_warnings,
            timing,
        )
        validation_started = time.perf_counter()
        new_mc = normalize_mc_items(first_list(data, ["multiple_choice", "multiple_choice_questions", "mc_questions", "mcq", "mc"]), {question_signature(q["question"]) for q in mc_questions})
        new_open = normalize_open_items(first_list(data, ["open_ended", "open_ended_questions", "open_questions", "short_answer", "essay_questions"]), {question_signature(q["question"]) for q in open_questions})
        attach_question_metadata(new_mc, text, "repair-001")
        attach_question_metadata(new_open, text, "repair-001")
        mc_questions.extend(new_mc[:missing_mc])
        open_questions.extend(new_open[:missing_open])
        add_seconds(timing, "validation_seconds", validation_started)
        repair["successful"] = len(mc_questions) >= target_mc and len(open_questions) >= target_open
        audit_warnings.append(
            f"Repair generation produced {len(new_mc[:missing_mc])} MC and {len(new_open[:missing_open])} open question(s)."
        )
    except Exception as exc:
        audit_warnings.append(f"Repair generation failed: {type(exc).__name__}: {exc}")
        if debug:
            debug("Repair generation failed", f"{type(exc).__name__}: {exc}")
    finally:
        add_seconds(timing, "repair_seconds", repair_started)

    if not repair["successful"]:
        audit_warnings.append(
            f"Repair generation did not fully reach targets; keeping {len(mc_questions)} MC and {len(open_questions)} open question(s)."
        )
    return repair


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
    timing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timing = timing if timing is not None else new_timing()
    chunking_started = time.perf_counter()
    chunks = chunk_text(text)
    add_seconds(timing, "chunking_seconds", chunking_started)
    target_planning = resolve_target_plan(text, word_count, pages_total, len(chunks), "full_coverage", args)
    target_mc = int(target_planning["target_multiple_choice"])
    target_open = int(target_planning["target_open_ended"])
    mc_targets = distribute_counts(target_mc, len(chunks))
    open_targets = distribute_counts(target_open, len(chunks))
    max_parallel_chunks = min(normalize_max_parallel_chunks(args), max(1, len(chunks)))
    timing["parallelism_used"] = max_parallel_chunks
    mc_questions: list[dict[str, Any]] = []
    open_questions: list[dict[str, Any]] = []
    coverage_notes: list[dict[str, Any]] = []
    pages_covered_by_chunks: set[int] = set()
    seen_mc: set[str] = set()
    seen_open: set[str] = set()
    audit_warnings: list[str] = []
    if extraction_warning:
        audit_warnings.append(f"Extraction warning: {extraction_warning}")

    if max_parallel_chunks > 1:
        results, execution_warnings = chunk_results_parallel(
            course,
            source_pdf,
            chunks,
            mc_targets,
            open_targets,
            args,
            max_parallel_chunks,
            progress,
            debug,
            timing,
        )
    else:
        results, execution_warnings = chunk_results_sequential(
            course,
            source_pdf,
            chunks,
            mc_targets,
            open_targets,
            args,
            progress,
            debug,
            timing,
        )
    audit_warnings.extend(execution_warnings)

    validation_started = time.perf_counter()
    for result in sorted(results, key=lambda item: int(item["index"])):
        index = int(result["index"])
        chunk_id = str(result["chunk_id"])
        chunk = str(result["chunk"])
        data = result["data"]
        merge_model_timing(timing, result.get("timing"))
        audit_warnings.extend(str(warning) for warning in ensure_list(result.get("warnings")) if str(warning).strip())
        raw_mc = first_list(data, ["multiple_choice", "multiple_choice_questions", "mc_questions", "mcq", "mc"])
        raw_open = first_list(data, ["open_ended", "open_ended_questions", "open_questions", "short_answer", "essay_questions"])
        new_mc = normalize_mc_items(raw_mc, seen_mc)
        new_open = normalize_open_items(raw_open, seen_open)
        attach_question_metadata(new_mc, chunk, chunk_id)
        attach_question_metadata(new_open, chunk, chunk_id)
        notes = normalized_coverage_notes(data)
        coverage_notes.extend(notes)
        pages_covered_by_chunks.update(source_pages_from_text(chunk))
        if (new_mc or new_open) and not notes:
            message = f"{chunk_id} produced usable questions but no coverage_notes."
            audit_warnings.append(message)
            if debug:
                debug(f"Coverage notes missing: {chunk_id}", message)
        mc_questions.extend(new_mc)
        open_questions.extend(new_open)
        discarded = max(0, len(raw_mc) - len(new_mc)) + max(0, len(raw_open) - len(new_open))
        if discarded:
            audit_warnings.append(f"Discarded {discarded} duplicate or invalid generation(s) in {chunk_id}.")
        if not new_mc and not new_open:
            audit_warnings.append(f"{chunk_id} produced no usable questions.")
        if progress:
            progress(f"Merged {len(mc_questions)} MC / {len(open_questions)} open")
    add_seconds(timing, "validation_seconds", validation_started)

    processed_chunks = len(results)
    failed_chunks = max(0, len(chunks) - processed_chunks)

    repair_generation = refill_missing_questions(
        course,
        source_pdf,
        text,
        coverage_notes,
        mc_questions,
        open_questions,
        target_mc,
        target_open,
        args,
        progress,
        debug,
        audit_warnings,
        timing,
    )

    too_many_failed = failed_chunks > len(chunks) // 2
    if processed_chunks == 0 or too_many_failed or not mc_questions or not open_questions:
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
    exam["coverage_notes"] = coverage_notes
    exam = apply_coverage_metadata(exam, "full_coverage", len(chunks), processed_chunks, failed_chunks, " ".join(warnings) if warnings else None)
    return apply_exam_audit(
        exam,
        source_pdf,
        text,
        pages_total,
        len(chunks),
        processed_chunks,
        failed_chunks,
        audit_warnings,
        timing=timing,
        repair_generation=repair_generation,
        pages_covered_by_chunks=sorted(pages_covered_by_chunks),
        target_planning=target_planning,
    )


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
    overall_started = time.perf_counter()
    timing = new_timing()
    course_dir = pdf_path.parent
    course = course_dir.name
    exams_dir = course_dir / "exams"
    exam_dir = exams_dir / slugify(pdf_path.stem)

    if exam_dir.exists() and not args.overwrite:
        print(f"SKIP existing: {exam_dir}", flush=True)
        return None

    cache_payload, cache_key, cache_path = load_preprocessing_cache(root, pdf_path, args, debug=debug, progress=progress)
    if cache_payload:
        text = str(cache_payload.get("extracted_text") or "")
        extraction_warning = cache_payload.get("extraction_warning")
        if extraction_warning is not None:
            extraction_warning = str(extraction_warning)
        page_count_value = cache_payload.get("page_count")
        page_count = int(page_count_value) if isinstance(page_count_value, int) else None
    else:
        extraction_started = time.perf_counter()
        text, extraction_warning, page_count = extract_pdf_text(pdf_path, debug=debug)
        add_seconds(timing, "extraction_seconds", extraction_started)
        write_preprocessing_cache(cache_path, cache_key, text, extraction_warning, page_count, chunk_text(text), debug=debug)
    word_count = count_words(text)
    if progress:
        pages = page_count if page_count is not None else "unknown"
        progress(f"Extracted {word_count:,} words from {pages} pages")

    audit_warnings: list[str] = []
    if extraction_warning:
        audit_warnings.append(f"Extraction warning: {extraction_warning}")
    last_error: Exception | None = None
    exam: dict[str, Any] | None = None
    max_attempts = max(0, args.retries) + 1
    coverage_mode = resolve_coverage_mode(text, args)
    target_planning: dict[str, Any] | None = None

    if coverage_mode == "full_coverage":
        if progress:
            progress("Using full-coverage generation")
        exam = generate_full_coverage_exam(course, pdf_path.name, text, extraction_warning, word_count, args, progress, debug, page_count, timing)
    else:
        if progress:
            progress("Using representative generation")
        prompt_text = chunk_for_prompt(text)
        if len(text) > len(prompt_text):
            chunk_note = "Long PDF text was chunked into representative excerpts for LLM generation."
            extraction_warning = f"{extraction_warning} {chunk_note}".strip() if extraction_warning else chunk_note
            audit_warnings.append(chunk_note)
        target_planning = resolve_target_plan(text, word_count, page_count, max(1, len(chunk_text(text))), coverage_mode, args)
        base_mc = int(target_planning["target_multiple_choice"])
        base_open = int(target_planning["target_open_ended"])

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
                model_started = time.perf_counter()
                timing["model_calls"] = int(timing.get("model_calls") or 0) + 1
                raw = post_ollama(args.endpoint, args.model, prompt, args.timeout)
                timing["model_seconds"] = round(float(timing.get("model_seconds") or 0.0) + (time.perf_counter() - model_started), 4)
                if debug:
                    debug(f"Ollama raw response: {pdf_path.name} representative attempt {attempt + 1}", raw)
                validation_started = time.perf_counter()
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
                add_seconds(timing, "validation_seconds", validation_started)
                exam = apply_exam_audit(exam, pdf_path.name, text, page_count, 1, 1, 0, audit_warnings, timing=timing, target_planning=target_planning)
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
        if target_planning is None:
            target_planning = resolve_target_plan(text, word_count, page_count, max(1, len(chunk_text(text))), coverage_mode, args)
        exam = apply_exam_audit(exam, pdf_path.name, text, page_count, 1, 1, 0, audit_warnings, timing=timing, target_planning=target_planning)

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
            timing=timing,
            target_planning=target_planning,
        )

    if exam_dir.exists() and args.overwrite:
        shutil.rmtree(exam_dir)
    exam_dir.mkdir(parents=True, exist_ok=True)
    write_started = time.perf_counter()
    timing["total_seconds"] = round(time.perf_counter() - overall_started, 4)
    (exam_dir / "index.html").write_text(render_exam_html(template_dir, exam), encoding="utf-8")
    (exam_dir / "exam.json").write_text(json.dumps(exam, ensure_ascii=False, indent=2), encoding="utf-8")
    (exam_dir / "source.txt").write_text(text, encoding="utf-8")
    add_seconds(timing, "write_seconds", write_started)
    timing["total_seconds"] = round(time.perf_counter() - overall_started, 4)
    if isinstance(exam.get("audit"), dict):
        exam["audit"]["timing"] = timing
    (exam_dir / "index.html").write_text(render_exam_html(template_dir, exam), encoding="utf-8")
    (exam_dir / "exam.json").write_text(json.dumps(exam, ensure_ascii=False, indent=2), encoding="utf-8")
    if debug:
        debug(f"AUDIT SUMMARY: {pdf_path.name}", format_audit_summary(exam["audit"]))
        debug(f"TIMING SUMMARY: {pdf_path.name}", format_timing_summary(timing))
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

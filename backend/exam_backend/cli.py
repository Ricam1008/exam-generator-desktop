#!/usr/bin/env python3
"""Tiny backend service for the desktop exam generator app."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import shutil
import sys
import threading
import traceback
import uuid
import importlib.util
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, parse, request

PACKAGE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_DIR))

import generate_exams  # type: ignore  # noqa: E402
import generate_final_exams  # type: ignore  # noqa: E402
import local_server  # type: ignore  # noqa: E402

DEFAULT_MODEL = "gemma4:31b-cloud"
DEFAULT_OLLAMA = "http://localhost:11434"
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Exam Generator Output"
METRICS_LIMIT = 80


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def metrics_path() -> Path:
    return Path.home() / ".exam-generator-desktop" / "generation_metrics.json"


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"
    message: str = "Starting"
    progress: int = 0
    started_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    result: dict[str, Any] | None = None
    error: str | None = None
    logs: list[str] = field(default_factory=list)
    log_path: str | None = None
    log_url: str | None = None


class State:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.preview_root: Path | None = None
        self.selected_model = DEFAULT_MODEL
        self.lock = threading.Lock()


STATE = State()


def slugify(value: str) -> str:
    return generate_exams.slugify(value, max_length=80)


def fallback_log_dir() -> Path:
    return Path.home() / ".exam-generator-desktop" / "logs"


def generation_log_path(job: Job, base_dir: Path) -> Path:
    return base_dir / "logs" / f"generation-{job.id}.log"


def ensure_job_log_path(job: Job) -> Path:
    if not job.log_path:
        attach_job_log_path(job, fallback_log_dir() / f"generation-{job.id}.log")
    assert job.log_path is not None
    return Path(job.log_path)


def attach_job_log_path(job: Job, path: Path) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = Path(job.log_path) if job.log_path else None
    if previous and previous != path and previous.exists():
        existing = previous.read_text(encoding="utf-8", errors="replace")
        path.write_text(existing, encoding="utf-8")
    elif not path.exists():
        path.write_text("", encoding="utf-8")
    job.log_path = str(path)
    job.log_url = f"/api/jobs/{job.id}/log"


def append_job_log_file(job: Job, text: str) -> None:
    try:
        path = ensure_job_log_path(job)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        # In-memory logs should keep working even if the filesystem refuses a log write.
        return


def job_log(job: Job, message: str) -> None:
    job.updated_at = now_iso()
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    line = f"{stamp} {message}"
    job.logs.append(line)
    append_job_log_file(job, f"{line}\n")


def job_debug(job: Job, title: str, content: str) -> None:
    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    append_job_log_file(job, f"\n--- {stamp} {title} ---\n{content}\n--- END {title} ---\n")


def text_response(handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def job_log_text(job: Job) -> str:
    if job.log_path:
        path = Path(job.log_path)
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
    return "\n".join(job.logs) + ("\n" if job.logs else "")


def log_job_configuration(job: Job, payload: dict[str, Any], model: str, mode: str, output_path: str) -> None:
    job_debug(
        job,
        "Job configuration",
        json.dumps(
            {
                "job_id": job.id,
                "kind": job.kind,
                "mode": mode,
                "model": model,
                "input_path": payload.get("input_path"),
                "output_path": output_path,
                "overwrite": bool(payload.get("overwrite", False)),
                "started_at": job.started_at,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def load_metrics() -> dict[str, Any]:
    path = metrics_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"runs": []}


def save_metrics(data: dict[str, Any]) -> None:
    path = metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def record_generation_metric(model: str, mode: str, source_chars: int, source_words: int, duration_seconds: float) -> None:
    if source_chars <= 0 or duration_seconds <= 0:
        return
    data = load_metrics()
    runs = data.get("runs")
    if not isinstance(runs, list):
        runs = []
    runs.append({
        "model": (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        "mode": mode,
        "source_chars": source_chars,
        "source_words": source_words,
        "duration_seconds": round(duration_seconds, 1),
        "seconds_per_100k_chars": round(duration_seconds / max(1, source_chars) * 100_000, 3),
        "created_at": now_iso(),
    })
    data["runs"] = runs[-METRICS_LIMIT:]
    save_metrics(data)


def historical_estimate_minutes(model: str, mode: str, source_chars: int) -> tuple[int, int, int] | None:
    if source_chars <= 0:
        return None
    selected = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    runs = [
        item for item in load_metrics().get("runs", [])
        if isinstance(item, dict)
        and item.get("model") == selected
        and item.get("mode") == mode
        and float(item.get("source_chars") or 0) > 0
        and float(item.get("duration_seconds") or 0) > 0
    ][-10:]
    if not runs:
        return None
    rates = [float(item["duration_seconds"]) / float(item["source_chars"]) for item in runs]
    average_seconds = (sum(rates) / len(rates)) * source_chars
    low = max(1, math.ceil(average_seconds * 0.75 / 60))
    high = max(low, math.ceil(average_seconds * 1.35 / 60))
    return low, high, len(runs)


def ollama_json(path: str, payload: dict[str, Any] | None = None, timeout: int = 5) -> Any:
    url = DEFAULT_OLLAMA.rstrip("/") + path
    if payload is None:
        with request.urlopen(url, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    else:
        req = request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    return json.loads(raw) if raw.strip().startswith(("{", "[")) else raw


def available_ollama_models() -> list[str]:
    tags = ollama_json("/api/tags")
    if not isinstance(tags, dict):
        return []
    names = []
    for item in tags.get("models", []):
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
            names.append(item["name"].strip())
    return sorted(set(names), key=str.casefold)


def model_is_available(model: str, names: list[str]) -> bool:
    return model in names or any(name.startswith(model + ":") for name in names)


def check_dependencies(output_path: str | None = None, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    ok = True
    selected_model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    available_models: list[str] = []

    try:
        root_response = request.urlopen(DEFAULT_OLLAMA, timeout=3).read().decode("utf-8", errors="replace")
        reachable = "Ollama" in root_response or bool(root_response.strip())
    except Exception as exc:
        reachable = False
        root_response = str(exc)
    checks.append({"id": "ollama", "label": "Ollama reachable", "ok": reachable, "detail": root_response})
    ok = ok and reachable

    model_ok = False
    model_detail = f"Run: ollama pull {selected_model}"
    if reachable:
        try:
            available_models = available_ollama_models()
            model_ok = model_is_available(selected_model, available_models)
            if model_ok:
                model_detail = "Model found"
            elif available_models:
                model_detail = f"Model not installed. Run: ollama pull {selected_model}"
            else:
                model_detail = f"No Ollama models found. Run: ollama pull {DEFAULT_MODEL}"
        except Exception as exc:
            model_detail = str(exc)
    checks.append({"id": "model", "label": f"Model {selected_model}", "ok": model_ok, "detail": model_detail})
    ok = ok and model_ok

    templates_ok = all((PACKAGE_DIR / "templates" / name).exists() for name in ["index_template.html", "app.js", "styles.css"])
    checks.append({"id": "backend", "label": "Python backend resources", "ok": templates_ok, "detail": str(PACKAGE_DIR)})
    ok = ok and templates_ok

    pypdf_ok = importlib.util.find_spec("pypdf") is not None
    pdfplumber_ok = importlib.util.find_spec("pdfplumber") is not None
    pdf_detail = "pypdf and pdfplumber available" if pdfplumber_ok else "pypdf available; pdfplumber missing, table/layout extraction will be weaker"
    checks.append({
        "id": "pypdf",
        "label": "PDF text and layout extractor",
        "ok": pypdf_ok,
        "detail": pdf_detail if pypdf_ok else "Run: python3 -m pip install --user pypdf pdfplumber",
    })
    ok = ok and pypdf_ok

    out = Path(output_path).expanduser() if output_path else DEFAULT_OUTPUT_DIR
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
        out_detail = str(out)
    except Exception as exc:
        writable = False
        out_detail = str(exc)
    checks.append({"id": "output", "label": "Output folder writable", "ok": writable, "detail": out_detail})
    ok = ok and writable

    checks.append({"id": "port", "label": "Preview route", "ok": True, "detail": "Preview is served through the backend; no separate preview port is required."})

    return {
        "ok": ok,
        "checks": checks,
        "default_output": str(DEFAULT_OUTPUT_DIR),
        "available_models": available_models,
        "default_model": DEFAULT_MODEL,
        "selected_model": selected_model,
    }


def test_model(model: str) -> dict[str, Any]:
    selected_model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    payload = {
        "model": selected_model,
        "stream": False,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "options": {"temperature": 0},
    }
    try:
        result = ollama_json("/api/chat", payload, timeout=30)
        content = ""
        if isinstance(result, dict):
            message = result.get("message")
            if isinstance(message, dict):
                content = str(message.get("content") or "").strip()
            content = content or str(result.get("response") or "").strip()
        if not content:
            return {"ok": False, "model": selected_model, "detail": "Ollama responded, but the model returned no message."}
        STATE.selected_model = selected_model
        return {"ok": True, "model": selected_model, "detail": "Model responded"}
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        return {"ok": False, "model": selected_model, "detail": detail or str(exc)}
    except error.URLError as exc:
        return {"ok": False, "model": selected_model, "detail": f"Could not reach Ollama: {exc}"}
    except TimeoutError:
        return {"ok": False, "model": selected_model, "detail": "Model test timed out."}
    except Exception as exc:
        return {"ok": False, "model": selected_model, "detail": str(exc)}


def scan_folder(input_path: str, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    root = Path(input_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Input folder does not exist.")
    pdfs = [p for p in sorted(root.rglob("*.pdf")) if "exams" not in [part.lower() for part in p.parts]]
    courses: dict[str, int] = {}
    total_bytes = 0
    size_buckets = {"small": 0, "medium": 0, "large": 0, "huge": 0}
    low_minutes = 0
    high_minutes = 0
    for pdf in pdfs:
        try:
            rel = pdf.relative_to(root)
            course = rel.parts[0] if len(rel.parts) > 1 else root.name
        except ValueError:
            course = pdf.parent.name
        courses[course] = courses.get(course, 0) + 1
        try:
            size = pdf.stat().st_size
        except OSError:
            size = 0
        total_bytes += size
        if size < 250_000:
            size_buckets["small"] += 1
            low_minutes += 3
            high_minutes += 6
        elif size < 1_000_000:
            size_buckets["medium"] += 1
            low_minutes += 5
            high_minutes += 10
        elif size < 5_000_000:
            size_buckets["large"] += 1
            low_minutes += 8
            high_minutes += 18
        else:
            size_buckets["huge"] += 1
            low_minutes += 14
            high_minutes += 35

    final_low = len(courses) * 35
    final_high = len(courses) * 90
    estimated_source_chars = total_bytes
    selected_model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    all_basis = "file-size heuristic"
    finals_basis = "course-count heuristic"
    history_runs_used = 0
    all_history = historical_estimate_minutes(selected_model, "all", estimated_source_chars)
    if all_history:
        low_minutes, high_minutes, history_runs_used = all_history
        all_basis = f"previous runs with {selected_model}"
    finals_history = historical_estimate_minutes(selected_model, "finals", estimated_source_chars)
    if finals_history:
        final_low, final_high, finals_runs = finals_history
        history_runs_used = max(history_runs_used, finals_runs)
        finals_basis = f"previous final runs with {selected_model}"
    return {
        "input_path": str(root),
        "pdf_count": len(pdfs),
        "courses": courses,
        "estimate": {
            "generate_all_minutes_low": low_minutes,
            "generate_all_minutes_high": high_minutes,
            "generate_finals_minutes_low": final_low,
            "generate_finals_minutes_high": final_high,
            "total_pdf_mb": round(total_bytes / 1_000_000, 1),
            "estimated_source_chars": estimated_source_chars,
            "size_buckets": size_buckets,
            "basis": {"generate_all": all_basis, "generate_finals": finals_basis},
            "history_runs_used": history_runs_used,
            "model": selected_model,
            "note": "Estimate uses previous runs for the selected model when available; otherwise it falls back to file size. It improves after each completed generation.",
        },
    }


def ensure_separate_output(input_path: str, output_path: str) -> None:
    src_root = Path(input_path).expanduser().resolve()
    out_root = Path(output_path).expanduser().resolve()
    if out_root == src_root or out_root.is_relative_to(src_root):
        raise ValueError("Choose an output folder outside the input folder so source PDFs stay read-only.")


def backup_existing_project(input_path: str, output_path: str, overwrite: bool = False) -> Path | None:
    if not overwrite:
        return None
    src_root = Path(input_path).expanduser().resolve()
    output_root = Path(output_path).expanduser().resolve()
    project_root = output_root / slugify(src_root.name)
    if not project_root.exists():
        return None
    timestamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_root = output_root / ".backups" / timestamp / project_root.name
    backup_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(project_root, backup_root)
    return backup_root


def materialize_input(input_path: str, output_path: str, overwrite: bool = False) -> Path:
    src_root = Path(input_path).expanduser().resolve()
    out_root = Path(output_path).expanduser().resolve() / slugify(src_root.name)
    out_root.mkdir(parents=True, exist_ok=True)
    for pdf in sorted(src_root.rglob("*.pdf")):
        if "exams" in [part.lower() for part in pdf.parts]:
            continue
        rel = pdf.relative_to(src_root)
        dest = out_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not overwrite:
            continue
        shutil.copy2(pdf, dest)
    return out_root


def generator_args(root: Path, overwrite: bool = False, example: bool = False, model: str = DEFAULT_MODEL) -> argparse.Namespace:
    coverage_mode = "representative" if example else "auto"
    return argparse.Namespace(
        root=str(root), overwrite=overwrite, only_folder=None, limit=None,
        target_mode="auto", target_mc=None, target_open=None,
        min_mc=None, max_mc=None, min_open=None, max_open=None,
        endpoint="http://localhost:11434/api/chat", model=(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        timeout=600, retries=3, allow_heuristic_fallback=True, coverage_mode=coverage_mode, max_parallel_chunks=2,
    )


def final_args(root: Path, overwrite: bool = False, model: str = DEFAULT_MODEL) -> argparse.Namespace:
    return argparse.Namespace(
        root=str(root), overwrite=overwrite, only_folder=None,
        target_mc=120, target_open=30, mc_batch_size=15, open_batch_size=5,
        endpoint="http://localhost:11434/api/chat", model=(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        timeout=900, retries=2,
    )


def run_generation(job: Job, payload: dict[str, Any]) -> None:
    started = dt.datetime.now().astimezone()
    metric_chars = 0
    metric_words = 0
    try:
        input_path = payload.get("input_path")
        output_path = payload.get("output_path") or str(DEFAULT_OUTPUT_DIR)
        mode = payload.get("mode", "example")
        overwrite = bool(payload.get("overwrite", False))
        model = str(payload.get("model") or STATE.selected_model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        STATE.selected_model = model
        log_job_configuration(job, payload, model, mode, output_path)
        job_debug(
            job,
            "Dependency status",
            json.dumps(
                {
                    "python": sys.version.split()[0],
                    "pypdf": importlib.util.find_spec("pypdf") is not None,
                    "pdfplumber": importlib.util.find_spec("pdfplumber") is not None,
                    "templates": all((PACKAGE_DIR / "templates" / name).exists() for name in ["index_template.html", "app.js", "styles.css"]),
                    "ollama_endpoint": DEFAULT_OLLAMA,
                    "model": model,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        if not input_path:
            raise ValueError("Input folder is required.")
        job.message = "Preparing output workspace"
        job_log(job, "Preparing output workspace")
        ensure_separate_output(input_path, output_path)
        backup_path = backup_existing_project(input_path, output_path, overwrite=overwrite)
        if backup_path:
            job_log(job, f"Backup created: {backup_path}")
        project_root = materialize_input(input_path, output_path, overwrite=overwrite)
        attach_job_log_path(job, generation_log_path(job, project_root))
        job_log(job, f"Output workspace: {project_root}")
        job_log(job, f"Full log: {job.log_path}")
        STATE.preview_root = project_root
        debug_log = lambda title, content: job_debug(job, title, content)

        if mode in {"example", "all"}:
            args = generator_args(project_root, overwrite=overwrite, example=mode == "example", model=model)
            pdfs = generate_exams.find_pdfs(project_root, args.only_folder)
            if mode == "example":
                pdfs = pdfs[:1]
            total = max(1, len(pdfs))
            job_debug(job, "PDF queue", "\n".join(str(pdf) for pdf in pdfs) or "No PDFs found.")
            failures = []
            for index, pdf in enumerate(pdfs, start=1):
                job.message = f"Generating {pdf.name}"
                job.progress = int((index - 1) / total * 90)
                job_log(job, f"Starting {index}/{total}: {pdf.name}")
                try:
                    result = generate_exams.write_exam_folder(
                        pdf,
                        project_root,
                        args,
                        PACKAGE_DIR / "templates",
                        progress=lambda message: job_log(job, message),
                        debug=debug_log,
                    )
                    if result:
                        job_debug(job, f"Result metadata: {pdf.name}", json.dumps(result, ensure_ascii=False, indent=2))
                        job_log(job, f"Wrote {result['source_pdf']} ({result['mc_count']} MC, {result['open_count']} open)")
                        source_file = Path(result["exam_dir"]) / "source.txt"
                        try:
                            source_text = source_file.read_text(encoding="utf-8", errors="replace")
                            metric_chars += len(source_text)
                            metric_words += len(source_text.split())
                        except OSError:
                            pass
                except Exception as exc:
                    job_debug(job, f"Traceback: {pdf.name}", traceback.format_exc())
                    if mode == "example":
                        raise
                    failures.append(pdf.name)
                    job_log(job, f"FAILED {pdf.name}: {exc}")
                    continue
            if failures and len(failures) == len(pdfs):
                raise RuntimeError(f"All PDFs failed: {', '.join(failures)}")
            if failures:
                job_log(job, f"Completed with {len(failures)} failed PDF(s)")
            job.message = "Writing index pages"
            job_log(job, "Writing index pages")
            generate_exams.write_index_pages(project_root)
        elif mode == "finals":
            args = final_args(project_root, overwrite=overwrite, model=model)
            courses = generate_final_exams.course_dirs(project_root, None)
            total = max(1, len(courses))
            for index, course in enumerate(courses, start=1):
                job.message = f"Generating final exam for {course.name}"
                job.progress = int((index - 1) / total * 90)
                job_log(job, f"Starting final exam {index}/{total}: {course.name}")
                result = generate_final_exams.write_final_exam(args, course, debug=debug_log)
                if result:
                    job_debug(job, f"Final result metadata: {course.name}", json.dumps(result, ensure_ascii=False, indent=2))
                    job_log(job, f"Final {result['course']}: {result['mc']} MC, {result['open']} open")
                    source_file = course / "exams" / "final-exam" / "source.txt"
                    try:
                        source_text = source_file.read_text(encoding="utf-8", errors="replace")
                        metric_chars += len(source_text)
                        metric_words += len(source_text.split())
                    except OSError:
                        metric_words += int(result.get("words") or 0)
            job.message = "Writing index pages"
            job_log(job, "Writing index pages")
            generate_exams.write_index_pages(project_root)
        else:
            raise ValueError(f"Unknown generation mode: {mode}")

        job.status = "done"
        job.progress = 100
        job.message = "Done"
        duration = (dt.datetime.now().astimezone() - started).total_seconds()
        job_debug(job, "Job finished", f"status=done\nduration_seconds={duration:.2f}\nfinished_at={now_iso()}")
        if mode in {"all", "finals"} and metric_chars > 0:
            record_generation_metric(model, mode, metric_chars, metric_words, duration)
            job_log(job, f"Saved estimate sample for {model}: {metric_words:,} words in {int(duration // 60)} min")
        job_log(job, "Done")
        job.result = {"project_root": str(project_root), "index_url": "/preview/exam_index.html"}
    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
        job.message = "Failed"
        job_debug(job, "Traceback", traceback.format_exc())
        job_debug(job, "Job finished", f"status=error\nfinished_at={now_iso()}")
        job_log(job, f"Failed: {exc}")


class Handler(BaseHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        parsed_path = parse.urlparse(self.path)
        if parsed_path.path == "/api/health":
            json_response(self, 200, {"ok": True, "version": "0.1.0"})
            return
        if parsed_path.path.startswith("/api/jobs/"):
            tail = parsed_path.path[len("/api/jobs/"):]
            if tail.endswith("/log"):
                job_id = tail[:-len("/log")].rstrip("/")
                job = STATE.jobs.get(job_id)
                if not job:
                    text_response(self, 404, "Job not found\n")
                    return
                text_response(self, 200, job_log_text(job))
                return
            job_id = tail.rsplit("/", 1)[-1]
            job = STATE.jobs.get(job_id)
            if not job:
                json_response(self, 404, {"error": "Job not found"})
                return
            json_response(self, 200, job.__dict__)
            return
        if parsed_path.path.startswith("/preview/"):
            self.serve_preview(parsed_path.path[len("/preview/"):])
            return
        json_response(self, 404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed_path = parse.urlparse(self.path)
        try:
            payload = read_json(self)
            if parsed_path.path == "/api/check":
                json_response(self, 200, check_dependencies(payload.get("output_path"), payload.get("model", DEFAULT_MODEL)))
            elif parsed_path.path == "/api/test-model":
                json_response(self, 200, test_model(str(payload.get("model") or DEFAULT_MODEL)))
            elif parsed_path.path == "/api/scan":
                json_response(self, 200, scan_folder(payload.get("input_path", ""), payload.get("model", DEFAULT_MODEL)))
            elif parsed_path.path == "/api/generate":
                job = Job(id=str(uuid.uuid4()), kind=str(payload.get("mode", "example")))
                STATE.jobs[job.id] = job
                attach_job_log_path(job, fallback_log_dir() / f"generation-{job.id}.log")
                job_log(job, f"Queued {job.kind} generation")
                threading.Thread(target=run_generation, args=(job, payload), daemon=True).start()
                json_response(self, 200, {"job_id": job.id})
            elif parsed_path.path == "/api/set-preview-root":
                root = Path(payload.get("root", "")).expanduser().resolve()
                if not root.is_dir():
                    raise ValueError("Preview root does not exist")
                STATE.preview_root = root
                json_response(self, 200, {"ok": True, "index_url": "/preview/exam_index.html"})
            elif parsed_path.path == "/grade-open-answer":
                result = local_server.call_ollama(payload, "http://localhost:11434/api/chat", STATE.selected_model or DEFAULT_MODEL, 180)
                json_response(self, 200, result)
            else:
                json_response(self, 404, {"error": "Not found"})
        except error.URLError as exc:
            json_response(self, 502, {"error": f"Could not reach Ollama: {exc}"})
        except Exception as exc:
            json_response(self, 400, {"error": str(exc)})

    def serve_preview(self, rel_path: str) -> None:
        if STATE.preview_root is None:
            json_response(self, 404, {"error": "No preview root selected"})
            return
        safe_rel = parse.unquote(rel_path).lstrip("/") or "exam_index.html"
        target = (STATE.preview_root / safe_rel).resolve()
        try:
            target.relative_to(STATE.preview_root.resolve())
        except ValueError:
            json_response(self, 403, {"error": "Forbidden"})
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.exists():
            json_response(self, 404, {"error": "File not found"})
            return
        content_type = "text/html; charset=utf-8" if target.suffix == ".html" else "application/json; charset=utf-8" if target.suffix == ".json" else "text/plain; charset=utf-8"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(json.dumps({"event": "ready", "port": port}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if args.command == "serve":
        return serve(args.port)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

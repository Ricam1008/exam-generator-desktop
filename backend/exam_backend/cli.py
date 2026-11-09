#!/usr/bin/env python3
"""Tiny backend service for the desktop exam generator app."""

from __future__ import annotations

import argparse
import copy
import hashlib
import os
import secrets
import tempfile
import time
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

from exam_backend import generate_exams, generate_final_exams, local_server, runtime, providers

if __name__ == "__main__":
    sys.modules["exam_backend.cli"] = sys.modules[__name__]

DEFAULT_MODEL = "gemma4:31b-cloud"
DEFAULT_OLLAMA = "http://localhost:11434"
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Exam Generator Output"
METRICS_LIMIT = 80


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def metrics_path() -> Path:
    return providers.settings_dir() / "generation_metrics.json"


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
    cancel_requested: bool = False
    diagnostics: bool = False


class State:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.preview_root: Path | None = None
        self.selected_model = DEFAULT_MODEL
        self.lock = threading.RLock()
        self.events: dict[str, threading.Event] = {}
        self.workers: dict[str, threading.Thread] = {}
        self.workspaces: dict[str, dict] = {}
        self.preview_tokens: dict[str, dict] = {}
        self.token = secrets.token_urlsafe(32)
        self.port = 8766


STATE = State()


def is_cancel_requested(job: Job) -> bool:
    return job.cancel_requested or job.status == "canceled"


def request_job_cancel(job: Job) -> bool:
    if job.status not in {"running", "stopping"}:
        return False
    with STATE.lock:
        if job.status not in {"running", "stopping"}:
            return False
        job.cancel_requested = True
        job.status = "stopping"
        job.message = "Wird gestoppt …"
        STATE.events.setdefault(job.id, threading.Event()).set()
    job_log(job, "Stopp angefordert. Ein bereits angenommener Cloud-Auftrag kann trotzdem berechnet werden.")
    return True


def slugify(value: str) -> str:
    return generate_exams.slugify(value, max_length=80)


def fallback_log_dir() -> Path:
    return providers.settings_dir() / "logs"


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
    line = f"{stamp} {redact_log(message)}"
    job.logs.append(line)
    job.logs[:] = job.logs[-200:]
    append_job_log_file(job, f"{line}\n")


def redact_log(text: str) -> str:
    import re
    return re.sub(r"(?i)(bearer\s+|api[_-]?key[\"\s:=]+|refresh_token[\"\s:=]+|access_token[\"\s:=]+)[^\s\",}]+", r"\1[REDACTED]", str(text))


def job_debug(job: Job, title: str, content: str) -> None:
    if not job.diagnostics:
        return
    content = redact_log(content)
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
    if handler.headers.get("Transfer-Encoding"):
        raise ValueError("Transfer-Encoding wird nicht unterstützt.")
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length < 0 or length > 1_048_576:
        raise ValueError("Anfrage zu groß (maximal 1 MiB).")
    if not length:
        return {}
    payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Die Anfrage muss ein JSON-Objekt sein.")
    return payload


def load_metrics() -> dict[str, Any]:
    path = metrics_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"runs": []}


def save_metrics(data: dict[str, Any]) -> None:
    path = metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime.atomic_json(path, data)


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
        with tempfile.NamedTemporaryFile(prefix=".exam-write-", dir=out):
            pass
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
    pdfs = [p for p in sorted((p for p in root.rglob("*") if p.suffix.lower() == ".pdf")) if "exams" not in [part.lower() for part in p.parts]]
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
    actual = (out_root / slugify(src_root.name)).resolve()
    if out_root == src_root or out_root.is_relative_to(src_root) or actual == src_root or actual.is_relative_to(src_root) or src_root.is_relative_to(actual):
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
    ensure_separate_output(input_path, output_path)
    src_root = Path(input_path).expanduser().resolve()
    out_root = Path(output_path).expanduser().resolve() / slugify(src_root.name)
    out_root.mkdir(parents=True, exist_ok=True)
    for pdf in sorted((p for p in src_root.rglob("*") if p.suffix.lower() == ".pdf")):
        if "exams" in [part.lower() for part in pdf.parts]:
            continue
        rel = pdf.relative_to(src_root)
        dest = out_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and generate_exams.pdf_file_hash(dest) == generate_exams.pdf_file_hash(pdf):
            continue
        if dest.exists():
            backup = dest.with_name(dest.name + ".previous-" + uuid.uuid4().hex)
            shutil.copy2(dest, backup)
        shutil.copy2(pdf, dest)
    return out_root


def generator_args(root: Path, overwrite: bool = False, example: bool = False, model: str = DEFAULT_MODEL) -> argparse.Namespace:
    coverage_mode = "representative" if example else "auto"
    return argparse.Namespace(
        root=str(root), overwrite=overwrite, only_folder=None, limit=None,
        target_mode="auto", target_mc=None, target_open=None,
        min_mc=None, max_mc=None, min_open=None, max_open=None,
        endpoint="http://localhost:11434/api/chat", model=(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        timeout=600, retries=2, allow_heuristic_fallback=False, coverage_mode=coverage_mode, max_parallel_chunks=2,
    )


def final_args(root: Path, overwrite: bool = False, model: str = DEFAULT_MODEL) -> argparse.Namespace:
    return argparse.Namespace(
        root=str(root), overwrite=overwrite, only_folder=None,
        target_mc=120, target_open=30, mc_batch_size=15, open_batch_size=5,
        endpoint="http://localhost:11434/api/chat", model=(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        timeout=900, retries=2,
    )


def settings_path() -> Path:
    return providers.settings_dir() / 'workspaces.json'


def registry_data():
    try:
        data = json.loads(settings_path().read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'workspaces': {}, 'grading_profile_id': None}
    except (OSError, ValueError) as exc:
        raise ValueError('Die Arbeitsbereichsliste konnte nicht gelesen werden. Sie wurde nicht überschrieben.') from exc
    if not isinstance(data, dict) or not isinstance(data.get('workspaces'), dict):
        raise ValueError('Die Arbeitsbereichsliste ist beschädigt. Sie wurde nicht überschrieben.')
    return data


def load_registry():
    with STATE.lock:
        data = registry_data()
        STATE.workspaces.update(data['workspaces'])
        return data


def save_registry(data=None):
    with STATE.lock:
        data = registry_data() if data is None else data
        data['workspaces'] = copy.deepcopy(STATE.workspaces)
        runtime.atomic_json(settings_path(), data)


def scan_documents(payload):
    from . import documents
    root = payload.get('input_path')
    if not isinstance(root, str) or not root.strip():
        raise ValueError('Bitte einen Materialordner auswählen.')
    sources = documents.discover_sources(root, payload.get('paths'))
    courses = {}
    for source in sources:
        courses[source['course']] = courses.get(source['course'], 0) + 1
    return {'input_path': str(Path(root).expanduser().resolve()), 'sources': sources, 'courses': courses, 'pdf_count': sum(s['format'].lower().lstrip('.') == 'pdf' for s in sources)}


def generation_options(payload):
    extraction_modes = payload.get('extraction_modes', {})
    assignments = payload.get('course_assignments', {})
    if not isinstance(extraction_modes, dict) or any(not isinstance(k, str) or v not in {'auto', 'ocr'} for k, v in extraction_modes.items()):
        raise ValueError('Ungültige Einstellungen für die Texterkennung.')
    if not isinstance(assignments, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip() or len(v) > 200 for k, v in assignments.items()):
        raise ValueError('Ungültige Lehrveranstaltungszuordnung.')
    language = payload.get('language', 'de')
    difficulty = payload.get('difficulty', 'balanced')
    coverage = payload.get('coverage_mode', 'auto')
    mode = payload.get('mode', 'example')
    target_mode = payload.get('target_mode', 'auto')
    if language not in {'de', 'en'} or difficulty not in {'basic', 'balanced', 'challenging'} or coverage not in {'auto', 'representative', 'full_coverage'} or mode not in {'example', 'all', 'finals'} or target_mode not in {'auto', 'manual'}:
        raise ValueError('Ungültige Prüfungseinstellungen.')
    result = {k: payload.get(k, v) for k, v in {'language': language, 'difficulty': difficulty, 'coverage_mode': coverage, 'mode': mode, 'target_mode': target_mode}.items()}
    if target_mode == 'manual':
        for key in ('target_mc', 'target_open'):
            value = payload.get(key)
            if type(value) is not int or not 0 <= value <= 150:
                raise ValueError('Fragenanzahl muss zwischen 0 und 150 liegen.')
            result[key] = value
        if not result['target_mc'] and not result['target_open']:
            raise ValueError('Mindestens eine Frage auswählen.')
    return result


def start_job(payload):
    snapshot = copy.deepcopy(payload)
    generation_options(snapshot)
    # Reject overlap before creating a log, workspace, or permission probe.
    ensure_separate_output(snapshot.get('input_path', ''), snapshot.get('output_path') or str(DEFAULT_OUTPUT_DIR))
    scan = scan_documents(snapshot)
    selected = snapshot.get('source_ids')
    if not isinstance(selected, list) or not selected or any(not isinstance(x, str) for x in selected):
        raise ValueError('Bitte mindestens ein Dokument auswählen.')
    known = {s['id'] for s in scan['sources']}
    if not set(selected) <= known:
        raise ValueError('Die Materialauswahl hat sich geändert. Bitte erneut einlesen.')
    snapshot['_sources'] = [s for s in scan['sources'] if s['id'] in selected]
    from . import providers
    profiles = providers.list_profiles()
    profile = next((p for p in profiles if p['id'] == snapshot.get('profile_id', 'ollama')), None)
    if not profile:
        raise ValueError('KI-Verbindung nicht gefunden.')
    snapshot['profile_id'] = profile['id']
    snapshot['profile_snapshot'] = {k: profile.get(k) for k in ('id', 'kind', 'name', 'base_url', 'model', 'credential_saved')}
    snapshot['model'] = snapshot.get('model') or profile.get('model')
    if not snapshot['model']:
        raise ValueError('Bitte ein Modell auswählen.')
    with STATE.lock:
        if any(j.status in {'running', 'stopping'} for j in STATE.jobs.values()):
            raise ValueError('Ein Auftrag läuft bereits. Bitte warten oder stoppen.')
        job = Job(id=uuid.uuid4().hex, kind=snapshot.get('mode', 'example'), diagnostics=snapshot.get('diagnostics') is True)
        STATE.jobs[job.id] = job
        STATE.events[job.id] = threading.Event()
        for key in list(STATE.jobs)[:-40]:
            if STATE.jobs[key].status not in {'running', 'stopping'}:
                STATE.jobs.pop(key, None)
                STATE.events.pop(key, None)
                STATE.workers.pop(key, None)
        worker = threading.Thread(target=run_generation, args=(job, snapshot), daemon=True)
        STATE.workers[job.id] = worker
        worker.start()
    return job


def run_generation(job: Job, payload: dict[str, Any]) -> None:
    from . import documents
    staging = None
    event = STATE.events.setdefault(job.id, threading.Event())
    started = time.perf_counter()
    try:
        input_path = payload.get('input_path')
        if not input_path:
            raise ValueError('Input folder is required.')
        output = Path(payload.get('output_path') or DEFAULT_OUTPUT_DIR).expanduser().resolve()
        ensure_separate_output(input_path, str(output))
        options = generation_options(payload)
        sources = payload.get('_sources') or scan_documents(payload)['sources']
        if 'source_ids' in payload:
            sources = [s for s in sources if s['id'] in payload['source_ids']]
        if options['mode'] == 'example':
            sources = sources[:1]
        if not sources:
            raise ValueError('Keine ausgewählten Dokumente gefunden.')
        src_root = Path(input_path).expanduser().resolve()
        workspace_id = hashlib.sha256((str(src_root) + '\0' + str(output)).encode()).hexdigest()[:16]
        destination = output / f'{slugify(src_root.name)}--{workspace_id}'
        if destination.is_symlink():
            raise ValueError('Der Arbeitsbereich darf kein symbolischer Link sein.')
        if destination.resolve() == src_root or destination.resolve().is_relative_to(src_root) or src_root.is_relative_to(destination.resolve()):
            raise ValueError('Arbeitsbereich und Originalmaterial müssen getrennt bleiben.')
        output.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.exam-run-', dir=output))
        if destination.exists():
            shutil.copytree(destination, staging, dirs_exist_ok=True, symlinks=False)
        attach_job_log_path(job, generation_log_path(job, staging))
        model = payload.get('model') or DEFAULT_MODEL
        profile_id = payload.get('profile_id') or 'ollama'
        settings = dict(options, profile_id=profile_id, model=model, profile_snapshot=payload.get('profile_snapshot'), extraction_modes=payload.get('extraction_modes', {}))
        signature = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        manifest_path = staging / 'workspace.json'
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            manifest = {'version': 2, 'id': workspace_id, 'name': src_root.name, 'sources': {}, 'exams': {}}
        summary = {'generated': [], 'reused': [], 'skipped': [], 'failed': []}
        extracted = []
        with runtime.generation_context(profile_id=profile_id, model=model, cancel_event=event, language=options['language'], difficulty=options['difficulty'], profile_snapshot=payload.get('profile_snapshot')) as context:
            for index, source in enumerate(sources):
                runtime.check_cancel()
                job.message = f'Material aufbereiten: {source["name"]}'
                job.progress = int(index / len(sources) * 25)
                job_log(job, job.message)
                try:
                    # A scan is a snapshot, not permission to substitute changed content.
                    if generate_exams.pdf_file_hash(Path(source['path'])) != source['fingerprint']:
                        raise ValueError('Datei wurde seit dem Einlesen geändert. Bitte erneut einlesen.')
                    doc = documents.extract_document(Path(source['path']), staging / '.exam-generator-cache', cancel_event=event, mode=payload.get('extraction_modes', {}).get(source['id'], 'auto'))
                    runtime.check_cancel()
                    doc.update({k: source[k] for k in ('id', 'name', 'fingerprint', 'path')})
                    course = payload.get('course_assignments', {}).get(source['id'], source['course'])
                    if not isinstance(course, str) or not course.strip():
                        raise ValueError('Kursname fehlt.')
                    course_dir = staging / (slugify(course) + '--' + hashlib.sha256(course.encode()).hexdigest()[:8])
                    course_dir.mkdir(exist_ok=True)
                    synthetic = course_dir / ('source-' + source['id'] + Path(source['name']).suffix.lower())
                    doc['course'] = course
                    context['documents'][str(synthetic)] = doc
                    extracted.append((source, doc, course_dir, synthetic))
                    manifest['sources'][source['id']] = dict(source, course=course)
                except runtime.Canceled:
                    raise
                except Exception as exc:
                    summary['failed'].append({'name': source['name'], 'error': str(exc)})
                    job_log(job, f'{source["name"]}: {exc}')
            if not extracted:
                raise ValueError('Kein Dokument konnte aufbereitet werden. Bitte die Materialhinweise prüfen.')
            tasks = []
            if options['mode'] == 'finals':
                groups = {}
                for item in extracted:
                    groups.setdefault(item[2], []).append(item)
                for course_dir, items in groups.items():
                    tasks.append((course_dir, items))
            else:
                tasks = [(item[2], [item]) for item in extracted]
            for index, (course_dir, items) in enumerate(tasks):
                runtime.check_cancel()
                source, doc, _, synthetic = items[0]
                key = ('final:' + course_dir.name) if options['mode'] == 'finals' else source['id']
                fingerprint = hashlib.sha256(''.join(item[0]['fingerprint'] + hashlib.sha256(item[1]['text'].encode()).hexdigest() for item in items).encode()).hexdigest()
                entry = manifest['exams'].get(key, {})
                exam_dir = course_dir / 'exams' / ('final-exam' if options['mode'] == 'finals' else synthetic.stem)
                if entry.get('fingerprint') == fingerprint and entry.get('settings') == signature and (exam_dir / 'exam.json').exists() and not payload.get('overwrite'):
                    summary['reused'].append(source['name'] if options['mode'] != 'finals' else course_dir.name)
                    continue
                job.message = f'Prüfung erstellen: {source["name"] if options["mode"] != "finals" else course_dir.name}'
                job.progress = 25 + int(index / len(tasks) * 65)
                job_log(job, job.message)
                try:
                    if options['mode'] == 'finals':
                        args = final_args(staging, overwrite=True, model=model)
                        args.course_label = manifest['sources'][source['id']]['course']
                        if options['target_mode'] == 'manual':
                            args.target_mc, args.target_open = options['target_mc'], options['target_open']
                        args.source_entries = [dict(source_pdf=item[0]['name'], exam_folder=item[0]['id'], text=item[1]['text'], words=generate_exams.count_words(item[1]['text']), warning=' '.join(item[1].get('warnings', [])) or None, pages_total=item[1].get('page_count') or 0, visuals_detected=0) for item in items]
                        result = generate_final_exams.write_final_exam(args, course_dir, debug=(lambda t, c: job_debug(job, t, c)) if job.diagnostics else None)
                    else:
                        args = generator_args(staging, overwrite=True, example=options['mode'] == 'example', model=model)
                        args.coverage_mode = options['coverage_mode']
                        args.target_mode = options['target_mode']
                        args.target_mc, args.target_open = options.get('target_mc'), options.get('target_open')
                        result = generate_exams.write_exam_folder(synthetic, staging, args, PACKAGE_DIR / 'templates', progress=lambda m: job_log(job, m), debug=(lambda t, c: job_debug(job, t, c)) if job.diagnostics else None)
                    runtime.check_cancel()
                    if not result:
                        raise ValueError('Keine Prüfung erzeugt.')
                    previous_path = entry.get('path')
                    if isinstance(previous_path, str):
                        previous = (staging / previous_path).resolve()
                        try:
                            relative = previous.relative_to(staging.resolve())
                        except ValueError:
                            raise ValueError('Ungültiger Prüfungsort im Arbeitsbereich.')
                        if previous != exam_dir.resolve() and previous.is_dir() and len(relative.parts) == 3 and relative.parts[1] == 'exams' and not any(part.startswith('.') for part in relative.parts):
                            history = staging / '.history'
                            history.mkdir(exist_ok=True)
                            os.replace(previous, history / (source['id'] + '-' + uuid.uuid4().hex))
                    manifest['exams'][key] = {'fingerprint': fingerprint, 'settings': signature, 'path': str(exam_dir.relative_to(staging)), 'provider_id': profile_id, 'model': model}
                    summary['generated'].append(source['name'] if options['mode'] != 'finals' else course_dir.name)
                except runtime.Canceled:
                    raise
                except runtime.ProviderFailure as exc:
                    summary['failed'].append({'name': source['name'], 'error': str(exc)})
                    summary['skipped'].extend(item[1][0][0]['name'] for item in tasks[index + 1:])
                    job_log(job, f'KI-Verbindung fehlgeschlagen: {exc}')
                    break
                except Exception as exc:
                    summary['failed'].append({'name': source['name'], 'error': str(exc)})
                    job_log(job, f'Prüfung fehlgeschlagen: {exc}')
            runtime.check_cancel()
            if not summary['generated'] and not summary['reused']:
                raise ValueError('Keine verwendbare Prüfung erstellt. Bitte Verbindung und Material prüfen.')
            manifest.update({'updated_at': now_iso(), 'settings': settings})
            runtime.atomic_json(manifest_path, manifest)
            generate_exams.write_index_pages(staging)
            with STATE.lock:
                runtime.check_cancel()
                backup = runtime.replace_directory(staging, destination)
                staging = None
                STATE.workspaces[workspace_id] = {'id': workspace_id, 'name': src_root.name, 'root': str(destination), 'exam_count': len(manifest['exams']), 'updated_at': now_iso()}
                try:
                    save_registry()
                except BaseException:
                    os.replace(destination, destination.with_name('.failed-' + job.id))
                    if backup:
                        os.replace(backup, destination)
                    STATE.workspaces.pop(workspace_id, None)
                    raise
                job.log_path = str(destination / 'logs' / f'generation-{job.id}.log')
                job.result = dict(summary, workspace_id=workspace_id, project_root=str(destination), index_url='/preview/' + workspace_id + '/exam_index.html', usage=context['usage'])
                job.status = 'partial' if summary['failed'] else 'done'
                job.progress = 100
                job.message = 'Mit Hinweisen abgeschlossen' if summary['failed'] else 'Abgeschlossen'
                job_log(job, job.message)
    except (runtime.Canceled, KeyboardInterrupt):
        job.status = 'canceled'
        job.message = 'Gestoppt'
    except (Exception, runtime.ProviderFailure) as exc:
        job.status = 'canceled' if event.is_set() else 'error'
        job.error = str(exc)
        job.message = 'Gestoppt' if event.is_set() else 'Fehlgeschlagen'
        job_log(job, str(exc))
        job_debug(job, 'Traceback', traceback.format_exc())
    finally:
        if staging and staging.exists():
            # Preserve logs even though incomplete exam output is discarded.
            try:
                text = job_log_text(job)
                attach_job_log_path(job, fallback_log_dir() / f'generation-{job.id}.log')
                Path(job.log_path).write_text(text, encoding='utf-8')
            except OSError:
                pass
            shutil.rmtree(staging)
        job.updated_at = now_iso()


from exam_backend.service import Handler, serve


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    serve_parser = sub.add_parser('serve')
    serve_parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    return serve(args.port)


if __name__ == '__main__':
    raise SystemExit(main())

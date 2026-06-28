#!/usr/bin/env python3
"""Tiny backend service for the desktop exam generator app."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import threading
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


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"
    message: str = "Starting"
    progress: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None
    logs: list[str] = field(default_factory=list)


class State:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.preview_root: Path | None = None
        self.lock = threading.Lock()


STATE = State()


def slugify(value: str) -> str:
    return generate_exams.slugify(value, max_length=80)


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


def check_dependencies(output_path: str | None = None, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    ok = True

    try:
        root_response = request.urlopen(DEFAULT_OLLAMA, timeout=3).read().decode("utf-8", errors="replace")
        reachable = "Ollama" in root_response or bool(root_response.strip())
    except Exception as exc:
        reachable = False
        root_response = str(exc)
    checks.append({"id": "ollama", "label": "Ollama reachable", "ok": reachable, "detail": root_response})
    ok = ok and reachable

    model_ok = False
    model_detail = f"Run: ollama pull {model}"
    if reachable:
        try:
            tags = ollama_json("/api/tags")
            names = [item.get("name", "") for item in tags.get("models", [])]
            model_ok = any(name == model or name.startswith(model + ":") for name in names)
            model_detail = "Model found" if model_ok else model_detail
        except Exception as exc:
            model_detail = str(exc)
    checks.append({"id": "model", "label": f"Model {model}", "ok": model_ok, "detail": model_detail})
    ok = ok and model_ok

    templates_ok = all((PACKAGE_DIR / "templates" / name).exists() for name in ["index_template.html", "app.js", "styles.css"])
    checks.append({"id": "backend", "label": "Python backend resources", "ok": templates_ok, "detail": str(PACKAGE_DIR)})
    ok = ok and templates_ok

    pypdf_ok = importlib.util.find_spec("pypdf") is not None
    checks.append({
        "id": "pypdf",
        "label": "PDF text extractor",
        "ok": pypdf_ok,
        "detail": "pypdf available" if pypdf_ok else "Run: python3 -m pip install --user pypdf",
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

    return {"ok": ok, "checks": checks, "default_output": str(DEFAULT_OUTPUT_DIR)}


def scan_folder(input_path: str) -> dict[str, Any]:
    root = Path(input_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Input folder does not exist.")
    pdfs = [p for p in sorted(root.rglob("*.pdf")) if "exams" not in [part.lower() for part in p.parts]]
    courses: dict[str, int] = {}
    for pdf in pdfs:
        try:
            rel = pdf.relative_to(root)
            course = rel.parts[0] if len(rel.parts) > 1 else root.name
        except ValueError:
            course = pdf.parent.name
        courses[course] = courses.get(course, 0) + 1
    return {"input_path": str(root), "pdf_count": len(pdfs), "courses": courses}


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


def generator_args(root: Path, overwrite: bool = False, example: bool = False) -> argparse.Namespace:
    min_mc, max_mc = (12, 20) if example else (40, 60)
    min_open, max_open = (4, 8) if example else (10, 20)
    return argparse.Namespace(
        root=str(root), overwrite=overwrite, only_folder=None, limit=None,
        min_mc=min_mc, max_mc=max_mc, min_open=min_open, max_open=max_open,
        endpoint="http://localhost:11434/api/chat", model=DEFAULT_MODEL,
        timeout=600, retries=3, allow_heuristic_fallback=True,
    )


def final_args(root: Path, overwrite: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        root=str(root), overwrite=overwrite, only_folder=None,
        target_mc=120, target_open=30, mc_batch_size=15, open_batch_size=5,
        endpoint="http://localhost:11434/api/chat", model=DEFAULT_MODEL,
        timeout=900, retries=2,
    )


def run_generation(job: Job, payload: dict[str, Any]) -> None:
    try:
        input_path = payload.get("input_path")
        output_path = payload.get("output_path") or str(DEFAULT_OUTPUT_DIR)
        mode = payload.get("mode", "example")
        overwrite = bool(payload.get("overwrite", False))
        if not input_path:
            raise ValueError("Input folder is required.")
        ensure_separate_output(input_path, output_path)
        backup_path = backup_existing_project(input_path, output_path, overwrite=overwrite)
        if backup_path:
            job.logs.append(f"Backup created: {backup_path}")
        project_root = materialize_input(input_path, output_path, overwrite=overwrite)
        job.logs.append(f"Output workspace: {project_root}")
        STATE.preview_root = project_root

        if mode in {"example", "all"}:
            args = generator_args(project_root, overwrite=overwrite, example=mode == "example")
            pdfs = generate_exams.find_pdfs(project_root, args.only_folder)
            if mode == "example":
                pdfs = pdfs[:1]
            total = max(1, len(pdfs))
            for index, pdf in enumerate(pdfs, start=1):
                job.message = f"Generating {pdf.name}"
                job.progress = int((index - 1) / total * 90)
                result = generate_exams.write_exam_folder(pdf, project_root, args, PACKAGE_DIR / "templates")
                if result:
                    job.logs.append(f"Wrote {result['source_pdf']}")
            generate_exams.write_index_pages(project_root)
        elif mode == "finals":
            args = final_args(project_root, overwrite=overwrite)
            courses = generate_final_exams.course_dirs(project_root, None)
            total = max(1, len(courses))
            for index, course in enumerate(courses, start=1):
                job.message = f"Generating final exam for {course.name}"
                job.progress = int((index - 1) / total * 90)
                result = generate_final_exams.write_final_exam(args, course)
                if result:
                    job.logs.append(f"Final {result['course']}: {result['mc']} MC, {result['open']} open")
            generate_exams.write_index_pages(project_root)
        else:
            raise ValueError(f"Unknown generation mode: {mode}")

        job.status = "done"
        job.progress = 100
        job.message = "Done"
        job.result = {"project_root": str(project_root), "index_url": "/preview/exam_index.html"}
    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
        job.message = "Failed"


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
            job_id = parsed_path.path.rsplit("/", 1)[-1]
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
            elif parsed_path.path == "/api/scan":
                json_response(self, 200, scan_folder(payload.get("input_path", "")))
            elif parsed_path.path == "/api/generate":
                job = Job(id=str(uuid.uuid4()), kind=str(payload.get("mode", "example")))
                STATE.jobs[job.id] = job
                threading.Thread(target=run_generation, args=(job, payload), daemon=True).start()
                json_response(self, 200, {"job_id": job.id})
            elif parsed_path.path == "/api/set-preview-root":
                root = Path(payload.get("root", "")).expanduser().resolve()
                if not root.is_dir():
                    raise ValueError("Preview root does not exist")
                STATE.preview_root = root
                json_response(self, 200, {"ok": True, "index_url": "/preview/exam_index.html"})
            elif parsed_path.path == "/grade-open-answer":
                result = local_server.call_ollama(payload, "http://localhost:11434/api/chat", DEFAULT_MODEL, 180)
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

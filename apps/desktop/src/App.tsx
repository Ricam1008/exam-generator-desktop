import { useEffect, useMemo, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import { openUrl } from "@tauri-apps/plugin-opener";

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

type Check = { id: string; label: string; ok: boolean; detail: string };
type ScanResult = { input_path: string; pdf_count: number; courses: Record<string, number> };
type Job = {
  id: string;
  kind: string;
  status: string;
  message: string;
  progress: number;
  started_at?: string;
  updated_at?: string;
  result?: { project_root: string; index_url: string };
  error?: string;
  logs: string[];
};

const API = "http://127.0.0.1:8766";

function defaultOutputPath() {
  return "~/Documents/Exam Generator Output";
}

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function isTauri() {
  return typeof window !== "undefined" && Boolean(window.__TAURI_INTERNALS__);
}

function secondsSince(value: string | undefined, now: number) {
  if (!value) return null;
  const then = new Date(value).getTime();
  if (!Number.isFinite(then)) return null;
  return Math.max(0, Math.round((now - then) / 1000));
}

function formatDuration(seconds: number | null) {
  if (seconds === null) return "Unknown";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function formatClock(value: string | undefined) {
  if (!value) return "Unknown";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "Unknown";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed: ${response.status}`);
  return data as T;
}

export default function App() {
  const [inputPath, setInputPath] = useState("");
  const [outputPath, setOutputPath] = useState(defaultOutputPath());
  const [checks, setChecks] = useState<Check[]>([]);
  const [scan, setScan] = useState<ScanResult | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [backendReady, setBackendReady] = useState(false);
  const [backendStarting, setBackendStarting] = useState(false);
  const [now, setNow] = useState(Date.now());

  const allRequiredOk = useMemo(() => checks.filter((item) => item.id !== "port").every((item) => item.ok), [checks]);

  async function checkBackend() {
    setError("");
    try {
      let response = await fetch(`${API}/api/health`).catch(() => null);
      if (!response?.ok && isTauri()) {
        setBackendStarting(true);
        await invoke("start_backend");
        for (let attempt = 0; attempt < 12; attempt += 1) {
          await sleep(400);
          response = await fetch(`${API}/api/health`).catch(() => null);
          if (response?.ok) break;
        }
      }
      if (!response?.ok) throw new Error("Backend unavailable");
      setBackendReady(true);
      const data = await post<{ checks: Check[]; default_output: string }>("/api/check", { output_path: outputPath });
      setChecks(data.checks);
      if (outputPath === defaultOutputPath()) setOutputPath(data.default_output);
    } catch (err) {
      setBackendReady(false);
      const detail = err instanceof Error ? err.message : String(err);
      setError(isTauri() ? `Could not start the local backend automatically: ${detail}` : "Backend is not running. For browser development, run scripts/dev-backend.sh first.");
    } finally {
      setBackendStarting(false);
    }
  }

  async function chooseInput() {
    const selected = await open({ directory: true, multiple: false });
    if (typeof selected === "string") setInputPath(selected);
  }

  async function chooseOutput() {
    const selected = await open({ directory: true, multiple: false });
    if (typeof selected === "string") setOutputPath(selected);
  }

  async function scanInput() {
    setError("");
    try {
      const result = await post<ScanResult>("/api/scan", { input_path: inputPath });
      setScan(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function generate(mode: "example" | "all" | "finals") {
    setError("");
    setJob(null);
    try {
      const started = await post<{ job_id: string }>("/api/generate", { input_path: inputPath, output_path: outputPath, mode, overwrite: false });
      const timestamp = new Date().toISOString();
      setJob({ id: started.job_id, kind: mode, status: "running", message: "Starting", progress: 0, started_at: timestamp, updated_at: timestamp, logs: [] });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function openPreview() {
    if (!job?.result) return;
    await post("/api/set-preview-root", { root: job.result.project_root });
    await openUrl(`${API}${job.result.index_url}`);
  }

  useEffect(() => {
    void checkBackend();
  }, []);

  useEffect(() => {
    if (!job || job.status !== "running") return;
    setNow(Date.now());
    const timer = window.setInterval(async () => {
      try {
        const next = await fetch(`${API}/api/jobs/${job.id}`).then((res) => res.json());
        setJob(next);
        setNow(Date.now());
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [job?.id, job?.status]);

  useEffect(() => {
    if (!job || job.status !== "running") return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [job?.id, job?.status]);

  const elapsed = job ? secondsSince(job.started_at, now) : null;
  const quietFor = job ? secondsSince(job.updated_at, now) : null;
  const latestLog = job && job.logs.length ? job.logs[job.logs.length - 1] : "";

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="kicker">Local study tool</p>
          <h1>Exam Generator</h1>
          <p className="lede">Choose a folder of lecture PDFs, generate exams, and open the local exam index.</p>
        </div>
        <button className="secondary" onClick={checkBackend} disabled={backendStarting}>{backendStarting ? "Starting..." : "Check again"}</button>
      </header>

      {error && <section className="notice error">{error}</section>}

      <section className="panel">
        <h2>Status</h2>
        <div className="checks">
          {checks.length === 0 && <p className="muted">Backend status will appear here.</p>}
          {checks.map((check) => (
            <article className="check" key={check.id}>
              <span className={check.ok ? "dot ok" : "dot bad"} />
              <div>
                <strong>{check.label}</strong>
                <p>{check.detail}</p>
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className="panel grid-two">
        <label>
          Input folder
          <div className="path-row">
            <input value={inputPath} onChange={(event) => setInputPath(event.target.value)} placeholder="Folder with course PDFs" />
            <button className="secondary" onClick={chooseInput}>Browse</button>
          </div>
        </label>
        <label>
          Output folder
          <div className="path-row">
            <input value={outputPath} onChange={(event) => setOutputPath(event.target.value)} />
            <button className="secondary" onClick={chooseOutput}>Browse</button>
          </div>
        </label>
      </section>

      <section className="panel actions-panel">
        <div>
          <h2>Generate</h2>
          <p className="muted">Input folders are read-only. Output is written to a separate workspace.</p>
        </div>
        <div className="actions">
          <button onClick={scanInput} disabled={!inputPath}>Scan folder</button>
          <button onClick={() => generate("example")} disabled={!backendReady || !allRequiredOk || !inputPath}>Generate example</button>
          <button onClick={() => generate("all")} disabled={!backendReady || !allRequiredOk || !inputPath}>Generate all</button>
          <button onClick={() => generate("finals")} disabled={!backendReady || !allRequiredOk || !inputPath}>Generate finals</button>
        </div>
      </section>

      {scan && (
        <section className="panel">
          <h2>Folder review</h2>
          <p>{scan.pdf_count} PDFs found in {Object.keys(scan.courses).length} course folders.</p>
          <div className="course-list">
            {Object.entries(scan.courses).map(([course, count]) => <span key={course}>{course}: {count}</span>)}
          </div>
        </section>
      )}

      {job && (
        <section className="panel">
          <div className="progress-head">
            <div>
              <h2>Progress</h2>
              <p className="muted">{job.message}</p>
            </div>
            <strong>{job.progress}%</strong>
          </div>
          <progress value={job.progress} max={100} />
          <div className="progress-meta">
            <div>
              <span>Elapsed</span>
              <strong>{formatDuration(elapsed)}</strong>
            </div>
            <div>
              <span>Last backend activity</span>
              <strong>{formatClock(job.updated_at)}</strong>
            </div>
            <div>
              <span>No update for</span>
              <strong>{formatDuration(quietFor)}</strong>
            </div>
          </div>
          {job.status === "running" && quietFor !== null && quietFor >= 180 && (
            <p className="muted">Long Ollama call in progress. If this stays unchanged for more than 10 minutes on an example exam, restart the app and try another PDF.</p>
          )}
          {latestLog && <p className="latest-log">{latestLog}</p>}
          {job.status === "error" && <p className="error-text">{job.error}</p>}
          {job.status === "done" && <button onClick={openPreview}>Open exam index</button>}
          <pre>{job.logs.slice(-12).join("\n")}</pre>
        </section>
      )}
    </main>
  );
}

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
type CheckResponse = {
  checks: Check[];
  default_output: string;
  available_models: string[];
  default_model: string;
  selected_model: string;
};
type ModelTest = { ok: boolean; model: string; detail: string };
type ScanEstimate = {
  generate_all_minutes_low: number;
  generate_all_minutes_high: number;
  generate_finals_minutes_low: number;
  generate_finals_minutes_high: number;
  total_pdf_mb: number;
  estimated_source_chars: number;
  size_buckets: Record<string, number>;
  basis: { generate_all: string; generate_finals: string };
  history_runs_used: number;
  model: string;
  note: string;
};
type ScanResult = { input_path: string; pdf_count: number; courses: Record<string, number>; estimate?: ScanEstimate };
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
  log_path?: string;
  log_url?: string;
};

const API = "http://127.0.0.1:8766";
const FALLBACK_MODEL = "gemma4:31b-cloud";
const MODEL_STORAGE_KEY = "exam-generator:selected-model";

function defaultOutputPath() {
  return "~/Documents/Exam Generator Output";
}

function savedModel() {
  try {
    return window.localStorage.getItem(MODEL_STORAGE_KEY) || FALLBACK_MODEL;
  } catch {
    return FALLBACK_MODEL;
  }
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

function formatMinutesRange(low: number | undefined, high: number | undefined) {
  if (low === undefined || high === undefined) return "Unknown";
  const formatOne = (minutes: number) => {
    if (minutes < 60) return `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    return rest ? `${hours} h ${rest} min` : `${hours} h`;
  };
  return `${formatOne(low)} - ${formatOne(high)}`;
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
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState(savedModel());
  const [modelTest, setModelTest] = useState<ModelTest | null>(null);
  const [modelTesting, setModelTesting] = useState(false);
  const [scan, setScan] = useState<ScanResult | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [backendReady, setBackendReady] = useState(false);
  const [backendStarting, setBackendStarting] = useState(false);
  const [now, setNow] = useState(Date.now());

  const coreChecksOk = useMemo(() => checks.filter((item) => item.id !== "port" && item.id !== "model").every((item) => item.ok), [checks]);
  const ollamaReachable = checks.find((item) => item.id === "ollama")?.ok ?? false;
  const selectedModelAvailable = useMemo(
    () => availableModels.includes(selectedModel) || availableModels.some((name) => name.startsWith(`${selectedModel}:`)),
    [availableModels, selectedModel],
  );
  const modelReady = Boolean(modelTest?.ok && modelTest.model === selectedModel && selectedModelAvailable);
  const canGenerate = Boolean(backendReady && coreChecksOk && modelReady && inputPath);
  const modelOptions = useMemo(() => {
    const options = [...availableModels];
    if (selectedModel && !options.includes(selectedModel)) options.unshift(selectedModel);
    return options;
  }, [availableModels, selectedModel]);

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
      const data = await post<CheckResponse>("/api/check", { output_path: outputPath, model: selectedModel });
      setChecks(data.checks);
      setAvailableModels(data.available_models || []);
      if (!selectedModel && data.default_model) setSelectedModel(data.default_model);
      if (outputPath === defaultOutputPath()) setOutputPath(data.default_output);
    } catch (err) {
      setBackendReady(false);
      const detail = err instanceof Error ? err.message : String(err);
      setError(isTauri() ? `Could not start the local backend automatically: ${detail}` : "Backend is not running. For browser development, run scripts/dev-backend.sh first.");
    } finally {
      setBackendStarting(false);
    }
  }

  async function testSelectedModel(model: string) {
    setModelTesting(true);
    setModelTest({ ok: false, model, detail: "Testing..." });
    try {
      const result = await post<ModelTest>("/api/test-model", { model });
      setModelTest(result);
    } catch (err) {
      setModelTest({ ok: false, model, detail: err instanceof Error ? err.message : String(err) });
    } finally {
      setModelTesting(false);
    }
  }

  function chooseModel(model: string) {
    setSelectedModel(model);
    setModelTest(null);
    try {
      window.localStorage.setItem(MODEL_STORAGE_KEY, model);
    } catch {
      // Local storage is a convenience only.
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
      const result = await post<ScanResult>("/api/scan", { input_path: inputPath, model: selectedModel });
      setScan(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function generate(mode: "example" | "all" | "finals") {
    setError("");
    setJob(null);
    try {
      const started = await post<{ job_id: string }>("/api/generate", { input_path: inputPath, output_path: outputPath, mode, overwrite: false, model: selectedModel });
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

  async function openFullLog() {
    if (!job) return;
    await openUrl(`${API}${job.log_url || `/api/jobs/${job.id}/log`}`);
  }

  useEffect(() => {
    void checkBackend();
  }, []);

  useEffect(() => {
    if (!backendReady || !selectedModel) return;
    if (!ollamaReachable) {
      setModelTest({ ok: false, model: selectedModel, detail: "Could not reach Ollama" });
      return;
    }
    if (availableModels.length === 0) {
      setModelTest({ ok: false, model: selectedModel, detail: `No models installed. Run: ollama pull ${FALLBACK_MODEL}` });
      return;
    }
    if (!selectedModelAvailable) {
      setModelTest({ ok: false, model: selectedModel, detail: `Model not installed. Run: ollama pull ${selectedModel}` });
      return;
    }
    void testSelectedModel(selectedModel);
  }, [backendReady, ollamaReachable, selectedModel, selectedModelAvailable, availableModels.join("|")]);

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
        <div className="model-picker">
          <label>
            Ollama model
            <select value={selectedModel} onChange={(event) => chooseModel(event.target.value)} disabled={!backendReady || modelOptions.length === 0}>
              {modelOptions.length === 0 && <option value={selectedModel}>{selectedModel}</option>}
              {modelOptions.map((model) => <option value={model} key={model}>{model}</option>)}
            </select>
          </label>
          <div className={`model-status ${modelReady ? "ok" : modelTesting ? "neutral" : "bad"}`}>
            <strong>{modelTesting ? "Testing..." : modelReady ? "Model responded" : "Model not ready"}</strong>
            <p>{modelTest?.detail || (availableModels.length === 0 ? `No models installed. Run: ollama pull ${FALLBACK_MODEL}` : "Choose an installed Ollama model.")}</p>
          </div>
        </div>
        <div className="checks">
          {checks.length === 0 && <p className="muted">Backend status will appear here.</p>}
          {checks.filter((check) => check.id !== "model").map((check) => (
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
          <button onClick={() => generate("example")} disabled={!canGenerate}>Generate example</button>
          <button onClick={() => generate("all")} disabled={!canGenerate}>Generate all</button>
          <button onClick={() => generate("finals")} disabled={!canGenerate}>Generate finals</button>
        </div>
      </section>

      {scan && (
        <section className="panel">
          <h2>Folder review</h2>
          <p>{scan.pdf_count} PDFs found in {Object.keys(scan.courses).length} course folders.</p>
          {scan.estimate && (
            <div className="estimate-grid">
              <div className="estimate-card">
                <span>Generate all estimate</span>
                <strong>{formatMinutesRange(scan.estimate.generate_all_minutes_low, scan.estimate.generate_all_minutes_high)}</strong>
              </div>
              <div className="estimate-card">
                <span>Generate finals estimate</span>
                <strong>{formatMinutesRange(scan.estimate.generate_finals_minutes_low, scan.estimate.generate_finals_minutes_high)}</strong>
              </div>
              <div className="estimate-card">
                <span>PDF data</span>
                <strong>{scan.estimate.total_pdf_mb} MB</strong>
              </div>
              <div className="estimate-card">
                <span>Estimated source text</span>
                <strong>{scan.estimate.estimated_source_chars.toLocaleString()} chars</strong>
              </div>
            </div>
          )}
          <div className="course-list">
            {Object.entries(scan.courses).map(([course, count]) => <span key={course}>{course}: {count}</span>)}
          </div>
          {scan.estimate && (
            <p className="estimate-note">
              {scan.estimate.note} Basis: all = {scan.estimate.basis.generate_all}; finals = {scan.estimate.basis.generate_finals}.
            </p>
          )}
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
          <div className="job-actions">
            {job.status === "done" && <button onClick={openPreview}>Open exam index</button>}
            <button className="secondary" onClick={openFullLog}>Open full log</button>
          </div>
          <pre>{job.logs.slice(-12).join("\n")}</pre>
        </section>
      )}
    </main>
  );
}

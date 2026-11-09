import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import { openUrl } from "@tauri-apps/plugin-opener";

declare global { interface Window { __TAURI_INTERNALS__?: unknown } }
type Area = "material" | "create" | "learn" | "settings";
type Session = { url: string; token: string };
type Profile = { id: string; kind: "ollama" | "deepseek" | "compatible" | "chatgpt"; name: string; base_url: string; model: string; has_credential?: boolean };
type Source = { id: string; name: string; path: string; relative_path: string; course: string; format: string; fingerprint: string };
type Extraction = { text: string; method: string; warnings: string[] };
type Check = { id: string; label: string; ok: boolean; detail: string };
type Workspace = { id: string; name: string; exam_count: number; updated_at?: string };
type JobResult = { usage_summary?: { input_tokens: number | null; output_tokens: number | null; reasoning_tokens: number | null; cached_input_tokens: number | null; incomplete?: boolean }; workspace_id: string; generated: number | unknown[]; reused: number | unknown[]; skipped: number | unknown[]; failed: number | unknown[] };
type Estimate = { low_seconds: number | null; high_seconds: number | null; basis: string; sample_count: number; measured_at?: string | null };
type Execution = { location: string; basis: string; model_identity: string; model?: string; context_length?: number | null; hardware?: Record<string, unknown> | null; warnings?: string[] };
type Suitability = { status: string; checks: { name?: string; id?: string; ok: boolean | null; detail: string }[]; sample_count?: number; request_count?: number; trial_count?: number; duration_seconds?: number; concurrency_comparison?: { active_requests: number; duration_seconds: number; valid: boolean; useful_questions_per_minute: number }[] };
type MaterialDocument = { id: string; name: string; relative_path: string; course: string; words: number | null; characters: number | null; metadata: Record<string, number | string | string[] | null>; warnings: string[]; failed?: boolean };
export type Preparation = { analysis_id: string; input_signature: string; material: { documents: MaterialDocument[]; totals: Record<string, number | null>; warnings: string[] }; exams: { key: string; course: string; sources: { id: string; name: string; relative_path: string }[]; target_mc: number | null; target_open: number | null; reused: boolean; available: boolean; nominal_calls: number | null }[]; counts: { planned: number; valid: number; new: number; reused: number; unavailable: number }; execution: Execution; recommendation: { exam_slots: number; request_slots: number; reasons: string[] }; estimate: Estimate; suitability: Suitability; warnings: string[] };
type Calibration = { execution: Execution; suitability: Suitability; benchmark?: Record<string, unknown>; sample?: { source?: string; blueprint?: { topics?: string[] }; mc?: { multiple_choice?: { id: string; question: string; options?: { id: string; text: string; is_correct: boolean }[]; explanation: string }[] }; open?: { open_ended?: { id: string; question: string; answer_value: string | number; expected_answer: string }[] } } };
type Job = { id: string; kind?: string; status: string; message: string; progress: number; started_at?: string; updated_at?: string; error?: string; logs: string[]; result?: JobResult | Preparation | Calibration; workload?: { total: number; completed: number; active: number; queued: number }; estimate?: Estimate };
type PendingPreparation = { id: string; signature: string; kind: string };
type ReasoningEffort = "off" | "low" | "high" | "max";
type Saved = { reasoningEffort?: ReasoningEffort; inputPath?: string; outputPath?: string; profileId?: string; model?: string; sourceIds?: string[]; language?: "de" | "en" };
const STORAGE_KEY = "exam-generator:preferences-v2";
const AREAS: { id: Area; name: string }[] = [{ id: "material", name: "Material" }, { id: "create", name: "Prüfung erstellen" }, { id: "learn", name: "Lernen" }, { id: "settings", name: "Einstellungen" }];
const PROVIDERS = { ollama: "Ollama", deepseek: "DeepSeek", compatible: "Kompatible API", chatgpt: "ChatGPT" };
const blankProfile: Profile = { id: "", kind: "deepseek", name: "DeepSeek", base_url: "https://api.deepseek.com", model: "" };
function readSaved(): Saved {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return {
      inputPath: typeof value.inputPath === "string" ? value.inputPath : undefined,
      outputPath: typeof value.outputPath === "string" ? value.outputPath : undefined,
      profileId: typeof value.profileId === "string" ? value.profileId : undefined,
      model: typeof value.model === "string" ? value.model : undefined,
      sourceIds: Array.isArray(value.sourceIds) ? value.sourceIds.filter((id: unknown) => typeof id === "string") : undefined,
      language: value.language === "en" ? "en" : "de",
      reasoningEffort: ["off", "low", "high", "max"].includes(value.reasoningEffort) ? value.reasoningEffort : undefined,
    };
  } catch { return {}; }
}
function message(error: unknown) { return error instanceof Error ? error.message : String(error); }
function count(value: number | unknown[] | undefined) { return Array.isArray(value) ? value.length : value || 0; }
function active(job: Job | null) { return Boolean(job && ["running", "stopping", "queued"].includes(job.status)); }
function duration(start?: string, end?: string) { const seconds = start ? Math.max(0, Math.floor(((end ? new Date(end).getTime() : Date.now()) - new Date(start).getTime()) / 1000)) : NaN; return Number.isFinite(seconds) ? `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")} min` : "–"; }
export function ThinkingControl({ value = "low", onChange, disabled = false }: { value?: ReasoningEffort; onChange: (value: ReasoningEffort) => void; disabled?: boolean }) {
  return <label>Denktiefe (DeepSeek)<select value={value} disabled={disabled} onChange={event => onChange(event.target.value as ReasoningEffort)}><option value="off">Aus</option><option value="low">Niedrig (Standard)</option><option value="high">Hoch</option><option value="max">Maximal</option></select><span className="field-note">Niedrig ist der sparsamere Ausgangspunkt. Qualität mit einer Stichprobe prüfen. Medium entspricht bei DeepSeek Hoch.</span></label>;
}
export function plannedExamCount(sources: { id: string; course: string }[], assignments: Record<string, string>, mode: string) {
  return mode === "example" ? Math.min(1, sources.length) : mode === "finals" ? new Set(sources.map(source => assignments[source.id] ?? source.course)).size : sources.length;
}
export function estimateLabel(estimate?: Estimate) {
  if (estimate?.low_seconds == null || estimate.high_seconds == null) return "Noch nicht kalibriert";
  const minutes = (seconds: number) => Math.max(1, Math.ceil(seconds / 60));
  return estimate.high_seconds === 0 ? "Keine neue KI-Generierung erforderlich" : `${minutes(estimate.low_seconds)}–${minutes(estimate.high_seconds)} Min.`;
}
export function jobTitle(kind: string | undefined, status: string) {
  if (status === "stopping") return "Wird gestoppt …";
  if (status === "canceled") return "Gestoppt";
  if (kind === "preflight") return status === "error" ? "Materialanalyse fehlgeschlagen" : status === "done" ? "Materialanalyse bereit" : status === "partial" ? "Materialanalyse mit Hinweisen" : "Material wird analysiert";
  if (kind === "calibration") return status === "error" ? "Modellstichprobe fehlgeschlagen" : status === "done" || status === "partial" ? "Modellstichprobe abgeschlossen" : "Modellstichprobe läuft";
  return status === "done" ? "Prüfungen bereit" : status === "partial" ? "Mit Lücken abgeschlossen" : status === "error" ? "Prüfung nicht erstellt" : "Prüfung wird erstellt";
}
export function applicablePlanningResult(job: Job, pending: PendingPreparation | null, currentSignature: string) {
  if (!pending || pending.id !== job.id || pending.signature !== currentSignature || !["done", "partial"].includes(job.status) || !job.result) return null;
  if (pending.kind === "preflight" && "analysis_id" in job.result) return { kind: "preflight" as const, signature: pending.signature, result: job.result };
  if (pending.kind === "calibration" && "suitability" in job.result && "execution" in job.result) return { kind: "calibration" as const, signature: pending.signature, result: job.result };
  return null;
}
function descriptive(value: unknown) { return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("de-AT") : "nicht ermittelt"; }
function executionName(location: string) { return ({ local: "Lokal", cloud: "Cloud", remote: "Anderer Server", unknown: "Nicht ermittelt" } as Record<string, string>)[location] || location; }
function suitabilityName(status: string) { return ({ untested: "Nicht geprüft", sample_passed: "Stichprobe bestanden", limited: "Mit Einschränkungen", unsuitable: "Für diese Konfiguration ungeeignet" } as Record<string, string>)[status] || status; }
function SuitabilitySummary({ result }: { result: Suitability }) {
  return <div className="suitability"><h4>Modelleignung: {suitabilityName(result.status)}</h4><p className="field-note">{result.sample_count ? `${result.sample_count} geprüfte Stichprobenfragen; eine bestandene Stichprobe garantiert keine fehlerfreie Prüfung.` : "Die Verbindung allein belegt noch keine fachlich gute Prüfung."}{result.request_count != null ? ` ${result.request_count} Testanfragen.` : ""}</p>{result.checks?.length > 0 && <ul className="check-list">{result.checks.map((check, index) => <li key={`${check.name || check.id}-${index}`}><strong>{check.ok === true ? "Bestanden" : check.ok === false ? "Prüfen" : "Nicht geprüft"}: {check.name || check.id}</strong><span>{check.detail}</span></li>)}</ul>}{result.concurrency_comparison?.length ? <ul className="check-list">{result.concurrency_comparison.map(comparison => <li key={comparison.active_requests}><strong>{comparison.active_requests} gleichzeitige KI-Anfragen</strong><span>{comparison.valid ? `${descriptive(comparison.useful_questions_per_minute)} verwendbare Fragen/Minute · ${Math.ceil(comparison.duration_seconds)} Sekunden` : "Keine belastbare Durchsatzmessung: Testantworten waren nicht verwendbar."}</span></li>)}</ul> : null}</div>;
}
function sampleText(value: unknown) { return typeof value === "string" || typeof value === "number" ? String(value) : "Nicht verwendbare Angabe"; }
export function CalibrationSample({ sample }: { sample: Calibration["sample"] }) {
  if (!sample) return null;
  const multiple = Array.isArray(sample.mc?.multiple_choice) ? sample.mc.multiple_choice.filter(question => question && typeof question === "object") : [];
  const opened = Array.isArray(sample.open?.open_ended) ? sample.open.open_ended.filter(question => question && typeof question === "object") : [];
  return <details className="sample-review"><summary>Stichprobenfragen fachlich prüfen</summary><p className="field-note">Prüfe Sprache, Erklärungen und Anspruch anhand des künstlichen Quelltexts. Die Markierungen unten zeigen die Antworten des Modells und können falsch sein.</p><h4>Künstlicher Quelltext</h4><pre tabIndex={0}>{sampleText(sample.source)}</pre><h4>Multiple-Choice-Fragen</h4><ol className="planned-exams">{multiple.map((question, index) => <li key={index}><strong>{sampleText(question.question)}</strong><ul>{(Array.isArray(question.options) ? question.options : []).filter(option => option && typeof option === "object").map((option, optionIndex) => <li key={optionIndex}>{sampleText(option.text)}{option.is_correct === true ? " (vom Modell als richtig markiert)" : ""}</li>)}</ul><p>{sampleText(question.explanation)}</p></li>)}</ol><h4>Offene Fragen</h4><ol className="planned-exams">{opened.map((question, index) => <li key={index}><strong>{sampleText(question.question)}</strong><p>{sampleText(question.expected_answer)}</p></li>)}</ol></details>;
}
export function PreparationSummary({ result }: { result: Preparation }) {
  const total = result.material.totals;
  return <section className="preparation-summary" aria-label="Materialanalyse und Planung">
    <h3>{result.counts.planned} geplante {result.counts.planned === 1 ? "Prüfung" : "Prüfungen"}</h3>
    <p>{result.counts.new} neu · {result.counts.reused} wiederverwendbar · {result.counts.unavailable} nicht verfügbar</p>
    <dl className="material-statistics">{[["Dokumente", "documents"], ["Erkannte Wörter", "words"], ["PDF-Seiten", "pages"], ["Folien", "slides"], ["Bildvorkommen", "image_occurrences"], ["Tabellen", "tables"]].map(([label, key]) => <div key={key}><dt>{label}</dt><dd>{descriptive(total[key])}</dd>{Boolean(total[key + "_unknown_documents"]) && <span className="field-note">Bei {total[key + "_unknown_documents"]} Dokumenten nicht anwendbar oder nicht ermittelt</span>}</div>)}</dl>
    <p className="field-note">Bildvorkommen sind eingebettete Rasterbilder. Diagramme und Bildinhalte werden dadurch nicht automatisch verstanden. Für Word werden keine Seitenzahlen erfunden.</p>
    <div className="preparation-estimate"><strong>Geschätzte Dauer: {estimateLabel(result.estimate)}</strong><p className="muted">{result.estimate.basis} · {result.estimate.sample_count} vergleichbare Messungen{result.estimate.measured_at ? ` · Stand ${new Date(result.estimate.measured_at).toLocaleString("de-AT")}` : ""}</p></div>
    <p><strong>{executionName(result.execution.location)}</strong> · {result.execution.model || "Gewähltes Modell"}</p><p className="field-note">{result.execution.basis}{result.execution.context_length != null ? ` · Kontext: ${descriptive(result.execution.context_length)} Tokens` : " · Kontext nicht ermittelt"}</p>
    <p className="preparation-recommendation"><strong>Empfohlen: {result.recommendation.exam_slots} {result.recommendation.exam_slots === 1 ? "Prüfung" : "Prüfungen"} und höchstens {result.recommendation.request_slots} gleichzeitige KI-Anfragen</strong></p><ul className="field-note">{result.recommendation.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>
    <SuitabilitySummary result={result.suitability} />
    <details><summary>Geplante Prüfungen und Quellen</summary><ul className="planned-exams">{result.exams.map(exam => <li key={exam.key}><strong>{exam.course}</strong><span>{exam.available ? exam.reused ? "Wiederverwendbar" : `${descriptive(exam.target_mc)} MC · ${descriptive(exam.target_open)} offene Fragen · mindestens ${descriptive(exam.nominal_calls)} KI-Anfragen` : "Kein verwendbares Material"}</span><ul>{exam.sources.map(source => <li key={source.id}>{source.relative_path || source.name}</li>)}</ul></li>)}</ul></details>
    <details><summary>Beschreibungen der Dokumente</summary><ul className="planned-exams">{result.material.documents.map(doc => <li key={doc.id}><strong>{doc.relative_path || doc.name}</strong><span>{doc.failed ? "Inhalt konnte nicht gelesen werden" : `${descriptive(doc.words)} Wörter · ${descriptive(doc.metadata?.size_bytes)} Bytes`}</span><span>Seiten: {descriptive(doc.metadata?.pages)} · Folien: {descriptive(doc.metadata?.slides)} · Bildvorkommen: {descriptive(doc.metadata?.image_occurrences)}</span>{doc.warnings?.length > 0 && <ul className="warning-list">{doc.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}</li>)}</ul></details>
    {[...result.warnings, ...(result.material.warnings || []), ...(result.execution.warnings || [])].length > 0 && <ul className="warning-list">{[...new Set([...result.warnings, ...(result.material.warnings || []), ...(result.execution.warnings || [])])].map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
  </section>;
}
async function openExternal(url: string) { if (window.__TAURI_INTERNALS__) await openUrl(url); else window.location.assign(url); }

export default function App() {
  const [saved] = useState(readSaved);
  const session = useRef<Session>({ url: "http://127.0.0.1:8766", token: "" });
  const mounted = useRef(true), connectVersion = useRef(0), scanVersion = useRef(0), extractionVersion = useRef(0), modelVersion = useRef(0);
  const [area, setArea] = useState<Area>("material");
  const [ready, setReady] = useState(false), [connecting, setConnecting] = useState(false);
  const [error, setError] = useState(""), [notice, setNotice] = useState(""), [busy, setBusy] = useState("");
  const [inputPath, setInputPath] = useState(saved.inputPath || ""), [paths, setPaths] = useState<string[]>([]);
  const [outputPath, setOutputPath] = useState(saved.outputPath || "~/Documents/Exam Generator Output");
  const [sources, setSources] = useState<Source[]>([]), [sourceIds, setSourceIds] = useState<string[]>([]);
  const [courseAssignments, setCourseAssignments] = useState<Record<string, string>>({});
  const [extractionModes, setExtractionModes] = useState<Record<string, "auto" | "ocr">>({});
  const [scanned, setScanned] = useState(false), [inspecting, setInspecting] = useState<Source | null>(null), [extraction, setExtraction] = useState<Extraction | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]), [profileId, setProfileId] = useState(saved.profileId || ""), [model, setModel] = useState(saved.model || ""), [models, setModels] = useState<string[]>([]);
  const [modelTest, setModelTest] = useState<{ ok: boolean; detail: string } | null>(null);
  const [profileDraft, setProfileDraft] = useState<Profile>(blankProfile), [apiKey, setApiKey] = useState(""), [gradingProfileId, setGradingProfileId] = useState("");
  const [oauth, setOauth] = useState<{ status: string; detail?: string }>({ status: "signed_out" });
  const [checks, setChecks] = useState<Check[]>([]), [tools, setTools] = useState<{ available?: boolean; ready?: boolean; detail?: string; message?: string } | null>(null);
  const [library, setLibrary] = useState<Workspace[]>([]), [job, setJob] = useState<Job | null>(null), [fullLog, setFullLog] = useState("");
  const [mode, setMode] = useState<"example" | "all" | "finals">("example"), [language, setLanguage] = useState<"de" | "en">(saved.language || "de");
  const [difficulty, setDifficulty] = useState("balanced"), [coverage, setCoverage] = useState("auto"), [targetMode, setTargetMode] = useState("auto");
  const [targetMc, setTargetMc] = useState(20), [targetOpen, setTargetOpen] = useState(5), [diagnostics, setDiagnostics] = useState(false);
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>(saved.reasoningEffort || "low");
  const [locationOverride, setLocationOverride] = useState("auto"), [examSlots, setExamSlots] = useState(""), [requestSlots, setRequestSlots] = useState("");
  const [preparation, setPreparation] = useState<{ signature: string; result: Preparation } | null>(null);
  const [calibration, setCalibration] = useState<{ signature: string; result: Calibration } | null>(null), [compareConcurrency, setCompareConcurrency] = useState(false);
  const pendingPreparation = useRef<PendingPreparation | null>(null);
  const currentPreparationSignature = useRef(""), currentCalibrationSignature = useRef("");
  const [developerUrl, setDeveloperUrl] = useState("http://127.0.0.1:8766"), [developerToken, setDeveloperToken] = useState("");
  const [, tick] = useState(0);
  const profile = profiles.find(item => item.id === profileId), running = active(job);
  const selectedSources = sources.filter(source => sourceIds.includes(source.id));
  const modelOptions = [...new Set([model, ...models].filter(Boolean))];
  const selectedEffort = profile?.kind === "deepseek" ? reasoningEffort : undefined;
  const preparationPayload = { reasoning_effort: selectedEffort, input_path: inputPath, paths: paths.length ? paths : undefined, output_path: outputPath, source_ids: sourceIds, course_assignments: courseAssignments, extraction_modes: extractionModes, profile_id: profileId, model, mode, language, difficulty, coverage_mode: coverage, target_mode: targetMode, target_mc: targetMode === "manual" ? targetMc : undefined, target_open: targetMode === "manual" ? targetOpen : undefined, overwrite: false, location_override: locationOverride, max_parallel_exams: examSlots ? Number(examSlots) : undefined, max_active_requests: requestSlots ? Number(requestSlots) : undefined };
  const preparationSignature = JSON.stringify([preparationPayload, selectedSources.map(source => [source.id, source.fingerprint]), profile]);
  const calibrationSignature = JSON.stringify([profile, model, locationOverride, language, difficulty, selectedEffort]);
  currentPreparationSignature.current = preparationSignature;
  currentCalibrationSignature.current = calibrationSignature;
  const prepared = preparation?.signature === preparationSignature ? preparation.result : null;
  const calibrated = calibration?.signature === calibrationSignature ? calibration.result : null;
  const generationResult = job?.result && "workspace_id" in job.result ? job.result : null;
  const plannedCount = plannedExamCount(selectedSources, courseAssignments, mode);
  async function api<T>(path: string, body?: unknown): Promise<T> {
    const response = await fetch(`${session.current.url}${path}`, { method: body === undefined ? "GET" : "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${session.current.token}` }, body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await response.json().catch(() => ({ error: "Die Antwort konnte nicht gelesen werden. Bitte erneut versuchen." }));
    if (!response.ok) throw new Error(data.error || `Anfrage fehlgeschlagen (${response.status}).`);
    return data as T;
  }
  async function refreshProfiles() {
    const data = await api<{ profiles: Profile[]; grading_profile_id?: string }>("/api/profiles");
    if (!mounted.current) return;
    setProfiles(data.profiles); setGradingProfileId(data.grading_profile_id || "");
    setProfileId(current => data.profiles.some(item => item.id === current) ? current : data.profiles[0]?.id || "");
  }
  async function refreshLibrary() { const data = await api<{ workspaces: Workspace[] }>("/api/library"); if (mounted.current) setLibrary(data.workspaces); }
  async function connect(manual = false) {
    const version = ++connectVersion.current;
    setConnecting(true); setError(""); setReady(false);
    try {
      if (manual) { const url = new URL(developerUrl); if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname)) throw new Error("Die lokale Verbindung muss auf localhost oder 127.0.0.1 zeigen."); session.current = { url: url.origin, token: developerToken }; }
      else if (window.__TAURI_INTERNALS__) session.current = await invoke<Session>("start_backend");
      let connected = false;
      for (let attempt = 0; attempt < 20; attempt++) { try { await api("/api/health"); connected = true; break; } catch { if (attempt < 19) await new Promise(resolve => window.setTimeout(resolve, 250)); } }
      if (!connected) throw new Error("Der lokale Dienst ist nicht erreichbar. Starte die App erneut oder prüfe die Startdiagnose in den Einstellungen.");
      await refreshProfiles(); await refreshLibrary();
      if (mounted.current && version === connectVersion.current) setReady(true);
    } catch (err) { if (mounted.current && version === connectVersion.current) setError(message(err)); }
    finally { if (mounted.current && version === connectVersion.current) setConnecting(false); }
  }
  useEffect(() => { mounted.current = true; void connect(); return () => { mounted.current = false; connectVersion.current++; }; }, []);
  useEffect(() => { try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ inputPath, outputPath, profileId, model, sourceIds, language, reasoningEffort })); } catch { /* Preferences are optional; credentials are never stored here. */ } }, [inputPath, outputPath, profileId, model, sourceIds, language, reasoningEffort]);
  useEffect(() => { modelVersion.current++; setModelTest(null); setModels([]); if (profile) setModel(current => profile.id === saved.profileId && current ? current : profile.model || ""); }, [profileId]);
  useEffect(() => {
    if (!ready || !profileId) return;
    const version = ++modelVersion.current;
    void api<{ models: string[] }>("/api/models", { profile_id: profileId }).then(data => { if (mounted.current && version === modelVersion.current) { setModels(data.models || []); setModel(current => current || data.models?.[0] || ""); } }).catch(() => { /* Manual model entry works when discovery is unsupported. */ });
    return () => { modelVersion.current++; };
  }, [profileId, ready]);
  useEffect(() => {
    if (!job || !active(job)) return;
    const id = job.id; let canceled = false; let timer: number;
    const poll = async () => { try {
      const next = await api<Job>(`/api/jobs/${encodeURIComponent(id)}`);
      if (canceled) return;
      setJob(next);
      if (!active(next)) {
        const pending = pendingPreparation.current;
        const result = applicablePlanningResult(next, pending, pending?.kind === "calibration" ? currentCalibrationSignature.current : currentPreparationSignature.current);
        if (result?.kind === "preflight") setPreparation({ signature: result.signature, result: result.result });
        if (result?.kind === "calibration") setCalibration({ signature: result.signature, result: result.result });
        if (next.result && "workspace_id" in next.result) await refreshLibrary();
        return;
      }
    } catch (err) { if (!canceled) setError(`Fortschritt konnte nicht geladen werden: ${message(err)}`); } if (!canceled) timer = window.setTimeout(poll, 1500); };
    timer = window.setTimeout(poll, 300); const clock = window.setInterval(() => tick(value => value + 1), 1000);
    return () => { canceled = true; window.clearTimeout(timer); window.clearInterval(clock); };
  }, [job?.id, job?.status]);
  useEffect(() => {
    if (!ready || oauth.status !== "pending") return;
    let stopped = false;
    const timer = window.setInterval(() => { void api<typeof oauth>("/api/oauth/status").then(next => { if (stopped) return; setOauth(next); if (next.status === "connected") { void refreshProfiles(); setNotice("ChatGPT ist verbunden. Wähle jetzt ein Modell und prüfe die Verbindung."); } }).catch(err => { if (!stopped) setOauth({ status: "error", detail: message(err) }); }); }, 1500);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [ready, oauth.status]);
  function invalidateSources(path = inputPath, selectedPaths: string[] = []) { scanVersion.current++; extractionVersion.current++; setInputPath(path); setPaths(selectedPaths); setSources([]); setSourceIds([]); setExtractionModes({}); setScanned(false); setInspecting(null); setExtraction(null); }
  async function action(name: string, work: () => Promise<void>) { setBusy(name); setError(""); setNotice(""); try { await work(); } catch (err) { setError(message(err)); } finally { setBusy(""); } }
  async function chooseInput(files: boolean) { await action("Auswahl", async () => {
    const selected = await open(files ? { multiple: true, filters: [{ name: "Lernmaterial", extensions: ["pdf", "docx", "pptx", "txt", "md", "png", "jpg", "jpeg", "tif", "tiff", "webp"] }] } : { directory: true, multiple: false });
    if (Array.isArray(selected) && selected.length) { const sep = selected[0].includes("\\") ? "\\" : "/"; invalidateSources(selected[0].slice(0, selected[0].lastIndexOf(sep)), selected); } else if (typeof selected === "string") invalidateSources(selected);
  }); }
  async function scanInput() { const version = ++scanVersion.current; await action("Material prüfen", async () => {
    const data = await api<{ sources: Source[]; input_path: string }>("/api/scan", { input_path: inputPath, paths: paths.length ? paths : undefined });
    if (version !== scanVersion.current) return;
    setSources(data.sources); setInputPath(data.input_path); setScanned(true);
    const remembered = data.sources.filter(source => saved.sourceIds?.includes(source.id)).map(source => source.id);
    setSourceIds(remembered.length ? remembered : data.sources.map(source => source.id)); setCourseAssignments(Object.fromEntries(data.sources.map(source => [source.id, source.course])));
    setNotice(data.sources.length ? `${data.sources.length} ${data.sources.length === 1 ? "Dokument" : "Dokumente"} gefunden. Prüfe die Auswahl und bei Bedarf den extrahierten Inhalt.` : "Keine unterstützten Dokumente gefunden. Wähle einen anderen Ordner oder einzelne Dateien.");
  }); }
  async function inspectSource(source: Source, mode: "auto" | "ocr" = extractionModes[source.id] || "auto") { const version = ++extractionVersion.current; setInspecting(source); setExtraction(null); await action("Inhalt prüfen", async () => { const data = await api<Extraction>("/api/extract", { source_id: source.id, input_path: inputPath, paths: paths.length ? paths : undefined, mode }); if (version === extractionVersion.current) { setExtraction(data); setExtractionModes(current => ({ ...current, [source.id]: mode })); } }); }
  async function testModel() { const version = ++modelVersion.current; setModelTest(null); await action("Verbindung prüfen", async () => { const result = await api<{ ok: boolean; detail: string }>("/api/test-model", { profile_id: profileId, model, reasoning_effort: selectedEffort }); if (version === modelVersion.current) setModelTest(result); }); }
  async function startPreparation(kind: "preflight" | "calibration") {
    const signature = kind === "preflight" ? preparationSignature : calibrationSignature;
    await action(kind === "preflight" ? "Material analysieren" : "Modell testen", async () => {
      const result = await api<{ job_id: string }>(kind === "preflight" ? "/api/preflight" : "/api/calibrate", kind === "preflight" ? preparationPayload : { profile_id: profileId, model, reasoning_effort: selectedEffort, location_override: locationOverride, language, difficulty, compare_concurrency: compareConcurrency });
      pendingPreparation.current = { id: result.job_id, signature, kind };
      setFullLog(""); setJob({ id: result.job_id, kind, status: "running", message: kind === "preflight" ? "Material wird lokal analysiert" : "Synthetische Modellstichprobe läuft", progress: 0, started_at: new Date().toISOString(), logs: [] });
    });
  }
  async function startGeneration() { await action("Prüfung starten", async () => {
    const result = await api<{ job_id: string }>("/api/generate", { ...preparationPayload, analysis_id: prepared?.analysis_id, diagnostics });
    pendingPreparation.current = null;
    setFullLog(""); setJob({ id: result.job_id, kind: mode, status: "running", message: "Vorbereitung läuft", progress: 0, started_at: new Date().toISOString(), logs: [] });
  }); }
  async function showWorkspace(workspaceId: string) { await action("Prüfungen öffnen", async () => { const data = await api<{ url: string }>("/api/open-preview", { workspace_id: workspaceId }); await openExternal(data.url); }); }
  async function saveProfile() { await action("Verbindung speichern", async () => { const result = await api<Profile>("/api/profiles", { ...profileDraft, api_key: apiKey || undefined }); setApiKey(""); await refreshProfiles(); setProfileId(result.id); setModel(result.model || ""); setProfileDraft(result); setNotice("Verbindung gespeichert. Zugangsdaten werden im Schlüsselbund verwahrt."); }); }
  function switchProvider(kind: Profile["kind"]) { setApiKey(""); setProfileDraft(profiles.find(item => kind === "chatgpt" && item.kind === "chatgpt") || { id: kind === "chatgpt" ? "chatgpt" : "", kind, name: PROVIDERS[kind], base_url: kind === "deepseek" ? "https://api.deepseek.com" : kind === "ollama" ? "http://localhost:11434" : kind === "chatgpt" ? "https://api.openai.com/v1" : "", model: "" }); }
  function changeArea(next: Area) { setArea(next); setError(""); setNotice(""); }
  function selectTab(event: React.KeyboardEvent<HTMLButtonElement>, index: number) {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    const target = event.key === "Home" ? 0 : event.key === "End" ? AREAS.length - 1 : step ? (index + step + AREAS.length) % AREAS.length : -1;
    if (target < 0) return; event.preventDefault(); changeArea(AREAS[target].id); document.getElementById(`tab-${AREAS[target].id}`)?.focus();
  }
  const cloud = profile?.kind !== "ollama" || /cloud/i.test(model);
  const canGenerate = ready && Boolean(profile && model.trim() && selectedSources.length && outputPath.trim()) && selectedSources.every(source => (courseAssignments[source.id] ?? source.course).trim()) && !running && !busy && (targetMode !== "manual" || targetMc + targetOpen > 0);

  return <main className="shell" lang="de">
    <header className="topbar"><div><h1>Exam Generator</h1><p className="lede">Aus deinem Lernmaterial werden Prüfungen zum Üben.</p></div><div className="connection"><span className={`status-dot ${ready ? "ok" : ""}`} aria-hidden="true" /><span>{connecting ? "App wird gestartet …" : ready ? "Lokaler Dienst bereit" : "Lokaler Dienst nicht verbunden"}</span>{!ready && <button className="secondary small" disabled={connecting} onClick={() => void connect()}>Erneut verbinden</button>}</div></header>
    <nav className="tabs" role="tablist" aria-label="Arbeitsbereiche">{AREAS.map((item, index) => <button id={`tab-${item.id}`} role="tab" key={item.id} className={area === item.id ? "selected" : undefined} aria-selected={area === item.id} aria-controls={`panel-${item.id}`} tabIndex={area === item.id ? 0 : -1} onKeyDown={event => selectTab(event, index)} onClick={() => changeArea(item.id)}>{item.name}</button>)}</nav>
    {error && <div className="notice error" role="alert">{error}</div>}{notice && <div className="notice" role="status">{notice}</div>}
    {AREAS.filter(item => item.id !== area).map(item => <div key={item.id} id={`panel-${item.id}`} role="tabpanel" aria-labelledby={`tab-${item.id}`} hidden />)}
    <div id={`panel-${area}`} role="tabpanel" aria-labelledby={`tab-${area}`} className="workspace" tabIndex={0}>
      {area === "material" && <>
        <div className="section-heading"><div><h2>Material auswählen</h2><p className="muted">PDF, Word, PowerPoint, Text, Markdown und Bilder. Deine Originale bleiben unverändert.</p></div></div>
        <label htmlFor="input-path">Ordner mit Lernmaterial</label><div className="path-row"><input id="input-path" value={inputPath} onChange={event => invalidateSources(event.target.value)} placeholder="Ordner auswählen oder Pfad eingeben" disabled={running} /><button className="secondary" disabled={running || Boolean(busy) || !window.__TAURI_INTERNALS__} onClick={() => void chooseInput(false)}>Ordner wählen</button><button className="secondary" disabled={running || Boolean(busy) || !window.__TAURI_INTERNALS__} onClick={() => void chooseInput(true)}>Dateien wählen</button></div>
        {paths.length > 0 && <p className="field-note">{paths.length} einzelne Dateien ausgewählt.</p>}
        <div className="toolbar"><button disabled={!ready || !inputPath.trim() || Boolean(busy) || running} onClick={() => void scanInput()}>{busy === "Material prüfen" ? "Material wird geprüft …" : "Material prüfen"}</button><span className="muted">Es werden noch keine Inhalte an eine KI gesendet.</span></div>
        {scanned && <section className="source-section" aria-label="Gefundene Dokumente"><div className="section-heading"><h3>{sources.length} {sources.length === 1 ? "Dokument" : "Dokumente"} gefunden</h3><div className="text-actions"><button className="text-button" disabled={running} onClick={() => setSourceIds(sources.map(source => source.id))}>Alle auswählen</button><button className="text-button" disabled={running} onClick={() => setSourceIds([])}>Auswahl aufheben</button></div></div>
          {sources.length ? <><div className="source-table"><div className="source-table-head" aria-hidden="true"><span>Dokument</span><span>Lehrveranstaltung</span><span>Inhalt</span></div>{sources.map(source => <div className="source-row" key={source.id}><label className="source-choice"><input type="checkbox" checked={sourceIds.includes(source.id)} disabled={running} onChange={event => setSourceIds(current => event.target.checked ? [...current, source.id] : current.filter(id => id !== source.id))} /><span><strong>{source.name}</strong><span className="source-path" title={source.relative_path}>{source.relative_path}</span><span className="file-format">{source.format.toUpperCase()}</span></span></label><input aria-label={`Lehrveranstaltung für ${source.name}`} value={courseAssignments[source.id] ?? source.course} disabled={running} onChange={event => setCourseAssignments(current => ({ ...current, [source.id]: event.target.value }))} /><button className="secondary small" disabled={Boolean(busy)} onClick={() => void inspectSource(source)}>Inhalt prüfen</button></div>)}</div><div className="toolbar end"><span>{sourceIds.length} von {sources.length} ausgewählt</span><button disabled={!sourceIds.length} onClick={() => changeArea("create")}>Prüfung vorbereiten</button></div></> : <p className="empty-state">Hier ist noch kein unterstütztes Lernmaterial. Wähle einen anderen Ordner.</p>}
        </section>}
        {!scanned && <div className="empty-state"><h3>Erst das Material, dann die Prüfung.</h3><p>Prüfe die gefundenen Dateien und ihre Lehrveranstaltung. Bei Scans kannst du vor der Generierung kontrollieren, was erkannt wurde.</p></div>}
        {inspecting && <section className="extraction"><div className="section-heading"><div><h3>{inspecting.name}</h3><p className="muted">{extraction ? `Erkennung: ${extraction.method}` : busy === "Inhalt prüfen" ? "Inhalt wird lokal gelesen …" : "Inhalt konnte nicht geladen werden. Beachte den Fehlerhinweis oben."}</p></div><button className="text-button" onClick={() => { extractionVersion.current++; setInspecting(null); setExtraction(null); }}>Schließen</button></div>{extraction?.warnings?.length ? <ul className="warning-list">{extraction.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul> : null}{extraction && inspecting.format === "pdf" && <button className="secondary small" disabled={Boolean(busy)} onClick={() => void inspectSource(inspecting, "ocr")}>PDF vollständig mit lokaler Texterkennung prüfen</button>}{extraction && <pre className="extracted-text" tabIndex={0} aria-label="Extrahierter Quellentext">{extraction.text || "Kein nutzbarer Inhalt erkannt."}</pre>}</section>}
      </>}
      {area === "create" && <>
        <div className="section-heading"><div><h2>Prüfung erstellen</h2><p className="muted">{selectedSources.length ? `${selectedSources.length} ${selectedSources.length === 1 ? "Dokument" : "Dokumente"} ausgewählt.` : "Wähle zuerst dein Lernmaterial aus."}</p></div><button className="text-button" onClick={() => changeArea("material")}>Material ändern</button></div>
        <div className="form-grid"><label>Prüfungsart<select value={mode} disabled={running} onChange={event => setMode(event.target.value as typeof mode)}><option value="example">Kurze Beispielprüfung</option><option value="all">Eine Prüfung pro Dokument</option><option value="finals">Gesamtprüfung pro Lehrveranstaltung</option></select></label><label>Sprache der Prüfungen<select value={language} disabled={running} onChange={event => setLanguage(event.target.value as typeof language)}><option value="de">Deutsch</option><option value="en">Englisch</option></select></label><label>Schwierigkeit<select value={difficulty} disabled={running} onChange={event => setDifficulty(event.target.value)}><option value="balanced">Ausgewogen</option><option value="basic">Grundlagen</option><option value="challenging">Anspruchsvoll</option></select></label><label>Abdeckung<select value={coverage} disabled={running} onChange={event => setCoverage(event.target.value)}><option value="auto">Automatisch passend zum Material</option><option value="representative">Repräsentative Auswahl</option><option value="full_coverage">Alle erkannten Abschnitte verarbeiten</option></select></label><label>KI-Verbindung<select value={profileId} disabled={!ready || running} onChange={event => setProfileId(event.target.value)}><option value="">Verbindung auswählen</option>{profiles.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>Modell<input list="available-models" value={model} disabled={!profile || running} onChange={event => { modelVersion.current++; setModel(event.target.value); setModelTest(null); }} placeholder="Modell auswählen oder Namen eingeben" /><datalist id="available-models">{modelOptions.map(name => <option value={name} key={name} />)}</datalist></label></div>
        {profile?.kind === "deepseek" && <ThinkingControl value={reasoningEffort} disabled={running} onChange={value => { modelVersion.current++; setModelTest(null); setReasoningEffort(value); }} />}
        <div className="connection-detail"><span>{profile ? `${PROVIDERS[profile.kind]} · ${profile.base_url}` : "Richte deine KI-Verbindung in den Einstellungen ein."}</span><button className="text-button" disabled={!ready || !profile || !model || Boolean(busy) || running} onClick={() => void testModel()}>{busy === "Verbindung prüfen" ? "Wird geprüft …" : "Verbindung prüfen"}</button>{modelTest && <span role="status" className={modelTest.ok ? "success-text" : "error-text"}>{modelTest.ok ? "Verbindung erfolgreich. " : "Verbindung nicht bereit. "}{modelTest.detail}</span>}</div>
        <fieldset className="count-controls" disabled={running}><legend>Fragenanzahl</legend><label className="radio-label"><input type="radio" name="targets" checked={targetMode === "auto"} onChange={() => setTargetMode("auto")} />Automatisch an das Material anpassen</label><label className="radio-label"><input type="radio" name="targets" checked={targetMode === "manual"} onChange={() => setTargetMode("manual")} />Selbst festlegen</label>{targetMode === "manual" && <div className="form-grid compact"><label>Multiple-Choice-Fragen<input type="number" min={0} max={150} value={targetMc} onChange={event => setTargetMc(Math.max(0, Math.min(150, Math.floor(Number(event.target.value) || 0))))} /></label><label>Offene Fragen<input type="number" min={0} max={150} value={targetOpen} onChange={event => setTargetOpen(Math.max(0, Math.min(150, Math.floor(Number(event.target.value) || 0))))} /></label></div>}</fieldset>
        <label htmlFor="output-path">Ausgabeordner</label><div className="path-row output"><input id="output-path" value={outputPath} disabled={running} onChange={event => setOutputPath(event.target.value)} /><button className="secondary" disabled={running || !window.__TAURI_INTERNALS__} onClick={() => void action("Ausgabe wählen", async () => { const path = await open({ directory: true }); if (typeof path === "string") setOutputPath(path); })}>Ordner wählen</button></div><p className="field-note">Die App legt hier einen getrennten Arbeitsbereich an. Vorhandene Prüfungen und deine Originale bleiben erhalten.</p>
        <section className="preparation-controls" aria-label="Prüfung vorberechnen">
          <div className="section-heading"><div><h3>Vor dem Start</h3><p>{selectedSources.length} ausgewählte Dokumente · {plannedCount} geplante {plannedCount === 1 ? "Prüfung" : "Prüfungen"}</p></div><button className="secondary" disabled={!canGenerate} onClick={() => void startPreparation("preflight")}>{busy === "Material analysieren" ? "Analyse wird gestartet …" : "Material analysieren"}</button></div>
          <p className="field-note">Die Analyse liest deine Auswahl lokal und fragt verfügbare Modellmetadaten ab. Sie startet keine KI-Generierung. Nach Änderungen an Auswahl oder Einstellungen wird die Planung neu berechnet.</p>
          <fieldset className="concurrency-controls" disabled={running}><legend>Ausführung und parallele Verarbeitung</legend><div className="form-grid">
            <label>Wo läuft das Modell?<select value={locationOverride} onChange={event => setLocationOverride(event.target.value)}><option value="auto">Automatisch ermitteln</option><option value="local">Auf diesem Mac (eigene Angabe)</option><option value="cloud">In der Cloud (eigene Angabe)</option><option value="remote">Auf einem anderen Server</option><option value="unknown">Unbekannt</option></select></label>
            <label>Gleichzeitige Prüfungen<select value={examSlots} onChange={event => setExamSlots(event.target.value)}><option value="">Empfehlung verwenden</option><option value="1">1 Prüfung</option><option value="2">2 Prüfungen</option></select></label>
            <label>Gleichzeitige KI-Anfragen<select value={requestSlots} onChange={event => setRequestSlots(event.target.value)}><option value="">Empfehlung verwenden</option>{[1, 2, 3, 4].map(value => <option value={value} key={value}>{value} {value === 1 ? "Anfrage" : "Anfragen"}</option>)}</select></label>
          </div><p className="field-note">Lokal oder bei unbekannter Ausführung startet die Empfehlung vorsichtig mit einer Anfrage. Mehr parallele Anfragen brauchen zusätzlichen Speicher und können langsamer sein. Diese Einstellungen verändern keine Serverkonfiguration.</p></fieldset>
          {prepared ? <PreparationSummary result={prepared} /> : <p className="preparation-pending" role="status">{preparation ? "Die bisherigen Analysewerte gehören zu anderen Einstellungen. Bitte das Material erneut analysieren." : "Dauer, Materialbeschreibung und Modelleignung sind noch nicht ermittelt."}</p>}
          <details className="calibration-controls"><summary>Modell für diese Aufgabe testen</summary><p>Eine ausdrücklich gestartete Stichprobe prüft das gewählte Modell mit künstlichem deutschem Lernmaterial und bekannten Antworten. Deine ausgewählten Dokumente werden dabei nicht verwendet.</p><p className="field-note">Ziel: ein Prüfungsplan, sechs MC- und vier offene Fragen. Die Anfragen gehen an {profile ? `${profile.name} (${profile.base_url})` : "die gewählte KI-Verbindung"}; dabei können Kosten oder lokale Rechenlast entstehen. Höchstens {compareConcurrency ? "7 Testanfragen in 3 Minuten" : "3 Testanfragen in 2 Minuten"}; Verbindungsfehler können innerhalb dieser Grenzen Wiederholungen auslösen. Die Stichprobe kann gestoppt werden.</p><label className="checkbox-label"><input type="checkbox" checked={compareConcurrency} disabled={running} onChange={event => setCompareConcurrency(event.target.checked)} />Zusätzlich Durchsatz mit einer und zwei gleichzeitigen Anfragen vergleichen</label><p className="field-note">Der zusätzliche Vergleich verarbeitet dieselbe Testmenge zweimal und verursacht weitere Anfragen. Er startet nicht automatisch.</p><button className="secondary" disabled={!ready || !profile || !model.trim() || running || Boolean(busy)} onClick={() => void startPreparation("calibration")}>Synthetische Modellstichprobe starten</button>{calibrated && <><p className="field-note">Getestet: {calibrated.execution.model || model} · {executionName(calibrated.execution.location)}</p><SuitabilitySummary result={calibrated.suitability} /><CalibrationSample sample={calibrated.sample} /></>}</details>
        </section>
        <div className="generation-summary"><strong>{mode === "example" ? "Ein Beispiel zum Prüfen" : mode === "all" ? "Prüfungen aus deiner Auswahl" : "Gesamtprüfungen aus deiner Auswahl"}</strong><p>{cloud && profile ? `Die ausgewählten Inhalte werden zur Verarbeitung an ${profile.name} gesendet. Es werden keine Bilder ohne eigene Freigabe hochgeladen.` : "Die Verarbeitung erfolgt über Ollama. Ob ein Modell lokal oder in der Cloud läuft, hängt vom gewählten Modell ab."}</p><p className="muted">Die KI kann Fehler machen. Quellenhinweise und erkannte Lücken bleiben bei der Prüfung sichtbar.</p><button disabled={!canGenerate} onClick={() => void startGeneration()}>{busy === "Prüfung starten" ? "Wird gestartet …" : "Prüfung erstellen"}</button>{!selectedSources.length && <button className="text-button" onClick={() => changeArea("material")}>Material auswählen</button>}</div>
      </>}
      {area === "learn" && <><div className="section-heading"><div><h2>Deine Prüfungen</h2><p className="muted">Öffne einen Arbeitsbereich, um zu üben oder einen gespeicherten Versuch fortzusetzen.</p></div><button className="secondary" disabled={!ready || Boolean(busy)} onClick={() => void action("Prüfungen laden", refreshLibrary)}>Aktualisieren</button></div>{library.length ? <ul className="library-list">{library.map(item => <li key={item.id}><div><h3>{item.name}</h3><p className="muted">{item.exam_count} {item.exam_count === 1 ? "Prüfung" : "Prüfungen"}{item.updated_at ? ` · ${new Date(item.updated_at).toLocaleDateString("de-AT")}` : ""}</p></div><button disabled={Boolean(busy)} onClick={() => void showWorkspace(item.id)}>Prüfungen öffnen</button></li>)}</ul> : <div className="empty-state"><h3>Noch keine Prüfungen vorhanden.</h3><p>Erstelle zuerst eine Beispielprüfung aus deinem Lernmaterial.</p><button onClick={() => changeArea("material")}>Material auswählen</button></div>}</>}
      {area === "settings" && <>
        <div className="section-heading"><div><h2>KI-Verbindungen</h2><p className="muted">Zugangsdaten bleiben im Schlüsselbund. Sie werden nicht in Prüfungen oder Protokolle geschrieben.</p></div></div>
        <div className="profile-layout"><div className="profile-list"><h3>Gespeicherte Verbindungen</h3>{profiles.map(item => <button key={item.id} className={`profile-button ${profileDraft.id === item.id ? "selected" : ""}`} onClick={() => { setProfileDraft({ ...item }); setApiKey(""); }} disabled={running}><strong>{item.name}</strong><span>{PROVIDERS[item.kind]}{item.has_credential ? " · Zugang hinterlegt" : ""}</span></button>)}<button className="secondary" disabled={running} onClick={() => { setProfileDraft(blankProfile); setApiKey(""); }}>Neue Verbindung</button></div>
          <form className="profile-form" onSubmit={event => { event.preventDefault(); void saveProfile(); }}><label>Anbieter<select value={profileDraft.kind} disabled={running || Boolean(profileDraft.id)} onChange={event => switchProvider(event.target.value as Profile["kind"])}>{Object.entries(PROVIDERS).map(([kind, name]) => <option value={kind} key={kind}>{name}</option>)}</select></label><label>Name<input value={profileDraft.name} onChange={event => setProfileDraft(current => ({ ...current, name: event.target.value }))} required maxLength={80} disabled={running} /></label>{profileDraft.kind !== "chatgpt" && <label>Serveradresse<input type="url" value={profileDraft.base_url} onChange={event => setProfileDraft(current => ({ ...current, base_url: event.target.value }))} required placeholder="https://…" disabled={running} /></label>}<label>Standardmodell<input value={profileDraft.model} onChange={event => setProfileDraft(current => ({ ...current, model: event.target.value }))} placeholder="Kann später ausgewählt werden" disabled={running} /></label>{["deepseek", "compatible"].includes(profileDraft.kind) && <label>API-Schlüssel<input type="password" autoComplete="off" value={apiKey} onChange={event => setApiKey(event.target.value)} placeholder={profileDraft.has_credential ? "Gespeicherten Schlüssel beibehalten" : "API-Schlüssel eingeben"} disabled={running} /><span className="field-note">Leer lassen, um einen vorhandenen Schlüssel beizubehalten.</span></label>}
          {profileDraft.kind === "chatgpt" && <div className="oauth-section"><p>Verbinde dein ChatGPT-Konto über die offizielle Anmeldung. Verfügbarkeit und Limits hängen von deinem Konto ab.</p><div className="toolbar"><button className="secondary" type="button" disabled={!ready || Boolean(busy) || oauth.status === "pending" || running} onClick={() => void action("ChatGPT anmelden", async () => { const result = await api<{ url: string }>("/api/oauth/start", {}); setOauth({ status: "pending" }); await openExternal(result.url); })}>{oauth.status === "pending" ? "Anmeldung im Browser läuft …" : "Mit ChatGPT anmelden"}</button><button className="text-button" type="button" disabled={!ready || Boolean(busy) || running} onClick={() => void action("Anmeldung prüfen", async () => { setOauth(await api("/api/oauth/status")); await refreshProfiles(); })}>Status prüfen</button></div><p role="status">{oauth.status === "connected" ? "ChatGPT ist verbunden." : oauth.status === "pending" ? "Schließe die Anmeldung im Browser ab. Diese Ansicht aktualisiert sich automatisch." : oauth.detail || "Noch keine bestätigte Anmeldung."}</p>{oauth.status === "connected" && <button type="button" className="text-button danger-text" disabled={running} onClick={() => void action("ChatGPT trennen", async () => { await api("/api/oauth/disconnect", {}); setOauth({ status: "signed_out" }); await refreshProfiles(); })}>ChatGPT trennen</button>}</div>}
          <div className="toolbar"><button type="submit" disabled={!ready || Boolean(busy) || running}>{busy === "Verbindung speichern" ? "Wird gespeichert …" : "Verbindung speichern"}</button>{profileDraft.id && <button type="button" className="text-button danger-text" disabled={!ready || Boolean(busy) || running} onClick={() => void action("Verbindung entfernen", async () => { await api("/api/profiles/delete", { id: profileDraft.id }); setProfileDraft(blankProfile); setApiKey(""); await refreshProfiles(); })}>Verbindung entfernen</button>}</div></form>
        </div>
        <section className="settings-section"><h3>Bewertung offener Antworten</h3><label>Verbindung für die Bewertung<select value={gradingProfileId} disabled={!ready || running} onChange={event => { const value = event.target.value; void action("Bewertung speichern", async () => { await api("/api/settings", { grading_profile_id: value || null }); setGradingProfileId(value); }); }}><option value="">Dieselbe Verbindung wie bei der Generierung</option>{profiles.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><p className="field-note">Eine abweichende Verbindung wird ausdrücklich gewählt. Die App wechselt bei Fehlern nicht automatisch den Anbieter.</p></section>
        <section className="settings-section"><div className="section-heading"><h3>Dokumente und Texterkennung</h3><button className="secondary small" disabled={!ready || Boolean(busy)} onClick={() => void action("Dokumentwerkzeuge prüfen", async () => { setTools(await api("/api/tools")); })}>Werkzeuge prüfen</button></div><p>{tools ? tools.detail || tools.message || ((tools.ready ?? tools.available) ? "Dokumentwerkzeuge sind verfügbar." : "Nicht alle Dokumentwerkzeuge sind verfügbar. Prüfe die Installationshinweise der App.") : "Der Werkzeugstatus wurde noch nicht geprüft."}</p><p className="muted">Gewöhnliche Scans werden lokal erkannt. Zusätzliche Modelle werden nicht automatisch geladen. Cloud-Erkennung ist kein automatischer Ersatz bei Fehlern.</p></section>
        <details className="settings-section"><summary>Diagnose und lokale Verbindung</summary><label className="checkbox-label"><input type="checkbox" checked={diagnostics} disabled={running} onChange={event => setDiagnostics(event.target.checked)} />Ausführliche Diagnose für den nächsten Lauf speichern</label><p className="field-note">Kann Quellentext und KI-Antworten enthalten. Nur zur Fehlersuche einschalten.</p><button className="secondary" disabled={!ready || Boolean(busy)} onClick={() => void action("App prüfen", async () => { const result = await api<{ checks: Check[] }>("/api/check", { input_path: inputPath || undefined, output_path: outputPath, profile_id: profileId, model }); setChecks(result.checks); })}>App prüfen</button>{checks.length > 0 && <ul className="check-list">{checks.map(check => <li key={check.id}><strong>{check.ok ? "Bereit" : "Prüfen"}: {check.label}</strong><span>{check.detail}</span></li>)}</ul>}{!window.__TAURI_INTERNALS__ && <div className="developer-connection"><p>Browserentwicklung: Verbindung zum bereits gestarteten lokalen Dienst.</p><label>Lokale Adresse<input value={developerUrl} onChange={event => setDeveloperUrl(event.target.value)} /></label><label>Sitzungsschlüssel<input type="password" autoComplete="off" value={developerToken} onChange={event => setDeveloperToken(event.target.value)} /></label><button className="secondary" disabled={connecting} onClick={() => void connect(true)}>Lokal verbinden</button></div>}</details>
      </>}
    </div>
    {job && <section className="job-panel" aria-label="Auftragsfortschritt"><div className="section-heading"><div><h2>{jobTitle(job.kind, job.status)}</h2><p role="status" aria-live="polite">{job.message}</p></div><span className="elapsed">{duration(job.started_at, active(job) ? undefined : job.updated_at)}</span></div><progress aria-label="Fortschritt des Auftrags" value={Math.max(0, Math.min(100, job.progress))} max={100} />{job.workload && <p className="outcomes">{job.workload.completed} von {job.workload.total} Prüfungen fertig · {job.workload.active} aktiv · {job.workload.queued} warten</p>}{active(job) && job.estimate && <p className="field-note">Verbleibend: {estimateLabel(job.estimate)} · {job.estimate.basis}</p>}<div className="job-footer"><span>{Math.round(job.progress)} %</span>{job.status === "running" && <button className="secondary danger-text" disabled={Boolean(busy)} onClick={() => void action("Stoppen", async () => { const result = await api<{ job: Job }>(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, {}); setJob(result.job); })}>{job.kind === "preflight" ? "Analyse stoppen" : job.kind === "calibration" ? "Stichprobe stoppen" : "Generierung stoppen"}</button>}{generationResult && count(generationResult.generated) + count(generationResult.reused) > 0 && <button disabled={Boolean(busy)} onClick={() => void showWorkspace(generationResult.workspace_id)}>Prüfungen öffnen</button>}</div>{job.status === "stopping" && <p className="muted">Die App beendet laufende Arbeit. Bereits vom Anbieter angenommene Anfragen können noch berechnet werden.</p>}{job.error && <p className="error-text" role="alert">{job.error}</p>}{generationResult && <p className="outcomes">{count(generationResult.generated)} erstellt · {count(generationResult.reused)} wiederverwendet · {count(generationResult.skipped)} übersprungen · {count(generationResult.failed)} fehlgeschlagen</p>}{generationResult?.usage_summary && <p className="field-note">Gemeldeter Tokenverbrauch: {descriptive(generationResult.usage_summary.input_tokens)} Eingabe · {descriptive(generationResult.usage_summary.output_tokens)} Ausgabe (davon {descriptive(generationResult.usage_summary.reasoning_tokens)} Denken) · {descriptive(generationResult.usage_summary.cached_input_tokens)} Eingabe aus Anbieter-Cache.{generationResult.usage_summary.incomplete ? " Fehlgeschlagene Anfragen können zusätzliche, nicht gemeldete Kosten verursachen." : " Die Anbieterabrechnung ist maßgeblich."}</p>}<details><summary>Protokoll anzeigen</summary><pre tabIndex={0}>{fullLog || (job.logs || []).slice(-30).join("\n") || "Noch keine Protokolleinträge."}</pre><button className="text-button" disabled={!ready || Boolean(busy)} onClick={() => void action("Protokoll laden", async () => { const response = await fetch(`${session.current.url}/api/jobs/${encodeURIComponent(job.id)}/log`, { headers: { Authorization: `Bearer ${session.current.token}` } }); if (!response.ok) throw new Error("Das Protokoll konnte nicht geladen werden."); setFullLog(await response.text()); })}>Vollständiges Protokoll laden</button></details></section>}
  </main>;
}

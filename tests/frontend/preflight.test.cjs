const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const { createRequire } = require('node:module');
const appRoot = path.resolve(__dirname, '../../apps/desktop');
const appRequire = createRequire(path.join(appRoot, 'package.json'));
const ts = appRequire('typescript');
const React = appRequire('react');
const { renderToStaticMarkup } = appRequire('react-dom/server');
const sourcePath = path.join(appRoot, 'src/App.tsx');
const compiled = ts.transpileModule(fs.readFileSync(sourcePath, 'utf8'), {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const appModule = new Module(sourcePath, module);
appModule.filename = sourcePath;
appModule.paths = Module._nodeModulePaths(path.dirname(sourcePath));
appModule._compile(compiled, sourcePath);
const { PreparationSummary, CalibrationSample, ThinkingControl, plannedExamCount, estimateLabel, jobTitle, applicablePlanningResult } = appModule.exports;

test('DeepSeek thinking defaults to Low and exposes four effective levels', () => {
  const html = renderToStaticMarkup(React.createElement(ThinkingControl, { onChange() {} }));
  assert.match(html, /value="low" selected=""/);
  assert.match(html, /Medium entspricht bei DeepSeek Hoch/);
  for (const level of ['off', 'low', 'high', 'max']) {
    const selected = renderToStaticMarkup(React.createElement(ThinkingControl, { value: level, onChange() {}, disabled: true }));
    assert.match(selected, new RegExp(`value="${level}" selected=""`));
    assert.match(selected, /select disabled=""/);
  }
  assert.doesNotMatch(html, /value="medium"/);
});

test('planned counts follow edited groups and retain archive groups', () => {
  const sources = [{ id: 'old', course: 'M2/Archiv' }, { id: 'new', course: 'M2' }, { id: 'notes', course: 'M2' }];
  assert.equal(plannedExamCount(sources, {}, 'finals'), 2);
  assert.equal(plannedExamCount(sources, { old: 'M2' }, 'finals'), 1);
  assert.equal(plannedExamCount(sources, {}, 'all'), 3);
  assert.equal(plannedExamCount(sources, {}, 'example'), 1);
  assert.equal(plannedExamCount([], {}, 'example'), 0);
});

test('missing timing is uncalibrated; reuse needs no inference; ranges round conservatively', () => {
  assert.equal(estimateLabel(), 'Noch nicht kalibriert');
  assert.equal(estimateLabel({ low_seconds: null, high_seconds: null }), 'Noch nicht kalibriert');
  assert.equal(estimateLabel({ low_seconds: 0, high_seconds: 0 }), 'Keine neue KI-Generierung erforderlich');
  assert.equal(estimateLabel({ low_seconds: 61, high_seconds: 179 }), '2–3 Min.');
});

test('preparation shows source grouping, unavailable metadata and evidence without inventing figures', () => {
  const result = {
    analysis_id: 'test', input_signature: 'selection',
    material: {
      documents: [{ id: 'old', name: 'Lecture.docx', relative_path: 'M2/Archiv/Lecture.docx', course: 'M2/Archiv', words: 100, characters: 700, metadata: { pages: null, slides: null, image_occurrences: 0, size_bytes: 1400 }, warnings: ['Diagramm wurde nicht interpretiert.'] }],
      totals: { documents: 1, words: 100, pages: null, slides: null, image_occurrences: 0, tables: null }, warnings: [],
    },
    counts: { planned: 1, valid: 1, new: 1, reused: 0, unavailable: 0 },
    exams: [{ key: 'archive', course: 'M2/Archiv <script>', sources: [{ id: 'old', name: 'Lecture.docx', relative_path: 'M2/Archiv/Lecture.docx' }], target_mc: 10, target_open: 4, reused: false, available: true, nominal_calls: 3 }],
    execution: { location: 'unknown', basis: 'Servermetadaten nicht verfügbar.', model_identity: 'Unbekanntes Modell', context_length: null },
    recommendation: { exam_slots: 1, request_slots: 1, reasons: ['Noch keine belastbare Messung.'] },
    estimate: { low_seconds: null, high_seconds: null, basis: 'Noch keine Modellmessung.', sample_count: 0 },
    suitability: { status: 'untested', checks: [{ name: 'Kontext', ok: null, detail: 'Nicht bekannt.' }], sample_count: 0 }, warnings: [],
  };
  const html = renderToStaticMarkup(React.createElement(PreparationSummary, { result }));
  assert.match(html, /1 geplante Prüfung/);
  assert.match(html, /M2\/Archiv\/Lecture.docx/);
  assert.match(html, /PDF-Seiten<\/dt><dd>nicht ermittelt/);
  assert.match(html, /Bildvorkommen<\/dt><dd>0/);
  assert.match(html, /Noch nicht kalibriert/);
  assert.match(html, /Modelleignung: Nicht geprüft/);
  assert.match(html, /Kontext nicht ermittelt/);
  assert.match(html, /M2\/Archiv &lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /Diagramm wurde nicht interpretiert/);
});

test('analysis and calibration completion never claim exams were generated', () => {
  assert.equal(jobTitle('preflight', 'done'), 'Materialanalyse bereit');
  assert.equal(jobTitle('calibration', 'done'), 'Modellstichprobe abgeschlossen');
  assert.equal(jobTitle('preflight', 'partial'), 'Materialanalyse mit Hinweisen');
  assert.equal(jobTitle('calibration', 'canceled'), 'Gestoppt');
  assert.equal(jobTitle('finals', 'done'), 'Prüfungen bereit');
});

test('calibration lets users inspect the source and model answers, including malformed output', () => {
  const sample = { source: 'Gruppe A übte dreimal.', mc: { multiple_choice: [{ question: 'Wie oft übte Gruppe A?', explanation: 'Begründung des Modells', options: [{ text: 'Dreimal', is_correct: true }, { text: 'Viermal', is_correct: false }] }, { question: { invalid: true }, explanation: null, options: [null] }] }, open: { open_ended: [{ question: 'Was wurde nicht erhoben?', expected_answer: 'Eine Langzeitwirkung.' }] } };
  const html = renderToStaticMarkup(React.createElement(CalibrationSample, { sample }));
  assert.match(html, /Gruppe A übte dreimal/);
  assert.match(html, /Dreimal<!-- --> \(vom Modell als richtig markiert\)|Dreimal \(vom Modell als richtig markiert\)/);
  assert.match(html, /Markierungen unten zeigen die Antworten des Modells und können falsch sein/);
  assert.match(html, /Eine Langzeitwirkung/);
  assert.match(html, /Nicht verwendbare Angabe/);
});

test('a completed analysis cannot populate a changed selection or a newer job', () => {
  const result = { analysis_id: 'old-analysis' };
  const job = { id: 'old-job', status: 'done', result };
  const pending = { id: 'old-job', kind: 'preflight', signature: 'old-selection-and-model' };
  assert.equal(applicablePlanningResult(job, pending, 'changed-selection-or-model'), null);
  assert.equal(applicablePlanningResult(job, { ...pending, id: 'new-job' }, pending.signature), null);
  assert.equal(applicablePlanningResult({ ...job, status: 'error' }, pending, pending.signature), null);
  assert.equal(applicablePlanningResult(job, pending, pending.signature).result, result);
  assert.equal(applicablePlanningResult({ ...job, result: { workspace_id: 'generated-exams' } }, pending, pending.signature), null);
});

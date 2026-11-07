let exam = null;
let mcChecked = false;
let openGraded = false;
let fingerprint = '';
let saveTimer;
const openResults = new Map();
const $ = (selector) => document.querySelector(selector);
const asArray = (value) => Array.isArray(value) ? value : [];
const isPlainObject = (value) => value && typeof value === 'object' && !Array.isArray(value);
const en = () => exam?.metadata?.language === 'en';
const t = (de, english) => en() ? english : de;
const previewBase = location.pathname.match(/^\/preview\/[^/]+\//)?.[0];
const examPath = previewBase ? location.pathname.slice(previewBase.length).replace(/index\.html$/, 'exam.json') : '';
function escapeHtml(value) {
  return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;');
}
const DEFAULT_GRADING_RUBRIC = { '90-100': 'Precise, complete, source-backed answer.', '60-89': 'Correct core idea with gaps.', '40-59': 'Partially correct.', '0-39': 'Major gaps or errors.' };
function resolveGradingRubric(question) {
  if (isPlainObject(question.grading_rubric)) return question.grading_rubric;
  return exam?.rubric_templates?.[question.rubric_template] || DEFAULT_GRADING_RUBRIC;
}
function notice(message) {
  $('#save-status').textContent = message;
}
async function loadExam() {
  try {
    const response = await fetch('exam.json', {cache: 'no-store'});
    if (!response.ok) throw new Error('Prüfung konnte nicht geladen werden.');
    exam = await response.json();
  } catch (error) {
    const embedded = $('#embedded-exam-data');
    if (!embedded?.textContent?.trim()) throw error;
    exam = JSON.parse(embedded.textContent);
  }
  renderExam();
  bindEvents();
  try {
    let attempt;
    if (previewBase) {
      const response = await fetch(`${previewBase}attempt?exam_path=${encodeURIComponent(examPath)}`);
      const saved = await response.json();
      if (!response.ok) throw new Error(saved.error);
      fingerprint = saved.fingerprint;
      attempt = saved.attempt;
    } else {
      const data = new TextEncoder().encode(JSON.stringify(exam));
      fingerprint = [...new Uint8Array(await crypto.subtle.digest('SHA-256', data))].map(n => n.toString(16).padStart(2, '0')).join('');
      attempt = JSON.parse(localStorage.getItem(`exam-attempt:${fingerprint}`) || 'null');
    }
    if (isPlainObject(attempt)) restoreAttempt(attempt);
    notice(t('Lernstand wird lokal gespeichert.', 'Your progress is saved locally.'));
  } catch {
    notice(t('Lernstand konnte nicht geladen werden. Diese Sitzung bleibt vorerst nur im Fenster.', 'Progress could not be loaded. This session currently stays in this window.'));
  }
  updateProgress(false);
}
function sourceDetails(question) {
  const meta = question._meta || {};
  const refs = asArray(meta.source_refs);
  const pages = asArray(meta.evidence_pages);
  const reference = refs.length ? refs.map(ref => `${ref.source_name || ''} · ${ref.locator?.kind || ''} ${ref.locator?.index ?? ''}`).join(', ') : pages.length ? `${t('Seite', 'Page')} ${pages.join(', ')}` : t('Keine genaue Quellenstelle bestätigt.', 'No exact source reference confirmed.');
  return `<details class="source-details"><summary>${t('Quelle prüfen', 'Review source')}</summary><p>${escapeHtml(reference)}</p>${meta.source_excerpt ? `<blockquote>${escapeHtml(meta.source_excerpt)}</blockquote>` : ''}</details>`;
}
function renderExam() {
  const metadata = exam.metadata || {};
  document.documentElement.lang = en() ? 'en' : 'de';
  document.title = metadata.title || metadata.source_file || metadata.source_pdf || 'Prüfung';
  $('#exam-title').textContent = document.title;
  $('#exam-meta').textContent = `${metadata.course || ''} · ${metadata.source_file || metadata.source_pdf || ''}`;
  $('#grading-info').textContent = t('KI-Feedback ist optional. Verwendet wird die beim Erstellen gewählte Verbindung; eine abweichende Verbindung lässt sich in der App einstellen.', 'AI feedback is optional. It uses the connection selected for this exam unless overridden in the app.');
  $('#mc-heading').textContent = t('Multiple-Choice-Fragen', 'Multiple-choice questions');
  $('#mc-hint').textContent = t('Wähle alle richtigen Antworten. Falls keine passt, wähle „Keine Antwort ist richtig“.', 'Select every correct answer. If none is correct, select “None of these”.');
  $('#open-heading').textContent = t('Offene Fragen', 'Open questions');
  $('#open-hint').textContent = t('Formuliere deine Antwort. Du kannst sie selbst prüfen oder KI-Feedback anfordern.', 'Write your answer. Review it yourself or request AI feedback.');
  $('#check-mc').textContent = t('Antworten prüfen', 'Check answers');
  $('#grade-open').textContent = t('KI-Feedback anfordern', 'Request AI feedback');
  $('#skip-open').textContent = t('Ohne KI selbst prüfen', 'Review without AI');
  const warning = metadata.text_extraction_warning || metadata.coverage_warning;
  if (warning) { $('#warning-box').hidden = false; $('#warning-box').textContent = warning; }
  renderMcQuestions(); renderOpenQuestions();
}
function renderMcQuestions() {
  $('#mc-form').innerHTML = asArray(exam.multiple_choice).map((question, index) => `
    <article class="question-card" data-mc-question="${index}">
      <p class="question-meta">${t('Frage', 'Question')} ${index + 1} · ${escapeHtml(question.topic || '')}</p>
      <fieldset><legend>${escapeHtml(question.question)}</legend>
      <ul class="option-list">${asArray(question.options).map((option, optionIndex) => `
        <li class="option" data-question-index="${index}" data-option-index="${optionIndex}">
          <input id="mc-${index}-${optionIndex}" name="mc-${index}" type="checkbox" value="${optionIndex}">
          <label for="mc-${index}-${optionIndex}">${escapeHtml(option.text)}</label></li>`).join('')}
        <li class="option"><input id="none-${index}" name="none-${index}" type="checkbox"><label for="none-${index}">${t('Keine Antwort ist richtig', 'None of these')}</label></li></ul></fieldset>
      <div class="correction" id="mc-correction-${index}" hidden></div>${sourceDetails(question)}</article>`).join('');
}
function renderOpenQuestions() {
  $('#open-form').innerHTML = asArray(exam.open_ended).map((question, index) => `
    <article class="question-card" data-open-question="${index}"><label for="open-${index}"><span class="question-meta">${t('Frage', 'Question')} ${index + 1}</span><span class="question-prompt">${escapeHtml(question.question)}</span></label>
    <textarea id="open-${index}" placeholder="${t('Deine Antwort', 'Your answer')}"></textarea>
    <details><summary>${t('Musterantwort anzeigen', 'Show expected answer')}</summary><p>${escapeHtml(question.expected_answer)}</p></details>
    <div class="grading-result" id="open-result-${index}" hidden></div>${sourceDetails(question)}</article>`).join('');
}
function selectedIndexes(index) {
  return [...document.querySelectorAll(`input[name="mc-${index}"]:checked`)].map(input => Number(input.value));
}
function scoreMcQuestion(question, selected, noneSelected = false) {
  const correctIndexes = asArray(question.options).map((option, index) => option.is_correct === true ? index : null).filter(value => value !== null);
  const selectedSet = new Set(selected);
  const correctSet = new Set(correctIndexes);
  const correctSelections = selected.filter(index => correctSet.has(index)).length;
  const wrongSelections = selected.filter(index => !correctSet.has(index)).length;
  const missedCorrect = correctIndexes.filter(index => !selectedSet.has(index)).length;
  const score = correctIndexes.length ? (noneSelected ? 0 : Math.max(0, correctSelections - wrongSelections) / correctIndexes.length) : (noneSelected && selected.length === 0 ? 1 : 0);
  return {score, correctSelections, wrongSelections, missedCorrect, correctIndexes};
}
function checkMcAnswers() {
  const questions = asArray(exam.multiple_choice);
  let earned = 0;
  questions.forEach((question, index) => {
    const selected = selectedIndexes(index);
    const result = scoreMcQuestion(question, selected, $(`#none-${index}`).checked);
    earned += result.score;
    asArray(question.options).forEach((option, optionIndex) => {
      const element = document.querySelector(`.option[data-question-index="${index}"][data-option-index="${optionIndex}"]`);
      element.classList.toggle('correct-selected', selected.includes(optionIndex) && option.is_correct === true);
      element.classList.toggle('wrong-selected', selected.includes(optionIndex) && option.is_correct !== true);
      element.classList.toggle('missed-correct', !selected.includes(optionIndex) && option.is_correct === true);
    });
    const correction = $(`#mc-correction-${index}`);
    correction.hidden = false;
    correction.innerHTML = `<strong>${Math.round(result.score * 100)}%</strong><p>${escapeHtml(question.explanation || '')}</p>`;
  });
  mcChecked = true;
  $('#mc-summary').hidden = false;
  $('#mc-summary').textContent = `${t('Ergebnis', 'Score')}: ${earned.toFixed(2)} / ${questions.length}`;
  updateScoreSummary(); scheduleSave();
}
function renderList(items) {
  return asArray(items).length ? `<ul>${items.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul>` : '';
}
function renderOpenResult(index, result) {
  const container = $(`#open-result-${index}`);
  container.hidden = false;
  if (result.error) { container.textContent = result.error; return; }
  container.innerHTML = `<strong>${escapeHtml(result.score)} / 100 · ${escapeHtml(result.verdict)}</strong><p>${escapeHtml(result.feedback)}</p>${renderList(result.what_was_good)}${renderList(result.missing_key_points)}${renderList(result.conceptual_errors)}<details><summary>${t('Antwortvorschlag', 'Suggested answer')}</summary><p>${escapeHtml(result.model_answer || '')}</p></details>`;
}
async function callGrader(question, answer) {
  const body = previewBase ? {exam_path: examPath, question_id: question.id, student_answer: answer} : {question: question.question, student_answer: answer, expected_answer: question.expected_answer, key_concepts: asArray(question.key_concepts), grading_rubric: resolveGradingRubric(question), max_score: 100};
  const response = await fetch(previewBase ? `${previewBase}grade-open-answer` : '/grade-open-answer', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.error) throw new Error(data.error || t('Bewertung nicht erreichbar. Öffne die Prüfung über die App.', 'Grading unavailable. Open this exam through the app.'));
  return data;
}
async function gradeOpenAnswers() {
  const button = $('#grade-open'); button.disabled = true;
  button.textContent = t('Wird bewertet …', 'Grading …');
  try {
    for (const [index, question] of asArray(exam.open_ended).entries()) {
      const answer = $(`#open-${index}`).value.trim();
      if (!answer) continue;
      const saved = openResults.get(index);
      if (saved?.answer === answer && typeof saved.score === 'number') continue;
      try {
        const result = {...await callGrader(question, answer), answer};
        // A result belongs to the submitted answer, even if typing continued meanwhile.
        if ($(`#open-${index}`).value.trim() !== answer) continue;
        openResults.set(index, result); renderOpenResult(index, result); scheduleSave();
      } catch (error) {
        openResults.set(index, {answer, error: error.message}); renderOpenResult(index, openResults.get(index));
      }
    }
    openGraded = true; updateScoreSummary(); scheduleSave();
  } finally { button.disabled = false; button.textContent = t('Fehlende Bewertungen anfordern', 'Request remaining feedback'); }
}
function updateScoreSummary() {
  const graded = [...openResults.values()].filter(result => typeof result.score === 'number');
  $('#open-summary').hidden = !openGraded;
  $('#open-summary').textContent = `${t('Bewertete Antworten', 'Graded answers')}: ${graded.length} / ${asArray(exam.open_ended).length}${graded.length ? ` · Ø ${Math.round(graded.reduce((sum, result) => sum + result.score, 0) / graded.length)} / 100` : ''}`;
  $('#score-summary').hidden = !mcChecked && !openGraded;
  $('#score-summary').textContent = t('KI-Feedback dient der Übung und kann Fehler enthalten. Prüfe die Quellen bei Unklarheiten.', 'AI feedback is for practice and can contain errors. Review the sources when in doubt.');
}
function updateProgress(save = true) {
  if (!exam) return;
  const mc = asArray(exam.multiple_choice), open = asArray(exam.open_ended);
  const answered = mc.filter((_, index) => selectedIndexes(index).length || $(`#none-${index}`).checked).length + open.filter((_, index) => $(`#open-${index}`).value.trim()).length;
  $('#progress-count').textContent = `${answered} / ${mc.length + open.length} ${t('beantwortet', 'answered')}`;
  $('#progress-bar').value = mc.length + open.length ? Math.round(answered / (mc.length + open.length) * 100) : 0;
  if (save) scheduleSave();
}
function bindEvents() {
  document.addEventListener('change', event => {
    const match = event.target.id?.match(/^(?:mc|none)-(\d+)/);
    if (match) {
      const index = Number(match[1]);
      if (event.target.id.startsWith('none-') && event.target.checked) document.querySelectorAll(`input[name="mc-${index}"]`).forEach(input => { input.checked = false; });
      else if (event.target.checked) $(`#none-${index}`).checked = false;
      mcChecked = false; $('#mc-summary').hidden = true; $(`#mc-correction-${index}`).hidden = true;
      document.querySelectorAll(`.option[data-question-index="${index}"]`).forEach(item => item.classList.remove('correct-selected', 'wrong-selected', 'missed-correct'));
    }
    updateProgress(); updateScoreSummary();
  });
  document.addEventListener('input', event => {
    const match = event.target.id?.match(/^open-(\d+)$/);
    if (match) { const index = Number(match[1]); openResults.delete(index); $(`#open-result-${index}`).hidden = true; }
    updateProgress(); updateScoreSummary();
  });
  $('#check-mc').addEventListener('click', checkMcAnswers);
  $('#grade-open').addEventListener('click', gradeOpenAnswers);
  $('#skip-open').addEventListener('click', () => { document.querySelectorAll('#open-form details').forEach(details => { details.open = true; }); });
}
function attemptData() {
  return {mc: asArray(exam.multiple_choice).map((_, index) => ({selected: selectedIndexes(index), none: $(`#none-${index}`).checked})), open: asArray(exam.open_ended).map((_, index) => $(`#open-${index}`).value), results: [...openResults], mcChecked, openGraded};
}
function restoreAttempt(attempt) {
  asArray(attempt.mc).forEach((answer, index) => { asArray(answer.selected).forEach(option => { const input = $(`#mc-${index}-${option}`); if (input) input.checked = true; }); if ($(`#none-${index}`)) $(`#none-${index}`).checked = answer.none === true; });
  asArray(attempt.open).forEach((answer, index) => { if ($(`#open-${index}`)) $(`#open-${index}`).value = String(answer); });
  asArray(attempt.results).forEach(([index, result]) => { if ($(`#open-${index}`)?.value.trim() === result.answer) { openResults.set(index, result); renderOpenResult(index, result); } });
  openGraded = attempt.openGraded === true;
  if (attempt.mcChecked) checkMcAnswers();
  updateScoreSummary();
}
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveAttempt, 400);
}
async function saveAttempt() {
  if (!fingerprint) return;
  try {
    const attempt = attemptData();
    if (previewBase) {
      const response = await fetch(`${previewBase}attempt`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({exam_path: examPath, fingerprint, attempt}), keepalive: true});
      if (!response.ok) throw new Error();
    } else localStorage.setItem(`exam-attempt:${fingerprint}`, JSON.stringify(attempt));
    notice(t('Lernstand gespeichert.', 'Progress saved.'));
  } catch { notice(t('Speichern fehlgeschlagen. Lass das Fenster geöffnet und versuche es erneut.', 'Saving failed. Keep this window open and try again.')); }
}
window.addEventListener('pagehide', () => { if (fingerprint) { clearTimeout(saveTimer); void saveAttempt(); } });
loadExam().catch(error => { document.body.innerHTML = `<main class="exam-shell"><p role="alert">${escapeHtml(error.message)}</p></main>`; });

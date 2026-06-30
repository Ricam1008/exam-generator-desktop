let exam = null;
let mcChecked = false;
let openGraded = false;
const openResults = new Map();
const DEFAULT_GRADING_RUBRIC = {
  "90-100": "Precise, complete answer covering all key concepts.",
  "61-89": "Mostly correct answer with minor to noticeable gaps.",
  "41-60": "On topic but incomplete or imprecise.",
  "21-40": "Partially relevant with major conceptual gaps.",
  "0-20": "Mostly wrong, vague, or empty."
};

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function isPlainObject(value) {
  return value && typeof value === "object" && !Array.isArray(value);
}

function resolveGradingRubric(question) {
  if (isPlainObject(question.grading_rubric) && Object.keys(question.grading_rubric).length) {
    return question.grading_rubric;
  }
  const templates = isPlainObject(exam?.rubric_templates) ? exam.rubric_templates : {};
  const template = templates[question.rubric_template];
  if (isPlainObject(template) && Object.keys(template).length) {
    return template;
  }
  return DEFAULT_GRADING_RUBRIC;
}

async function loadExam() {
  try {
    const response = await fetch("exam.json", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Could not load exam.json (${response.status})`);
    }
    exam = await response.json();
  } catch (error) {
    const embedded = document.getElementById("embedded-exam-data");
    if (!embedded?.textContent?.trim()) {
      throw error;
    }
    exam = JSON.parse(embedded.textContent);
  }
  renderExam();
  bindEvents();
  updateProgress();
}

function renderExam() {
  const metadata = exam.metadata || {};
  const title = metadata.title || metadata.source_pdf || "Local exam";
  document.title = title;
  $("#exam-title").textContent = title;
  $("#exam-meta").textContent = `${metadata.course || "Unknown course"} · Source: ${metadata.source_pdf || "unknown PDF"}`;

  if (metadata.text_extraction_warning) {
    const warningBox = $("#warning-box");
    warningBox.hidden = false;
    warningBox.textContent = metadata.text_extraction_warning;
  }

  renderMcQuestions();
  renderOpenQuestions();
}

function renderMcQuestions() {
  const form = $("#mc-form");
  form.innerHTML = asArray(exam.multiple_choice).map((question, questionIndex) => {
    const options = asArray(question.options).map((option, optionIndex) => `
      <li class="option" data-question-index="${questionIndex}" data-option-index="${optionIndex}">
        <input id="mc-${questionIndex}-${optionIndex}" name="mc-${questionIndex}" type="checkbox" value="${optionIndex}">
        <label for="mc-${questionIndex}-${optionIndex}">${escapeHtml(option.text)}</label>
      </li>
    `).join("");

    return `
      <article class="question-card" data-mc-question="${questionIndex}">
        <p class="question-meta">Question ${questionIndex + 1} · ${escapeHtml(question.topic || "lecture concept")}</p>
        <h3>${escapeHtml(question.question)}</h3>
        <ul class="option-list">${options}</ul>
        <div class="correction" id="mc-correction-${questionIndex}" hidden></div>
      </article>
    `;
  }).join("");
}

function renderOpenQuestions() {
  const form = $("#open-form");
  form.innerHTML = asArray(exam.open_ended).map((question, questionIndex) => `
    <article class="question-card" data-open-question="${questionIndex}">
      <p class="question-meta">Question ${questionIndex + 1} · max ${Number(question.max_score || 100)} points</p>
      <h3>${escapeHtml(question.question)}</h3>
      <textarea id="open-${questionIndex}" placeholder="Type your answer here"></textarea>
      <div class="grading-result" id="open-result-${questionIndex}" hidden></div>
    </article>
  `).join("");
}

function bindEvents() {
  document.addEventListener("change", updateProgress);
  document.addEventListener("input", updateProgress);
  $("#check-mc").addEventListener("click", checkMcAnswers);
  $("#grade-open").addEventListener("click", gradeOpenAnswers);
  $("#skip-open").addEventListener("click", () => {
    openGraded = true;
    const summary = $("#open-summary");
    summary.hidden = false;
    summary.innerHTML = "<strong>Open-answer grading skipped.</strong><p>You can still review your written answers against the stored expected answers later.</p>";
    updateScoreSummary();
  });
}

function selectedIndexes(questionIndex) {
  return [...document.querySelectorAll(`input[name="mc-${questionIndex}"]:checked`)].map((input) => Number(input.value));
}

function scoreMcQuestion(question, selected) {
  const options = asArray(question.options);
  const correctIndexes = options.map((option, index) => option.is_correct ? index : null).filter((value) => value !== null);
  const selectedSet = new Set(selected);
  const correctSet = new Set(correctIndexes);
  const correctSelections = selected.filter((index) => correctSet.has(index)).length;
  const wrongSelections = selected.filter((index) => !correctSet.has(index)).length;
  const missedCorrect = correctIndexes.filter((index) => !selectedSet.has(index)).length;
  const raw = correctSelections - wrongSelections;
  const denominator = Math.max(correctIndexes.length, 1);
  const score = Math.max(0, raw) / denominator;
  return { score, correctSelections, wrongSelections, missedCorrect, correctIndexes };
}

function checkMcAnswers() {
  const questions = asArray(exam.multiple_choice);
  let earned = 0;

  questions.forEach((question, questionIndex) => {
    const selected = selectedIndexes(questionIndex);
    const result = scoreMcQuestion(question, selected);
    earned += result.score;

    asArray(question.options).forEach((option, optionIndex) => {
      const optionElement = document.querySelector(`.option[data-question-index="${questionIndex}"][data-option-index="${optionIndex}"]`);
      optionElement.classList.remove("correct-selected", "wrong-selected", "missed-correct");
      if (selected.includes(optionIndex) && option.is_correct) optionElement.classList.add("correct-selected");
      if (selected.includes(optionIndex) && !option.is_correct) optionElement.classList.add("wrong-selected");
      if (!selected.includes(optionIndex) && option.is_correct) optionElement.classList.add("missed-correct");
    });

    const correction = $(`#mc-correction-${questionIndex}`);
    correction.hidden = false;
    correction.innerHTML = `
      <strong>Score: ${Math.round(result.score * 100)}%</strong>
      <div class="tag-list">
        <span class="tag good">${result.correctSelections} selected correct</span>
        <span class="tag bad">${result.wrongSelections} selected wrong</span>
        <span class="tag missed">${result.missedCorrect} missed correct</span>
      </div>
      <p>${escapeHtml(question.explanation || "No explanation stored.")}</p>
    `;
  });

  mcChecked = true;
  const percent = questions.length ? Math.round((earned / questions.length) * 100) : 0;
  const summary = $("#mc-summary");
  summary.hidden = false;
  summary.innerHTML = `<strong>Multiple-choice score: ${earned.toFixed(2)} / ${questions.length} (${percent}%)</strong>`;
  updateScoreSummary();
}

async function callGrader(question, answer) {
  const response = await fetch("/grade-open-answer", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question: question.question,
      student_answer: answer,
      expected_answer: question.expected_answer,
      key_concepts: asArray(question.key_concepts),
      grading_rubric: resolveGradingRubric(question),
      max_score: Number(question.max_score || 100)
    })
  });

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `Grading server returned ${response.status}`);
  }
  if (data.error) {
    throw new Error(data.error);
  }
  return data;
}

function renderList(items, emptyText) {
  const values = asArray(items).filter(Boolean);
  if (!values.length) return `<p>${escapeHtml(emptyText)}</p>`;
  return `<ul>${values.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function renderOpenResult(questionIndex, result) {
  const container = $(`#open-result-${questionIndex}`);
  container.hidden = false;

  if (result.error) {
    container.innerHTML = `
      <strong class="bad">LLM grading unavailable.</strong>
      <p>${escapeHtml(result.error)}</p>
    `;
    return;
  }

  const minorOrVagueIssues = asArray(result.minor_or_unimportant_issues).length
    ? result.minor_or_unimportant_issues
    : result.unsupported_or_vague_parts;

  container.innerHTML = `
    <strong>Punkte: ${Number(result.score || 0)} / ${Number(result.max_score || 100)} · ${escapeHtml(result.grade_band || "ohne Einstufung")}</strong>
    <p>${escapeHtml(result.verdict || "")}</p>
    <h4>Was gut war</h4>
    ${renderList(result.what_was_good, "Keine besonderen Stärken gemeldet.")}
    <h4>Was wirklich fehlt</h4>
    ${renderList(result.missing_key_points, "Keine zentralen fehlenden Punkte gemeldet.")}
    <h4>Fachliche Fehler</h4>
    ${renderList(result.conceptual_errors, "Keine fachlichen Fehler gemeldet.")}
    <h4>Unklare oder vage Stellen</h4>
    ${renderList(minorOrVagueIssues, "Keine unklaren oder nur nebensächlichen Probleme gemeldet.")}
    <h4>Feedback</h4>
    <p>${escapeHtml(result.feedback || "")}</p>
    <h4>Bessere Prüfungsantwort</h4>
    <p>${escapeHtml(result.model_answer || "")}</p>
  `;
}

async function gradeOpenAnswers() {
  const button = $("#grade-open");
  button.disabled = true;
  button.textContent = "Grading...";
  const questions = asArray(exam.open_ended);

  for (let index = 0; index < questions.length; index += 1) {
    const question = questions[index];
    const answer = $(`#open-${index}`).value.trim();
    if (!answer) {
      const result = { score: 0, max_score: 100, grade_band: "0-19", verdict: "Keine Antwort abgegeben.", what_was_good: [], missing_key_points: asArray(question.key_concepts), conceptual_errors: [], minor_or_unimportant_issues: [], feedback: "Schreib zuerst eine Antwort, damit du sinnvolles Feedback bekommen kannst.", model_answer: question.expected_answer || "" };
      openResults.set(index, result);
      renderOpenResult(index, result);
      continue;
    }

    try {
      const result = await callGrader(question, answer);
      openResults.set(index, result);
      renderOpenResult(index, result);
    } catch (error) {
      const result = { error: `${error.message}. You can continue without LLM grading.`, score: null };
      openResults.set(index, result);
      renderOpenResult(index, result);
      break;
    }
  }

  openGraded = true;
  button.disabled = false;
  button.textContent = "Grade open answers with LLM";
  const summary = $("#open-summary");
  summary.hidden = false;
  const graded = [...openResults.values()].filter((result) => typeof result.score === "number");
  const average = graded.length ? Math.round(graded.reduce((sum, result) => sum + Number(result.score || 0), 0) / graded.length) : 0;
  summary.innerHTML = `<strong>Open-answer grading: ${graded.length} graded · average ${average} / 100</strong>`;
  updateScoreSummary();
}

function updateProgress() {
  if (!exam) return;
  const mcTotal = asArray(exam.multiple_choice).length;
  const openTotal = asArray(exam.open_ended).length;
  const answeredMc = asArray(exam.multiple_choice).filter((_, index) => selectedIndexes(index).length > 0).length;
  const answeredOpen = asArray(exam.open_ended).filter((_, index) => $(`#open-${index}`)?.value.trim()).length;
  const total = mcTotal + openTotal;
  const answered = answeredMc + answeredOpen;
  $("#progress-count").textContent = `${answered} / ${total} answered`;
  $("#progress-bar").value = total ? Math.round((answered / total) * 100) : 0;
}

function updateScoreSummary() {
  if (!mcChecked && !openGraded) return;
  const container = $("#score-summary");
  container.hidden = false;
  const parts = [];
  if (mcChecked) {
    const questions = asArray(exam.multiple_choice);
    const earned = questions.reduce((sum, question, index) => sum + scoreMcQuestion(question, selectedIndexes(index)).score, 0);
    parts.push(`MC: ${earned.toFixed(2)} / ${questions.length}`);
  }
  if (openGraded) {
    const graded = [...openResults.values()].filter((result) => typeof result.score === "number");
    if (graded.length) {
      const avg = Math.round(graded.reduce((sum, result) => sum + Number(result.score || 0), 0) / graded.length);
      parts.push(`Open average: ${avg} / 100`);
    } else {
      parts.push("Open grading skipped or unavailable");
    }
  }
  container.innerHTML = `<strong>Score summary</strong><p>${escapeHtml(parts.join(" · "))}</p>`;
}

loadExam().catch((error) => {
  document.body.innerHTML = `<main class="exam-shell"><section class="warning"><strong>Could not load exam.</strong><p>${escapeHtml(error.message)}</p></section></main>`;
});

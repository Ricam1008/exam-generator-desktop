const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../../backend/exam_backend/templates/app.js'), 'utf8');
const scoreSource = source.slice(source.indexOf('function scoreMcQuestion('), source.indexOf('\nfunction checkMcAnswers('));
const context = vm.createContext({});
vm.runInContext('const asArray = x => Array.isArray(x) ? x : [];\n' + scoreSource, context);
const score = (correct, selected, none = false) => context.scoreMcQuestion({ options: correct.map(is_correct => ({ is_correct })) }, selected, none).score;
test('a submitted none-correct answer earns credit; untouched questions do not', () => {
  assert.equal(score([false,false,false,false], [], true), 1);
  assert.equal(score([false,false,false,false], [], false), 0);
  assert.equal(score([false,false,false,false], [0], true), 0);
});
test('ordinary questions retain partial credit and wrong-answer penalties', () => {
  assert.equal(score([true,true,false,false], [0]), .5);
  assert.equal(score([true,true,false,false], [0,1]), 1);
  assert.equal(score([true,true,false,false], [0,2]), 0);
  assert.equal(score([true,true,false,false], [], true), 0);
});

test('source review renders every verified passage with localized locators and escaping', () => {
  const sourceContext = vm.createContext({});
  const fn = source.slice(source.indexOf('function sourceDetails('), source.indexOf('\n}', source.indexOf('function sourceDetails(')) + 2);
  vm.runInContext(`const asArray = x => Array.isArray(x) ? x : [];
    const t = (de, en) => de;
    const escapeHtml = x => String(x).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');\n` + fn, sourceContext);
  const html = sourceContext.sourceDetails({_meta: {source_refs: [
    {source_name:'Skript.docx',locator:{kind:'section',index:12},quote:'Erster Beleg <script>'},
    {source_name:'Tabelle.pdf',locator:{kind:'page',index:5},quote:'Zweiter Beleg'}
  ]}});
  assert.match(html, /Skript.docx · Abschnitt 12/);
  assert.match(html, /Tabelle.pdf · Seite 5/);
  assert.match(html, /Erster Beleg &lt;script&gt;/);
  assert.match(html, /Zweiter Beleg/);
  assert.equal((html.match(/<blockquote>/g) || []).length, 2);
  assert.match(sourceContext.sourceDetails({_meta:{}}), /Keine genaue Quellenstelle bestätigt/);
});

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

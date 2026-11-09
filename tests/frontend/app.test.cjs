const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const appRoot = path.resolve(__dirname, '../../apps/desktop');
const { createRequire } = require('node:module');
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
const App = appModule.exports.default;
function render(value) {
  global.window = {};
  global.localStorage = { getItem: () => value, setItem: () => {} };
  try { return renderToStaticMarkup(React.createElement(App)); }
  finally { delete global.window; delete global.localStorage; }
}
test('corrupted or outdated preferences do not prevent opening the app', () => {
  for (const value of ['null', '[]', '1', '"text"', '{broken', '{"inputPath":false,"language":42,"sourceIds":[null,1]}']) {
    const html = render(value);
    assert.match(html, /Material auswählen/);
    assert.match(html, /lang="de"/);
    assert.doesNotMatch(html, /No models installed|Keine Modelle installiert/);
  }
});
test('all four keyboard tabs reference a real, labelled panel', () => {
  const html = render('{}');
  for (const area of ['material', 'create', 'learn', 'settings']) {
    assert.ok(html.includes(`aria-controls="panel-${area}"`));
    assert.ok(html.includes(`id="panel-${area}"`));
    assert.ok(html.includes(`aria-labelledby="tab-${area}"`));
  }
  assert.match(html, /Lokaler Dienst nicht verbunden/);
  assert.match(html, /Material prüfen/);
});

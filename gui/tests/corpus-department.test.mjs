import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { readFile } from "node:fs/promises";
const source = await readFile(new URL("../public/console/app.js", import.meta.url), "utf8");
const block = source.slice(source.indexOf("  function renderCorpusDepartmentOptions("), source.indexOf("  function updateCorpusChatTarget("));
function harness(departments) {
  const select = {value: "", disabled: true, innerHTML: ""};
  const context = {
    state: {departments}, $: () => select, escapeHtml: String,
    renderIndexIssueDepartments() {}, updateCorpusChatTarget() {},
  };
  const render = vm.runInNewContext(`${block}\nrenderCorpusDepartmentOptions`, context);
  return {select, render};
}
test("Cloud registry departments are selectable in the corpus search test", () => {
  const h = harness([
    {code: "cs", name: "컴공", cloudOnly: true, unifiedMcp: true},
    {code: "ee", name: "전자", cloudOnly: true, unifiedMcp: true},
  ]);
  h.render();
  assert.equal(h.select.disabled, false);
  assert.match(h.select.innerHTML, /value="cs"/);
  assert.match(h.select.innerHTML, /value="ee"/);
});
test("a single registered department is preselected", () => {
  const h = harness([{code: "cs", name: "컴공", cloudOnly: true}]);
  h.render();
  assert.equal(h.select.value, "cs");
});
test("the selector stays disabled only when no department is registered", () => {
  const h = harness([]);
  h.render();
  assert.equal(h.select.disabled, true);
});

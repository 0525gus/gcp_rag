import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../public/console/app.js", import.meta.url), "utf8");
const html = await readFile(new URL("../public/console/index.html", import.meta.url), "utf8");
const block = source.slice(source.indexOf("  function renderSummary()"), source.indexOf("  function renderDepartments()"));

function harness(statuses) {
  const select = { value: "ALL" };
  const counts = {};
  const cards = ["OK", "WARN", "FAIL", "CHECKING"].map((filter) => {
    const attrs = {};
    return { dataset: { summaryFilter: filter }, attrs, setAttribute: (k, v) => { attrs[k] = v; },
      closest: (selector) => (selector === "[data-summary-filter]" ? cards.find((c) => c.dataset.summaryFilter === filter) : null) };
  });
  const $ = (selector) => {
    if (selector === "#statusFilter") return select;
    if (selector === ".checking-card") return { classList: { toggle() {} } };
    return (counts[selector] ||= { textContent: "" });
  };
  let renders = 0;
  const context = {
    $, $$: () => cards,
    state: { departments: statuses.map((s, i) => ({ code: `d${i}`, s })) },
    effectiveStatus: (dept) => dept.s,
  };
  const ui = vm.runInNewContext(`${block}\n({ renderSummary, toggleSummaryFilter })`, context);
  context.renderDepartments = () => { renders += 1; ui.renderSummary(); };
  const click = (filter) => ui.toggleSummaryFilter({ target: cards.find((c) => c.dataset.summaryFilter === filter) });
  const pressed = () => cards.filter((c) => c.attrs["aria-pressed"] === "true").map((c) => c.dataset.summaryFilter);
  return { ui, select, counts, click, pressed, renders: () => renders };
}

test("summary cards are buttons whose filters exist in the status select", () => {
  const filters = [...html.matchAll(/data-summary-filter="([A-Z]+)"/g)].map((m) => m[1]);
  assert.deepEqual(filters, ["OK", "WARN", "FAIL", "CHECKING"]);
  for (const filter of filters) assert.match(html, new RegExp(`<option value="${filter}">`));
  assert.equal((html.match(/<button type="button" class="summary-card/g) || []).length, 4);
});

test("clicking a summary card filters, clicking it again clears", () => {
  const h = harness(["OK", "WARN", "FAIL", "OK"]);
  h.ui.renderSummary();
  assert.equal(h.counts["#okCount"].textContent, 2);
  assert.deepEqual(h.pressed(), []);
  h.click("WARN");
  assert.equal(h.select.value, "WARN");
  assert.deepEqual(h.pressed(), ["WARN"]);
  h.click("FAIL");
  assert.equal(h.select.value, "FAIL");
  assert.deepEqual(h.pressed(), ["FAIL"]);
  h.click("FAIL");
  assert.equal(h.select.value, "ALL");
  assert.deepEqual(h.pressed(), []);
  assert.equal(h.renders(), 3);
});

test("choosing a filter in the select reflects on the matching card only", () => {
  const h = harness(["OK"]);
  h.select.value = "ATTENTION";
  h.ui.renderSummary();
  assert.deepEqual(h.pressed(), []);
  h.select.value = "OK";
  h.ui.renderSummary();
  assert.deepEqual(h.pressed(), ["OK"]);
});

test("clicks outside a summary card are ignored", () => {
  const h = harness(["OK"]);
  h.ui.toggleSummaryFilter({ target: { closest: () => null } });
  assert.equal(h.select.value, "ALL");
  assert.equal(h.renders(), 0);
});

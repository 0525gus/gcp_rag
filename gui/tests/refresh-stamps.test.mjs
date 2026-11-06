import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../public/console/app.js", import.meta.url), "utf8");
const html = await readFile(new URL("../public/console/index.html", import.meta.url), "utf8");
const freshnessBlock = source.slice(source.indexOf("  const REFRESH_TTL_MS"), source.indexOf("  const indexIssueState"));
const issueBlock = source.slice(source.indexOf("  const indexIssueState ="), source.indexOf("  function renderCorpusDepartmentOptions()"));

function freshnessHarness() {
  const stamps = ["departments", "syncRuns", "indexIssues", "environment", "commonRuntime"]
    .map((key) => ({ dataset: { refreshStamp: key }, textContent: "", title: "" }));
  const calls = [];
  const clock = { now: Date.parse("2026-11-06T05:00:00Z") };
  const context = {
    Date: class extends Date { static now() { return clock.now; } },
    state: { currentView: "dashboard", departments: [] },
    window: { location: { hash: "" }, history: { pushState() {}, replaceState() {} }, scrollTo() {}, setTimeout() {} },
    document: { body: { classList: { remove() {} } } },
    $: () => ({ value: "", focus() {} }),
    $$: (selector) => selector.includes('="')
      ? stamps.filter((stamp) => selector.includes(`"${stamp.dataset.refreshStamp}"`))
      : selector === "[data-refresh-stamp]" ? stamps : [],
    loadSyncRuns: (quiet) => calls.push(["sync", quiet]),
    loadEnvironment: () => calls.push(["environment"]),
    loadCommonRuntimeState: (quiet) => calls.push(["runtime", quiet]),
    refreshDriveSaStatusInline: () => calls.push(["driveSa"]),
    loadDepartments: () => calls.push(["departments"]),
    refreshRuntimeEnvIfStale() {},
    indexIssueState: { loaded: false },
  };
  const ui = vm.runInNewContext(`${freshnessBlock}\n({ setFreshness, needsRefresh, switchView, prefetchViews, freshness })`, context);
  const stamp = (key) => stamps.find((item) => item.dataset.refreshStamp === key);
  return { ui, calls, clock, stamp, context };
}

test("every 조회 view carries a refresh stamp", () => {
  for (const key of ["departments", "syncRuns", "indexIssues", "environment", "commonRuntime"]) {
    assert.match(html, new RegExp(`data-refresh-stamp="${key}"`));
  }
});

test("stamp shows loading, the refresh time with relative age, and keeps the last success on failure", () => {
  const h = freshnessHarness();
  h.ui.setFreshness("syncRuns", "loading");
  assert.equal(h.stamp("syncRuns").textContent, "불러오는 중…");
  h.ui.setFreshness("syncRuns", "ok");
  assert.match(h.stamp("syncRuns").textContent, /^\d{2}:\d{2}:\d{2} 갱신 · 방금$/);
  const time = h.stamp("syncRuns").textContent.slice(0, 8);
  h.clock.now += 5 * 60 * 1000;
  h.ui.setFreshness("syncRuns", "loading");
  assert.equal(h.stamp("syncRuns").textContent, `갱신 중… · 마지막 ${time}`);
  h.ui.setFreshness("syncRuns", "fail");
  assert.equal(h.stamp("syncRuns").textContent, `갱신 실패 · 마지막 성공 ${time}`);
  assert.equal(h.stamp("syncRuns").dataset.status, "fail");
  assert.equal(h.stamp("departments").textContent, "", "other stamps are untouched");
});

test("prefetch starts every background query once and skips ones already in flight", () => {
  const h = freshnessHarness();
  h.ui.prefetchViews();
  assert.deepEqual(h.calls, [["sync", true], ["runtime", true], ["driveSa"]]);
  for (const key of ["syncRuns", "commonRuntime", "driveSa"]) h.ui.setFreshness(key, "loading");
  h.ui.prefetchViews();
  assert.equal(h.calls.length, 3);
});

test("opening a view reuses fresh results and re-queries quietly only after they go stale", () => {
  const h = freshnessHarness();
  h.ui.setFreshness("syncRuns", "ok");
  h.ui.switchView("sync", "none");
  assert.deepEqual(h.calls, []);
  h.clock.now += 61 * 1000;
  h.ui.switchView("sync", "none");
  assert.deepEqual(h.calls, [["sync", true]]);
});

test("background index-issue refresh keeps the visible list until the full scan finishes", async () => {
  const nodes = new Map();
  const $ = (id) => {
    if (!nodes.has(id)) nodes.set(id, { value: "", textContent: "", innerHTML: "", open: false, close() {},
      classList: { add() {}, remove() {}, contains: () => false, toggle() {} } });
    return nodes.get(id);
  };
  $("#indexIssuesDepartment").value = "cs";
  const pages = [];
  const marks = [];
  const ui = vm.runInNewContext(`${issueBlock}\n({ loadIndexIssues, indexIssueState })`, {
    $, state: { departments: [] }, clearTimeout() {}, setTimeout() {},
    setFreshness: (key, status) => marks.push(status),
    api: () => new Promise((resolve) => pages.push(resolve)),
    escapeHtml: (v) => String(v ?? ""),
  });
  const doc = (fileId) => ({ fileId, name: fileId, status: "FAILED", statusLabel: "오류", path: "", reason: "x" });
  Object.assign(ui.indexIssueState, { items: [doc("old")], loaded: true, scanned: 1 });

  const pending = ui.loadIndexIssues(false, { background: true });
  pages.shift()({ items: [doc("new-1")], nextCursor: "c2", scanned: 1 });
  await new Promise((r) => setImmediate(r));
  assert.equal(ui.indexIssueState.items.map((item) => item.fileId).join(","), "old", "old list stays mid-scan");
  pages.shift()({ items: [doc("new-2")], nextCursor: "", scanned: 1 });
  await pending;
  assert.equal(ui.indexIssueState.items.map((item) => item.fileId).join(","), "new-1,new-2");
  assert.equal(ui.indexIssueState.scanned, 2);
  assert.deepEqual(marks, ["loading", "ok"]);
});

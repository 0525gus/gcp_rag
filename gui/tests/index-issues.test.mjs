import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../public/console/app.js", import.meta.url), "utf8");
const block = source.slice(source.indexOf("  const indexIssueState ="), source.indexOf("  function renderCorpusDepartmentOptions()"));
function screen(api) {
  const nodes = new Map();
  const $ = (id) => {
    if (!nodes.has(id)) {
      const classes = new Set();
      nodes.set(id, { value: "", textContent: "", innerHTML: "", scrollIntoView() {},
        open: false, showModal() { this.open = true; }, close() { this.open = false; },
        classList: { add: (v) => classes.add(v), remove: (v) => classes.delete(v), contains: (v) => classes.has(v),
          toggle: (v, on) => on ? classes.add(v) : classes.delete(v) } });
    }
    return nodes.get(id);
  };
  $("#indexIssuesDepartment").value = "cs";
  const timers = [];
  const ui = vm.runInNewContext(`${block}\n({ loadIndexIssues, openIndexIssue, closeIndexIssue, renderIndexIssues, setIssueFilter, indexIssueState, refreshIssueReprocess, startIssueReprocess })`, {
    $, api, state: { departments: [] },
    clearTimeout() {}, setTimeout(fn) { timers.push(fn); return timers.length; },
    crypto: { randomUUID: () => "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" },
    escapeHtml: (v) => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
  });
  return { $, ui, timers };
}
const item = (fileId) => ({ fileId, name: fileId, status: "PARSED", statusLabel: "색인 완료 미확인", path: "", reason: "waiting" });

test("late results from a different department never replace current list", async () => {
  let first;
  const s = screen((url) => url.includes("/cs/") ? new Promise((r) => { first = r; }) : Promise.resolve({ items: [item("ee-doc")], scanned: 1, nextCursor: "" }));
  const pending = s.ui.loadIndexIssues();
  s.$("#indexIssuesDepartment").value = "ee";
  await s.ui.loadIndexIssues();
  first({ items: [item("cs-doc")], scanned: 500, nextCursor: "next" });
  await pending;
  assert.equal(s.ui.indexIssueState.items[0].fileId, "ee-doc");
  assert.equal(s.ui.indexIssueState.scanned, 1);
});

test("no matches in a scanned page does not claim all documents are healthy", async () => {
  const s = screen(async () => ({ items: [], scanned: 500, nextCursor: "more" }));
  await s.ui.loadIndexIssues();
  assert.match(s.$("#indexIssuesCoverage").textContent, /아직 조회하지 않은/);
  assert.equal(s.$("#moreIndexIssues").classList.contains("hidden"), false);
});

test("next-page failure preserves prior documents and allows retry", async () => {
  let fail = false;
  const s = screen(async () => {
    if (fail) throw new Error("permission denied");
    return { items: [item("a")], scanned: 500, nextCursor: "more" };
  });
  await s.ui.loadIndexIssues();
  fail = true;
  await s.ui.loadIndexIssues(true);
  assert.equal(s.ui.indexIssueState.items.length, 1);
  assert.equal(s.ui.indexIssueState.cursor, "more");
  assert.equal(s.$("#indexIssuesError").textContent, "permission denied");
});

test("untrusted document text is escaped and detail is ignored after department switch", async () => {
  let detail;
  const s = screen((url) => url.includes("?cursor=") ? Promise.resolve({ items: [item('<img src=x onerror="bad">')], scanned: 1, nextCursor: "" })
    : new Promise((r) => { detail = r; }));
  await s.ui.loadIndexIssues();
  assert.ok(!s.$("#indexIssuesList").innerHTML.includes("<img"));
  const pending = s.ui.openIndexIssue("file");
  s.$("#indexIssuesDepartment").value = "ee";
  detail({ resolved: true, message: "foreign detail" });
  await pending;
  assert.notEqual(s.$("#indexIssueDetailBody").textContent, "foreign detail");
});

test("document rows open a dialog with escaped details and original link", async () => {
  const s = screen(async (url) => url.endsWith("/reprocess") ? { runs: [], document: item("a") }
    : { item: { ...item("a"), name: "<script>" }, jobs: [], queues: [], evidence: [], notices: [], batchNotice: "batch" });
  s.ui.indexIssueState.items = [item("a")];
  s.ui.indexIssueState.loaded = true;
  s.ui.renderIndexIssues();
  assert.match(s.$("#indexIssuesList").innerHTML, /class="issue-row".*data-index-issue="a"/);
  await s.ui.openIndexIssue("a");
  assert.equal(s.$("#indexIssueDetail").open, true);
  assert.equal(s.$("#issueSourceLink").href, "https://drive.google.com/file/d/a/view");
  assert.equal(s.$("#startIssueReprocess").disabled, false);
  s.ui.closeIndexIssue();
  assert.equal(s.$("#indexIssueDetail").open, false);
  assert.equal(s.ui.indexIssueState.fileId, "");
});

test("double click only submits one selected document and waits for completion", async () => {
  let finish;
  let posts = 0;
  const s = screen((url, opts) => {
    if (opts?.method === "POST") {
      posts++;
      assert.equal(url, "/api/v1/departments/cs/index-issues/a/reprocess");
      assert.equal(opts.body.requestId, "a".repeat(32));
      return new Promise((resolve) => { finish = resolve; });
    }
    return Promise.resolve({ runs: [{ state: "ACTIVE", executionId: "run" }], document: item("a") });
  });
  Object.assign(s.ui.indexIssueState, { code: "cs", fileId: "a", detailReady: true, canRetry: true });
  const pending = s.ui.startIssueReprocess();
  await s.ui.startIssueReprocess();
  assert.equal(posts, 1);
  finish({ run: { state: "ACTIVE" } });
  await pending;
  assert.equal(s.$("#startIssueReprocess").disabled, true);
  assert.match(s.$("#issueReprocessStatus").textContent, /처리 중/);
  assert.equal(s.timers.length, 1);
});

test("unknown submission outcome keeps request ID and requires status check", async () => {
  const s = screen(async () => { throw new Error("timeout"); });
  Object.assign(s.ui.indexIssueState, { code: "cs", fileId: "a", detailReady: true, canRetry: true });
  await s.ui.startIssueReprocess();
  assert.equal(s.$("#startIssueReprocess").disabled, true);
  assert.match(s.$("#issueReprocessStatus").textContent, /상태 새로고침/);
  assert.equal(s.ui.indexIssueState.requestId, "a".repeat(32));
});

test("closed dialog ignores late history and does not restart polling", async () => {
  let finish;
  const s = screen(() => new Promise((r) => { finish = r; }));
  Object.assign(s.ui.indexIssueState, { code: "cs", fileId: "a", detailReady: true });
  const pending = s.ui.refreshIssueReprocess();
  s.ui.closeIndexIssue();
  finish({ runs: [{ state: "ACTIVE" }], document: item("a") });
  await pending;
  assert.equal(s.timers.length, 0);
  assert.equal(s.ui.indexIssueState.canRetry, false);
});

test("completed workflow with remaining metadata-only issue is not called resolved", async () => {
  const s = screen(async () => ({ runs: [{ state: "SUCCEEDED", executionId: "run", result: { reason: "NO_BODY_EXTRACTOR" } }],
    document: { ...item("a"), statusLabel: "본문 없이 색인" } }));
  Object.assign(s.ui.indexIssueState, { code: "cs", fileId: "a", detailReady: true });
  await s.ui.refreshIssueReprocess();
  assert.match(s.$("#issueReprocessStatus").textContent, /현재 문서 상태: 본문 없이 색인/);
  assert.doesNotMatch(s.$("#issueReprocessStatus").textContent, /현재 보류·오류 대상이 아닙니다/);
});

test("status tabs filter locally, preserve search, and prioritize errors without changing stored order", async () => {
  let reads = 0;
  const s = screen(async () => {
    reads++;
    return { items: [item("pending"), { ...item("failed"), status: "FAILED" },
      { ...item("held"), status: "SKIPPED" }, { ...item("body"), status: "BODY_MISSING" }], scanned: 4, nextCursor: "" };
  });
  await s.ui.loadIndexIssues();
  const html = s.$("#indexIssuesList").innerHTML;
  assert.ok(html.indexOf('data-index-issue="failed"') < html.indexOf('data-index-issue="pending"'));
  assert.equal(s.ui.indexIssueState.items[0].fileId, "pending");
  s.ui.setIssueFilter("HELD");
  assert.match(s.$("#indexIssuesList").innerHTML, /data-index-issue="held"/);
  assert.match(s.$("#indexIssuesList").innerHTML, /data-index-issue="body"/);
  assert.doesNotMatch(s.$("#indexIssuesList").innerHTML, /data-index-issue="failed"/);
  assert.match(s.$("#indexIssuesCounts").innerHTML, /data-issue-filter="HELD" aria-pressed="true"/);
  s.$("#indexIssuesSearch").value = "body";
  s.ui.setIssueFilter("");
  assert.match(s.$("#indexIssuesList").innerHTML, /data-index-issue="body"/);
  assert.doesNotMatch(s.$("#indexIssuesList").innerHTML, /data-index-issue="held"/);
  assert.equal(reads, 1);
});

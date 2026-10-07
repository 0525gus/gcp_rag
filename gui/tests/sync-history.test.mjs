import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
const source = await readFile(new URL("../public/console/app.js", import.meta.url), "utf8");
const block = source.slice(source.indexOf("  function renderSyncHistory()"), source.indexOf("  async function openSyncRunDetail("));
function render(run, items = [], fileReview = null) {
  const nodes = new Map();
  const $ = (id) => { if (!nodes.has(id)) nodes.set(id, { innerHTML: "", textContent: "", insertAdjacentHTML(_, html) { this.innerHTML += html; } }); return nodes.get(id); };
  const ui = vm.runInNewContext(`${block}\n({renderSyncRunDetail, renderSyncHistory})`, {
    $, state: {syncRuns: [run]}, syncNumber: (v) => Number(v) || 0,
    escapeHtml: (v) => String(v ?? "").replaceAll("<", "&lt;"),
    syncStartedAt: () => "date", syncDuration: () => "duration",
  });
  ui.renderSyncRunDetail({run, items, logs: [], fileReview}); ui.renderSyncHistory(); return $;
}
test("failed run without totals shows unknown metrics and execution error even without log entries", () => {
  const $ = render({state: "FAILED", error: "index tasks did not complete before deadline <unsafe>"}, Array.from({length:116}, () => ({status:"DELETED", fileId:"same-id"})));
  assert.equal(($("#syncRunDetailMetrics").innerHTML.match(/확인 필요/g) || []).length, 4);
  assert.match($("#syncHistoryRows").innerHTML, /확인 필요/);
  assert.equal($("#syncRunDetailItemCount").textContent, "116건의 처리 기록");
  assert.equal($("#syncRunDetailLogCount").textContent, "1건");
  assert.match($("#syncRunDetailLogs").innerHTML, /제한 시간/);
  assert.doesNotMatch($("#syncRunDetailLogs").innerHTML, /<unsafe>/);
});
test("confirmed zero totals stay zero and successful runs do not gain errors", () => {
  const $ = render({state:"SUCCEEDED", totals:{listed:0, unchanged:0, gcsUploaded:0, indexed:0, failed:0, indexFailed:0}});
  assert.doesNotMatch($("#syncRunDetailMetrics").innerHTML, /확인 필요/);
  assert.equal(($("#syncRunDetailMetrics").innerHTML.match(/<b>0<\/b>/g) || []).length,4);
  assert.equal($("#syncRunDetailLogCount").textContent, "0건");
});
test("missing execution error details never claims a failed run was healthy", () => {
 const $ = render({state:"FAILED"});
 assert.match($("#syncRunDetailLogs").innerHTML, /오류 상세를 조회하지 못했습니다/);
});

test("historical file review groups documents without replacing original execution failure", () => {
 const $ = render({state:"FAILED",error:"timeout"}, [], {reviewStatus:"REVIEWED", files:[
  {fileId:"a",status:"DONE",corpora:{faculty:{status:"DONE"}}},
  {fileId:"b",status:"FAILED",name:"<unsafe>",corpora:{faculty:{status:"FAILED",reason:"empty"}}},
  {fileId:"c",status:"PARTIAL",corpora:{faculty:{status:"DONE"},student:{status:"UNKNOWN"}}},
 ]});
 assert.match($("#syncRunDetailSummary").innerHTML,/실행 오류/);
 assert.match($("#syncRunFileReview").innerHTML,/완료 확인/);
 assert.match($("#syncRunFileReview").innerHTML,/실패 확인/);
 assert.match($("#syncRunFileReview").innerHTML,/일부 완료/);
 assert.doesNotMatch($("#syncRunFileReview").innerHTML,/<unsafe>/);
});

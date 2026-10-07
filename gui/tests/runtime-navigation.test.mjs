import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../public/console/app.js", import.meta.url), "utf8");
const block = source.slice(source.indexOf("  async function refreshRuntimeEnvIfStale()"), source.indexOf("  async function prepareResourcePlan("));

function harness() {
  const state = { runtimeEnvChecking: false, runtimeEnvSignature: "" };
  const requests = [], notices = [];
  let report = { status: "DRIFT", services: [{ serviceName: "rag-parser", status: "DRIFT", staleKeys: [{ key: "DOCAI_PROCESSOR_ID" }] }] };
  const refresh = vm.runInNewContext(`${block}\nrefreshRuntimeEnvIfStale`, {
    state,
    api: async (path, options) => {
      requests.push({ path, method: options?.method || "GET" });
      return report;
    },
    toast: (...args) => notices.push(args),
    beginCommonRuntimeDeployment: () => assert.fail("Navigation must not deploy"),
    showCommonRuntimeDeploymentModal: () => assert.fail("Navigation must not open a modal"),
  });
  return { state, requests, notices, refresh, setReport: (value) => { report = value; } };
}

test("repeated navigation with DocAI drift only reads status and notifies once", async () => {
  const h = harness();
  await h.refresh();
  await h.refresh();
  assert.equal(h.notices.length, 1);
  assert.deepEqual(h.requests, [
    { path: "/api/v1/runtime-env", method: "GET" },
    { path: "/api/v1/runtime-env", method: "GET" },
  ]);
  assert.equal(h.state.runtimeEnvChecking, false);
});

test("resolved drift clears notice suppression for a later configuration change", async () => {
  const h = harness();
  await h.refresh();
  const drift = h.state.runtimeEnv;
  h.setReport({ status: "OK", services: [] });
  await h.refresh();
  assert.equal(h.state.runtimeEnvSignature, "");
  h.setReport(drift);
  await h.refresh();
  assert.equal(h.notices.length, 2);
});

test("navigation leaves an existing deployment running without reopening it", async () => {
  const h = harness();
  h.state.commonRuntimeDeployment = { status: "RUNNING" };
  await h.refresh();
  assert.equal(h.requests.length, 0);
  assert.equal(h.notices.length, 0);
});

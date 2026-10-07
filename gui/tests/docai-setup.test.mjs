import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../public/console/app.js", import.meta.url), "utf8");
const block = source.slice(source.indexOf("  const docaiSetup ="), source.indexOf("  function commonSetupPayload()"));

function screen(api) {
  const nodes = new Map();
  const node = (id) => {
    if (!nodes.has(id)) {
      const classes = new Set();
      nodes.set(id, { value: "", disabled: false, innerHTML: "", textContent: "", listeners: {},
        classList: { add: (v) => classes.add(v), remove: (v) => classes.delete(v) },
        addEventListener(event, callback) { this.listeners[event] = callback; },
      });
    }
    return nodes.get(id);
  };
  node("commonSetupForm").elements = { projectId: { value: "project-test" } };
  const ui = vm.runInNewContext(`${block}\n({ renderDocaiSetup, lookupDocai, createDocai, checkDocai, docaiSelections })`, {
    $: (selector) => node(selector.slice(1)), document: { getElementById: node }, api,
    escapeHtml: (value) => String(value).replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
  });
  ui.renderDocaiSetup();
  node("docai-ocr-location").value = "us";
  return { ui, node };
}

test("failed lookup cannot enable creation or a stale plan", async () => {
  const { ui, node } = screen(async () => { throw new Error("HTTP 403"); });
  await ui.lookupDocai("ocr");
  assert.equal(node("docai-ocr-create").disabled, true);
  assert.match(node("docai-ocr-status").textContent, /403/);
  assert.match(node("docai-ocr-status").textContent, /없음/);
});

test("late lookup from a previous project does not enable creation", async () => {
  let resolve;
  const { ui, node } = screen(() => new Promise((r) => { resolve = r; }));
  const request = ui.lookupDocai("ocr");
  node("commonSetupForm").elements.projectId.value = "project-other";
  resolve({ processors: [], canCreate: true, planId: "old" });
  await request;
  assert.equal(node("docai-ocr-create").disabled, true);
});

test("creation requires the looked-up scope and selects the returned processor", async () => {
  const calls = [];
  let created = false;
  const { ui, node } = screen(async (path, options) => {
    calls.push({ path, body: JSON.parse(JSON.stringify(options.body)) });
    if (path.endsWith("/create")) {
      created = true;
      return { processorId: "123", location: "us" };
    }
    return { projectId: "project-test", kind: "ocr", location: "us", displayName: "rag-ocr",
      processors: created ? [{ id: "123", name: "rag-ocr", state: "ENABLED" }] : [],
      canCreate: !created, planId: created ? "" : "plan-1", reason: "ready" };
  });
  await ui.createDocai("ocr");
  assert.equal(calls.length, 0);
  await ui.lookupDocai("ocr");
  node("docai-ocr-create").listeners.click();
  assert.match(node("docai-ocr-plan-text").textContent, /project-test \/ us/);
  assert.match(node("docai-ocr-plan-text").textContent, /과금/);
  await ui.createDocai("ocr");
  assert.deepEqual(calls[1].body, { planId: "plan-1" });
  assert.equal(node("docai-ocr-processor").value, "123");
  assert.equal(ui.docaiSelections().fallback.processorId, "");
  assert.equal(node("docai-ocr-create").disabled, true);
});

test("changing region invalidates a previously prepared creation", async () => {
  let creations = 0;
  const { ui, node } = screen(async (path, options) => {
    if (path.endsWith("/create")) creations++;
    if (options.body.location === "eu") throw new Error("HTTP 403");
    return { projectId: "project-test", location: "us", processors: [], canCreate: true, planId: "plan-1" };
  });
  await ui.lookupDocai("ocr");
  node("docai-ocr-location").value = "eu";
  await node("docai-ocr-location").listeners.change();
  await ui.createDocai("ocr");
  assert.equal(creations, 0);
  assert.equal(node("docai-ocr-create").disabled, true);
});

test("missing connections are visible before choosing a location", () => {
  const { node } = screen(async () => { throw new Error("must not look up an unspecified region"); });
  assert.match(node("docaiSetupSummary").textContent, /Layout Parser: 미설정/);
  assert.match(node("docaiSetupSummary").textContent, /Enterprise Document OCR: 미설정/);
});

test("choosing a location automatically looks up that processor kind", async () => {
  const calls = [];
  const { node } = screen(async (path, options) => {
    calls.push({ path, ...options.body });
    return { processors: [], canCreate: true, reason: "ready" };
  });
  await node("docai-ocr-location").listeners.change();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].kind, "ocr");
  assert.equal(calls[0].location, "us");
  assert.match(node("docai-ocr-status").textContent, /프로세서 없음/);
  assert.equal(node("docai-ocr-create").disabled, false);
});

test("a saved processor missing from the refreshed list is not silently cleared", async () => {
  const { ui, node } = screen(async () => ({ processors: [], canCreate: true, reason: "ready" }));
  node("docai-ocr-processor").value = "previous-id";
  await ui.lookupDocai("ocr");
  assert.equal(ui.docaiSelections().ocr.processorId, "previous-id");
  assert.match(node("docai-ocr-processor").innerHTML, /확인 필요/);
});

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../public/console/app.js", import.meta.url), "utf8");
const html = await readFile(new URL("../public/console/index.html", import.meta.url), "utf8");
const startup = source.slice(source.indexOf("  async function checkInitialSetup()"), source.indexOf("  async function init()"));
const docaiGate = source.slice(source.indexOf("  function needsDocaiSetup("), source.indexOf("  function showCommonSetup("));
const loginComplete = source.slice(source.indexOf("  async function closeLoginOnlySetup("), source.indexOf("  async function pollGcloudLogin("));

function harness(environment, session = async () => ({ nonce: "local" }), docaiAvailable = true) {
  const events = [];
  const nodes = new Map();
  const $ = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, {
      dataset: {}, classList: {
        add: (name) => events.push(`${selector}:add:${name}`),
        remove: (name) => events.push(`${selector}:remove:${name}`),
      },
    });
    return nodes.get(selector);
  };
  const ui = vm.runInNewContext(`${docaiGate}\n${loginComplete}\n${startup}\n({ run: checkInitialSetup, login: closeLoginOnlySetup })`, {
    $, state: {}, api: session, loadEnvironment: environment,
    docaiKinds: { fallback: "Layout", ocr: "OCR" },
    openDocaiSetup: async () => { events.push("docai"); return docaiAvailable; },
    applySetupMode: () => {},
    showCommonSetupShell: () => events.push("checking"),
    needsCommonSetup: (env) => !env.commonExists || !env.gcloudAuthenticated,
    showCommonSetup: () => events.push("setup"),
    loadDepartments: () => events.push("departments"),
    reconnectRun: () => {}, reconnectMcpDeployment: () => {}, reconnectCommonRuntimeDeployment: () => {},
    refreshRuntimeEnvIfStale: () => events.push("runtime"),
  });
  return { ...ui, events, $ };
}

test("initial HTML shows checking before any API request and disables dashboard interaction", () => {
  assert.match(html, /class="setup-gate" id="setupGate" data-mode="checking"/);
  assert.match(html, /id="setupTitle">공통 셋업 확인 중/);
  assert.match(html, /id="commonSetupForm" class="hidden"/);
  assert.match(html, /class="app-shell" inert/);
});

test("dashboard work waits for common setup check", async () => {
  let resolve, entered;
  const started = new Promise((r) => { entered = r; });
  const h = harness(() => new Promise((r) => { resolve = r; entered(); }));
  const pending = h.run();
  await started;
  assert.deepEqual(h.events, ["checking"]);
  resolve({ commonExists: true, commonValid: true, gcloudAuthenticated: true });
  await pending;
  assert.ok(h.events.indexOf("#setupGate:add:hidden") < h.events.indexOf("departments"));
  assert.equal(h.$(".app-shell").inert, false);
});

test("missing setup opens setup without loading departments", async () => {
  const h = harness(async () => ({ commonExists: false, gcloudAuthenticated: true }));
  await h.run();
  assert.deepEqual(h.events, ["checking", "setup"]);
});

test("failed environment check remains on checking screen with retry", async () => {
  const h = harness(async () => null);
  await h.run();
  assert.ok(h.events.includes("#retryInitialSetup:remove:hidden"));
  assert.ok(!h.events.includes("departments"));
  assert.ok(!h.events.includes("#setupGate:add:hidden"));
});

test("retry can recover from a failed session without rebinding events", async () => {
  let failed = true;
  const h = harness(async () => ({ commonExists: true, commonValid: true, gcloudAuthenticated: true }), async () => {
    if (failed) throw new Error("offline");
    return { nonce: "local" };
  });
  await h.run();
  failed = false;
  await h.run();
  assert.equal(h.events.filter((event) => event === "departments").length, 1);
});

for (const missing of ["fallback", "ocr"]) {
  test(`missing ${missing} shows DocAI common setup before dashboard`, async () => {
    const h = harness(async () => ({ commonExists: true, commonValid: true, gcloudAuthenticated: true,
      docaiConfigured: { fallback: true, ocr: true, [missing]: false } }));
    await h.run();
    assert.ok(h.events.indexOf("docai") < h.events.indexOf("departments"));
    assert.ok(!h.events.includes("#setupGate:add:hidden"));
    assert.equal(h.$("#closeDocaiSetup").textContent, "나중에 설정");
    assert.equal(h.$(".app-shell").inert, false);
  });
}

test("configured DocAI connections proceed directly to dashboard", async () => {
  const h = harness(async () => ({ commonExists: true, commonValid: true, gcloudAuthenticated: true,
    docaiConfigured: { fallback: true, ocr: true } }));
  await h.run();
  assert.ok(!h.events.includes("docai"));
  assert.ok(h.events.includes("#setupGate:add:hidden"));
});

test("DocAI settings lookup failure keeps initial retry available", async () => {
  const h = harness(async () => ({ commonExists: true, commonValid: true, gcloudAuthenticated: true,
    docaiConfigured: { fallback: false, ocr: false } }), undefined, false);
  await h.run();
  assert.ok(h.events.includes("#retryInitialSetup:remove:hidden"));
  assert.ok(!h.events.includes("departments"));
});

test("login completion also shows missing DocAI common setup", async () => {
  const h = harness(async () => null);
  await h.login({ commonValid: true, docaiConfigured: { fallback: false, ocr: true } });
  assert.ok(h.events.indexOf("docai") < h.events.indexOf("departments"));
  assert.ok(!h.events.includes("#setupGate:add:hidden"));
});

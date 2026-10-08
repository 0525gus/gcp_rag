import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { readFile } from "node:fs/promises";
const source = await readFile(new URL("../public/console/app.js", import.meta.url), "utf8");
const switchBlock = source.slice(source.indexOf("  function switchView("), source.indexOf("  const indexIssueState"));
const routeBlock = source.slice(source.indexOf("  function restorePageRoute("), source.indexOf("  const drawerParserOptions"));
function harness(hash = "#dashboard") {
  const state = {currentView: "dashboard", selectedCode: null, departments: [{code: "cs"}]};
  const visits = [], scrolls = [], opened = [];
  const window = {location: {hash}, scrollY: 420, scrollTo: value => scrolls.push(value.top), history: {}};
  for (const method of ["pushState", "replaceState"]) window.history[method] = (_, __, next) => { visits.push([method, next]); window.location.hash = next; };
  const context = {state, window, $$: () => [], document: {body: {classList: {remove(){}}}}, refreshRuntimeEnvIfStale(){}, toast(){}, openDrawer: (...args) => opened.push(args)};
  const api = vm.runInNewContext(`${switchBlock}\n${routeBlock}\n({switchView, restorePageRoute})`, context);
  return {state, window, visits, scrolls, opened, ...api};
}
test("department navigation keeps the list scroll position and clears selection on return", () => {
  const h = harness(); h.state.selectedCode = "cs"; h.switchView("department");
  assert.equal(h.window.location.hash, "#departments/cs");
  h.window.scrollY = 0; h.switchView("dashboard");
  assert.equal(h.scrolls.at(-1), 420); assert.equal(h.state.selectedCode, null);
});
test("direct and browser-back department routes open without adding another history entry", () => {
  const h = harness("#departments/cs"); h.restorePageRoute();
  assert.deepEqual(h.opened[0], ["cs", true, "none"]); assert.equal(h.visits.length, 0);
});
test("missing and malformed department routes return to the list safely", () => {
  for (const hash of ["#departments/missing", "#departments/%broken"]) {
    const h = harness(hash); h.restorePageRoute();
    assert.equal(h.state.currentView, "dashboard"); assert.equal(h.window.location.hash, "#dashboard");
    assert.equal(h.visits[0][0], "replaceState");
  }
});

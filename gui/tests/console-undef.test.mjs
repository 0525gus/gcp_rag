// app.js 는 번들링 없이 브라우저에 그대로 나가므로 오타·삭제된 변수 참조를
// 잡아줄 컴파일 단계가 없다. `node --check` 는 문법만 봐서 못 잡는다.
// (실제로 리팩터링 중 남은 `projects` 참조가 첫 실행 화면을 통째로 멈췄다.)
import { readFile } from "node:fs/promises";
import assert from "node:assert/strict";
import test from "node:test";
import { Linter } from "eslint";

const BROWSER_GLOBALS = [
  "window", "document", "fetch", "navigator", "console", "CSS", "URL", "Request",
  "setTimeout", "clearTimeout", "setInterval", "clearInterval", "localStorage",
  "sessionStorage", "Intl", "AbortController", "EventSource", "FormData", "Blob",
  "requestAnimationFrame", "getComputedStyle", "alert", "location", "history",
];

test("console app.js has no undefined references", async () => {
  const code = await readFile(new URL("../public/console/app.js", import.meta.url), "utf8");
  const linter = new Linter({ configType: "flat" });
  const messages = linter.verify(code, {
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: Object.fromEntries(BROWSER_GLOBALS.map((name) => [name, "readonly"])),
    },
    rules: { "no-undef": "error" },
  });
  assert.deepEqual(
    messages.map((m) => `line ${m.line}: ${m.message}`),
    [],
  );
});

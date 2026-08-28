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

test("every $(\"#id\") in app.js resolves to markup that declares it", async () => {
  // index.html 과 app.js 는 서로 다른 파일이라 ID 가 어긋나도 아무도 안 알려준다.
  // $("#없는것") 은 null 을 돌려주고, 그 다음 줄에서야 터진다.
  const [js, html] = await Promise.all([
    readFile(new URL("../public/console/app.js", import.meta.url), "utf8"),
    readFile(new URL("../public/console/index.html", import.meta.url), "utf8"),
  ]);
  const referenced = new Set([...js.matchAll(/\$\("#([A-Za-z0-9_-]+)"\)/g)].map((m) => m[1]));
  // JS 템플릿이 만들어 넣는 ID 도 선언으로 친다(예: 환경 카드의 상태 확인 버튼).
  const declared = new Set(
    [...html.matchAll(/id="([A-Za-z0-9_-]+)"/g), ...js.matchAll(/id="([A-Za-z0-9_-]+)"/g)]
      .map((m) => m[1]),
  );
  assert.deepEqual([...referenced].filter((id) => !declared.has(id)), []);
});

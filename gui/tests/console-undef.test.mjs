import { readFile } from "node:fs/promises";
import assert from "node:assert/strict";
import test from "node:test";
import { Linter } from "eslint";

const BROWSER_GLOBALS = [
  "window", "document", "fetch", "navigator", "console", "CSS", "URL", "Request",
  "setTimeout", "clearTimeout", "setInterval", "clearInterval", "localStorage",
  "sessionStorage", "Intl", "AbortController", "EventSource", "FormData", "Blob",
  "requestAnimationFrame", "getComputedStyle", "alert", "location", "history",
  "URLSearchParams", "crypto",
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
  const [js, html] = await Promise.all([
    readFile(new URL("../public/console/app.js", import.meta.url), "utf8"),
    readFile(new URL("../public/console/index.html", import.meta.url), "utf8"),
  ]);
  const referenced = new Set([...js.matchAll(/\$\("#([A-Za-z0-9_-]+)"\)/g)].map((m) => m[1]));
  const declared = new Set(
    [...html.matchAll(/id="([A-Za-z0-9_-]+)"/g), ...js.matchAll(/id="([A-Za-z0-9_-]+)"/g)]
      .map((m) => m[1]),
  );
  assert.deepEqual([...referenced].filter((id) => !declared.has(id)), []);
});

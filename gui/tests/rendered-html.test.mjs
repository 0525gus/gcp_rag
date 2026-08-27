import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("root routes to the local operations console", async () => {
  const response = await render();
  assert.ok([301, 302, 307, 308].includes(response.status));
  assert.equal(new URL(response.headers.get("location")).pathname, "/console/index.html");
});

test("ships the finished Korean console without starter artifacts", async () => {
  const [html, css, js, page, layout, packageJson] = await Promise.all([
    readFile(new URL("../public/console/index.html", import.meta.url), "utf8"),
    readFile(new URL("../public/console/app.css", import.meta.url), "utf8"),
    readFile(new URL("../public/console/app.js", import.meta.url), "utf8"),
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  assert.match(html, /GCP RAG 학과 관리/);
  assert.match(html, /상태 대시보드/);
  assert.match(html, /새 학과 연결/);
  assert.match(html, /단일 코퍼스/);
  assert.match(html, /학생 분리/);
  assert.match(html, /id="corpusGenerateToggle"/);
  assert.match(html, /Gemini 답변/);
  assert.match(js, /staffButton\.textContent = single \? "단일" : "교직원"/);
  assert.match(css, /corpus-audience-toggle\.single-corpus/);
  assert.match(css, /\.corpus-chat-feed \{[^}]*min-height: 0/);
  assert.match(css, /\.corpus-chat-feed \{[^}]*overflow-y:\s*auto/);
  assert.match(html, /id="mcpDeploymentModal"/);
  assert.match(html, /MCP DEPLOYMENT/);
  assert.match(js, /\/api\/v1\/mcp-deployments/);
  assert.match(js, /data-deploy-mcp/);
  assert.match(html, /id="drivePreflightStatus"/);
  assert.match(html, /id="driveConflictStatus"/);
  assert.match(js, /allowDuplicateDriveIds/);
  assert.match(html, /<div class="field tag-field drive-id-field">/);
  assert.match(html, /<label for="driveIds">공유드라이브 ID/);
  assert.match(html, /폴더 정보 확인/);
  assert.match(js, /\/api\/v1\/departments\/folder-lookup/);
  assert.match(html, /<span>동기화 관리<\/span>/);
  assert.match(html, /id="syncActivePanel"/);
  assert.match(js, /\/api\/v1\/sync-runs/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(js, /검증 중…/);
  assert.match(js, /loader-ring/);
  assert.match(page, /redirect\("\/console\/index\.html"\)/);
  assert.match(layout, /lang="ko"/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  assert.doesNotMatch(html + css + js, /https?:\/\//);
  await assert.rejects(access(new URL("../app/_sites-preview/SkeletonPreview.tsx", import.meta.url)));
});

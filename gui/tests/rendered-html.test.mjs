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

  assert.match(html, /GCP RAG 서비스 관리/);
  assert.match(html, /GCP RAG Console/);
  assert.match(html, /서비스 현황/);
  assert.match(html, /신규 등록/);
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
  assert.match(js, /\/api\/v1\/cloud-mcp-services/);
  assert.match(js, /CLOUD METADATA/);
  assert.match(css, /\.cloud-only-mark/);
  assert.match(js, /data-deploy-mcp/);
  assert.match(html, /id="drivePreflightStatus"/);
  assert.match(html, /id="driveConflictStatus"/);
  assert.match(html, /id="drawerMore"/);
  assert.match(html, /관련 리소스와 설정 삭제/);
  assert.match(js, /allowDuplicateDriveIds/);
  assert.match(html, /<div class="field tag-field drive-id-field">/);
  assert.match(html, /<label for="driveIds">공유드라이브 ID/);
  assert.match(js, /submitDriveIdsOnEnter/);
  assert.match(js, /driveIds\.addEventListener\("keydown", submitDriveIdsOnEnter\)/);
  assert.match(html, /폴더 정보 확인/);
  assert.match(js, /\/api\/v1\/departments\/folder-lookup/);
  assert.match(js, /\/api\/v1\/departments\/drive-folders/);
  assert.match(html, /id="driveFolderBrowser"/);
  assert.match(css, /\.drive-folder-browser/);
  assert.match(css, /\.field input, \.field select \{ height: 48px; min-height: 48px;/);
  assert.match(js, /\{ expand: false, quiet: true \}/);
  assert.match(js, /loadedDriveFolderDescendants/);
  assert.match(js, /selectionLocked/);
  assert.match(js, /drive-tree-indent/);
  assert.match(js, /drive-tree-children/);
  assert.match(html, /id="teardownModal"/);
  assert.match(html, /id="drawerDelete"/);
  assert.match(html, /id="commonRuntimeTeardown"/);
  assert.match(js, /\/teardown-plan/);
  assert.match(js, /api\/v1\/teardowns\//);
  assert.match(css, /\.button\.danger \{/);
  assert.match(js, /data-open-sync/);
  assert.match(js, /MANUAL_SYNC/);
  assert.match(html, /id="retryTeardown"/);
  assert.match(js, /refreshRuntimeEnvIfStale/);
  assert.match(js, /envOnly/);
  assert.match(css, /\.drive-tree-children \{/);
  assert.doesNotMatch(js, /2단계 미리 조회/);
  assert.match(html, /<span>동기화<\/span>/);
  assert.match(html, /<span>검색 테스트<\/span>/);
  assert.match(html, /<span>운영 환경<\/span>/);
  assert.match(html, /id="syncActivePanel"/);
  assert.match(js, /\/api\/v1\/sync-runs/);
  assert.match(js, /현재 실행 위치/);
  assert.match(js, /최근 처리 항목/);
  assert.match(js, /이 학과 동기화 실행 중/);
  assert.match(css, /\.sync-live-detail/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(js, /검증 중…/);
  assert.match(js, /field\.startsWith\("drive\."\)/);
  assert.match(js, /Object\.values\(fieldErrors\)/);
  assert.match(js, /clearChangedFormFieldError/);
  assert.match(js, /form\.querySelector\(`\[name=/);
  assert.match(js, /loader-ring/);
  assert.match(page, /redirect\("\/console\/index\.html"\)/);
  assert.match(layout, /lang="ko"/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  assert.doesNotMatch(html + css + js, /https?:\/\//);
  await assert.rejects(access(new URL("../app/_sites-preview/SkeletonPreview.tsx", import.meta.url)));
});

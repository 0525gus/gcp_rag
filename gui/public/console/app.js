(() => {
  "use strict";

  const state = {
    nonce: "",
    departments: [],
    environment: null,
    currentView: "dashboard",
    currentStep: 1,
    activeRunId: null,
    checkingCodes: new Set(),
    selectedCode: null,
    pollTimer: null,
    editingCode: null,
    editingRevision: null,
    setupResourceRequest: 0,
    authPollTimer: null,
    authLoginPending: false,
    departmentResourceRequest: 0,
    departmentBuckets: [],
    drivePreflightSignature: "",
    folderLookupSignature: "",
    folderLookupPending: false,
    folderLookupById: new Map(),
    codeAvailable: null,
    codeAvailabilityRequest: 0,
    codeAvailabilityTimer: null,
    resourcePlan: null,
    provisionRun: null,
    provisionPollTimer: null,
    commonResourcePlan: null,
    commonProvisionPollTimer: null,
    driveSaStatus: null,
    driveSaPlan: null,
    driveSaPollTimer: null,
    mcpDeployment: null,
    mcpDeploymentCode: "",
    mcpDeploymentPollTimer: null,
    mcpServers: new Map(),
    mcpServerRequests: new Map(),
    driveConflicts: [],
    allowDuplicateDriveIds: false,
    corpusMode: "split",
    corpusAudience: "staff",
    corpusMessages: [],
    corpusQueryPending: false,
    syncRuns: [],
    syncTargets: [],
    syncMode: "delta",
    syncStartPending: false,
    syncPollTimer: null,
    syncRequest: 0,
    syncPreferredDepartment: "",
  };

  const statusLabels = {
    OK: "정상",
    WARN: "확인 필요",
    FAIL: "오류",
    CHECKING: "확인 중",
    SKIP: "건너뜀",
    UNKNOWN: "미확인",
    STALE: "변경됨",
  };
  const layerLabels = {
    LOCAL: "설정",
    RESOURCE: "GCP 리소스",
    DEPLOY: "배포",
    RUNTIME: "서비스",
    SYNC: "동기화",
  };
  const statusRank = { FAIL: 5, WARN: 4, CHECKING: 3, STALE: 2, UNKNOWN: 1, SKIP: 0, OK: 0 };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  function badge(status) {
    const normal = statusLabels[status] ? status : "UNKNOWN";
    return `<span class="status-badge status-${normal}">${statusLabels[normal]}</span>`;
  }

  async function api(path, options = {}) {
    const config = { ...options, headers: { ...(options.headers || {}) } };
    if (config.body && typeof config.body !== "string") {
      config.headers["Content-Type"] = "application/json";
      config.body = JSON.stringify(config.body);
    }
    if (config.method && config.method !== "GET") {
      config.headers["X-Local-Session"] = state.nonce;
    }
    const response = await fetch(path, config);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data?.error?.message || data?.detail || `HTTP ${response.status}`);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return data;
  }

  function toast(title, message, type = "") {
    const item = document.createElement("div");
    item.className = `toast ${type}`;
    item.innerHTML = `<span>${type === "fail" ? "×" : "✓"}</span><div><b>${escapeHtml(title)}</b><p>${escapeHtml(message)}</p></div>`;
    $("#toastRegion").append(item);
    window.setTimeout(() => item.remove(), 4300);
  }

  function switchView(view) {
    state.currentView = view;
    $$(".view").forEach((item) => item.classList.toggle("active", item.id === `${view}View`));
    $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
    document.body.classList.remove("menu-open");
    if (view === "environment") loadEnvironment();
    if (view === "sync") loadSyncRuns();
    if (view === "corpus") {
      renderCorpusDepartmentOptions();
      window.setTimeout(() => $("#corpusChatInput").focus(), 120);
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function renderCorpusDepartmentOptions() {
    const select = $("#corpusDepartment");
    if (!select) return;
    const current = select.value;
    select.innerHTML = '<option value="">학과 선택</option>' + state.departments.map((dept) =>
      `<option value="${escapeHtml(dept.code)}">${escapeHtml(dept.name)} · ${escapeHtml(dept.code)}</option>`
    ).join("");
    if (state.departments.some((dept) => dept.code === current)) select.value = current;
    else if (state.departments.length === 1) select.value = state.departments[0].code;
    select.disabled = state.departments.length === 0;
    updateCorpusChatTarget();
  }

  function updateCorpusChatTarget(clear = false) {
    const code = $("#corpusDepartment")?.value || "";
    const dept = state.departments.find((item) => item.code === code);
    const single = dept?.corpusMode === "single";
    const audienceToggle = $(".corpus-audience-toggle");
    const staffButton = $('[data-corpus-audience="staff"]');
    const studentButton = $('[data-corpus-audience="student"]');
    audienceToggle.classList.toggle("single-corpus", Boolean(single));
    staffButton.textContent = single ? "단일" : "교직원";
    studentButton.classList.toggle("hidden", Boolean(single));
    studentButton.disabled = Boolean(single);
    studentButton.title = single ? "이 조직은 단일 코퍼스로 운영됩니다." : "";
    if (single && state.corpusAudience === "student") {
      state.corpusAudience = "staff";
      $$('[data-corpus-audience]').forEach((item) => item.classList.toggle("active", item.dataset.corpusAudience === "staff"));
    }
    const generate = Boolean($("#corpusGenerateToggle")?.checked);
    const audienceLabel = single ? "단일" : (state.corpusAudience === "staff" ? "교직원" : "학생");
    $("#corpusChatTarget").textContent = dept ? `${dept.name} · ${audienceLabel} 코퍼스` : "코퍼스를 선택해 주세요";
    $("#corpusChatMeta").textContent = dept
      ? `${code} · ${generate ? "Gemini 답변" : "검색 결과"} · 상위 5개`
      : (generate ? "Gemini 답변 · 상위 5개" : "실제 검색 결과 · 상위 5개");
    if (clear) clearCorpusChat();
  }

  function corpusContextHtml(context) {
    const source = context.sourceDisplayName || context.sourceUri || `검색 결과 ${context.rank}`;
    const score = Number.isFinite(Number(context.score)) ? Number(context.score).toFixed(4) : "—";
    return `<article class="corpus-context-card">
      <header><b title="${escapeHtml(source)}">${escapeHtml(source)}</b><span>#${escapeHtml(context.rank)} · ${escapeHtml(score)}</span></header>
      <p>${escapeHtml(context.text || "본문이 없습니다.")}</p>
      ${context.sourceUri ? `<small title="${escapeHtml(context.sourceUri)}">${escapeHtml(context.sourceUri)}</small>` : ""}
    </article>`;
  }

  function renderCorpusChat() {
    const feed = $("#corpusChatFeed");
    if (!state.corpusMessages.length && !state.corpusQueryPending) {
      const hint = $("#corpusGenerateToggle")?.checked
        ? "검색된 원문을 근거로 Gemini가 답합니다. 별도 키 없이 현재 gcloud 계정을 사용합니다."
        : "답변을 생성하지 않고 검색된 원문과 출처를 그대로 보여줍니다.";
      feed.innerHTML = `<div class="corpus-chat-empty"><span class="chat-empty-mark">R</span><h2>코퍼스에 질문해 보세요</h2><p>${hint}</p></div>`;
      return;
    }
    const messages = state.corpusMessages.map((message) => {
      if (message.role === "user") return `<article class="corpus-message user"><div class="corpus-message-bubble">${escapeHtml(message.text)}</div><div class="corpus-message-meta">${escapeHtml(message.target)}</div></article>`;
      if (message.error) return `<article class="corpus-message assistant"><div class="corpus-message-bubble">${escapeHtml(message.error)}</div><div class="corpus-message-meta">조회 실패</div></article>`;
      const contexts = message.result.contexts || [];
      const answer = message.result.answer || "";
      const answerError = message.result.answerError || "";
      const answerHtml = answer
        ? `<div class="corpus-answer-bubble">${escapeHtml(answer)}</div>`
        : "";
      const answerErrorHtml = answerError
        ? `<div class="corpus-answer-error">${escapeHtml(answerError)}</div>`
        : "";
      const body = contexts.length
        ? `<div class="corpus-context-list">${contexts.map(corpusContextHtml).join("")}</div>`
        : '<div class="corpus-message-bubble">검색된 문서가 없습니다. 코퍼스에 문서가 색인되었는지 확인해 주세요.</div>';
      const summary = answer || answerError ? `Gemini 답변 · 근거 ${contexts.length}개` : `검색 결과 ${contexts.length}개`;
      const meta = message.result.answerModel
        ? `${escapeHtml(message.result.answerModel)} · ${escapeHtml(message.result.latencyMs)}ms`
        : `${escapeHtml(message.result.latencyMs)}ms · Vertex RAG 원문 조회`;
      return `<article class="corpus-message assistant"><div class="corpus-result-summary"><b>${escapeHtml(summary)}</b><span>${escapeHtml(String(message.result.audience === "staff" ? "교직원" : "학생"))}</span></div>${answerHtml}${answerErrorHtml}${body}<div class="corpus-message-meta">${meta}</div></article>`;
    }).join("");
    const loading = state.corpusQueryPending
      ? `<article class="corpus-message assistant"><div class="corpus-message-bubble corpus-chat-loading"><span class="loader-ring"></span>${$("#corpusGenerateToggle")?.checked ? "관련 문서를 찾고 답변을 작성하고 있습니다." : "코퍼스에서 관련 문서를 찾고 있습니다."}</div></article>`
      : "";
    feed.innerHTML = messages + loading;
    feed.scrollTop = feed.scrollHeight;
  }

  function clearCorpusChat() {
    state.corpusMessages = [];
    renderCorpusChat();
  }

  async function submitCorpusQuery(event) {
    event.preventDefault();
    if (state.corpusQueryPending) return;
    const code = $("#corpusDepartment").value;
    const input = $("#corpusChatInput");
    const query = input.value.trim();
    if (!code) {
      toast("학과를 선택해 주세요", "조회할 학과 코퍼스가 필요합니다.", "fail");
      $("#corpusDepartment").focus();
      return;
    }
    if (!query) {
      input.focus();
      return;
    }
    const dept = state.departments.find((item) => item.code === code);
    const audienceLabel = dept?.corpusMode === "single" ? "단일" : (state.corpusAudience === "staff" ? "교직원" : "학생");
    state.corpusMessages.push({ role: "user", text: query, target: `${dept?.name || code} · ${audienceLabel}` });
    state.corpusQueryPending = true;
    input.value = "";
    $("#sendCorpusQuery").disabled = true;
    $("#corpusDepartment").disabled = true;
    $("#corpusGenerateToggle").disabled = true;
    $$('[data-corpus-audience]').forEach((button) => { button.disabled = true; });
    renderCorpusChat();
    try {
      const result = await api("/api/v1/corpus-query", {
        method: "POST",
        body: { code, audience: state.corpusAudience, query, topK: 5, generate: Boolean($("#corpusGenerateToggle").checked) },
      });
      state.corpusMessages.push({ role: "assistant", result });
    } catch (error) {
      state.corpusMessages.push({ role: "assistant", error: error.message });
    } finally {
      state.corpusQueryPending = false;
      $("#sendCorpusQuery").disabled = false;
      $("#corpusDepartment").disabled = state.departments.length === 0;
      $("#corpusGenerateToggle").disabled = false;
      $$('[data-corpus-audience]').forEach((button) => { button.disabled = false; });
      updateCorpusChatTarget(false);
      renderCorpusChat();
      input.focus();
    }
  }

  function layerStatus(result, layer) {
    if (!result?.checks?.length) return "UNKNOWN";
    const matches = result.checks.filter((item) => item.layer === layer);
    if (!matches.length) return "UNKNOWN";
    return matches.reduce((worst, item) =>
      (statusRank[item.status] || 0) > (statusRank[worst] || 0) ? item.status : worst, "OK");
  }

  function relativeTime(value) {
    if (!value) return "—";
    const diff = Date.now() - new Date(value).getTime();
    if (!Number.isFinite(diff)) return "—";
    const minutes = Math.max(0, Math.round(diff / 60000));
    if (minutes < 1) return "방금 전";
    if (minutes < 60) return `${minutes}분 전`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours}시간 전`;
    return `${Math.round(hours / 24)}일 전`;
  }

  function effectiveStatus(dept) {
    return state.checkingCodes.has(dept.code) ? "CHECKING" : (dept.lastStatus || "UNKNOWN");
  }

  async function loadDepartmentMcpServers(code, force = false) {
    if (force) state.mcpServers.delete(code);
    if (state.mcpServers.has(code)) return state.mcpServers.get(code);
    if (state.mcpServerRequests.has(code)) return state.mcpServerRequests.get(code);
    const request = api(`/api/v1/departments/${encodeURIComponent(code)}/mcp-servers`)
      .then((data) => {
        state.mcpServers.set(code, data);
        return data;
      })
      .finally(() => state.mcpServerRequests.delete(code));
    state.mcpServerRequests.set(code, request);
    return request;
  }

  async function writeClipboard(value) {
    if (!value) throw new Error("복사할 값이 없습니다.");
    await navigator.clipboard.writeText(value);
  }

  function renderDrawerMcpServers(code, data = null, error = "") {
    const root = $("#drawerMcpServers");
    if (error) {
      root.innerHTML = `<div class="drawer-mcp-heading"><b>MCP SERVERS</b><small>Cloud Run</small></div><div class="drawer-mcp-loading">${escapeHtml(error)}</div>`;
      return;
    }
    if (!data) {
      root.innerHTML = '<div class="drawer-mcp-heading"><b>MCP SERVERS</b><small>실제 배포 URL 조회 중</small></div><div class="drawer-mcp-loading">교직원·학생 MCP 서버를 확인하고 있습니다.</div>';
      return;
    }
    const servers = data.servers || [];
    root.innerHTML = `<div class="drawer-mcp-heading"><b>MCP SERVERS</b><small>${escapeHtml(data.region || "Cloud Run")}</small></div>
      <div class="drawer-mcp-grid">${servers.map((server) => {
        const ready = server.status === "READY";
        const statusLabel = ready ? "준비됨" : server.status === "NOT_READY" ? "배포 확인 필요" : "아직 배포되지 않음";
        const url = server.url || "URL 없음";
        const urlElement = server.healthUrl
          ? `<a class="mcp-server-url" href="${escapeHtml(server.healthUrl)}" target="_blank" rel="noopener">${escapeHtml(url)}</a>`
          : `<span class="mcp-server-url">${escapeHtml(url)}</span>`;
        return `<article class="drawer-mcp-card" data-status="${escapeHtml(server.status)}">
          <span class="mcp-audience-mark">${server.audience === "staff" ? "교" : "학"}</span>
          <div class="mcp-server-main">
            <div class="mcp-server-title"><span class="mcp-ready-dot" title="${escapeHtml(statusLabel)}"></span><b>${escapeHtml(server.serviceName)}</b></div>
            ${urlElement}
            <div class="mcp-server-actions"><button type="button" class="mcp-url-copy" data-copy-mcp-url="${escapeHtml(server.url || "")}" ${server.url ? "" : "disabled"}>URL 복사</button><button type="button" class="mcp-url-copy mcp-key-copy" data-copy-mcp-key="${escapeHtml(server.audience)}" data-department-code="${escapeHtml(code)}">키 복사</button></div>
          </div>
        </article>`;
      }).join("")}</div>`;
  }

  async function copySingleMcpServer(event) {
    const button = event.target.closest("[data-copy-mcp-url], [data-copy-mcp-key]");
    if (!button) return;
    const originalLabel = button.dataset.copyMcpKey ? "키 복사" : "URL 복사";
    try {
      button.disabled = true;
      if (button.dataset.copyMcpKey) {
        const result = await api(`/api/v1/departments/${encodeURIComponent(button.dataset.departmentCode)}/mcp-keys/${encodeURIComponent(button.dataset.copyMcpKey)}`, { method: "POST" });
        await writeClipboard(`Bearer ${result.key}`);
        result.key = "";
      } else {
        await writeClipboard(button.dataset.copyMcpUrl);
      }
      button.textContent = "복사됨";
      button.classList.add("is-copied");
      window.setTimeout(() => {
        button.textContent = originalLabel;
        button.classList.remove("is-copied");
        button.disabled = false;
      }, 1400);
    } catch (error) {
      button.textContent = originalLabel;
      button.disabled = false;
      toast("MCP 정보를 복사하지 못했습니다", error.message, "fail");
    }
  }

  function renderSummary() {
    const counts = { OK: 0, WARN: 0, FAIL: 0, CHECKING: 0 };
    state.departments.forEach((dept) => {
      const status = effectiveStatus(dept);
      if (counts[status] !== undefined) counts[status] += 1;
    });
    $("#okCount").textContent = counts.OK;
    $("#warnCount").textContent = counts.WARN;
    $("#failCount").textContent = counts.FAIL;
    $("#checkingCount").textContent = counts.CHECKING;
    $(".checking-card").classList.toggle("is-live", counts.CHECKING > 0);
  }

  function renderDepartments() {
    const query = $("#departmentSearch").value.trim().toLowerCase();
    const filter = $("#statusFilter").value;
    const rows = state.departments
      .filter((dept) => !query || dept.name.toLowerCase().includes(query) || dept.code.includes(query))
      .filter((dept) => {
        const status = effectiveStatus(dept);
        if (filter === "ALL") return true;
        if (filter === "ATTENTION") return ["WARN", "FAIL"].includes(status);
        return status === filter;
      })
      .sort((a, b) => {
        const statusDiff = (statusRank[effectiveStatus(b)] || 0) - (statusRank[effectiveStatus(a)] || 0);
        return statusDiff || a.code.localeCompare(b.code);
      });

    $("#departmentMeta").textContent = `${state.departments.length}개 학과`;
    $("#emptyState").classList.toggle("hidden", state.departments.length > 0);
    $(".table-wrap").classList.toggle("hidden", state.departments.length === 0);
    $("#departmentRows").innerHTML = rows.map((dept) => {
      const result = dept.lastResult;
      const overall = effectiveStatus(dept);
      const checkedAt = result?.checkedAt;
      const initials = dept.code.slice(0, 2).toUpperCase();
      const layers = ["LOCAL", "RESOURCE", "DEPLOY", "RUNTIME", "SYNC"];
      return `<tr data-code="${escapeHtml(dept.code)}">
        <td><div class="department-cell"><span class="department-avatar">${escapeHtml(initials)}</span><span><b>${escapeHtml(dept.name)}</b><small>${escapeHtml(dept.code)}</small></span></div></td>
        <td>${badge(overall)}</td>
        ${layers.map((layer) => `<td>${badge(overall === "CHECKING" ? "CHECKING" : layerStatus(result, layer))}</td>`).join("")}
        <td class="time-cell" title="${escapeHtml(checkedAt || "")}">${overall === "CHECKING" ? "확인 중" : relativeTime(checkedAt)}</td>
        <td><div class="row-actions"><button class="row-button" data-action="check" title="다시 확인" aria-label="${escapeHtml(dept.name)} 다시 확인">↻</button><button class="row-button" data-action="detail" title="상세" aria-label="${escapeHtml(dept.name)} 상세">›</button></div></td>
      </tr>`;
    }).join("");

    $$("#departmentRows tr").forEach((row) => {
      row.addEventListener("click", (event) => {
        const button = event.target.closest("button");
        const code = row.dataset.code;
        if (button?.dataset.action === "check") startStatus([code]);
        else openDrawer(code);
      });
    });
    renderSummary();
  }

  async function loadDepartments() {
    try {
      const data = await api("/api/v1/departments");
      state.departments = data.departments || [];
      renderDepartments();
      renderCorpusDepartmentOptions();
    } catch (error) {
      toast("학과 목록을 불러오지 못했습니다", error.message, "fail");
    }
  }

  function syncNumber(value) {
    const number = Number(value || 0);
    return Number.isFinite(number) ? number : 0;
  }

  function syncDuration(startTime, endTime = "") {
    const started = new Date(startTime).getTime();
    const finished = endTime ? new Date(endTime).getTime() : Date.now();
    if (!Number.isFinite(started) || !Number.isFinite(finished)) return "—";
    const seconds = Math.max(0, Math.round((finished - started) / 1000));
    if (seconds < 60) return `${seconds}초`;
    const minutes = Math.floor(seconds / 60);
    const remain = seconds % 60;
    if (minutes < 60) return `${minutes}분 ${remain}초`;
    return `${Math.floor(minutes / 60)}시간 ${minutes % 60}분`;
  }

  function syncStartedAt(value) {
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "—";
    return date.toLocaleString("ko-KR", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  }

  function selectedSyncTarget() {
    const code = $("#syncDepartment")?.value || "";
    return state.syncTargets.find((item) => item.code === code) || null;
  }

  function syncTargetRows(target) {
    if (!target) return '<p>학과를 선택하면 대상 범위를 표시합니다.</p>';
    const corpora = target.corpora || {};
    return `<div class="sync-target-row"><span>공유드라이브</span><b>${syncNumber(target.driveIds?.length)}개</b></div>
      <div class="sync-target-row"><span>동기화 폴더</span><b>${syncNumber(target.syncFolderIds?.length)}개</b></div>
      <div class="sync-target-row"><span>코퍼스</span><b>${corpora.staff ? (corpora.student ? "교직원 · 학생" : "단일") : "설정 확인 필요"}</b></div>`;
  }

  function renderSyncControls() {
    const select = $("#syncDepartment");
    const current = state.syncPreferredDepartment || select.value;
    select.innerHTML = '<option value="">학과 선택</option>' + state.syncTargets.map((target) =>
      `<option value="${escapeHtml(target.code)}">${escapeHtml(target.name)} · ${escapeHtml(target.code)}</option>`
    ).join("");
    if (state.syncTargets.some((item) => item.code === current)) select.value = current;
    else if (state.syncTargets.length === 1) select.value = state.syncTargets[0].code;
    state.syncPreferredDepartment = "";
    select.disabled = state.syncTargets.length === 0 || state.syncStartPending;
    const target = selectedSyncTarget();
    $("#syncTargetSummary").innerHTML = syncTargetRows(target);
    $$('[data-sync-mode]').forEach((button) => {
      button.classList.toggle("active", button.dataset.syncMode === state.syncMode);
      button.disabled = state.syncStartPending;
    });
    const start = $("#startManualSync");
    start.disabled = !target || state.syncStartPending;
    start.textContent = state.syncStartPending
      ? "Workflow 시작 중…"
      : state.syncMode === "backfill" ? "전체 다시 적재" : "변경분 동기화 실행";
  }

  function syncPhaseLabel(run) {
    const phase = String(run.progress?.phase || "");
    if (run.state === "ACTIVE" && phase === "COMPLETE") return "최종 정합성 확인";
    return ({
      LISTING: "Drive 파일 조사 중",
      INGESTING: "파일 변환·업로드 중",
      INDEXING: "RAG 코퍼스 색인 중",
      COMPLETE: "Drive 처리 완료",
      FAILED: "처리 실패",
    })[phase] || (run.effectiveMode === "backfill" ? "Workflow 준비 중" : "변경분 동기화 중");
  }

  function renderSyncActiveRun() {
    const run = state.syncRuns.find((item) => item.state === "ACTIVE");
    $("#syncActiveEmpty").classList.toggle("hidden", Boolean(run));
    const content = $("#syncActiveContent");
    content.classList.toggle("hidden", !run);
    if (!run) {
      content.innerHTML = "";
      return;
    }
    const progress = run.progress || {};
    const totals = Object.keys(progress.totals || {}).length ? progress.totals : (run.totals || {});
    const listed = syncNumber(totals.listed);
    const processed = syncNumber(progress.processed);
    const indexed = syncNumber(totals.indexed);
    const uploaded = syncNumber(totals.gcsUploaded);
    const failures = syncNumber(totals.failed) + syncNumber(totals.indexFailed);
    const percent = listed > 0 ? Math.min(99, Math.round((processed / listed) * 100)) : 0;
    const hasMeasuredProgress = run.effectiveMode === "backfill" && listed > 0;
    const modeLabel = run.mode === "delta" && run.effectiveMode === "backfill"
      ? "초기 전체 적재로 자동 전환"
      : run.effectiveMode === "backfill" ? "전체 다시 적재" : "변경분 동기화";
    const drivePosition = progress.driveCount
      ? `Drive ${syncNumber(progress.driveIndex)} / ${syncNumber(progress.driveCount)}`
      : `${syncNumber(run.driveIds?.length)}개 Drive`;
    content.innerHTML = `<div class="sync-run-heading">
      <div><b>${escapeHtml(run.departmentName || run.departmentCode || "전체 동기화")}</b><small>${escapeHtml(modeLabel)} · ${escapeHtml(drivePosition)}</small></div>
      <span class="sync-run-state">실행 중</span>
    </div>
    <div class="sync-progress-copy"><div><b>${escapeHtml(syncPhaseLabel(run))}</b><span>${hasMeasuredProgress ? `${processed.toLocaleString()} / ${listed.toLocaleString()}개 처리` : "진행 상태를 확인하고 있습니다."}</span></div><strong>${hasMeasuredProgress ? `${percent}%` : "LIVE"}</strong></div>
    <div class="sync-progress-track${hasMeasuredProgress ? "" : " indeterminate"}"><span style="width:${hasMeasuredProgress ? percent : 34}%"></span></div>
    <div class="sync-metric-grid">
      <div class="sync-metric"><span>처리</span><b>${processed.toLocaleString()}</b></div>
      <div class="sync-metric"><span>GCS 업로드</span><b>${uploaded.toLocaleString()}</b></div>
      <div class="sync-metric"><span>색인</span><b>${indexed.toLocaleString()}</b></div>
      <div class="sync-metric fail"><span>실패</span><b>${failures.toLocaleString()}</b></div>
    </div>
    <div class="sync-run-foot"><code>${escapeHtml(run.executionId || run.runId)}</code><span>경과 ${escapeHtml(syncDuration(run.startTime))}</span></div>`;
  }

  function renderSyncHistory() {
    const rows = state.syncRuns;
    $("#syncHistoryMeta").textContent = `${rows.length}개 실행 · ${rows.filter((item) => item.state === "ACTIVE").length}개 진행 중`;
    $("#syncHistoryRows").innerHTML = rows.length ? rows.map((run) => {
      const totals = Object.keys(run.totals || {}).length ? run.totals : (run.progress?.totals || {});
      const processed = syncNumber(run.progress?.processed) || syncNumber(totals.listed);
      const failed = syncNumber(totals.failed) + syncNumber(totals.indexFailed);
      const stateLabel = ({ ACTIVE: "진행 중", SUCCEEDED: "완료", FAILED: "실패", CANCELLED: "취소" })[run.state] || run.state;
      return `<tr>
        <td><div class="department-cell"><span class="department-avatar">${escapeHtml((run.departmentCode || "WF").slice(0, 2).toUpperCase())}</span><span><b>${escapeHtml(run.departmentName || run.departmentCode || "자동 실행")}</b><small>${escapeHtml(run.executionId)}</small></span></div></td>
        <td>${run.mode === "delta" && run.effectiveMode === "backfill" ? "변경분 → 전체" : run.effectiveMode === "backfill" ? "전체 적재" : "변경분"}</td>
        <td><span class="sync-history-state ${escapeHtml(run.state.toLowerCase())}">${escapeHtml(stateLabel)}</span></td>
        <td>${processed.toLocaleString()}</td><td>${syncNumber(totals.indexed).toLocaleString()}</td><td>${failed.toLocaleString()}</td>
        <td>${escapeHtml(syncStartedAt(run.startTime))}</td><td>${escapeHtml(syncDuration(run.startTime, run.endTime))}</td>
      </tr>`;
    }).join("") : '<tr><td colspan="8" class="sync-history-empty">아직 동기화 실행 이력이 없습니다.</td></tr>';
  }

  function renderSyncManagement() {
    renderSyncControls();
    renderSyncActiveRun();
    renderSyncHistory();
  }

  async function loadSyncRuns(quiet = false) {
    window.clearTimeout(state.syncPollTimer);
    const requestId = ++state.syncRequest;
    const refresh = $("#refreshSyncRuns");
    if (!quiet) {
      refresh.disabled = true;
      refresh.textContent = "조회 중…";
    }
    try {
      const data = await api("/api/v1/sync-runs?limit=20");
      if (requestId !== state.syncRequest) return;
      state.syncRuns = data.runs || [];
      state.syncTargets = data.departments || [];
      renderSyncManagement();
      if (state.syncRuns.some((item) => item.state === "ACTIVE")) {
        state.syncPollTimer = window.setTimeout(() => loadSyncRuns(true), 2000);
      }
    } catch (error) {
      if (requestId !== state.syncRequest) return;
      $("#syncHistoryMeta").textContent = "조회 실패";
      $("#syncHistoryRows").innerHTML = `<tr><td colspan="8" class="sync-history-empty">${escapeHtml(error.message)}</td></tr>`;
      if (!quiet) toast("동기화 이력을 불러오지 못했습니다", error.message, "fail");
    } finally {
      if (!quiet && requestId === state.syncRequest) {
        refresh.disabled = false;
        refresh.textContent = "실행 이력 새로고침";
      }
    }
  }

  function closeSyncConfirmModal() {
    $("#syncConfirmModal").classList.remove("open");
    $("#syncConfirmModal").setAttribute("aria-hidden", "true");
    $("#syncStartError").classList.add("hidden");
  }

  function requestManualSync() {
    const target = selectedSyncTarget();
    if (!target || state.syncStartPending) return;
    if (state.syncMode === "delta") {
      submitManualSync();
      return;
    }
    $("#syncConfirmTarget").innerHTML = `<div class="sync-target-row"><span>학과</span><b>${escapeHtml(target.name)} · ${escapeHtml(target.code)}</b></div>${syncTargetRows(target)}`;
    $("#syncConfirmModal").classList.add("open");
    $("#syncConfirmModal").setAttribute("aria-hidden", "false");
    $("#confirmManualSync").focus();
  }

  async function submitManualSync() {
    const target = selectedSyncTarget();
    if (!target || state.syncStartPending) return;
    state.syncStartPending = true;
    renderSyncControls();
    const confirm = $("#confirmManualSync");
    confirm.disabled = true;
    confirm.textContent = "Workflow 시작 중…";
    try {
      const run = await api("/api/v1/sync-runs", {
        method: "POST",
        body: { departmentCode: target.code, mode: state.syncMode },
      });
      closeSyncConfirmModal();
      toast(
        state.syncMode === "backfill" ? "전체 다시 적재를 시작했습니다" : "변경분 동기화를 시작했습니다",
        `${target.name} · ${run.executionId || "Workflow 실행"}`,
        "ok",
      );
      await loadSyncRuns(true);
    } catch (error) {
      const banner = $("#syncStartError");
      if ($("#syncConfirmModal").classList.contains("open")) {
        banner.textContent = error.message;
        banner.classList.remove("hidden");
      } else {
        toast("동기화를 시작하지 못했습니다", error.message, "fail");
      }
    } finally {
      state.syncStartPending = false;
      confirm.disabled = false;
      confirm.textContent = "전체 다시 적재 시작";
      renderSyncControls();
    }
  }

  function openSyncManagement(code = "") {
    state.syncPreferredDepartment = code;
    switchView("sync");
  }

  function renderEnvironment(env) {
    const projectMatch = env.gcloudProject && env.gcloudProject === env.configuredProject;
    const cards = [
      { icon: "⌂", label: "저장소", value: env.repository, detail: `${env.departmentCount}개 학과 설정` },
      { icon: "G", label: "GCP 프로젝트", value: env.configuredProject || "미설정", detail: env.region },
      { icon: "SA", label: "Drive 확인 서비스 계정", value: env.serviceAccount || "확인 필요", detail: "공유 드라이브 연결에 사용", copy: env.serviceAccount, check: true },
      { icon: "›_", label: "gcloud", value: env.gcloudInstalled ? (env.gcloudAuthenticated ? "로그인됨" : "로그인 필요") : "설치되지 않음", detail: env.gcloudAccount || "활성 계정 없음" },
      { icon: "Py", label: "Python", value: env.pythonVersion, detail: "로컬 API 런타임" },
      { icon: "↔", label: "프로젝트 일치", value: projectMatch ? "일치" : "확인 필요", detail: env.gcloudProject || "gcloud 프로젝트 없음" },
      { icon: "●", label: "서버 경계", value: "127.0.0.1", detail: "외부 네트워크 비공개" },
    ];
    $("#environmentGrid").innerHTML = cards.map((card) => `
      <article class="environment-card">
        <span class="env-icon">${escapeHtml(card.icon)}</span>
        <div>
          <span>${escapeHtml(card.label)}</span>
          <div class="environment-value-row">
            <b>${escapeHtml(card.value)}</b>
            ${card.copy ? `<button type="button" class="copy-value-button" data-copy-value="${escapeHtml(card.copy)}" aria-label="서비스 계정 복사">복사</button>` : ""}
            ${card.check ? `<button type="button" class="copy-value-button" id="checkDriveSa">상태 확인</button>` : ""}
          </div>
          <small>${escapeHtml(card.detail)}</small>
        </div>
      </article>`).join("");
  }

  const DRIVE_SA_ISSUE_LABELS = {
    gcloudAuth: "gcloud 로그인이 필요합니다",
    projectNumber: "프로젝트 번호를 확인하지 못했습니다",
    computeApi: "Compute Engine API 가 꺼져 있어 기본 서비스 계정이 없습니다",
    iamCredentialsApi: "IAM Credentials API 가 꺼져 있습니다",
    serviceAccountMissing: "기본 Compute 서비스 계정을 찾지 못했습니다",
    tokenCreator: "현재 계정에 이 서비스 계정을 가장할 권한이 없습니다",
  };

  function openDriveSaModal() {
    const modal = $("#driveSaModal");
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
  }

  function closeDriveSaModal() {
    window.clearTimeout(state.driveSaPollTimer);
    const modal = $("#driveSaModal");
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
    state.driveSaPlan = null;
    $("#driveSaError").classList.add("hidden");
    $("#confirmDriveSaRepair").classList.add("hidden");
    $("#closeDriveSa").textContent = "취소";
  }

  function renderDriveSaStatus(status) {
    $("#driveSaProject").textContent = status.projectId || "—";
    $("#driveSaAccount").textContent = status.account || "—";
    const ok = status.status === "OK";
    $("#driveSaModalDescription").textContent = ok
      ? "공유 드라이브 연결에 바로 쓸 수 있습니다."
      : "아래 문제로 공유 드라이브 확인이 막혀 있습니다.";
    const issues = (status.issues || [])
      .map((key) => `<li data-tone="fail"><b>${escapeHtml(DRIVE_SA_ISSUE_LABELS[key] || key)}</b></li>`)
      .join("");
    $("#driveSaBody").innerHTML = `
      <ul class="setup-provision-list">
        <li data-tone="${ok ? "ok" : "fail"}">
          <b>${escapeHtml(status.serviceAccount || "서비스 계정 미확인")}</b>
          <span>${escapeHtml(ok ? "정상" : "확인 필요")}</span>
          ${status.detail ? `<small>${escapeHtml(status.detail)}</small>` : ""}
        </li>
      </ul>
      ${issues ? `<h5 class="drive-sa-heading">확인된 문제</h5><ul class="setup-provision-list">${issues}</ul>` : ""}`;
  }

  function renderDriveSaPlan(plan) {
    const steps = (plan.steps || [])
      .map((item) => {
        const label = item.status
          ? { PENDING: "대기 중", RUNNING: "진행 중", COMPLETE: "완료", FAILED: "실패" }[item.status] || item.status
          : "실행 예정";
        const tone = item.status === "FAILED" ? "fail" : (item.status === "COMPLETE" ? "ok" : "warn");
        return `<li data-tone="${tone}">
          <b>${escapeHtml(item.label)}</b><span>${escapeHtml(label)}</span>
          <small>${escapeHtml(item.target || "")}${item.role ? ` · ${escapeHtml(item.role)}` : ""}</small>
          ${item.detail ? `<small>${escapeHtml(item.detail)}</small>` : ""}
        </li>`;
      })
      .join("");
    const verified = plan.verification && plan.verification.status;
    $("#driveSaBody").innerHTML = `
      <h5 class="drive-sa-heading">다음 조치를 진행합니다</h5>
      <ul class="setup-provision-list">${steps}</ul>
      ${verified ? `<h5 class="drive-sa-heading">조치 후 재확인</h5>
        <ul class="setup-provision-list">
          <li data-tone="${verified === "OK" ? "ok" : "fail"}">
            <b>${escapeHtml(verified === "OK" ? "가장 토큰 발급 확인됨" : "여전히 확인 필요")}</b>
            <small>${escapeHtml(plan.verification.detail || "")}</small>
          </li>
        </ul>` : ""}`;
  }

  async function checkDriveServiceAccount() {
    const button = $("#checkDriveSa");
    if (button) { button.disabled = true; button.textContent = "확인 중…"; }
    openDriveSaModal();
    $("#driveSaBody").innerHTML = '<p class="drive-sa-loading">서비스 계정 가장 토큰을 발급해 보는 중입니다…</p>';
    $("#driveSaError").classList.add("hidden");
    $("#confirmDriveSaRepair").classList.add("hidden");
    try {
      const status = await api("/api/v1/drive-service-account/status");
      state.driveSaStatus = status;
      renderDriveSaStatus(status);
      if (status.status !== "OK") {
        // 자동 조치가 가능한 문제일 때만 확인 버튼을 연다.
        const fixable = (status.issues || []).some((key) =>
          ["computeApi", "iamCredentialsApi", "serviceAccountMissing", "tokenCreator"].includes(key));
        if (fixable) {
          $("#confirmDriveSaRepair").classList.remove("hidden");
          $("#confirmDriveSaRepair").disabled = false;
          $("#confirmDriveSaRepair").textContent = "조치 내용 확인";
        }
      }
    } catch (error) {
      const banner = $("#driveSaError");
      banner.textContent = error.message;
      banner.classList.remove("hidden");
    } finally {
      if (button) { button.disabled = false; button.textContent = "상태 확인"; }
    }
  }

  async function planDriveSaRepair() {
    const button = $("#confirmDriveSaRepair");
    button.disabled = true;
    button.textContent = "확인 중…";
    try {
      const plan = await api("/api/v1/drive-service-account/repair-plans", { method: "POST" });
      state.driveSaPlan = plan;
      renderDriveSaPlan(plan);
      $("#driveSaModalDescription").textContent = "아래 조치를 진행할까요? 확인을 누르면 실제로 적용됩니다.";
      button.disabled = false;
      button.textContent = "확인";
    } catch (error) {
      const banner = $("#driveSaError");
      banner.textContent = error.message;
      banner.classList.remove("hidden");
      button.classList.add("hidden");
    }
  }

  async function pollDriveSaRepair(runId) {
    window.clearTimeout(state.driveSaPollTimer);
    try {
      const run = await api(`/api/v1/drive-service-account/repairs/${runId}`);
      renderDriveSaPlan(run);
      if (run.status === "RUNNING") {
        state.driveSaPollTimer = window.setTimeout(() => pollDriveSaRepair(runId), 1500);
        return;
      }
      const ok = run.status === "COMPLETED";
      $("#driveSaModalDescription").textContent = ok
        ? "조치가 끝났고 서비스 계정이 정상입니다."
        : "일부 조치가 끝나지 않았습니다. 내용을 확인해 주세요.";
      $("#confirmDriveSaRepair").classList.add("hidden");
      $("#closeDriveSa").textContent = "닫기";
      toast(
        ok ? "서비스 계정을 사용할 수 있습니다" : "서비스 계정 조치 미완료",
        ok ? "공유 드라이브 확인을 다시 시도해 보세요." : "권한이 없으면 프로젝트 관리자에게 요청해야 합니다.",
        ok ? "ok" : "fail",
      );
      await loadEnvironment();
    } catch (error) {
      const banner = $("#driveSaError");
      banner.textContent = error.message;
      banner.classList.remove("hidden");
    }
  }

  async function confirmDriveSaAction() {
    // 1단계: 계획 보기 → 2단계: 실제 적용
    if (!state.driveSaPlan) {
      await planDriveSaRepair();
      return;
    }
    const button = $("#confirmDriveSaRepair");
    button.disabled = true;
    button.textContent = "진행 중…";
    try {
      const run = await api("/api/v1/drive-service-account/repairs", {
        method: "POST",
        body: { planId: state.driveSaPlan.planId },
      });
      renderDriveSaPlan(run);
      pollDriveSaRepair(run.runId);
    } catch (error) {
      const banner = $("#driveSaError");
      banner.textContent = error.message;
      banner.classList.remove("hidden");
      // 계획은 1회용이라 실패하면 처음부터 다시 확인해야 한다.
      state.driveSaPlan = null;
      button.disabled = false;
      button.textContent = "조치 내용 확인";
    }
  }

  async function copyEnvironmentValue(event) {
    const button = event.target.closest("[data-copy-value]");
    if (!button) return;
    const value = button.dataset.copyValue || "";
    try {
      await navigator.clipboard.writeText(value);
      button.textContent = "복사됨";
      button.classList.add("is-copied");
      window.setTimeout(() => {
        button.textContent = "복사";
        button.classList.remove("is-copied");
      }, 1400);
    } catch (_) {
      toast("복사하지 못했습니다", "서비스 계정 주소를 직접 선택해 복사해 주세요.", "fail");
    }
  }

  async function loadEnvironment() {
    try {
      const env = await api("/api/v1/environment");
      state.environment = env;
      $("#projectName").textContent = env.configuredProject || "프로젝트 미설정";
      $("#regionName").textContent = env.region || "region";
      $("#formProject").textContent = env.configuredProject || "—";
      $("#formRegion").textContent = env.region || "—";
      renderEnvironment(env);
      return env;
    } catch (error) {
      toast("실행 환경을 읽지 못했습니다", error.message, "fail");
      return null;
    }
  }

  function commonSetupPayload() {
    const form = $("#commonSetupForm");
    const value = (name) => form.elements[name]?.value?.trim() || "";
    return {
      projectId: value("projectId"),
      region: value("region"),
      artifactRepo: value("artifactRepo"),
      firestoreDatabase: value("firestoreDatabase"),
    };
  }

  function clearSetupErrors() {
    $$('[data-setup-error]').forEach((item) => { item.textContent = ""; });
    $$("#commonSetupForm input, #commonSetupForm select").forEach((item) => item.classList.remove("invalid"));
    $("#setupValidationBanner").classList.add("hidden");
  }

  function showSetupErrors(errors = {}) {
    clearSetupErrors();
    let first = null;
    Object.entries(errors).forEach(([field, messages]) => {
      const label = $(`[data-setup-error="${CSS.escape(field)}"]`);
      const input = $("#commonSetupForm").elements[field];
      if (label) label.textContent = messages.join(" ");
      input?.classList.add("invalid");
      if (!first) first = input;
    });
    first?.focus();
  }

  function renderCommonBootstrap(env) {
    const form = $("#commonSetupForm");
    const authBox = $("#setupAuth");
    const projects = env?.gcloudProjects || [];
    const ready = Boolean(env?.gcloudInstalled && env?.gcloudAuthenticated && projects.length);
    authBox.dataset.status = ready ? "ready" : "required";

    if (!env?.gcloudInstalled) {
      $("#setupAuthTitle").textContent = "gcloud 설치 필요";
      $("#setupAuthDetail").textContent = "Google Cloud CLI를 설치한 뒤 서버를 다시 시작해 주세요.";
    } else if (!env?.gcloudAuthenticated) {
      $("#setupAuthTitle").textContent = "gcloud 로그인이 필요합니다";
      $("#setupAuthDetail").textContent = "터미널에서 gcloud auth login 실행 후 ‘다시 확인’을 눌러 주세요.";
    } else if (!projects.length) {
      $("#setupAuthTitle").textContent = "접근 가능한 프로젝트가 없습니다";
      $("#setupAuthDetail").textContent = `${env.gcloudAccount || "현재 계정"}의 프로젝트 권한을 확인해 주세요.`;
    } else {
      $("#setupAuthTitle").textContent = "gcloud 로그인 확인됨";
      $("#setupAuthDetail").textContent = `${env.gcloudAccount} · 접근 가능한 프로젝트 ${projects.length}개`;
    }

    form.elements.projectId.innerHTML = projects.length
      ? projects.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.id)}</option>`).join("")
      : '<option value="">로그인 후 프로젝트를 선택할 수 있습니다</option>';
    form.elements.projectId.disabled = !ready;
    if (ready && projects.some((item) => item.id === env.gcloudProject)) form.elements.projectId.value = env.gcloudProject;

    const regions = env?.availableRegions || [];
    form.elements.region.innerHTML = regions.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)} · ${escapeHtml(item.id)}</option>`).join("");
    form.elements.region.disabled = !ready;
    if (regions.some((item) => item.id === "asia-northeast3")) form.elements.region.value = "asia-northeast3";
    form.elements.artifactRepo.disabled = true;
    form.elements.firestoreDatabase.disabled = true;
    form.elements.artifactRepo.innerHTML = '<option value="">기존 리소스 조회 대기 중</option>';
    form.elements.firestoreDatabase.innerHTML = '<option value="">기존 리소스 조회 대기 중</option>';
    $("#setupProvisionCallout").classList.add("hidden");
    $("#setupProvisionPlan").classList.add("hidden");
    $("#setupResourceStatus").textContent = ready ? "기존 리소스를 불러오는 중입니다." : "gcloud 로그인 후 기존 리소스를 불러옵니다.";
    $("#setupResourceStatus").dataset.status = "";
    $("#createCommonConfig").disabled = true;
    const authButton = $("#refreshGcloudAuth");
    authButton.disabled = !env?.gcloudInstalled;
    authButton.textContent = state.authLoginPending
      ? "로그인 다시 열기"
      : (env?.gcloudAuthenticated ? "다시 확인" : "gcloud 로그인");
    return ready;
  }

  function renderCommonProvisionPlan(plan) {
    const services = (plan.services || []).map((item) => {
      const label = item.status
        ? { PENDING: "대기 중", RUNNING: "활성화 중", COMPLETE: "활성화 완료", SKIPPED: "이미 켜짐", FAILED: "실패" }[item.status] || item.status
        : (item.known ? (item.enabled ? "이미 켜져 있음" : "켜야 함") : "확인 필요");
      const tone = item.status === "FAILED" ? "fail" : (item.status === "COMPLETE" || item.enabled ? "ok" : "warn");
      return `<li data-tone="${tone}"><b>${escapeHtml(item.name)}</b><span>${escapeHtml(label)}</span>${item.detail ? `<small>${escapeHtml(item.detail)}</small>` : ""}</li>`;
    }).join("");
    const resources = (plan.resources || []).map((item) => {
      const label = item.status
        ? { PENDING: "대기 중", RUNNING: "생성 중", COMPLETE: "생성 완료", SKIPPED: "이미 있음", FAILED: "실패" }[item.status] || item.status
        : (item.exists ? "이미 있음 · 건너뜀" : "새로 만듦");
      const tone = item.status === "FAILED" ? "fail" : (item.status === "COMPLETE" || item.exists ? "ok" : "warn");
      return `<li data-tone="${tone}"><b>${escapeHtml(item.label)}</b><span>${escapeHtml(item.displayName)} · ${escapeHtml(label)}</span>${item.warning && !item.exists ? `<small class="warn">${escapeHtml(item.warning)}</small>` : ""}${item.detail ? `<small>${escapeHtml(item.detail)}</small>` : ""}</li>`;
    }).join("");
    $("#setupProvisionPlanBody").innerHTML = `
      <p class="setup-provision-scope">${escapeHtml(plan.projectId || "")} · ${escapeHtml(plan.region || "")}</p>
      <h5>켤 API</h5><ul class="setup-provision-list">${services}</ul>
      <h5>만들 리소스</h5><ul class="setup-provision-list">${resources}</ul>`;
  }

  async function planCommonResources() {
    const button = $("#planCommonResources");
    const form = $("#commonSetupForm");
    button.disabled = true;
    button.textContent = "확인 중…";
    try {
      const plan = await api("/api/v1/common-config/resource-plans", {
        method: "POST",
        body: {
          projectId: form.elements.projectId.value,
          region: form.elements.region.value,
        },
      });
      state.commonResourcePlan = plan;
      renderCommonProvisionPlan(plan);
      $("#setupProvisionPlan").classList.remove("hidden");
      $("#setupProvisionCallout").classList.add("hidden");
      $("#confirmCommonProvision").disabled = false;
      $("#confirmCommonProvision").textContent = "확인하고 진행";
      $("#cancelCommonProvision").classList.remove("hidden");
    } catch (error) {
      toast("생성 계획을 만들지 못했습니다", error.message, "fail");
    } finally {
      button.disabled = false;
      button.textContent = "공통 리소스 만들기";
    }
  }

  async function pollCommonProvisionRun(runId) {
    window.clearTimeout(state.commonProvisionPollTimer);
    try {
      const run = await api(`/api/v1/common-config/resource-provisioning/${runId}`);
      renderCommonProvisionPlan(run);
      if (run.status === "RUNNING") {
        state.commonProvisionPollTimer = window.setTimeout(() => pollCommonProvisionRun(runId), 1500);
        return;
      }
      const failed = (run.resources || []).filter((item) => item.status === "FAILED").length;
      $("#confirmCommonProvision").classList.add("hidden");
      $("#cancelCommonProvision").textContent = "닫기";
      $("#cancelCommonProvision").classList.remove("hidden");
      toast(
        failed ? "일부 공통 리소스 생성 실패" : "공통 리소스를 준비했습니다",
        failed ? "실패 항목을 확인한 뒤 다시 시도해 주세요." : "이제 설정을 저장할 수 있습니다.",
        failed ? "fail" : "ok",
      );
      await loadSetupResources();
    } catch (error) {
      toast("생성 상태를 읽지 못했습니다", error.message, "fail");
      $("#confirmCommonProvision").disabled = false;
      $("#confirmCommonProvision").textContent = "다시 확인";
    }
  }

  async function confirmCommonProvision() {
    if (!state.commonResourcePlan) return;
    const button = $("#confirmCommonProvision");
    button.disabled = true;
    button.textContent = "진행 중…";
    try {
      const run = await api("/api/v1/common-config/resource-provisioning", {
        method: "POST",
        body: { planId: state.commonResourcePlan.planId },
      });
      renderCommonProvisionPlan(run);
      pollCommonProvisionRun(run.runId);
    } catch (error) {
      toast("리소스 생성을 시작하지 못했습니다", error.message, "fail");
      button.disabled = false;
      button.textContent = "확인하고 진행";
      // 계획은 1회용이라 실패하면 다시 떠야 한다.
      state.commonResourcePlan = null;
      $("#setupProvisionPlan").classList.add("hidden");
      $("#setupProvisionCallout").classList.remove("hidden");
    }
  }

  function cancelCommonProvision() {
    window.clearTimeout(state.commonProvisionPollTimer);
    state.commonResourcePlan = null;
    $("#setupProvisionPlan").classList.add("hidden");
    $("#confirmCommonProvision").classList.remove("hidden");
    $("#confirmCommonProvision").disabled = false;
    $("#confirmCommonProvision").textContent = "확인하고 진행";
    $("#cancelCommonProvision").textContent = "취소";
    loadSetupResources();
  }

  async function loadSetupResources() {
    const form = $("#commonSetupForm");
    const project = form.elements.projectId.value;
    const region = form.elements.region.value;
    const status = $("#setupResourceStatus");
    const requestId = ++state.setupResourceRequest;
    form.elements.artifactRepo.disabled = true;
    form.elements.firestoreDatabase.disabled = true;
    form.elements.artifactRepo.innerHTML = '<option value="">조회 중…</option>';
    form.elements.firestoreDatabase.innerHTML = '<option value="">조회 중…</option>';
    $("#createCommonConfig").disabled = true;
    status.dataset.status = "";
    status.textContent = "선택한 프로젝트의 기존 리소스를 조회하고 있습니다.";
    if (!project || !region) return;

    try {
      const resources = await api(`/api/v1/common-config/resources?project=${encodeURIComponent(project)}&region=${encodeURIComponent(region)}`);
      if (requestId !== state.setupResourceRequest) return;
      const repositories = resources.artifactRepositories || [];
      const databases = resources.firestoreDatabases || [];
      form.elements.artifactRepo.innerHTML = repositories.length
        ? repositories.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.id)} · ${escapeHtml(item.format)}</option>`).join("")
        : '<option value="">Docker 저장소 없음</option>';
      form.elements.firestoreDatabase.innerHTML = databases.length
        ? databases.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.id)}${item.location ? ` · ${escapeHtml(item.location)}` : ""}</option>`).join("")
        : '<option value="">Native Firestore DB 없음</option>';
      form.elements.artifactRepo.disabled = !repositories.length;
      form.elements.firestoreDatabase.disabled = !databases.length;
      if (repositories.some((item) => item.id === "rag-mcp")) form.elements.artifactRepo.value = "rag-mcp";
      if (databases.some((item) => item.id === "rag-sync-state")) form.elements.firestoreDatabase.value = "rag-sync-state";
      const ready = Boolean(repositories.length && databases.length);
      status.dataset.status = ready ? "ready" : "error";
      status.textContent = ready
        ? `Docker 저장소 ${repositories.length}개 · Firestore DB ${databases.length}개 확인됨`
        : "필요한 리소스가 없습니다. 아래에서 바로 만들 수 있습니다.";
      $("#createCommonConfig").disabled = !ready;
      // 없을 때만 생성 안내를 띄운다 — 있으면 굳이 또 만들 이유가 없다.
      $("#setupProvisionCallout").classList.toggle("hidden", ready);
    } catch (error) {
      if (requestId !== state.setupResourceRequest) return;
      status.dataset.status = "error";
      status.textContent = `리소스를 조회하지 못했습니다: ${error.message}`;
    }
  }

  function showCommonSetupShell() {
    // gcloud 응답 전 껍데기. 여기서 보이는 값은 전부 "확인 중" 이어야 한다 —
    // 비어 있는 채로 두면 로그인이 안 된 것처럼 읽힌다.
    const gate = $("#setupGate");
    if (!gate.classList.contains("hidden")) return;
    gate.classList.remove("hidden");
    $("#setupAuth").dataset.status = "checking";
    $("#setupAuthTitle").textContent = "gcloud 로그인 확인 중";
    $("#setupAuthDetail").textContent = "활성 계정과 접근 가능한 프로젝트를 확인합니다.";
    $("#refreshGcloudAuth").disabled = true;
    $("#createCommonConfig").disabled = true;
  }

  function showCommonSetup(env) {
    const ready = renderCommonBootstrap(env);
    $("#setupGate").classList.remove("hidden");
    window.setTimeout(() => $("#commonSetupForm").elements.projectId.focus(), 100);
    if (ready) loadSetupResources();
  }

  async function pollGcloudLogin(attempt = 0) {
    const env = await loadEnvironment();
    if (env?.gcloudAuthenticated) {
      state.authLoginPending = false;
      window.clearTimeout(state.authPollTimer);
      const ready = renderCommonBootstrap(env);
      if (ready) await loadSetupResources();
      toast("gcloud 로그인을 확인했습니다", env.gcloudAccount || "활성 계정", "ok");
      return;
    }
    if (attempt >= 90) {
      state.authLoginPending = false;
      renderCommonBootstrap(env);
      $("#setupAuthTitle").textContent = "로그인 확인 시간이 초과됐습니다";
      $("#setupAuthDetail").textContent = "로그인을 완료한 뒤 ‘gcloud 로그인’을 다시 눌러 주세요.";
      return;
    }
    state.authPollTimer = window.setTimeout(() => pollGcloudLogin(attempt + 1), 2000);
  }

  async function refreshGcloudSetup() {
    const button = $("#refreshGcloudAuth");
    window.clearTimeout(state.authPollTimer);
    button.disabled = true;
    button.textContent = "확인 중…";
    $("#setupAuth").dataset.status = "checking";
    try {
      const current = state.environment || await loadEnvironment();
      if (current?.gcloudInstalled && !current.gcloudAuthenticated) {
        await api("/api/v1/gcloud-auth/login", { method: "POST" });
        state.authLoginPending = true;
        $("#setupAuth").dataset.status = "required";
        $("#setupAuthTitle").textContent = "브라우저에서 Google 계정에 로그인하세요";
        $("#setupAuthDetail").textContent = "로그인이 완료되면 프로젝트 목록을 자동으로 불러옵니다.";
        button.disabled = false;
        button.textContent = "로그인 다시 열기";
        pollGcloudLogin();
        return;
      }
      const env = await loadEnvironment();
      if (env && renderCommonBootstrap(env)) await loadSetupResources();
    } catch (error) {
      state.authLoginPending = false;
      button.disabled = false;
      button.textContent = "gcloud 로그인";
      $("#setupAuth").dataset.status = "required";
      $("#setupAuthTitle").textContent = "gcloud 로그인을 시작하지 못했습니다";
      $("#setupAuthDetail").textContent = error.message;
    }
  }

  async function submitCommonSetup(event) {
    event.preventDefault();
    clearSetupErrors();
    const button = $("#createCommonConfig");
    button.disabled = true;
    button.textContent = "설정 저장 중…";
    try {
      const result = await api("/api/v1/common-config", { method: "POST", body: commonSetupPayload() });
      $("#setupGate").classList.add("hidden");
      toast("공통 설정을 생성했습니다", result.path, "ok");
      await Promise.all([loadEnvironment(), loadDepartments()]);
      switchView("dashboard");
    } catch (error) {
      const errors = error.data?.error?.fieldErrors;
      if (errors) showSetupErrors(errors);
      const banner = $("#setupValidationBanner");
      banner.textContent = error.message;
      banner.classList.remove("hidden");
    } finally {
      const form = $("#commonSetupForm");
      button.disabled = form.elements.artifactRepo.disabled || form.elements.firestoreDatabase.disabled;
      button.textContent = "설정 저장하고 시작";
    }
  }

  function runResultToDepartments(results) {
    results.forEach((result) => {
      const dept = state.departments.find((item) => item.code === result.code);
      if (dept) {
        if (dept.lastResult?.checkedAt !== result.checkedAt) state.mcpServers.delete(result.code);
        dept.lastResult = result;
        dept.lastStatus = result.overall;
      }
      state.checkingCodes.delete(result.code);
    });
    renderDepartments();
    if (state.selectedCode) openDrawer(state.selectedCode, false);
  }

  async function pollRun(runId) {
    window.clearTimeout(state.pollTimer);
    try {
      const run = await api(`/api/v1/status-runs/${runId}`);
      runResultToDepartments(run.departments || []);
      const completed = (run.departments || []).length;
      $("#runProgress").textContent = run.currentDepartment
        ? `${run.currentDepartment} 확인 중 · ${completed}/${run.scope.length}`
        : `${completed}/${run.scope.length}`;
      if (run.status === "RUNNING") {
        state.pollTimer = window.setTimeout(() => pollRun(runId), 700);
        return;
      }
      state.activeRunId = null;
      state.checkingCodes.clear();
      $("#runStrip").classList.add("hidden");
      $("#lastChecked").textContent = `마지막 확인 ${new Date().toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" })}`;
      renderDepartments();
      toast(
        run.status === "COMPLETED" ? "상태 확인 완료" : "상태 확인 중단",
        `${completed}개 학과 결과를 반영했습니다.`,
        run.status === "FAILED" ? "fail" : "ok",
      );
      await loadDepartments();
    } catch (error) {
      state.activeRunId = null;
      state.checkingCodes.clear();
      $("#runStrip").classList.add("hidden");
      renderDepartments();
      toast("상태 확인에 실패했습니다", error.message, "fail");
    }
  }

  async function startStatus(codes = []) {
    if (!state.nonce || state.activeRunId) return;
    const scope = codes.length ? codes : state.departments.map((item) => item.code);
    if (!scope.length) {
      toast("확인할 학과가 없습니다", "먼저 학과 설정을 추가해 주세요.");
      return;
    }
    scope.forEach((code) => state.checkingCodes.add(code));
    renderDepartments();
    $("#runStrip").classList.remove("hidden");
    $("#runProgress").textContent = `${scope.length}개 학과 준비 중`;
    try {
      const run = await api("/api/v1/status-runs", {
        method: "POST",
        body: { departments: codes, offline: false, strict: false },
      });
      state.activeRunId = run.runId;
      pollRun(run.runId);
    } catch (error) {
      state.checkingCodes.clear();
      $("#runStrip").classList.add("hidden");
      renderDepartments();
      toast("상태 확인을 시작하지 못했습니다", error.message, "fail");
    }
  }

  function openDrawer(code, announce = true) {
    const dept = state.departments.find((item) => item.code === code);
    if (!dept) return;
    state.selectedCode = code;
    $("#drawerTitle").textContent = dept.name;
    $("#drawerPath").textContent = dept.path;
    const overall = effectiveStatus(dept);
    $("#drawerSummary").innerHTML = `${badge(overall)}<span class="status-badge status-UNKNOWN">${escapeHtml(code)}</span>`;
    const cachedMcp = state.mcpServers.get(code);
    renderDrawerMcpServers(code, cachedMcp || null);
    if (!cachedMcp) {
      loadDepartmentMcpServers(code)
        .then((data) => { if (state.selectedCode === code) renderDrawerMcpServers(code, data); })
        .catch((error) => { if (state.selectedCode === code) renderDrawerMcpServers(code, null, error.message); });
    }
    const result = dept.lastResult;
    if (!result?.checks?.length) {
      $("#drawerContent").innerHTML = `<div class="empty-state"><h3>아직 검사 결과가 없습니다</h3><p>이 학과의 상태를 확인하면 단계별 결과가 표시됩니다.</p></div>`;
    } else {
      $("#drawerContent").innerHTML = Object.entries(layerLabels).map(([layer, label]) => {
        const checks = result.checks.filter((item) => item.layer === layer);
        if (!checks.length) return "";
        return `<section class="check-layer"><h3>${label.toUpperCase()}</h3><div class="check-list">${checks.map((item) => `
          <article class="check-row" data-status="${escapeHtml(item.status)}">
            <span class="check-dot">${item.status === "OK" ? "✓" : item.status === "FAIL" ? "×" : item.status === "WARN" ? "!" : "–"}</span>
            <div class="check-main"><div><b>${escapeHtml(item.name)}</b><small>${item.latencyMs ? `${item.latencyMs}ms` : statusLabels[item.status]}</small></div><p>${escapeHtml(item.detail)}</p>${item.actionType === "MCP_DEPLOY" ? `<button type="button" class="check-deploy-button" data-deploy-mcp="${escapeHtml(item.departmentCode || code)}">MCP 배포</button>` : item.action ? `<div class="check-action">${escapeHtml(item.action)}</div>` : ""}</div>
          </article>`).join("")}</div></section>`;
      }).join("");
    }
    $("#drawerCheck").dataset.code = code;
    $("#drawerEdit").dataset.code = code;
    const drawer = $("#detailDrawer");
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    if (announce) window.setTimeout(() => $(".drawer-panel .icon-button").focus(), 230);
  }

  function closeDrawer() {
    state.selectedCode = null;
    $("#detailDrawer").classList.remove("open");
    $("#detailDrawer").setAttribute("aria-hidden", "true");
  }

  function splitIds(value) {
    return [...new Set(String(value || "").split(/[,\r\n]+/).map((item) => item.trim()).filter(Boolean))];
  }

  function setCodeAvailability(status, message = "") {
    const output = $("#codeAvailabilityStatus");
    output.dataset.status = status || "";
    output.textContent = message;
    state.codeAvailable = status === "ok" ? true : status === "fail" ? false : null;
    updateResourceProvisionAvailability();
  }

  async function checkDepartmentCode(immediate = false) {
    const form = $("#departmentForm");
    const code = form.elements.code.value.trim().toLowerCase();
    if (state.editingCode) {
      setCodeAvailability("ok", "현재 학과 코드입니다.");
      return true;
    }
    if (!/^[a-z][a-z0-9-]{1,19}$/.test(code)) {
      setCodeAvailability(code ? "fail" : "", code ? "코드 형식을 확인해 주세요." : "");
      return false;
    }
    const requestId = ++state.codeAvailabilityRequest;
    setCodeAvailability("checking", "학과 코드 중복을 확인하고 있습니다.");
    if (!immediate) await new Promise((resolve) => window.setTimeout(resolve, 0));
    try {
      const result = await api(`/api/v1/departments/code-availability?code=${encodeURIComponent(code)}`);
      if (requestId !== state.codeAvailabilityRequest || form.elements.code.value.trim().toLowerCase() !== code) return false;
      setCodeAvailability(result.available ? "ok" : "fail", result.reason);
      return Boolean(result.available);
    } catch (error) {
      if (requestId !== state.codeAvailabilityRequest) return false;
      setCodeAvailability("fail", error.message);
      return false;
    }
  }

  function scheduleDepartmentCodeCheck() {
    window.clearTimeout(state.codeAvailabilityTimer);
    state.codeAvailabilityRequest += 1;
    setCodeAvailability("", "");
    state.codeAvailabilityTimer = window.setTimeout(() => checkDepartmentCode(), 320);
  }

  const provisionFieldNames = {
    bucketHwp: "hwpBucket",
    bucketSource: "sourceBucket",
    corpusStaff: "staffCorpus",
    corpusStudent: "studentCorpus",
  };

  function setCorpusMode(mode) {
    state.corpusMode = mode === "single" ? "single" : "split";
    const single = state.corpusMode === "single";
    $$('[data-corpus-mode]').forEach((button) => {
      const active = button.dataset.corpusMode === state.corpusMode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    $("#studentCorpusField").classList.toggle("hidden", single);
    $("#studentMinField").classList.toggle("hidden", single);
    $("#studentFolderField").classList.toggle("hidden", single);
    $(".scope-grid").classList.toggle("single-corpus", single);
    $("#staffCorpusLabel").textContent = single ? "기본 코퍼스" : "교직원 코퍼스";
    $("#staffCorpusHelp").textContent = single ? "조직의 전체 문서를 검색하는 유일한 코퍼스" : "학과 전체 문서 검색용";
    $("#corpusModeHelp").textContent = single
      ? "학생용 코퍼스와 MCP를 만들지 않고, 모든 동기화 문서를 하나의 코퍼스로 운영합니다."
      : "교직원 전체와 학생 공개 문서를 서로 다른 코퍼스로 운영합니다.";
    $("#provisionCorpora").textContent = single ? "코퍼스 1개 만들기" : "코퍼스 2개 만들기";
    updateResourceProvisionAvailability();
  }

  function missingProvisionResources(scope = "all") {
    const form = $("#departmentForm");
    const groups = {
      all: state.corpusMode === "single" ? ["bucketHwp", "bucketSource", "corpusStaff"] : Object.keys(provisionFieldNames),
      buckets: ["bucketHwp", "bucketSource"],
      corpora: state.corpusMode === "single" ? ["corpusStaff"] : ["corpusStaff", "corpusStudent"],
    };
    return groups[scope].filter((key) => !form.elements[provisionFieldNames[key]].value);
  }

  function updateResourceProvisionAvailability() {
    const form = $("#departmentForm");
    if (!form) return;
    const identityReady = Boolean(
      form.elements.name.value.trim()
      && /^[a-z][a-z0-9-]{1,19}$/.test(form.elements.code.value.trim())
      && (state.editingCode || state.codeAvailable === true)
    );
    const missingBuckets = missingProvisionResources("buckets");
    const missingCorpora = missingProvisionResources("corpora");
    const corpusTotal = state.corpusMode === "single" ? 1 : 2;
    $("#provisionBuckets").disabled = !identityReady || missingBuckets.length === 0;
    $("#provisionCorpora").disabled = !identityReady || missingCorpora.length === 0;
    $("#provisionAllResources").disabled = !identityReady || (missingBuckets.length + missingCorpora.length === 0);
    $("#resourceProvisionCallout").classList.toggle("hidden", missingBuckets.length + missingCorpora.length === 0);
    $("#bucketResourceMeta").textContent = missingBuckets.length
      ? `${2 - missingBuckets.length}/2 연결됨 · 누락 리소스 생성 가능`
      : "2/2 연결됨 · 보호 설정 확인 대상";
    $("#corpusResourceMeta").textContent = missingCorpora.length
      ? `${corpusTotal - missingCorpora.length}/${corpusTotal} 연결됨 · 누락 리소스 생성 가능`
      : (state.corpusMode === "single" ? "1/1 연결됨 · 단일 코퍼스" : "2/2 연결됨 · 교직원/학생 분리");
  }

  function renderResourcePlan(data) {
    const resources = data.resources || [];
    $("#resourcePlanProject").textContent = data.projectId || "—";
    $("#resourcePlanRegion").textContent = data.region || "—";
    const settings = [];
    if (resources.some((item) => item.kind === "bucket")) settings.push("버킷 외부 공개 차단 · Uniform Access · Soft Delete 7일");
    if (resources.some((item) => item.kind === "corpus")) settings.push(`코퍼스 ${data.corpusConfig?.embeddingModel || "다국어 임베딩"} · RAG Managed DB`);
    $("#resourcePlanProtection").innerHTML = `<span>✓</span><p><b>생성 기본값</b>${escapeHtml(settings.join(" / "))}</p>`;
    $("#resourcePlanList").innerHTML = resources.map((item) => {
      const status = item.status || "PLANNED";
      const icon = status === "COMPLETE" ? "✓" : status === "FAILED" ? "!" : "○";
      const detail = item.detail && !["대기 중", "생성 요청 중"].includes(item.detail) ? `<p>${escapeHtml(item.detail)}</p>` : "";
      const resourceName = item.kind === "bucket" ? item.value : item.displayName;
      const nameControl = status === "PLANNED"
        ? `<input class="resource-plan-name-input" data-resource-plan-key="${escapeHtml(item.key)}" data-resource-kind="${escapeHtml(item.kind)}" value="${escapeHtml(resourceName)}" maxlength="${item.kind === "bucket" ? 63 : 128}" aria-label="${escapeHtml(item.label)} 이름">`
        : `<small>${escapeHtml(resourceName)}</small>`;
      return `<div class="resource-plan-item" data-status="${escapeHtml(status)}">
        <span class="resource-plan-kind">${item.kind === "bucket" ? "GCS" : "RAG"}</span>
        <div class="resource-plan-copy"><b>${escapeHtml(item.label)}</b>${nameControl}${detail}</div>
        <span class="resource-plan-state" aria-label="${escapeHtml(status)}">${icon}</span>
      </div>`;
    }).join("");
  }

  function openResourceModal() {
    const modal = $("#resourceProvisionModal");
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    window.setTimeout(() => $("#confirmResourceProvision").focus(), 220);
  }

  function closeResourceModal() {
    const modal = $("#resourceProvisionModal");
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
  }

  function deploymentStepIcon(status) {
    if (status === "COMPLETE") return "✓";
    if (status === "FAILED") return "!";
    if (status === "RUNNING") return "";
    return "○";
  }

  function renderMcpDeployment(run) {
    state.mcpDeployment = run;
    const running = run.status === "RUNNING";
    const complete = run.status === "COMPLETED";
    const failed = run.status === "FAILED";
    $("#mcpDeploymentDepartment").textContent = `${run.name || run.code} · ${run.code}`;
    $("#mcpDeploymentServices").textContent = `${(run.serviceNames || []).length}개 · ${run.corpusMode === "single" ? "단일" : "교직원/학생"}`;
    $("#mcpDeploymentSteps").innerHTML = (run.steps || []).map((step) => `<article class="deployment-step" data-status="${escapeHtml(step.status)}">
      <span class="deployment-step-mark">${deploymentStepIcon(step.status)}</span>
      <div class="deployment-step-copy"><b>${escapeHtml(step.label)}</b><p>${escapeHtml(step.detail || "대기 중")}</p></div>
      <span class="deployment-step-state">${escapeHtml(({ PENDING: "대기", RUNNING: "진행 중", COMPLETE: "완료", FAILED: "실패" })[step.status] || step.status)}</span>
    </article>`).join("");
    const logs = run.logs || [];
    $("#mcpDeploymentLogWrap").classList.toggle("hidden", logs.length === 0);
    $("#mcpDeploymentLog").textContent = logs.join("\n");
    $("#mcpDeploymentLog").scrollTop = $("#mcpDeploymentLog").scrollHeight;
    const error = $("#mcpDeploymentError");
    error.textContent = run.error || "";
    error.classList.toggle("hidden", !run.error);
    $("#mcpDeploymentTitle").textContent = complete ? "MCP 배포 완료" : failed ? "MCP 배포 확인 필요" : running ? "MCP를 배포하고 있습니다" : "MCP 배포";
    $("#mcpDeploymentDescription").textContent = complete
      ? "Cloud Run Ready와 Health 확인까지 완료했습니다."
      : failed ? "실패한 단계와 배포 로그를 확인한 뒤 다시 시도할 수 있습니다."
        : running ? "창을 닫아도 배포는 백그라운드에서 계속됩니다." : "학과 설정을 Cloud Run 서비스로 배포합니다.";
    const start = $("#startMcpDeployment");
    start.classList.toggle("hidden", running);
    start.textContent = complete ? "동기화 관리로 이동" : failed ? "다시 배포" : "지금 MCP 배포";
    $("#closeMcpDeployment").textContent = running || complete || failed ? "닫기" : "나중에 배포";
  }

  function openMcpDeploymentPrompt(code) {
    const dept = state.departments.find((item) => item.code === code);
    if (!dept) return;
    state.mcpDeploymentCode = code;
    renderMcpDeployment({
      code,
      name: dept.name,
      corpusMode: dept.corpusMode || "split",
      status: "READY",
      serviceNames: dept.corpusMode === "single" ? [`rag-mcp-${code}-staff`] : [`rag-mcp-${code}-staff`, `rag-mcp-${code}-student`],
      steps: [
        { key: "config", label: "설정 확인", status: "PENDING", detail: "YAML 및 MCP 키 확인" },
        { key: "image", label: "MCP 이미지", status: "PENDING", detail: "Artifact Registry 확인" },
        { key: "deploy", label: "Cloud Run 배포", status: "PENDING", detail: "서비스 생성 또는 업데이트" },
        { key: "ready", label: "Ready 확인", status: "PENDING", detail: "최신 revision 확인" },
        { key: "health", label: "Health 확인", status: "PENDING", detail: "실제 서비스 응답 확인" },
      ],
      logs: [],
    });
    $("#mcpDeploymentModal").classList.add("open");
    $("#mcpDeploymentModal").setAttribute("aria-hidden", "false");
    window.setTimeout(() => $("#startMcpDeployment").focus(), 180);
  }

  function closeMcpDeployment() {
    $("#mcpDeploymentModal").classList.remove("open");
    $("#mcpDeploymentModal").setAttribute("aria-hidden", "true");
  }

  async function pollMcpDeployment(runId) {
    window.clearTimeout(state.mcpDeploymentPollTimer);
    try {
      const run = await api(`/api/v1/mcp-deployments/${runId}`);
      renderMcpDeployment(run);
      if (run.status === "RUNNING") {
        state.mcpDeploymentPollTimer = window.setTimeout(() => pollMcpDeployment(runId), 1100);
        return;
      }
      state.mcpServers.delete(run.code);
      await loadDepartments();
      if (state.selectedCode === run.code) {
        loadDepartmentMcpServers(run.code).then((data) => renderDrawerMcpServers(run.code, data)).catch(() => {});
      }
      toast(run.status === "COMPLETED" ? "MCP 배포를 완료했습니다" : "MCP 배포를 완료하지 못했습니다", run.status === "COMPLETED" ? `${run.serviceNames.length}개 서비스가 준비되었습니다.` : run.error, run.status === "COMPLETED" ? "ok" : "fail");
    } catch (error) {
      const banner = $("#mcpDeploymentError");
      banner.textContent = error.message;
      banner.classList.remove("hidden");
    }
  }

  async function beginMcpDeployment(code = state.mcpDeploymentCode) {
    if (!code) return;
    state.mcpDeploymentCode = code;
    try {
      const run = await api(`/api/v1/departments/${encodeURIComponent(code)}/mcp-deployments`, { method: "POST" });
      renderMcpDeployment(run);
      pollMcpDeployment(run.runId);
    } catch (error) {
      const runningId = error.data?.error?.runId;
      if (runningId) {
        pollMcpDeployment(runningId);
        return;
      }
      const banner = $("#mcpDeploymentError");
      banner.textContent = error.message;
      banner.classList.remove("hidden");
    }
  }

  async function openOrStartMcpDeployment(code) {
    closeDrawer();
    try {
      const data = await api(`/api/v1/mcp-deployments?code=${encodeURIComponent(code)}&status=RUNNING`);
      const existing = data.runs?.[0];
      if (existing) {
        state.mcpDeploymentCode = code;
        renderMcpDeployment(existing);
        $("#mcpDeploymentModal").classList.add("open");
        $("#mcpDeploymentModal").setAttribute("aria-hidden", "false");
        pollMcpDeployment(existing.runId);
        return;
      }
      openMcpDeploymentPrompt(code);
      beginMcpDeployment(code);
    } catch (error) {
      toast("MCP 배포를 열지 못했습니다", error.message, "fail");
    }
  }

  async function prepareResourcePlan(scope) {
    const form = $("#departmentForm");
    const resources = missingProvisionResources(scope);
    if (!resources.length) {
      toast("이미 연결되어 있습니다", "선택한 범위에 새로 만들 리소스가 없습니다.", "ok");
      return;
    }
    const code = form.elements.code.value.trim().toLowerCase();
    const name = form.elements.name.value.trim();
    if (!name || !await checkDepartmentCode(true)) {
      const identityErrors = {};
      if (state.codeAvailable === false) identityErrors.code = [$("#codeAvailabilityStatus").textContent];
      if (!name) identityErrors.name = ["학과명을 입력해 주세요."];
      showFieldErrors(identityErrors);
      toast("학과 정보를 먼저 확인해 주세요", "사용 가능한 학과 코드와 학과명이 필요합니다.", "fail");
      return;
    }
    const error = $("#resourceProvisionError");
    error.classList.add("hidden");
    try {
      const plan = await api("/api/v1/departments/resource-plans", {
        method: "POST",
        body: { code, name, editingCode: state.editingCode || "", corpusMode: state.corpusMode, resources },
      });
      state.resourcePlan = plan;
      state.provisionRun = null;
      $("#resourceModalTitle").textContent = "학과 리소스 만들기";
      $("#resourceModalDescription").textContent = "생성 전 프로젝트와 리소스 이름을 확인해 주세요.";
      $("#confirmResourceProvision").classList.remove("hidden");
      $("#confirmResourceProvision").disabled = false;
      $("#confirmResourceProvision").textContent = `${plan.resources.length}개 리소스 만들기`;
      $("#closeResourceProvision").textContent = "취소";
      renderResourcePlan(plan);
      openResourceModal();
    } catch (requestError) {
      const fieldErrors = requestError.data?.error?.fieldErrors;
      if (fieldErrors) showFieldErrors(fieldErrors);
      toast("생성 계획을 만들지 못했습니다", requestError.message, "fail");
    }
  }

  async function applyProvisionedResources(run) {
    const selected = {};
    const created = { corpora: [], buckets: [] };
    (run.resources || []).forEach((item) => {
      if (item.status !== "COMPLETE" || !item.value) return;
      selected[provisionFieldNames[item.key]] = item.value;
      if (item.kind === "corpus") {
        created.corpora.push({ name: item.value, displayName: item.displayName || item.value });
      } else {
        created.buckets.push({ name: item.value, location: run.region || "", usedBy: [] });
      }
    });
    if (Object.keys(selected).length) await loadDepartmentResources(selected, created);
  }

  async function pollProvisionRun(runId) {
    window.clearTimeout(state.provisionPollTimer);
    try {
      const run = await api(`/api/v1/departments/resource-provisioning/${runId}`);
      state.provisionRun = run;
      renderResourcePlan(run);
      if (run.status === "RUNNING") {
        state.provisionPollTimer = window.setTimeout(() => pollProvisionRun(runId), 1300);
        return;
      }
      await applyProvisionedResources(run);
      const completed = (run.resources || []).filter((item) => item.status === "COMPLETE").length;
      const failed = (run.resources || []).filter((item) => item.status === "FAILED").length;
      $("#resourceModalTitle").textContent = failed ? "일부 리소스 확인 필요" : "학과 리소스 준비 완료";
      $("#resourceModalDescription").textContent = failed
        ? `${completed}개 완료 · ${failed}개 실패. 완료된 리소스는 그대로 연결했습니다.`
        : `${completed}개 리소스를 생성하고 설정에 연결했습니다.`;
      $("#confirmResourceProvision").classList.add("hidden");
      $("#closeResourceProvision").textContent = "닫기";
      toast(failed ? "일부 리소스 생성 실패" : "학과 리소스를 준비했습니다", failed ? "실패한 항목만 다시 시도할 수 있습니다." : `${completed}개 생성 완료`, failed ? "fail" : "ok");
    } catch (error) {
      const banner = $("#resourceProvisionError");
      banner.textContent = error.message;
      banner.classList.remove("hidden");
      $("#confirmResourceProvision").disabled = false;
      $("#confirmResourceProvision").textContent = "다시 확인";
    }
  }

  async function startResourceProvision() {
    if (!state.resourcePlan) return;
    const button = $("#confirmResourceProvision");
    const inputs = $$("#resourcePlanList [data-resource-plan-key]");
    const overrides = Object.fromEntries(inputs.map((input) => [input.dataset.resourcePlanKey, input.value.trim()]));
    const invalidInput = inputs.find((input) => {
      const value = input.value.trim().replace(/^gs:\/\//, "");
      if (input.dataset.resourceKind === "bucket") return !/^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$/.test(value);
      return !value || value.length > 128;
    });
    const bucketNames = inputs.filter((input) => input.dataset.resourceKind === "bucket").map((input) => input.value.trim().replace(/^gs:\/\//, ""));
    const corpusNames = inputs.filter((input) => input.dataset.resourceKind === "corpus").map((input) => input.value.trim());
    const duplicateNames = new Set(bucketNames).size !== bucketNames.length || new Set(corpusNames).size !== corpusNames.length;
    if (invalidInput || duplicateNames) {
      const banner = $("#resourceProvisionError");
      banner.textContent = duplicateNames ? "같은 종류의 두 리소스는 서로 다른 이름이어야 합니다." : "리소스 이름의 형식을 확인해 주세요.";
      banner.classList.remove("hidden");
      invalidInput?.focus();
      return;
    }
    button.disabled = true;
    button.textContent = "생성 시작 중…";
    $("#closeResourceProvision").textContent = "닫기";
    try {
      const run = await api("/api/v1/departments/resource-provisioning", {
        method: "POST",
        body: { planId: state.resourcePlan.planId, overrides },
      });
      state.provisionRun = run;
      $("#resourceModalTitle").textContent = "학과 리소스를 구성하고 있습니다";
      $("#resourceModalDescription").textContent = "창을 닫아도 현재 실행은 계속됩니다.";
      renderResourcePlan(run);
      pollProvisionRun(run.runId);
    } catch (error) {
      const banner = $("#resourceProvisionError");
      banner.textContent = error.message;
      banner.classList.remove("hidden");
      button.disabled = false;
      button.textContent = "리소스 만들기";
    }
  }

  function updateDrivePreflightAvailability() {
    const form = $("#departmentForm");
    const driveIds = splitIds(form.elements.driveIds.value);
    const signature = driveIds.join("\n");
    const button = $("#checkDriveIds");
    button.disabled = driveIds.length === 0 || button.classList.contains("is-checking");
    state.allowDuplicateDriveIds = false;
    if (state.drivePreflightSignature && state.drivePreflightSignature !== signature) {
      state.drivePreflightSignature = "";
      state.driveConflicts = [];
      const status = $("#drivePreflightStatus");
      status.dataset.status = "changed";
      status.textContent = "입력값이 변경되었습니다. 다시 연결을 확인해 주세요.";
      renderDriveConflictStatus([]);
    }
  }

  function renderDriveConflictStatus(conflicts = []) {
    const status = $("#driveConflictStatus");
    if (!status) return;
    if (!conflicts.length) {
      status.textContent = "";
      status.dataset.status = "";
      return;
    }
    status.dataset.status = "warn";
    status.textContent = conflicts.map((item) => {
      const owner = item.name || item.code;
      return `${owner} 학과와 중복된 공유드라이브 ID입니다: ${(item.driveIds || []).join(", ")}`;
    }).join(" · ");
  }

  async function checkDriveIds() {
    const form = $("#departmentForm");
    const driveIds = splitIds(form.elements.driveIds.value);
    if (!driveIds.length) return;
    const button = $("#checkDriveIds");
    const status = $("#drivePreflightStatus");
    const signature = driveIds.join("\n");
    button.classList.add("is-checking");
    button.disabled = true;
    status.dataset.status = "checking";
    status.textContent = "서비스 계정으로 공유 드라이브 연결을 확인하고 있습니다.";
    renderDriveConflictStatus([]);
    try {
      const result = await api("/api/v1/departments/drive-preflight", {
        method: "POST",
        body: { driveIds, code: form.elements.code.value.trim().toLowerCase() },
      });
      if (splitIds(form.elements.driveIds.value).join("\n") !== signature) return;
      state.drivePreflightSignature = signature;
      state.driveConflicts = result.driveConflicts || [];
      status.dataset.status = String(result.status || "WARN").toLowerCase();
      status.textContent = result.action
        ? `${result.detail} · ${result.action}`
        : result.detail;
      renderDriveConflictStatus(state.driveConflicts);
    } catch (error) {
      if (splitIds(form.elements.driveIds.value).join("\n") !== signature) return;
      state.drivePreflightSignature = "";
      state.driveConflicts = [];
      status.dataset.status = "fail";
      status.textContent = error.message;
      renderDriveConflictStatus([]);
    } finally {
      button.classList.remove("is-checking");
      updateDrivePreflightAvailability();
    }
  }

  function resetFolderLookup() {
    state.folderLookupSignature = "";
    state.folderLookupPending = false;
    state.folderLookupById.clear();
    const status = $("#folderLookupStatus");
    status.dataset.status = "";
    status.textContent = "";
    const button = $("#lookupFolderNames");
    button.classList.remove("is-checking");
    button.innerHTML = '<span class="inline-check-dot" aria-hidden="true"></span>폴더 정보 확인';
  }

  function updateFolderLookupAvailability() {
    const textarea = $("#departmentForm").elements.syncFolderIds;
    const folderIds = splitIds(textarea.value);
    const signature = folderIds.join("\n");
    if (state.folderLookupSignature && state.folderLookupSignature !== signature) {
      state.folderLookupSignature = "";
      state.folderLookupById.clear();
      const status = $("#folderLookupStatus");
      status.dataset.status = "changed";
      status.textContent = "입력값이 변경되었습니다. 폴더 정보를 다시 확인해 주세요.";
      updateTagPreview(textarea);
      renderStudentFolderPicker();
    }
    $("#lookupFolderNames").disabled = !folderIds.length || state.folderLookupPending;
  }

  async function lookupFolderNames() {
    const form = $("#departmentForm");
    const textarea = form.elements.syncFolderIds;
    const folderIds = splitIds(textarea.value);
    if (!folderIds.length || state.folderLookupPending) return;
    const signature = folderIds.join("\n");
    const button = $("#lookupFolderNames");
    const status = $("#folderLookupStatus");
    state.folderLookupPending = true;
    button.classList.add("is-checking");
    button.disabled = true;
    button.innerHTML = '<span class="inline-check-dot" aria-hidden="true"></span>폴더 확인 중…';
    status.dataset.status = "checking";
    status.textContent = "서비스 계정으로 실제 Drive 폴더 이름을 확인하고 있습니다.";
    try {
      const result = await api("/api/v1/departments/folder-lookup", {
        method: "POST",
        body: { folderIds },
      });
      if (splitIds(textarea.value).join("\n") !== signature) return;
      state.folderLookupSignature = signature;
      state.folderLookupById = new Map((result.folders || []).map((item) => [item.folderId, item]));
      updateTagPreview(textarea);
      renderStudentFolderPicker();
      const stats = result.stats || {};
      const failures = (result.folders || []).filter((item) => item.status !== "OK");
      status.dataset.status = failures.length ? "warn" : "ok";
      status.textContent = failures.length
        ? `${stats.resolved || 0}개 확인 · ${stats.failed || 0}개 실패 — ${failures.slice(0, 3).map((item) => `${item.folderId}: ${item.reason}`).join(" · ")}`
        : `${stats.resolved || 0}개 폴더의 실제 이름을 확인했습니다.`;
      toast(
        failures.length ? "일부 폴더를 확인하지 못했습니다" : "폴더 정보를 확인했습니다",
        failures.length ? "실패한 ID와 서비스 계정 권한을 확인해 주세요." : "표시 이름을 Drive 폴더명으로 바꿨습니다.",
        failures.length ? "fail" : "ok",
      );
    } catch (error) {
      if (splitIds(textarea.value).join("\n") !== signature) return;
      state.folderLookupSignature = "";
      state.folderLookupById.clear();
      updateTagPreview(textarea);
      renderStudentFolderPicker();
      status.dataset.status = "fail";
      status.textContent = error.message;
    } finally {
      state.folderLookupPending = false;
      button.classList.remove("is-checking");
      button.innerHTML = '<span class="inline-check-dot" aria-hidden="true"></span>폴더 정보 확인';
      updateFolderLookupAvailability();
    }
  }

  function formPayload() {
    const form = $("#departmentForm");
    const value = (name) => form.elements[name]?.value?.trim() || "";
    return {
      code: value("code").toLowerCase(),
      name: value("name"),
      corpusMode: state.corpusMode,
      corpora: { staff: value("staffCorpus"), student: state.corpusMode === "split" ? value("studentCorpus") : "" },
      buckets: { hwpOriginal: value("hwpBucket"), source: value("sourceBucket") },
      drive: {
        driveIds: splitIds(value("driveIds")),
        syncFolderIds: splitIds(value("syncFolderIds")),
        studentFolderIds: state.corpusMode === "split" ? splitIds(value("studentFolderIds")) : [],
      },
      minInstances: { staff: Number(value("staffMin") || 0), student: state.corpusMode === "split" ? Number(value("studentMin") || 0) : 0 },
    };
  }

  function updateBucketSelectionState() {
    const form = $("#departmentForm");
    const hwp = form.elements.hwpBucket;
    const source = form.elements.sourceBucket;
    const values = { hwpBucket: hwp.value, sourceBucket: source.value };

    [...hwp.options].forEach((option) => {
      option.disabled = Boolean(option.value && option.value === values.sourceBucket && option.value !== values.hwpBucket);
    });
    [...source.options].forEach((option) => {
      option.disabled = Boolean(option.value && option.value === values.hwpBucket && option.value !== values.sourceBucket);
    });

    for (const [name, outputId] of [["hwpBucket", "hwpBucketUsage"], ["sourceBucket", "sourceBucketUsage"]]) {
      const bucket = state.departmentBuckets.find((item) => item.name === values[name]);
      const usedBy = bucket?.usedBy || [];
      const output = $(`#${outputId}`);
      output.textContent = usedBy.length ? `${usedBy.join(", ")} 학과가 사용 중인 버킷입니다.` : "";
      output.classList.toggle("is-visible", usedBy.length > 0);
    }
    updateResourceProvisionAvailability();
  }

  async function loadDepartmentResources(selected = {}, created = { corpora: [], buckets: [] }) {
    const form = $("#departmentForm");
    const names = ["staffCorpus", "studentCorpus", "hwpBucket", "sourceBucket"];
    const desired = Object.fromEntries(names.map((name) => [name, selected[name] ?? form.elements[name].value]));
    const requestId = ++state.departmentResourceRequest;
    const button = $("#refreshDepartmentResources");
    button.disabled = true;
    button.textContent = "불러오는 중…";
    names.forEach((name) => {
      form.elements[name].disabled = true;
      form.elements[name].innerHTML = '<option value="">GCP 리소스 조회 중…</option>';
    });
    try {
      const resources = await api("/api/v1/departments/resource-options");
      if (requestId !== state.departmentResourceRequest) return;
      const corpora = [...(resources.corpora || [])];
      const buckets = [...(resources.buckets || [])];
      (created.corpora || []).forEach((item) => {
        if (!corpora.some((existing) => existing.name === item.name)) corpora.push(item);
      });
      (created.buckets || []).forEach((item) => {
        if (!buckets.some((existing) => existing.name === item.name)) buckets.push(item);
      });
      state.departmentBuckets = buckets;
      const corpusHtml = '<option value="">코퍼스 선택</option>' + corpora.map((item) => {
        const shortId = item.name.split("/").at(-1);
        return `<option value="${escapeHtml(item.name)}">${escapeHtml(item.displayName)} · ${escapeHtml(shortId)}</option>`;
      }).join("");
      const bucketHtml = '<option value="">버킷 선택</option>' + buckets.map((item) => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)} · ${escapeHtml(item.location)}</option>`).join("");
      for (const name of ["staffCorpus", "studentCorpus"]) {
        const value = desired[name];
        form.elements[name].innerHTML = corpora.length ? corpusHtml : '<option value="">사용 가능한 RAG 코퍼스 없음</option>';
        if (value && !corpora.some((item) => item.name === value)) {
          form.elements[name].insertAdjacentHTML("beforeend", `<option value="${escapeHtml(value)}">현재 설정 · 조회되지 않음</option>`);
        }
        form.elements[name].disabled = !corpora.length;
        form.elements[name].value = value || "";
      }
      for (const name of ["hwpBucket", "sourceBucket"]) {
        const value = desired[name];
        form.elements[name].innerHTML = bucketHtml || '<option value="">사용 가능한 보호 버킷 없음</option>';
        if (value && !buckets.some((item) => item.name === value)) {
          form.elements[name].insertAdjacentHTML("beforeend", `<option value="${escapeHtml(value)}">현재 설정 · 조회되지 않음</option>`);
        }
        form.elements[name].disabled = !buckets.length;
        if (value) form.elements[name].value = value;
      }
      updateBucketSelectionState();
      updateResourceProvisionAvailability();
      if (!corpora.length || !buckets.length) {
        toast("선택할 리소스가 부족합니다", "기존 리소스를 선택하거나 이 화면에서 새로 만들 수 있습니다.");
      }
    } catch (error) {
      if (requestId !== state.departmentResourceRequest) return;
      state.departmentBuckets = [];
      names.forEach((name) => { form.elements[name].innerHTML = '<option value="">리소스 조회 실패</option>'; });
      updateBucketSelectionState();
      updateResourceProvisionAvailability();
      toast("GCP 리소스를 불러오지 못했습니다", error.message, "fail");
    } finally {
      if (requestId === state.departmentResourceRequest) {
        button.disabled = false;
        button.textContent = "리소스 다시 불러오기";
        updateResourceProvisionAvailability();
      }
    }
  }

  function clearFieldErrors() {
    $$(".field-error").forEach((item) => { item.textContent = ""; });
    $$(".field input, .field textarea, .field select").forEach((item) => item.classList.remove("invalid"));
    $$(".field.has-error").forEach((item) => item.classList.remove("has-error"));
  }

  function showFieldErrors(errors = {}) {
    clearFieldErrors();
    let first = null;
    Object.entries(errors).forEach(([field, messages]) => {
      const label = $(`[data-error="${CSS.escape(field)}"]`);
      if (label) {
        label.textContent = messages.join(" ");
        const fieldRoot = label.closest(".field");
        const input = fieldRoot?.querySelector("input:not([type='hidden']), textarea, select");
        const focusTarget = input || fieldRoot?.querySelector(".folder-picker");
        input?.classList.add("invalid");
        fieldRoot?.classList.add("has-error");
        if (!first) first = focusTarget;
      }
    });
    first?.focus();
  }

  function validateStepOne() {
    clearFieldErrors();
    const payload = formPayload();
    const errors = {};
    if (!/^[a-z][a-z0-9-]{1,19}$/.test(payload.code)) errors.code = ["영문 소문자로 시작하는 2~20자 코드가 필요합니다."];
    else if (!state.editingCode && state.codeAvailable !== true) errors.code = [$("#codeAvailabilityStatus").textContent || "이미 사용 중인지 학과 코드를 확인해 주세요."];
    if (!payload.name) errors.name = ["학과명을 입력해 주세요."];
    if (!payload.corpora.staff) errors["corpora.staff"] = [state.corpusMode === "single" ? "기본 코퍼스를 입력해 주세요." : "교직원 코퍼스를 입력해 주세요."];
    if (state.corpusMode === "split" && !payload.corpora.student) errors["corpora.student"] = ["학생 코퍼스를 입력해 주세요."];
    if (state.corpusMode === "split" && payload.corpora.staff && payload.corpora.staff === payload.corpora.student) {
      errors["corpora.student"] = ["교직원 코퍼스와 다른 코퍼스를 선택해 주세요."];
    }
    if (!payload.buckets.hwpOriginal) errors["buckets.hwpOriginal"] = ["버킷 이름을 입력해 주세요."];
    if (!payload.buckets.source) errors["buckets.source"] = ["버킷 이름을 입력해 주세요."];
    if (payload.buckets.hwpOriginal && payload.buckets.hwpOriginal === payload.buckets.source) {
      errors["buckets.source"] = ["HWP 원본 버킷과 다른 버킷을 선택해 주세요."];
    }
    showFieldErrors(errors);
    return Object.keys(errors).length === 0;
  }

  function renderReview(payload, preview) {
    $("#previewFilename").textContent = `${payload.code || "department"}.yaml`;
    $("#yamlPreview").textContent = preview;
    const form = $("#departmentForm");
    const selectedText = (name, fallback) => form.elements[name]?.selectedOptions?.[0]?.textContent || fallback;
    const rows = [
      ["학과", `${payload.name} · ${payload.code}`],
      [state.corpusMode === "single" ? "단일 코퍼스" : "교직원 코퍼스", selectedText("staffCorpus", payload.corpora.staff)],
      ...(state.corpusMode === "split" ? [["학생 코퍼스", selectedText("studentCorpus", payload.corpora.student)]] : []),
      ["버킷", `${payload.buckets.hwpOriginal} / ${payload.buckets.source}`],
      ["Drive 범위", `${payload.drive.driveIds.length}개 drive · ${payload.drive.syncFolderIds.length}개 folder`],
      ["MCP", state.corpusMode === "single" ? "기본 서버 1개 · 키 자동 관리" : "교직원·학생 서버 2개 · 키 자동 관리"],
    ];
    $("#reviewSummary").innerHTML = rows.map(([label, value]) => `<div class="review-item"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`).join("");
  }

  async function prepareReview() {
    const payload = formPayload();
    try {
      const previewUrl = state.editingCode
        ? `/api/v1/departments/${state.editingCode}/preview`
        : "/api/v1/departments/preview";
      const result = await api(previewUrl, { method: "POST", body: payload });
      if (!result.valid) {
        showFieldErrors(result.fieldErrors);
        toast("입력값을 확인해 주세요", "표시된 항목을 수정한 뒤 다시 진행하세요.", "fail");
        return false;
      }
      clearFieldErrors();
      state.driveConflicts = result.driveConflicts || [];
      renderReview(payload, result.yamlPreview);
      return true;
    } catch (error) {
      toast("설정을 검증하지 못했습니다", error.message, "fail");
      return false;
    }
  }

  function showStep(step) {
    state.currentStep = step;
    $$(".wizard-step").forEach((item) => item.classList.toggle("active", Number(item.dataset.step) === step));
    $$("[data-step-marker]").forEach((item) => {
      const marker = Number(item.dataset.stepMarker);
      item.classList.toggle("active", marker === step);
      item.classList.toggle("complete", marker < step);
      if (marker < step) item.querySelector(":scope > span").textContent = "✓";
      else item.querySelector(":scope > span").textContent = String(marker);
    });
    $("#previousStep").classList.toggle("hidden", step === 1);
    $("#nextStep").classList.toggle("hidden", step === 3);
    $("#createDepartment").classList.toggle("hidden", step !== 3);
    $("#nextStep").textContent = step === 1 ? "다음: Drive 범위" : "다음: 검토·생성";
    $("#createDepartment").disabled = !$("#confirmCreate").checked;
    $(".wizard-shell").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function nextWizardStep() {
    if (state.currentStep === 1) {
      await checkDepartmentCode(true);
      if (validateStepOne()) showStep(2);
      return;
    }
    if (state.currentStep !== 2) return;
    const button = $("#nextStep");
    const previous = $("#previousStep");
    if (button.disabled || button.classList.contains("is-checking")) return;
    button.disabled = true;
    previous.disabled = true;
    button.classList.add("is-checking");
    button.setAttribute("aria-busy", "true");
    button.innerHTML = '<span class="loader-ring" aria-hidden="true"></span>검증 중…';
    try {
      if (await prepareReview()) showStep(3);
    } finally {
      button.classList.remove("is-checking");
      button.removeAttribute("aria-busy");
      button.innerHTML = "다음: 검토·생성";
      button.disabled = false;
      previous.disabled = false;
    }
  }

  function resetWizard() {
    $("#departmentForm").reset();
    $$('[data-corpus-mode]').forEach((button) => { button.disabled = false; });
    window.clearTimeout(state.codeAvailabilityTimer);
    state.editingCode = null;
    state.editingRevision = null;
    state.drivePreflightSignature = "";
    state.driveConflicts = [];
    state.allowDuplicateDriveIds = false;
    closeDriveConflictModal();
    state.codeAvailable = null;
    state.codeAvailabilityRequest += 1;
    state.resourcePlan = null;
    setCorpusMode("split");
    $("#departmentForm").elements.code.readOnly = false;
    setCodeAvailability("", "");
    setEditorMode(false);
    clearFieldErrors();
    $$(".tag-preview").forEach((item) => { item.innerHTML = ""; });
    $("#drivePreflightStatus").textContent = "";
    $("#drivePreflightStatus").dataset.status = "";
    renderDriveConflictStatus([]);
    resetFolderLookup();
    updateDrivePreflightAvailability();
    updateFolderLookupAvailability();
    updateBucketSelectionState();
    renderStudentFolderPicker();
    $("#confirmCreate").checked = false;
    $("#validationBanner").classList.add("hidden");
    $("#resourceProvisionError").classList.add("hidden");
    closeResourceModal();
    showStep(1);
  }

  function setEditorMode(editing) {
    $("#createEyebrow").textContent = editing ? "EDIT DEPARTMENT" : "NEW DEPARTMENT";
    $("#createTitle").textContent = editing ? "학과 설정 수정" : "새 학과 연결";
    $("#createDescription").textContent = editing
      ? "기존 MCP 키를 유지하면서 연결 설정을 안전하게 수정합니다."
      : "필요한 정보를 단계별로 입력하면 안전한 YAML을 생성합니다.";
    $("[data-step-marker='3'] b").textContent = editing ? "검토·저장" : "검토·생성";
    $("#confirmText").textContent = editing
      ? "변경 내용을 확인했으며 기존 YAML 설정을 업데이트합니다."
      : "이 파일은 git으로 복구되지 않으며 기존 파일을 덮어쓰지 않는다는 것을 확인했습니다.";
    $("#createDepartment").textContent = editing ? "변경사항 저장" : "YAML 생성";
  }

  async function beginEdit(code) {
    try {
      const config = await api(`/api/v1/departments/${code}/config`);
      resetWizard();
      state.editingCode = code;
      state.editingRevision = config.configRevision;
      setEditorMode(true);
      const form = $("#departmentForm");
      form.elements.code.value = config.code;
      form.elements.code.readOnly = true;
      setCodeAvailability("ok", "현재 학과 코드입니다.");
      form.elements.name.value = config.name || "";
      form.elements.driveIds.value = (config.drive?.driveIds || []).join("\n");
      form.elements.syncFolderIds.value = (config.drive?.syncFolderIds || []).join("\n");
      form.elements.studentFolderIds.value = (config.drive?.studentFolderIds || []).join("\n");
      form.elements.staffMin.value = config.minInstances?.staff ?? 0;
      form.elements.studentMin.value = config.minInstances?.student ?? 0;
      setCorpusMode(config.corpusMode || (config.corpora?.student ? "split" : "single"));
      $$('[data-corpus-mode]').forEach((button) => { button.disabled = true; });
      $("#corpusModeHelp").textContent += " 운영 중 구성 변경은 재색인과 기존 서비스 정리가 필요해 별도 마이그레이션으로 진행합니다.";
      $$("textarea").forEach(updateTagPreview);
      updateDrivePreflightAvailability();
      updateFolderLookupAvailability();
      renderStudentFolderPicker();
      closeDrawer();
      switchView("create");
      await loadDepartmentResources({
        staffCorpus: config.corpora?.staff || "",
        studentCorpus: config.corpora?.student || "",
        hwpBucket: config.buckets?.hwpOriginal || "",
        sourceBucket: config.buckets?.source || "",
      });
    } catch (error) {
      toast("설정을 불러오지 못했습니다", error.message, "fail");
    }
  }

  function driveConflictRows(conflicts = []) {
    return conflicts.map((item) => {
      const owner = item.name ? `${item.name} · ${item.code}` : item.code;
      const ids = (item.driveIds || []).join(", ");
      return `<div class="sync-target-row"><span>${escapeHtml(owner)}</span><b>${escapeHtml(ids)}</b></div>`;
    }).join("");
  }

  function openDriveConflictModal(conflicts = []) {
    state.driveConflicts = conflicts;
    $("#driveConflictTarget").innerHTML = driveConflictRows(conflicts);
    $("#confirmDriveConflict").textContent = state.editingCode ? "그래도 저장" : "그래도 생성";
    const modal = $("#driveConflictModal");
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    window.setTimeout(() => $("#confirmDriveConflict").focus(), 220);
  }

  function closeDriveConflictModal() {
    const modal = $("#driveConflictModal");
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
  }

  function confirmDuplicateDriveIds() {
    state.allowDuplicateDriveIds = true;
    closeDriveConflictModal();
    $("#departmentForm").requestSubmit();
  }

  async function submitDepartment(event) {
    event.preventDefault();
    if (!$("#confirmCreate").checked) return;
    if ((state.driveConflicts || []).length && !state.allowDuplicateDriveIds) {
      openDriveConflictModal(state.driveConflicts);
      return;
    }
    const button = $("#createDepartment");
    button.disabled = true;
    button.textContent = state.editingCode ? "저장 중…" : "생성 중…";
    try {
      const wasEditing = Boolean(state.editingCode);
      const payload = formPayload();
      if (state.editingCode) payload.configRevision = state.editingRevision;
      if (state.allowDuplicateDriveIds) payload.allowDuplicateDriveIds = true;
      const result = await api(
        state.editingCode ? `/api/v1/departments/${state.editingCode}` : "/api/v1/departments",
        { method: state.editingCode ? "PUT" : "POST", body: payload },
      );
      toast(state.editingCode ? "설정을 저장했습니다" : "YAML을 생성했습니다", result.path, "ok");
      resetWizard();
      await loadDepartments();
      switchView("dashboard");
      if (!wasEditing) openMcpDeploymentPrompt(result.code);
      startStatus([result.code]);
    } catch (error) {
      if (error.data?.error?.code === "DRIVE_ID_CONFLICT") {
        openDriveConflictModal(error.data.error.driveConflicts || []);
        return;
      }
      const fieldErrors = error.data?.error?.fieldErrors;
      if (fieldErrors) showFieldErrors(fieldErrors);
      const banner = $("#validationBanner");
      banner.textContent = error.message;
      banner.classList.remove("hidden");
      toast("YAML을 생성하지 못했습니다", error.message, "fail");
    } finally {
      button.textContent = state.editingCode ? "변경사항 저장" : "YAML 생성";
      button.disabled = !$("#confirmCreate").checked;
    }
  }

  function updateTagPreview(textarea) {
    const target = $(`[data-tags="${textarea.name}"]`);
    if (!target) return;
    target.innerHTML = splitIds(textarea.value).map((value) => {
      const folder = textarea.name === "syncFolderIds" ? state.folderLookupById.get(value) : null;
      if (folder?.status === "OK") {
        return `<span class="tag-chip named-folder-chip" title="${escapeHtml(value)}"><b>${escapeHtml(folder.name || "이름 없는 폴더")}</b><code>${escapeHtml(value)}</code></span>`;
      }
      if (folder) {
        return `<span class="tag-chip named-folder-chip is-failed" title="${escapeHtml(folder.reason || value)}"><b>${escapeHtml(value)}</b><small>확인 실패</small></span>`;
      }
      return `<span class="tag-chip" title="${escapeHtml(value)}">${escapeHtml(value)}</span>`;
    }).join("");
  }

  function renderStudentFolderPicker() {
    const form = $("#departmentForm");
    const syncIds = splitIds(form.elements.syncFolderIds.value);
    const studentInput = form.elements.studentFolderIds;
    if (state.corpusMode === "single") {
      studentInput.value = "";
      return;
    }
    const selected = new Set(splitIds(studentInput.value).filter((id) => syncIds.includes(id)));
    studentInput.value = syncIds.filter((id) => selected.has(id)).join("\n");

    const picker = $("#studentFolderPicker");
    const toggle = $("#toggleStudentFolders");
    toggle.disabled = syncIds.length === 0;
    toggle.textContent = syncIds.length > 0 && selected.size === syncIds.length ? "전체 해제" : "전체 선택";
    if (!syncIds.length) {
      picker.innerHTML = "<p>먼저 동기화 폴더 ID를 입력해 주세요.</p>";
      return;
    }
    picker.innerHTML = syncIds.map((id) => {
      const folder = state.folderLookupById.get(id);
      const name = folder?.status === "OK" ? folder.name : "";
      return `
      <label class="folder-option" title="${escapeHtml(id)}">
        <input type="checkbox" value="${escapeHtml(id)}" ${selected.has(id) ? "checked" : ""} />
        <span class="folder-option-copy">
          ${name ? `<b>${escapeHtml(name)}</b>` : ""}
          <code>${escapeHtml(id)}</code>
        </span>
      </label>
    `;
    }).join("");
  }

  function syncStudentFolderSelection() {
    const form = $("#departmentForm");
    const selected = $$("#studentFolderPicker input:checked").map((input) => input.value);
    form.elements.studentFolderIds.value = selected.join("\n");
    renderStudentFolderPicker();
  }

  function bindEvents() {
    $$(".nav-item").forEach((item) => item.addEventListener("click", () => {
      if (item.dataset.view === "create") {
        resetWizard();
        loadDepartmentResources();
      }
      switchView(item.dataset.view);
    }));
    $$('[data-go-create]').forEach((item) => item.addEventListener("click", () => { resetWizard(); switchView("create"); loadDepartmentResources(); }));
    $(".mobile-menu").addEventListener("click", () => document.body.classList.toggle("menu-open"));
    $("#checkAllButton").addEventListener("click", () => startStatus());
    $("#departmentSearch").addEventListener("input", renderDepartments);
    $("#statusFilter").addEventListener("change", renderDepartments);
    $("#corpusDepartment").addEventListener("change", () => updateCorpusChatTarget(true));
    $$('[data-corpus-audience]').forEach((button) => button.addEventListener("click", () => {
      state.corpusAudience = button.dataset.corpusAudience;
      $$('[data-corpus-audience]').forEach((item) => item.classList.toggle("active", item === button));
      updateCorpusChatTarget(true);
    }));
    $("#corpusGenerateToggle").addEventListener("change", () => {
      updateCorpusChatTarget(false);
      renderCorpusChat();
    });
    $("#corpusChatForm").addEventListener("submit", submitCorpusQuery);
    $("#clearCorpusChat").addEventListener("click", clearCorpusChat);
    $("#corpusChatInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        $("#corpusChatForm").requestSubmit();
      }
    });
    $("#nextStep").addEventListener("click", nextWizardStep);
    $("#previousStep").addEventListener("click", () => showStep(Math.max(1, state.currentStep - 1)));
    $("#cancelCreate").addEventListener("click", () => { resetWizard(); switchView("dashboard"); });
    $("#departmentForm").addEventListener("submit", submitDepartment);
    $("#commonSetupForm").addEventListener("submit", submitCommonSetup);
    $("#refreshGcloudAuth").addEventListener("click", refreshGcloudSetup);
    $("#confirmDriveSaRepair").addEventListener("click", confirmDriveSaAction);
    $$("[data-close-drive-sa]").forEach((item) => item.addEventListener("click", closeDriveSaModal));
    $("#planCommonResources").addEventListener("click", planCommonResources);
    $("#confirmCommonProvision").addEventListener("click", confirmCommonProvision);
    $("#cancelCommonProvision").addEventListener("click", cancelCommonProvision);
    $("#commonSetupForm").elements.projectId.addEventListener("change", loadSetupResources);
    $("#commonSetupForm").elements.region.addEventListener("change", loadSetupResources);
    $("#refreshDepartmentResources").addEventListener("click", () => loadDepartmentResources());
    $("#departmentForm").elements.hwpBucket.addEventListener("change", updateBucketSelectionState);
    $("#departmentForm").elements.sourceBucket.addEventListener("change", updateBucketSelectionState);
    $("#departmentForm").elements.staffCorpus.addEventListener("change", updateResourceProvisionAvailability);
    $("#departmentForm").elements.studentCorpus.addEventListener("change", updateResourceProvisionAvailability);
    $$('[data-corpus-mode]').forEach((button) => button.addEventListener("click", () => setCorpusMode(button.dataset.corpusMode)));
    $("#departmentForm").elements.name.addEventListener("input", updateResourceProvisionAvailability);
    $("#provisionAllResources").addEventListener("click", () => prepareResourcePlan("all"));
    $("#provisionBuckets").addEventListener("click", () => prepareResourcePlan("buckets"));
    $("#provisionCorpora").addEventListener("click", () => prepareResourcePlan("corpora"));
    $("#confirmResourceProvision").addEventListener("click", startResourceProvision);
    $$('[data-close-resource-modal]').forEach((item) => item.addEventListener("click", closeResourceModal));
    $$('[data-close-mcp-deployment]').forEach((item) => item.addEventListener("click", closeMcpDeployment));
    $("#startMcpDeployment").addEventListener("click", () => {
      if (state.mcpDeployment?.status === "COMPLETED") {
        const code = state.mcpDeployment.code;
        closeMcpDeployment();
        openSyncManagement(code);
        return;
      }
      beginMcpDeployment();
    });
    $("#departmentForm").elements.driveIds.addEventListener("input", updateDrivePreflightAvailability);
    $("#checkDriveIds").addEventListener("click", checkDriveIds);
    $("#lookupFolderNames").addEventListener("click", lookupFolderNames);
    $("#refreshSyncRuns").addEventListener("click", () => loadSyncRuns());
    $("#syncDepartment").addEventListener("change", renderSyncControls);
    $$('[data-sync-mode]').forEach((button) => button.addEventListener("click", () => {
      state.syncMode = button.dataset.syncMode;
      renderSyncControls();
    }));
    $("#startManualSync").addEventListener("click", requestManualSync);
    $("#confirmManualSync").addEventListener("click", submitManualSync);
    $$('[data-close-sync-modal]').forEach((item) => item.addEventListener("click", closeSyncConfirmModal));
    $("#confirmDriveConflict").addEventListener("click", confirmDuplicateDriveIds);
    $$('[data-close-drive-conflict]').forEach((item) => item.addEventListener("click", closeDriveConflictModal));
    $("#environmentGrid").addEventListener("click", (event) => {
      if (event.target.closest("#checkDriveSa")) return checkDriveServiceAccount();
      return copyEnvironmentValue(event);
    });
    $("#drawerMcpServers").addEventListener("click", copySingleMcpServer);
    $("#drawerContent").addEventListener("click", (event) => {
      const button = event.target.closest("[data-deploy-mcp]");
      if (button) openOrStartMcpDeployment(button.dataset.deployMcp);
    });
    $("#confirmCreate").addEventListener("change", (event) => { $("#createDepartment").disabled = !event.target.checked; });
    $$("textarea").forEach((item) => item.addEventListener("input", () => updateTagPreview(item)));
    $("#departmentForm").elements.syncFolderIds.addEventListener("input", renderStudentFolderPicker);
    $("#departmentForm").elements.syncFolderIds.addEventListener("input", updateFolderLookupAvailability);
    $("#studentFolderPicker").addEventListener("change", syncStudentFolderSelection);
    $("#toggleStudentFolders").addEventListener("click", () => {
      const options = $$("#studentFolderPicker input");
      const selectAll = options.some((input) => !input.checked);
      options.forEach((input) => { input.checked = selectAll; });
      syncStudentFolderSelection();
    });
    $("#departmentForm").elements.code.addEventListener("input", (event) => {
      event.target.value = event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "");
      scheduleDepartmentCodeCheck();
    });
    $$('[data-close-drawer]').forEach((item) => item.addEventListener("click", closeDrawer));
    $("#drawerEdit").addEventListener("click", (event) => beginEdit(event.currentTarget.dataset.code));
    $("#drawerCheck").addEventListener("click", (event) => startStatus([event.currentTarget.dataset.code]));
    $("#cancelRun").addEventListener("click", async () => {
      if (!state.activeRunId) return;
      try { await api(`/api/v1/status-runs/${state.activeRunId}`, { method: "DELETE" }); }
      catch (error) { toast("취소하지 못했습니다", error.message, "fail"); }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if ($("#syncConfirmModal").classList.contains("open")) closeSyncConfirmModal();
      else if ($("#mcpDeploymentModal").classList.contains("open")) closeMcpDeployment();
      else if ($("#resourceProvisionModal").classList.contains("open")) closeResourceModal();
      else if ($("#detailDrawer").classList.contains("open")) closeDrawer();
    });
  }

  async function reconnectRun() {
    try {
      const data = await api("/api/v1/status-runs?status=RUNNING");
      const run = data.runs?.[0];
      if (!run) return;
      state.activeRunId = run.runId;
      run.scope.forEach((code) => state.checkingCodes.add(code));
      $("#runStrip").classList.remove("hidden");
      renderDepartments();
      pollRun(run.runId);
    } catch (_) {
      // 재연결 실패는 새 실행을 막지 않는다.
    }
  }

  async function init() {
    bindEvents();
    try {
      const session = await fetch("/api/v1/session").then((response) => response.json());
      state.nonce = session.nonce;
      // 공통 설정 화면을 띄울지는 파일 존재 하나로 정해진다. gcloud 왕복(수 초)을
      // 기다릴 이유가 없어 먼저 띄우고, 계정·프로젝트는 뒤에서 채운다.
      if (session.commonExists === false) showCommonSetupShell();
      const [, env] = await Promise.all([loadDepartments(), loadEnvironment()]);
      if (env && !env.commonExists) showCommonSetup(env);
      else if (env?.commonValid) await reconnectRun();
    } catch (error) {
      toast("콘솔을 초기화하지 못했습니다", error.message, "fail");
    }
  }

  init();
})();

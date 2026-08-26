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
    window.scrollTo({ top: 0, behavior: "smooth" });
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
    } catch (error) {
      toast("학과 목록을 불러오지 못했습니다", error.message, "fail");
    }
  }

  function renderEnvironment(env) {
    const projectMatch = env.gcloudProject && env.gcloudProject === env.configuredProject;
    const cards = [
      { icon: "⌂", label: "저장소", value: env.repository, detail: `${env.departmentCount}개 학과 설정` },
      { icon: "G", label: "GCP 프로젝트", value: env.configuredProject || "미설정", detail: env.region },
      { icon: "SA", label: "Drive 확인 서비스 계정", value: env.serviceAccount || "확인 필요", detail: "공유 드라이브 연결에 사용", copy: env.serviceAccount },
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
          </div>
          <small>${escapeHtml(card.detail)}</small>
        </div>
      </article>`).join("");
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
        : "필요한 리소스가 없습니다. Artifact Registry와 Native Firestore DB를 먼저 생성해 주세요.";
      $("#createCommonConfig").disabled = !ready;
    } catch (error) {
      if (requestId !== state.setupResourceRequest) return;
      status.dataset.status = "error";
      status.textContent = `리소스를 조회하지 못했습니다: ${error.message}`;
    }
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
            <div class="check-main"><div><b>${escapeHtml(item.name)}</b><small>${item.latencyMs ? `${item.latencyMs}ms` : statusLabels[item.status]}</small></div><p>${escapeHtml(item.detail)}</p>${item.action ? `<div class="check-action">${escapeHtml(item.action)}</div>` : ""}</div>
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

  function updateDrivePreflightAvailability() {
    const form = $("#departmentForm");
    const driveIds = splitIds(form.elements.driveIds.value);
    const signature = driveIds.join("\n");
    const button = $("#checkDriveIds");
    button.disabled = driveIds.length === 0 || button.classList.contains("is-checking");
    if (state.drivePreflightSignature && state.drivePreflightSignature !== signature) {
      state.drivePreflightSignature = "";
      const status = $("#drivePreflightStatus");
      status.dataset.status = "changed";
      status.textContent = "입력값이 변경되었습니다. 다시 연결을 확인해 주세요.";
    }
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
    try {
      const result = await api("/api/v1/departments/drive-preflight", {
        method: "POST",
        body: { driveIds, code: form.elements.code.value.trim().toLowerCase() },
      });
      if (splitIds(form.elements.driveIds.value).join("\n") !== signature) return;
      state.drivePreflightSignature = signature;
      status.dataset.status = String(result.status || "WARN").toLowerCase();
      status.textContent = result.action
        ? `${result.detail} · ${result.action}`
        : result.detail;
    } catch (error) {
      if (splitIds(form.elements.driveIds.value).join("\n") !== signature) return;
      state.drivePreflightSignature = "";
      status.dataset.status = "fail";
      status.textContent = error.message;
    } finally {
      button.classList.remove("is-checking");
      updateDrivePreflightAvailability();
    }
  }

  function formPayload() {
    const form = $("#departmentForm");
    const value = (name) => form.elements[name]?.value?.trim() || "";
    return {
      code: value("code").toLowerCase(),
      name: value("name"),
      corpora: { staff: value("staffCorpus"), student: value("studentCorpus") },
      buckets: { hwpOriginal: value("hwpBucket"), source: value("sourceBucket") },
      drive: {
        driveIds: splitIds(value("driveIds")),
        syncFolderIds: splitIds(value("syncFolderIds")),
        studentFolderIds: splitIds(value("studentFolderIds")),
      },
      minInstances: { staff: Number(value("staffMin") || 0), student: Number(value("studentMin") || 0) },
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
  }

  async function loadDepartmentResources(selected = {}) {
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
      const corpora = resources.corpora || [];
      const buckets = resources.buckets || [];
      state.departmentBuckets = buckets;
      const corpusHtml = corpora.map((item) => {
        const shortId = item.name.split("/").at(-1);
        return `<option value="${escapeHtml(item.name)}">${escapeHtml(item.displayName)} · ${escapeHtml(shortId)}</option>`;
      }).join("");
      const bucketHtml = '<option value="">버킷 선택</option>' + buckets.map((item) => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)} · ${escapeHtml(item.location)}</option>`).join("");
      for (const name of ["staffCorpus", "studentCorpus"]) {
        const value = desired[name];
        form.elements[name].innerHTML = corpusHtml || '<option value="">RAG 코퍼스 없음</option>';
        if (value && !corpora.some((item) => item.name === value)) {
          form.elements[name].insertAdjacentHTML("beforeend", `<option value="${escapeHtml(value)}">현재 설정 · 조회되지 않음</option>`);
        }
        form.elements[name].disabled = !corpora.length;
        if (value) form.elements[name].value = value;
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
      if (!corpora.length || !buckets.length) {
        toast("선택할 GCP 리소스가 부족합니다", "RAG 코퍼스와 보호된 리전 버킷을 먼저 준비해 주세요.", "fail");
      }
    } catch (error) {
      if (requestId !== state.departmentResourceRequest) return;
      state.departmentBuckets = [];
      names.forEach((name) => { form.elements[name].innerHTML = '<option value="">리소스 조회 실패</option>'; });
      updateBucketSelectionState();
      toast("GCP 리소스를 불러오지 못했습니다", error.message, "fail");
    } finally {
      if (requestId === state.departmentResourceRequest) {
        button.disabled = false;
        button.textContent = "리소스 다시 불러오기";
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
    if (!payload.name) errors.name = ["학과명을 입력해 주세요."];
    if (!payload.corpora.staff) errors["corpora.staff"] = ["교직원 코퍼스를 입력해 주세요."];
    if (!payload.corpora.student) errors["corpora.student"] = ["학생 코퍼스를 입력해 주세요."];
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
      ["교직원 코퍼스", selectedText("staffCorpus", payload.corpora.staff)],
      ["학생 코퍼스", selectedText("studentCorpus", payload.corpora.student)],
      ["버킷", `${payload.buckets.hwpOriginal} / ${payload.buckets.source}`],
      ["Drive 범위", `${payload.drive.driveIds.length}개 drive · ${payload.drive.syncFolderIds.length}개 folder`],
      ["MCP 키", state.editingCode ? "기존 키 유지 · API 미노출" : "생성 시 자동 발급 · API 미노출"],
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
      if (validateStepOne()) showStep(2);
      return;
    }
    if (state.currentStep === 2 && await prepareReview()) showStep(3);
  }

  function resetWizard() {
    $("#departmentForm").reset();
    state.editingCode = null;
    state.editingRevision = null;
    state.drivePreflightSignature = "";
    $("#departmentForm").elements.code.readOnly = false;
    setEditorMode(false);
    clearFieldErrors();
    $$(".tag-preview").forEach((item) => { item.innerHTML = ""; });
    $("#drivePreflightStatus").textContent = "";
    $("#drivePreflightStatus").dataset.status = "";
    updateDrivePreflightAvailability();
    updateBucketSelectionState();
    renderStudentFolderPicker();
    $("#confirmCreate").checked = false;
    $("#validationBanner").classList.add("hidden");
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
      form.elements.name.value = config.name || "";
      form.elements.driveIds.value = (config.drive?.driveIds || []).join("\n");
      form.elements.syncFolderIds.value = (config.drive?.syncFolderIds || []).join("\n");
      form.elements.studentFolderIds.value = (config.drive?.studentFolderIds || []).join("\n");
      form.elements.staffMin.value = config.minInstances?.staff ?? 0;
      form.elements.studentMin.value = config.minInstances?.student ?? 0;
      $$("textarea").forEach(updateTagPreview);
      updateDrivePreflightAvailability();
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

  async function submitDepartment(event) {
    event.preventDefault();
    if (!$("#confirmCreate").checked) return;
    const button = $("#createDepartment");
    button.disabled = true;
    button.textContent = state.editingCode ? "저장 중…" : "생성 중…";
    try {
      const payload = formPayload();
      if (state.editingCode) payload.configRevision = state.editingRevision;
      const result = await api(
        state.editingCode ? `/api/v1/departments/${state.editingCode}` : "/api/v1/departments",
        { method: state.editingCode ? "PUT" : "POST", body: payload },
      );
      toast(state.editingCode ? "설정을 저장했습니다" : "YAML을 생성했습니다", result.path, "ok");
      resetWizard();
      await loadDepartments();
      switchView("dashboard");
      startStatus([result.code]);
    } catch (error) {
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
    target.innerHTML = splitIds(textarea.value).map((value) => `<span class="tag-chip" title="${escapeHtml(value)}">${escapeHtml(value)}</span>`).join("");
  }

  function renderStudentFolderPicker() {
    const form = $("#departmentForm");
    const syncIds = splitIds(form.elements.syncFolderIds.value);
    const studentInput = form.elements.studentFolderIds;
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
    picker.innerHTML = syncIds.map((id) => `
      <label class="folder-option" title="${escapeHtml(id)}">
        <input type="checkbox" value="${escapeHtml(id)}" ${selected.has(id) ? "checked" : ""} />
        <code>${escapeHtml(id)}</code>
      </label>
    `).join("");
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
    $("#nextStep").addEventListener("click", nextWizardStep);
    $("#previousStep").addEventListener("click", () => showStep(Math.max(1, state.currentStep - 1)));
    $("#cancelCreate").addEventListener("click", () => { resetWizard(); switchView("dashboard"); });
    $("#departmentForm").addEventListener("submit", submitDepartment);
    $("#commonSetupForm").addEventListener("submit", submitCommonSetup);
    $("#refreshGcloudAuth").addEventListener("click", refreshGcloudSetup);
    $("#commonSetupForm").elements.projectId.addEventListener("change", loadSetupResources);
    $("#commonSetupForm").elements.region.addEventListener("change", loadSetupResources);
    $("#refreshDepartmentResources").addEventListener("click", () => loadDepartmentResources());
    $("#departmentForm").elements.hwpBucket.addEventListener("change", updateBucketSelectionState);
    $("#departmentForm").elements.sourceBucket.addEventListener("change", updateBucketSelectionState);
    $("#departmentForm").elements.driveIds.addEventListener("input", updateDrivePreflightAvailability);
    $("#checkDriveIds").addEventListener("click", checkDriveIds);
    $("#environmentGrid").addEventListener("click", copyEnvironmentValue);
    $("#confirmCreate").addEventListener("change", (event) => { $("#createDepartment").disabled = !event.target.checked; });
    $$("textarea").forEach((item) => item.addEventListener("input", () => updateTagPreview(item)));
    $("#departmentForm").elements.syncFolderIds.addEventListener("input", renderStudentFolderPicker);
    $("#studentFolderPicker").addEventListener("change", syncStudentFolderSelection);
    $("#toggleStudentFolders").addEventListener("click", () => {
      const options = $$("#studentFolderPicker input");
      const selectAll = options.some((input) => !input.checked);
      options.forEach((input) => { input.checked = selectAll; });
      syncStudentFolderSelection();
    });
    $("#departmentForm").elements.code.addEventListener("input", (event) => {
      event.target.value = event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "");
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
      if (event.key === "Escape" && $("#detailDrawer").classList.contains("open")) closeDrawer();
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
      const [, env] = await Promise.all([loadDepartments(), loadEnvironment()]);
      if (env && !env.commonExists) showCommonSetup(env);
      else if (env?.commonValid) await reconnectRun();
    } catch (error) {
      toast("콘솔을 초기화하지 못했습니다", error.message, "fail");
    }
  }

  init();
})();

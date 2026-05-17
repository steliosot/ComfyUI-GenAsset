const STYLE_ID = "genasset-manager-style";
const GENASSET_NODE_LABELS = [
  "Test GenAsset Connection",
  "GenAsset Workflow Assistant",
  "Save To GenAsset",
  "Load Asset From GenAsset",
  "Load Version From GenAsset",
  "Save Metadata Patch To GenAsset",
  "Compare Two GenAsset Versions",
  "Create Branch Version In GenAsset",
  "Load Recipe To Widgets",
  "Find Assets In GenAsset",
  "List Asset Versions In GenAsset",
  "Load Current Version For Asset",
  "Promote Version In GenAsset",
  "Delete Version In GenAsset",
  "Fork Asset From Version In GenAsset",
  "Create Asset In GenAsset",
  "Rename Asset In GenAsset",
  "Upsert Asset Tags Fields",
  "Asset Summary In GenAsset",
  "GenAssetTestConnection",
  "GenAssetWorkflowAssistant",
  "GenAssetSaveGeneration",
  "GenAssetLoadVersion",
  "GenAssetLoadExactVersion",
  "GenAssetPatchVersionMetadata",
  "GenAssetCompareVersions",
  "GenAssetCreateBranchVersion",
  "GenAssetLoadRecipeToWidgets",
  "GenAssetFindAssets",
  "GenAssetListAssetVersions",
  "GenAssetLoadCurrentVersion",
  "GenAssetPromoteVersion",
  "GenAssetDeleteVersion",
  "GenAssetForkAssetFromVersion",
  "GenAssetCreateAsset",
  "GenAssetRenameAsset",
  "GenAssetUpsertAssetFields",
  "GenAssetAssetSummary",
];

function getComfyApp() {
  return window.comfyAPI?.app?.app || window.app;
}

function getComfyApi() {
  return window.comfyAPI?.api?.api;
}

function fetchGenAssetApi(path, options) {
  const comfyApi = getComfyApi();
  if (comfyApi?.fetchApi) return comfyApi.fetchApi(path, options);
  return fetch(path, options);
}

const state = {
  status: null,
  update: null,
  publicWorkflows: [],
  workspaceWorkflows: [],
  recentAssets: [],
  publicSearch: "",
  workspaceSearch: "",
  activeTab: "workflows",
  health: {
    loading: false,
    title: "Workflow Health",
    data: null,
    error: "",
  },
};

function resetTransientState() {
  state.publicWorkflows = [];
  state.workspaceWorkflows = [];
  state.recentAssets = [];
  state.publicSearch = "";
  state.workspaceSearch = "";
}

function ensureStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
    .genasset-manager-backdrop {
      position: fixed;
      inset: 0;
      z-index: 10000;
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(0, 0, 0, 0.58);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .genasset-manager-modal {
      width: min(1500px, calc(100vw - 28px));
      max-height: min(820px, calc(100vh - 28px));
      overflow: hidden;
      border: 1px solid #444850;
      border-radius: 8px;
      background: #17181b;
      color: #f5f5f5;
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.48);
    }
    .genasset-manager-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 22px;
      border-bottom: 1px solid #30333a;
      background: #1d1f23;
    }
    .genasset-manager-title {
      display: flex;
      align-items: baseline;
      gap: 10px;
      font-size: 24px;
      font-weight: 750;
    }
    .genasset-manager-version {
      color: #b9bec8;
      font-size: 13px;
      font-weight: 600;
    }
    .genasset-manager-close {
      border: 0;
      border-radius: 6px;
      background: #4b4f58;
      color: #fff;
      cursor: pointer;
      font-size: 22px;
      height: 38px;
      line-height: 1;
      width: 48px;
    }
    .genasset-manager-body {
      display: grid;
      grid-template-columns: 260px minmax(300px, 1fr) minmax(360px, 1.15fr) 250px;
      gap: 16px;
      align-items: start;
      max-height: calc(min(820px, calc(100vh - 28px)) - 76px);
      overflow: auto;
      padding: 16px;
    }
    .genasset-manager-tabs {
      display: flex;
      gap: 8px;
      padding: 12px 16px 0;
      border-bottom: 1px solid #30333a;
      background: #1d1f23;
    }
    .genasset-manager-tab {
      border: 1px solid #343841;
      border-bottom: 0;
      border-radius: 7px 7px 0 0;
      background: #202226;
      color: #b9bec8;
      cursor: pointer;
      font-size: 13px;
      font-weight: 750;
      min-height: 34px;
      padding: 0 14px;
    }
    .genasset-manager-tab-active {
      background: #17352b;
      border-color: #2f8f6f;
      color: #8ee7bd;
    }
    .genasset-manager-body-health {
      grid-template-columns: 300px minmax(0, 1fr);
    }
    .genasset-manager-section {
      border: 1px solid #343841;
      border-radius: 8px;
      background: #202226;
      padding: 14px;
    }
    .genasset-manager-section + .genasset-manager-section {
      margin-top: 12px;
    }
    .genasset-manager-section-title {
      align-items: center;
      display: flex;
      justify-content: space-between;
      gap: 10px;
      font-size: 14px;
      font-weight: 750;
      margin-bottom: 10px;
    }
    .genasset-manager-muted {
      color: #aeb4bf;
      font-size: 12px;
      line-height: 1.45;
    }
    .genasset-manager-small {
      color: #9ca3af;
      font-size: 11px;
      line-height: 1.45;
    }
    .genasset-manager-row {
      align-items: center;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 6px 0;
    }
    .genasset-manager-ok { color: #7dd3a7; }
    .genasset-manager-warn { color: #f6c76b; }
    .genasset-manager-bad { color: #ff8a8a; }
    .genasset-manager-button {
      border: 1px solid #4b5563;
      border-radius: 6px;
      background: #2b2f36;
      color: #f5f5f5;
      cursor: pointer;
      font-size: 12px;
      font-weight: 650;
      min-height: 30px;
      padding: 6px 10px;
    }
    .genasset-manager-button:hover { background: #363b44; }
    .genasset-manager-button-primary {
      border-color: #2f8f6f;
      background: #24785d;
    }
    .genasset-manager-button-primary:hover { background: #2f8f6f; }
    .genasset-manager-button-warning {
      border-color: #a56a20;
      background: #704516;
    }
    .genasset-manager-button-success {
      border-color: #2f8f6f;
      background: #24785d;
    }
    .genasset-manager-button:disabled {
      cursor: default;
      opacity: 0.55;
    }
    .genasset-manager-controls {
      display: grid;
      grid-template-columns: 1fr 1fr auto;
      gap: 8px;
      margin-bottom: 10px;
    }
    .genasset-manager-search-row {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 8px;
      margin-bottom: 10px;
    }
    .genasset-manager-input {
      border: 1px solid #3d424d;
      border-radius: 6px;
      background: #15171a;
      color: #f5f5f5;
      font-size: 12px;
      min-height: 30px;
      min-width: 0;
      padding: 0 9px;
    }
    .genasset-manager-input::placeholder { color: #7f8794; }
    .genasset-manager-select {
      border: 1px solid #3d424d;
      border-radius: 6px;
      background: #15171a;
      color: #f5f5f5;
      font-size: 12px;
      min-height: 30px;
      padding: 0 8px;
    }
    .genasset-manager-list {
      display: grid;
      gap: 8px;
    }
    .genasset-health-actions {
      display: grid;
      gap: 8px;
      margin-top: 12px;
    }
    .genasset-health-summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    .genasset-health-stat {
      border: 1px solid #343841;
      border-radius: 7px;
      background: #181a1e;
      padding: 10px;
    }
    .genasset-health-stat strong {
      display: block;
      font-size: 22px;
      line-height: 1.1;
      margin-bottom: 3px;
    }
    .genasset-health-stat span {
      color: #aeb4bf;
      font-size: 11px;
      font-weight: 650;
      text-transform: uppercase;
    }
    .genasset-health-table {
      border-collapse: collapse;
      width: 100%;
      font-size: 12px;
    }
    .genasset-health-table th,
    .genasset-health-table td {
      border-bottom: 1px solid #343841;
      padding: 8px 7px;
      text-align: left;
      vertical-align: top;
    }
    .genasset-health-table th {
      color: #aeb4bf;
      font-size: 10px;
      letter-spacing: .04em;
      text-transform: uppercase;
    }
    .genasset-health-chip {
      border-radius: 999px;
      display: inline-flex;
      font-size: 11px;
      font-weight: 750;
      padding: 3px 7px;
      white-space: nowrap;
    }
    .genasset-health-chip-good { background: #143d30; color: #8ee7bd; }
    .genasset-health-chip-bad { background: #4a2027; color: #ffb8c0; }
    .genasset-health-chip-warn { background: #4b3a18; color: #ffd77d; }
    .genasset-health-pre {
      background: #0d281f;
      border-radius: 8px;
      color: #d8f8e9;
      font-size: 12px;
      line-height: 1.45;
      max-height: 360px;
      overflow: auto;
      padding: 12px;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .genasset-health-link {
      color: #93c5fd;
      text-decoration: none;
    }
    .genasset-manager-card {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
      border: 1px solid #343841;
      border-radius: 7px;
      background: #25282e;
      padding: 10px 11px;
    }
    .genasset-manager-card-title {
      font-size: 13px;
      font-weight: 720;
      line-height: 1.25;
    }
    .genasset-manager-card-desc {
      color: #c8ced8;
      font-size: 11px;
      line-height: 1.35;
      margin-top: 4px;
    }
    .genasset-manager-card-meta {
      color: #9ca3af;
      font-size: 11px;
      margin-top: 6px;
    }
    .genasset-manager-recent-item {
      border-bottom: 1px solid #30333a;
      padding: 8px 0;
    }
    .genasset-manager-recent-item:last-child { border-bottom: 0; }
    .genasset-toolbar-button {
      align-items: center;
      border: 0;
      background: #24785d;
      color: #fff;
      cursor: pointer;
      display: inline-flex;
      font: inherit;
      font-size: 12px;
      font-weight: 500;
      gap: 8px;
      height: 100%;
      min-height: 32px;
      padding: 0 12px;
      white-space: nowrap;
    }
    .genasset-toolbar-button:hover { background: #2f8f6f; }
    .genasset-toolbar-button svg { height: 16px; width: 16px; }
    .genasset-node-badge {
      align-items: center;
      align-self: center;
      background: #030712;
      border: 1px solid rgba(32, 111, 85, 0.92);
      border-radius: 7px;
      box-shadow: 0 7px 18px rgba(32, 111, 85, 0.22), inset 0 0 0 1px rgba(232, 248, 239, 0.06);
      color: #f8fafc;
      display: inline-flex;
      flex: 0 0 auto;
      font-size: 12px;
      font-weight: 750;
      gap: 6px;
      letter-spacing: 0;
      line-height: 1;
      margin-left: 8px;
      min-height: 26px;
      padding: 0 9px;
      vertical-align: middle;
      white-space: nowrap;
    }
    .genasset-node-badge svg {
      color: #206f55;
      height: 14px;
      width: 14px;
    }
    @media (max-width: 940px) {
      .genasset-manager-body { grid-template-columns: 1fr; }
      .genasset-manager-body-health { grid-template-columns: 1fr; }
      .genasset-health-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .genasset-manager-controls { grid-template-columns: 1fr; }
      .genasset-manager-search-row { grid-template-columns: 1fr; }
    }
  `;
  document.head.appendChild(style);
}

function closeModal(backdrop) {
  if (backdrop?._genassetCloseOnEscape) {
    document.removeEventListener("keydown", backdrop._genassetCloseOnEscape);
  }
  backdrop?.remove();
}

async function readJsonResponse(response) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data?.error || `Request failed with HTTP ${response.status}`);
  return data;
}

function statusText(value, okText, badText, idleText = "Not checked") {
  if (value === true) return `<span class="genasset-manager-ok">✔ ${okText}</span>`;
  if (value === false) return `<span class="genasset-manager-bad">✖ ${badText}</span>`;
  return `<span class="genasset-manager-muted">• ${idleText}</span>`;
}

function formatRelativeDate(value) {
  if (!value) return "unknown";
  const time = new Date(value).getTime();
  if (!Number.isFinite(time)) return "unknown";
  const diff = Math.max(0, Date.now() - time);
  const minutes = Math.floor(diff / 60000);
  if (minutes < 2) return "just now";
  if (minutes < 60) return `${minutes} ${minutes === 1 ? "minute" : "minutes"} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} ${hours === 1 ? "hour" : "hours"} ago`;
  const days = Math.floor(hours / 24);
  return `${days} ${days === 1 ? "day" : "days"} ago`;
}

function emptyMessage(text) {
  const el = document.createElement("div");
  el.className = "genasset-manager-muted";
  el.textContent = text;
  return el;
}

function createManagerModal() {
  ensureStyle();
  document.querySelectorAll(".genasset-manager-backdrop").forEach((item) => item.remove());
  const backdrop = document.createElement("div");
  backdrop.className = "genasset-manager-backdrop";
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) closeModal(backdrop);
  });
  const closeOnEscape = (event) => {
    if (event.key === "Escape") {
      closeModal(backdrop);
    }
  };
  backdrop._genassetCloseOnEscape = closeOnEscape;
  document.addEventListener("keydown", closeOnEscape);

  const modal = document.createElement("div");
  modal.className = "genasset-manager-modal";
  modal.addEventListener("click", (event) => event.stopPropagation());

  const header = document.createElement("div");
  header.className = "genasset-manager-header";
  const title = document.createElement("div");
  title.className = "genasset-manager-title";
  title.innerHTML = `GenAsset <span class="genasset-manager-version">loading...</span>`;
  const close = document.createElement("button");
  close.className = "genasset-manager-close";
  close.type = "button";
  close.textContent = "×";
  close.addEventListener("click", () => closeModal(backdrop));
  header.append(title, close);

  const body = document.createElement("div");
  body.className = "genasset-manager-body";

  const tabs = document.createElement("div");
  tabs.className = "genasset-manager-tabs";
  tabs.innerHTML = `
    <button class="genasset-manager-tab" type="button" data-tab="workflows">Workflows</button>
    <button class="genasset-manager-tab" type="button" data-tab="health">Health</button>
  `;
  tabs.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeTab = button.dataset.tab || "workflows";
      renderAll(body, title);
    });
  });

  modal.append(header, tabs, body);
  backdrop.append(modal);
  document.body.appendChild(backdrop);
  return { backdrop, body, title };
}

function renderShell(body) {
  body.className = state.activeTab === "health" ? "genasset-manager-body genasset-manager-body-health" : "genasset-manager-body";
  if (state.activeTab === "health") {
    body.innerHTML = `
      <div class="genasset-manager-section" data-panel="health-controls"></div>
      <div class="genasset-manager-section" data-panel="health-results"></div>
    `;
    return;
  }
  body.innerHTML = `
    <div>
      <div class="genasset-manager-section" data-panel="status"></div>
      <div class="genasset-manager-section" data-panel="update"></div>
    </div>
    <div>
      <div class="genasset-manager-section" data-panel="public"></div>
    </div>
    <div>
      <div class="genasset-manager-section" data-panel="workspace"></div>
    </div>
    <div>
      <div class="genasset-manager-section" data-panel="recent"></div>
    </div>
  `;
}

function panel(body, name) {
  return body.querySelector(`[data-panel="${name}"]`);
}

function renderStatus(body) {
  const status = state.status || {};
  const el = panel(body, "status");
  const workspace = status.workspace?.name || "Default workspace";
  const tokenLine = status.token_configured ? `Token: ${status.token_source || "configured"}` : "Token: not configured";
  el.innerHTML = `
    <div class="genasset-manager-section-title">
      <span>Connection</span>
      <button class="genasset-manager-button" data-action="check-connection">Check</button>
    </div>
    <div class="genasset-manager-row"><span>Workspace</span><strong>${escapeHtml(workspace)}</strong></div>
    <div class="genasset-manager-row"><span>Token</span><span class="${status.token_configured ? "genasset-manager-ok" : "genasset-manager-warn"}">${escapeHtml(tokenLine)}</span></div>
    <div class="genasset-manager-row"><span>Connected</span>${statusText(status.connected, "Connected", "Not connected")}</div>
    <div class="genasset-manager-row"><span>API</span>${statusText(status.api_reachable, "API reachable", "API unreachable")}</div>
    <div class="genasset-manager-row"><span>Workspace</span>${statusText(status.workspace_synced, "Workspace synced", "Workspace not synced")}</div>
    ${status.error ? `<div class="genasset-manager-small genasset-manager-bad">${escapeHtml(status.error)}</div>` : ""}
  `;
  el.querySelector('[data-action="check-connection"]').addEventListener("click", () => checkConnection(body));
}

function renderUpdate(body) {
  const status = state.status || {};
  const update = state.update || {};
  const el = panel(body, "update");
  const version = status.version || "unknown";
  const lastUpdated = formatRelativeDate(status.last_updated);
  const isUpdating = Boolean(update.updating);
  const updateLine = isUpdating
    ? `<div class="genasset-manager-muted">• Updating GenAsset...</div>`
    : update.updated
      ? `<div class="genasset-manager-ok">✔ Updated successfully</div>`
      : update.ok
    ? update.update_available
      ? `<div class="genasset-manager-warn">⚠ Update available: v${escapeHtml(update.latest_version)}</div>`
      : `<div class="genasset-manager-ok">✔ Up to date</div>`
    : `<div class="genasset-manager-muted">• Update not checked</div>`;
  const updateButtonText = isUpdating ? "Updating..." : update.updated ? "Updated" : "Update";
  const canUpdate = Boolean(update.update_available) && !isUpdating && !update.updated;
  const updateButtonClass = update.updated ? "genasset-manager-button-success" : "genasset-manager-button-warning";
  el.innerHTML = `
    <div class="genasset-manager-section-title"><span>GenAsset Node</span></div>
    <div class="genasset-manager-row"><span>Version</span><strong>v${escapeHtml(version)}</strong></div>
    <div class="genasset-manager-row"><span>Last updated</span><span>${escapeHtml(lastUpdated)}</span></div>
    ${updateLine}
    ${update.message ? `<div class="genasset-manager-small ${update.ok === false ? "genasset-manager-bad" : "genasset-manager-muted"}">${escapeHtml(update.message)}</div>` : ""}
    ${update.restart_required ? `<div class="genasset-manager-small genasset-manager-warn">Restart ComfyUI, then refresh the browser to load the updated node.</div>` : ""}
    ${update.error ? `<div class="genasset-manager-small genasset-manager-bad">${escapeHtml(update.error)}</div>` : ""}
    <div style="display:flex; gap:8px; margin-top:10px; flex-wrap:wrap;">
      <button class="genasset-manager-button" data-action="check-update" ${isUpdating ? "disabled" : ""}>Check update</button>
      <button class="genasset-manager-button ${updateButtonClass}" data-action="run-update" ${canUpdate ? "" : "disabled"}>${escapeHtml(updateButtonText)}</button>
    </div>
  `;
  el.querySelector('[data-action="check-update"]').addEventListener("click", () => checkUpdate(body));
  el.querySelector('[data-action="run-update"]').addEventListener("click", () => runUpdate(body));
}

function workspaceOptions() {
  const status = state.status || {};
  const current = status.workspace || {};
  const raw = Array.isArray(status.workspaces) ? status.workspaces : [];
  const workspaces = raw.length ? raw : [{ id: current.id || "default", name: current.name || "Default workspace", organization: status.organization || null }];
  const organizations = [];
  const seen = new Set();
  for (const item of workspaces) {
    const org = item.organization || status.organization || { id: "default", name: "Default organization" };
    const id = org.id || "default";
    if (!seen.has(id)) {
      seen.add(id);
      organizations.push(org);
    }
  }
  return { organizations, workspaces };
}

function renderWorkspace(body) {
  const el = panel(body, "workspace");
  const { organizations, workspaces } = workspaceOptions();
  el.innerHTML = `
    <div class="genasset-manager-section-title">
      <span>Import Workspace Workflow</span>
      <button class="genasset-manager-button" data-action="load-workspace">Load</button>
    </div>
    <div class="genasset-manager-controls">
      <select class="genasset-manager-select" data-role="org-select">
        ${organizations.map((org) => `<option>${escapeHtml(org.name || "Default organization")}</option>`).join("")}
      </select>
      <select class="genasset-manager-select" data-role="workspace-select">
        ${workspaces.map((workspace) => `<option>${escapeHtml(workspace.name || "Default workspace")}</option>`).join("")}
      </select>
      <button class="genasset-manager-button" data-action="check-connection-small">Check</button>
    </div>
    <div class="genasset-manager-search-row">
      <input class="genasset-manager-input" data-role="workspace-search" type="search" placeholder="Search workspace workflows" value="${escapeHtml(state.workspaceSearch)}">
      <button class="genasset-manager-button" data-action="search-workspace">Search</button>
      <button class="genasset-manager-button" data-action="clear-workspace-search">Clear</button>
    </div>
    <div class="genasset-manager-small" style="margin-bottom:10px;">Shows the latest 10 importable workflows. Search can fetch more.</div>
    <div class="genasset-manager-list" data-list="workspace-workflows"></div>
  `;
  const searchInput = el.querySelector('[data-role="workspace-search"]');
  const runWorkspaceSearch = () => {
    state.workspaceSearch = searchInput.value.trim();
    loadWorkspaceWorkflows(body);
  };
  el.querySelector('[data-action="load-workspace"]').addEventListener("click", runWorkspaceSearch);
  el.querySelector('[data-action="search-workspace"]').addEventListener("click", runWorkspaceSearch);
  el.querySelector('[data-action="clear-workspace-search"]').addEventListener("click", () => {
    state.workspaceSearch = "";
    loadWorkspaceWorkflows(body);
  });
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") runWorkspaceSearch();
  });
  el.querySelector('[data-action="check-connection-small"]').addEventListener("click", () => checkConnection(body));
  renderWorkspaceWorkflowList(body);
}

function renderPublic(body) {
  const el = panel(body, "public");
  el.innerHTML = `
    <div class="genasset-manager-section-title">
      <span>Import Public Workflow</span>
      <button class="genasset-manager-button" data-action="load-public">Load</button>
    </div>
    <div class="genasset-manager-search-row">
      <input class="genasset-manager-input" data-role="public-search" type="search" placeholder="Search public workflows" value="${escapeHtml(state.publicSearch)}">
      <button class="genasset-manager-button" data-action="search-public">Search</button>
      <button class="genasset-manager-button" data-action="clear-public-search">Clear</button>
    </div>
    <div class="genasset-manager-list" data-list="public-workflows"></div>
  `;
  const searchInput = el.querySelector('[data-role="public-search"]');
  const renderPublicSearch = () => {
    state.publicSearch = searchInput.value.trim();
    renderPublicWorkflowList(body);
  };
  el.querySelector('[data-action="load-public"]').addEventListener("click", () => loadPublicWorkflows(body));
  el.querySelector('[data-action="search-public"]').addEventListener("click", renderPublicSearch);
  el.querySelector('[data-action="clear-public-search"]').addEventListener("click", () => {
    state.publicSearch = "";
    renderPublic(body);
  });
  searchInput.addEventListener("input", renderPublicSearch);
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") renderPublicSearch();
  });
  renderPublicWorkflowList(body);
}

function renderRecent(body) {
  const el = panel(body, "recent");
  el.innerHTML = `
    <div class="genasset-manager-section-title">
      <span>Recent Assets</span>
      <button class="genasset-manager-button" data-action="load-recent">Refresh</button>
    </div>
    <div class="genasset-manager-small" style="margin-bottom:8px;">Last synced assets and reusable workflows.</div>
    <div data-list="recent-assets"></div>
  `;
  el.querySelector('[data-action="load-recent"]').addEventListener("click", () => loadRecent(body));
  const list = el.querySelector('[data-list="recent-assets"]');
  if (!state.recentAssets.length) {
    list.appendChild(emptyMessage("Click Refresh to load recent workspace assets."));
    return;
  }
  for (const item of state.recentAssets.slice(0, 6)) {
    const row = document.createElement("div");
    row.className = "genasset-manager-recent-item";
    const meta = [item.workflow_name || "Workflow", formatRelativeDate(item.updated_at), item.user_name]
      .filter(Boolean)
      .join(" · ");
    row.innerHTML = `
      <div class="genasset-manager-card-title">${escapeHtml(item.name)}</div>
      <div class="genasset-manager-card-meta">${escapeHtml(meta)}</div>
    `;
    list.appendChild(row);
  }
}

function renderWorkspaceWorkflowList(body) {
  const list = body.querySelector('[data-list="workspace-workflows"]');
  if (!list) return;
  list.innerHTML = "";
  if (!state.workspaceWorkflows.length) {
    const message = state.workspaceSearch
      ? "No importable workspace workflows matched that search."
      : "Click Load to show the latest 10 importable workspace workflows.";
    list.appendChild(emptyMessage(message));
    return;
  }
  for (const item of state.workspaceWorkflows) {
    list.appendChild(workflowCard(item, (button) => importWorkspaceWorkflow(item, button)));
  }
}

function searchableText(item) {
  return [
    item.title,
    item.workflow_name,
    item.name,
    item.description,
    item.category,
    item.level,
    item.id,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function filteredPublicWorkflows() {
  const query = state.publicSearch.trim().toLowerCase();
  if (!query) return state.publicWorkflows;
  return state.publicWorkflows.filter((item) => searchableText(item).includes(query));
}

function renderPublicWorkflowList(body) {
  const list = body.querySelector('[data-list="public-workflows"]');
  if (!list) return;
  list.innerHTML = "";
  if (!state.publicWorkflows.length) {
    list.appendChild(emptyMessage("Click Load to show public GenAsset workflows."));
    return;
  }
  const workflows = filteredPublicWorkflows();
  if (!workflows.length) {
    list.appendChild(emptyMessage("No public workflows matched that search."));
    return;
  }
  for (const item of workflows) {
    list.appendChild(workflowCard(item, (button) => importPublicWorkflow(item, button)));
  }
}

function workflowCard(item, onImport) {
  const row = document.createElement("div");
  row.className = "genasset-manager-card";
  const title = item.title || item.workflow_name || item.name || item.id;
  const desc = item.description || item.name || "Ready-to-import GenAsset workflow.";
  const modelRequirements = Array.isArray(item.model_requirements) ? item.model_requirements.filter(Boolean) : [];
  const modelText = item.needs_model
    ? `Needs ${modelRequirements.length ? modelRequirements.join(", ") : "model"}`
    : item.node_count !== undefined
      ? "No model needed"
      : item.workflow_importable
        ? "Workflow saved"
        : "No workflow";
  const meta = item.node_count !== undefined
    ? `${item.level || "Beginner"} · ${item.category || "Workflow"} · ${item.node_count || 0} ${(item.node_count || 0) === 1 ? "node" : "nodes"} · ${modelText}`
    : `Current v${item.version_number || ""} · ${modelText}`.trim();
  row.innerHTML = `
    <div>
      <div class="genasset-manager-card-title">${escapeHtml(title)}</div>
      <div class="genasset-manager-card-desc">${escapeHtml(desc)}</div>
      <div class="genasset-manager-card-meta">${escapeHtml(meta)}</div>
    </div>
  `;
  const button = document.createElement("button");
  button.className = "genasset-manager-button genasset-manager-button-primary";
  button.type = "button";
  button.textContent = "Import";
  button.addEventListener("click", () => onImport(button));
  row.appendChild(button);
  return row;
}

function renderHealth(body) {
  const controls = panel(body, "health-controls");
  const results = panel(body, "health-results");
  const status = state.status || {};
  controls.innerHTML = `
    <div class="genasset-manager-section-title"><span>Workflow Health</span></div>
    <div class="genasset-manager-muted">Inspect the current graph before queueing. Resolver is suggestion-only; Doctor uses GenAsset AI through the configured workspace token.</div>
    <div class="genasset-manager-row" style="margin-top:12px;"><span>Base URL</span><strong>${escapeHtml(status.base_url || "https://genasset.xyz")}</strong></div>
    <div class="genasset-manager-row"><span>Token</span><span class="${status.token_configured ? "genasset-manager-ok" : "genasset-manager-warn"}">${escapeHtml(status.token_configured ? status.token_source || "configured" : "not configured")}</span></div>
    <div class="genasset-health-actions">
      <button class="genasset-manager-button genasset-manager-button-primary" data-action="health-doctor">Run Doctor</button>
      <button class="genasset-manager-button" data-action="health-resolve">Resolve Models</button>
      <button class="genasset-manager-button" data-action="health-repro">Preview Repro Lock</button>
      <button class="genasset-manager-button" data-action="health-refresh">Refresh Current Workflow</button>
    </div>
  `;
  controls.querySelector('[data-action="health-doctor"]').addEventListener("click", () => runHealthAction(body, "doctor"));
  controls.querySelector('[data-action="health-resolve"]').addEventListener("click", () => runHealthAction(body, "resolve"));
  controls.querySelector('[data-action="health-repro"]').addEventListener("click", () => runHealthAction(body, "repro"));
  controls.querySelector('[data-action="health-refresh"]').addEventListener("click", () => runHealthAction(body, "refresh"));

  renderHealthResults(results);
}

function renderHealthResults(results) {
  const health = state.health || {};
  if (health.loading) {
    results.innerHTML = `
      <div class="genasset-manager-section-title"><span>${escapeHtml(health.title || "Workflow Health")}</span></div>
      <div class="genasset-manager-muted">Reading the current workflow...</div>
    `;
    return;
  }
  if (health.error) {
    results.innerHTML = `
      <div class="genasset-manager-section-title"><span>${escapeHtml(health.title || "Workflow Health")}</span></div>
      <div class="genasset-manager-bad">${escapeHtml(health.error)}</div>
    `;
    return;
  }
  if (!health.data) {
    results.innerHTML = `
      <div class="genasset-manager-section-title"><span>Health results</span></div>
      <div class="genasset-manager-muted">Run Doctor or Resolve Models to inspect the current workflow.</div>
    `;
    return;
  }
  if (health.kind === "resolve") return renderModelResolverResults(results, health.data);
  if (health.kind === "repro") return renderReproResults(results, health.data);
  if (health.kind === "doctor") return renderDoctorResults(results, health.data);
  if (health.kind === "refresh") return renderWorkflowRefreshResults(results, health.data);
}

function healthStat(label, value) {
  return `<div class="genasset-health-stat"><strong>${escapeHtml(value ?? 0)}</strong><span>${escapeHtml(label)}</span></div>`;
}

function healthChip(text, kind) {
  const suffix = kind === "good" ? "good" : kind === "bad" ? "bad" : "warn";
  return `<span class="genasset-health-chip genasset-health-chip-${suffix}">${escapeHtml(text)}</span>`;
}

function renderModelResolverResults(results, data) {
  const summary = data.summary || {};
  const models = Array.isArray(data.models) ? data.models : [];
  const rows = models.map((model) => {
    const sources = Array.isArray(model.suggestions)
      ? model.suggestions.map((source) => `<a class="genasset-health-link" target="_blank" href="${escapeHtml(source.url)}">${escapeHtml(source.label)}</a>`).join(" · ")
      : "";
    return `
      <tr>
        <td>${healthChip(model.status || "unknown", model.status === "found" ? "good" : "bad")}</td>
        <td>${escapeHtml(model.type || "model")}</td>
        <td><strong>${escapeHtml(model.name || "")}</strong><div class="genasset-manager-small">${escapeHtml([model.node_class, model.input].filter(Boolean).join(" · "))}</div></td>
        <td>${escapeHtml((model.expected_folders || []).join(", "))}</td>
        <td>${sources || escapeHtml(model.matched_folder || "")}</td>
      </tr>
    `;
  }).join("");
  results.innerHTML = `
    <div class="genasset-manager-section-title"><span>Model Resolver</span></div>
    <div class="genasset-health-summary">
      ${healthStat("Models", summary.total)}
      ${healthStat("Found", summary.found)}
      ${healthStat("Missing", summary.missing)}
      ${healthStat("Mode", "Suggest")}
    </div>
    ${rows ? `
      <table class="genasset-health-table">
        <thead><tr><th>Status</th><th>Type</th><th>Model</th><th>Expected folder</th><th>Source</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    ` : `<div class="genasset-manager-muted">No model loader inputs were detected in this workflow.</div>`}
  `;
}

function renderReproResults(results, data) {
  const repro = data.repro_lock || data;
  results.innerHTML = `
    <div class="genasset-manager-section-title"><span>Repro Lock preview</span></div>
    <div class="genasset-manager-muted" style="margin-bottom:10px;">This metadata is attached automatically when Save To GenAsset creates a version.</div>
    <pre class="genasset-health-pre">${escapeHtml(JSON.stringify(repro, null, 2))}</pre>
  `;
}

function renderDoctorResults(results, data) {
  const diagnostics = data.diagnostics || {};
  const summary = diagnostics.summary || {};
  const doctor = data.doctor || {};
  const doctorText = doctor.summary || doctor.explanation || doctor.notes_md || doctor.message || JSON.stringify(doctor, null, 2);
  const issues = Array.isArray(diagnostics.issues) ? diagnostics.issues : [];
  const rows = issues.map((issue) => `
    <tr>
      <td>${healthChip(issue.severity || "info", issue.severity === "error" ? "bad" : "warn")}</td>
      <td>${escapeHtml(issue.kind || "")}</td>
      <td>${escapeHtml(issue.message || "")}</td>
    </tr>
  `).join("");
  results.innerHTML = `
    <div class="genasset-manager-section-title"><span>Workflow Doctor</span></div>
    <div class="genasset-health-summary">
      ${healthStat("Nodes", summary.node_count)}
      ${healthStat("Errors", summary.error_count)}
      ${healthStat("Warnings", summary.warning_count)}
      ${healthStat("Missing models", summary.missing_model_count)}
    </div>
    <div class="genasset-manager-section" style="margin-bottom:12px;">
      <div class="genasset-manager-section-title"><span>GenAsset AI guidance</span></div>
      <pre class="genasset-health-pre">${escapeHtml(typeof doctorText === "string" ? doctorText : JSON.stringify(doctorText, null, 2))}</pre>
    </div>
    ${rows ? `
      <table class="genasset-health-table">
        <thead><tr><th>Severity</th><th>Issue</th><th>Message</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    ` : `<div class="genasset-manager-muted">No deterministic issues were found.</div>`}
  `;
}

function renderWorkflowRefreshResults(results, data) {
  results.innerHTML = `
    <div class="genasset-manager-section-title"><span>Current workflow</span></div>
    <div class="genasset-health-summary">
      ${healthStat("Prompt nodes", data.prompt_nodes)}
      ${healthStat("Registered node types", data.known_node_types)}
      ${healthStat("Workflow nodes", data.workflow_nodes)}
      ${healthStat("Links", data.links)}
    </div>
  `;
}

async function runHealthAction(body, kind) {
  const titles = {
    doctor: "Running Workflow Doctor",
    resolve: "Resolving models",
    repro: "Previewing Repro Lock",
    refresh: "Refreshing current workflow",
  };
  state.health = { loading: true, title: titles[kind] || "Workflow Health", data: null, error: "", kind };
  renderIfModalOpen(body);
  try {
    const payload = await currentWorkflowPayload({
      base_url: state.status?.base_url || "https://genasset.xyz",
      token: "ComfyUI/user/genasset.json",
    });
    if (kind === "refresh") {
      state.health = {
        loading: false,
        title: "Current workflow",
        kind,
        error: "",
        data: {
          prompt_nodes: Object.keys(payload.prompt || {}).length,
          known_node_types: payload.known_node_types.length,
          workflow_nodes: Array.isArray(payload.workflow?.nodes) ? payload.workflow.nodes.length : 0,
          links: Array.isArray(payload.workflow?.links) ? payload.workflow.links.length : 0,
        },
      };
    } else {
      const route = kind === "doctor" ? "/genasset/health/doctor" : kind === "repro" ? "/genasset/health/repro" : "/genasset/health/resolve";
      const response = await fetchGenAssetApi(route, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await readJsonResponse(response);
      state.health = { loading: false, title: titles[kind] || "Workflow Health", kind, data, error: "" };
    }
  } catch (error) {
    state.health = {
      loading: false,
      title: kind === "doctor" ? "Workflow Doctor needs GenAsset AI" : titles[kind] || "Workflow Health",
      kind,
      data: null,
      error: error?.message || "Workflow health check failed.",
    };
  }
  renderIfModalOpen(body);
}

async function currentWorkflowPayload(extra = {}) {
  const comfyApp = getComfyApp();
  const graphToPrompt = comfyApp?.graphToPrompt || window.app?.graphToPrompt;
  if (!graphToPrompt) throw new Error("ComfyUI graphToPrompt is not available yet.");
  const graphPrompt = await graphToPrompt.call(comfyApp || window.app);
  const workflow = graphPrompt?.workflow || comfyApp?.graph?.serialize?.() || window.app?.graph?.serialize?.() || {};
  const prompt = graphPrompt?.output || graphPrompt?.prompt || graphPrompt || {};
  return {
    workflow,
    prompt,
    known_node_types: knownNodeTypes(),
    ...extra,
  };
}

function knownNodeTypes() {
  const types = new Set();
  const registry = window.LiteGraph?.registered_node_types || {};
  for (const key of Object.keys(registry)) {
    types.add(key);
    const value = registry[key];
    if (value?.comfyClass) types.add(value.comfyClass);
    if (value?.type) types.add(value.type);
  }
  return [...types];
}

function renderAll(body, title) {
  if (!body || !title || !document.body.contains(body)) return false;
  const version = state.status?.version ? `v${state.status.version}` : "";
  title.innerHTML = `GenAsset <span class="genasset-manager-version">${escapeHtml(version)}</span>`;
  const modal = body.closest(".genasset-manager-modal");
  modal?.querySelectorAll?.(".genasset-manager-tab").forEach((tab) => {
    tab.classList.toggle("genasset-manager-tab-active", tab.dataset.tab === state.activeTab);
  });
  renderShell(body);
  if (state.activeTab === "health") {
    renderHealth(body);
    return true;
  }
  renderStatus(body);
  renderUpdate(body);
  renderWorkspace(body);
  renderPublic(body);
  renderRecent(body);
  return true;
}

function renderIfModalOpen(body) {
  const modal = body?.closest?.(".genasset-manager-modal");
  const title = modal?.querySelector?.(".genasset-manager-title");
  return renderAll(body, title);
}

async function loadInitialStatus(body, title) {
  try {
    const response = await fetchGenAssetApi("/genasset/manager/status");
    state.status = await readJsonResponse(response);
  } catch (error) {
    state.status = { version: "unknown", connected: false, api_reachable: false, workspace_synced: false, error: error?.message || "Could not load status." };
  }
  renderAll(body, title);
}

async function checkConnection(body) {
  const buttons = body.querySelectorAll('[data-action="check-connection"], [data-action="check-connection-small"]');
  buttons.forEach((button) => (button.disabled = true));
  try {
    const response = await fetchGenAssetApi("/genasset/manager/status?check=1");
    state.status = await readJsonResponse(response);
  } catch (error) {
    state.status = { ...(state.status || {}), connected: false, api_reachable: false, workspace_synced: false, error: error?.message || "Connection check failed." };
  }
  renderIfModalOpen(body);
}

async function checkUpdate(body) {
  const button = body.querySelector('[data-action="check-update"]');
  if (button) button.disabled = true;
  try {
    const response = await fetchGenAssetApi("/genasset/manager/update-check");
    state.update = await readJsonResponse(response);
  } catch (error) {
    state.update = { ok: false, error: error?.message || "Update check failed." };
  }
  renderIfModalOpen(body);
}

async function runUpdate(body) {
  state.update = { ...(state.update || {}), updating: true, error: "", message: "" };
  renderIfModalOpen(body);
  try {
    const response = await fetchGenAssetApi("/genasset/manager/update", { method: "POST" });
    state.update = await readJsonResponse(response);
  } catch (error) {
    state.update = {
      ...(state.update || {}),
      ok: false,
      updating: false,
      updated: false,
      error: error?.message || "Update failed.",
      message: "GenAsset update failed.",
    };
  }
  renderIfModalOpen(body);
}

async function loadRecent(body) {
  const button = body.querySelector('[data-action="load-recent"]');
  if (button) button.disabled = true;
  try {
    const response = await fetchGenAssetApi("/genasset/manager/recent?page_size=6");
    const data = await readJsonResponse(response);
    state.recentAssets = Array.isArray(data.assets) ? data.assets : [];
  } catch (error) {
    state.recentAssets = [];
    alert(error?.message || "Could not load recent GenAsset assets.");
  }
  renderIfModalOpen(body);
}

async function loadWorkspaceWorkflows(body) {
  const button = body.querySelector('[data-action="load-workspace"]');
  const searchButton = body.querySelector('[data-action="search-workspace"]');
  if (button) button.disabled = true;
  if (searchButton) searchButton.disabled = true;
  const search = state.workspaceSearch.trim();
  const params = new URLSearchParams({
    page_size: search ? "30" : "10",
  });
  if (search) params.set("search", search);
  try {
    const response = await fetchGenAssetApi(`/genasset/manager/recent?${params.toString()}`);
    const data = await readJsonResponse(response);
    state.workspaceWorkflows = Array.isArray(data.workflows) ? data.workflows : [];
    state.recentAssets = Array.isArray(data.assets) ? data.assets.slice(0, 6) : state.recentAssets;
  } catch (error) {
    state.workspaceWorkflows = [];
    alert(error?.message || "Could not load workspace GenAsset workflows.");
  }
  renderIfModalOpen(body);
}

async function loadPublicWorkflows(body) {
  const button = body.querySelector('[data-action="load-public"]');
  if (button) button.disabled = true;
  try {
    const response = await fetchGenAssetApi("/genasset/catalog/workflows");
    const data = await readJsonResponse(response);
    state.publicWorkflows = Array.isArray(data.workflows) ? data.workflows : [];
  } catch (error) {
    state.publicWorkflows = [];
    alert(error?.message || "Could not load public GenAsset workflows.");
  }
  renderIfModalOpen(body);
}

async function importPublicWorkflow(item, button) {
  await importWorkflowFromUrl(`/genasset/catalog/workflows/${encodeURIComponent(item.id)}`, item.title || item.id, button);
}

async function importWorkspaceWorkflow(item, button) {
  await importWorkflowFromUrl(`/genasset/manager/workspace-workflows/${encodeURIComponent(item.id)}`, item.workflow_name || item.name || item.id, button);
}

async function importWorkflowFromUrl(url, title, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Importing...";
  try {
    const response = await fetchGenAssetApi(url);
    const data = await readJsonResponse(response);
    if (!data?.workflow?.nodes || !Array.isArray(data.workflow.nodes)) throw new Error("GenAsset did not return a visual workflow.");
    await loadWorkflowIntoComfy(data.workflow, data.title || title);
    closeModal(button.closest(".genasset-manager-backdrop"));
  } catch (error) {
    alert(error?.message || "Could not import GenAsset workflow.");
    if (document.body.contains(button)) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

async function loadWorkflowIntoComfy(workflow, title) {
  const comfyApp = getComfyApp();
  if (!comfyApp?.loadGraphData) throw new Error("ComfyUI is not ready to load workflows yet.");
  try {
    await comfyApp.loadGraphData(workflow, true, true, `GenAsset: ${title}`);
  } catch (error) {
    try {
      await comfyApp.loadGraphData(workflow);
    } catch {
      throw error;
    }
  }
}

function openManager() {
  resetTransientState();
  const { body, title } = createManagerModal();
  renderShell(body);
  renderAll(body, title);
  loadInitialStatus(body, title);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function isVisibleElement(element) {
  const rect = element.getBoundingClientRect();
  const style = window.getComputedStyle(element);
  return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0";
}

function managerButtonCandidates() {
  return [...document.querySelectorAll("button")].filter((button) => {
    const label = [button.getAttribute("aria-label"), button.getAttribute("title"), button.textContent]
      .filter(Boolean)
      .join(" ")
      .trim()
      .toLowerCase();
    return label.includes("manager") && isVisibleElement(button);
  });
}

function createToolbarButton() {
  const button = document.createElement("button");
  button.className = "genasset-toolbar-button";
  button.type = "button";
  button.title = "Open GenAsset manager";
  button.innerHTML = `
    ${genAssetLogoSvg()}
    <span>GenAsset</span>
  `;
  button.addEventListener("click", openManager);
  return button;
}

function genAssetLogoSvg() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <g fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 2.75 7.7 5.1 12 7.45l4.3-2.35L12 2.75Z"/>
        <path d="M7.7 5.1v4.7L12 12.15l4.3-2.35V5.1"/>
        <path d="M12 7.45v4.7"/>
        <path d="M6.9 10.15 2.6 12.5l4.3 2.35 4.3-2.35-4.3-2.35Z"/>
        <path d="M2.6 12.5v4.7l4.3 2.35 4.3-2.35v-4.7"/>
        <path d="M6.9 14.85v4.7"/>
        <path d="M17.1 10.15 12.8 12.5l4.3 2.35 4.3-2.35-4.3-2.35Z"/>
        <path d="M12.8 12.5v4.7l4.3 2.35 4.3-2.35v-4.7"/>
        <path d="M17.1 14.85v4.7"/>
      </g>
    </svg>
  `;
}

function createGenAssetNodeBadge() {
  const badge = document.createElement("span");
  badge.className = "genasset-node-badge";
  badge.innerHTML = `
    ${genAssetLogoSvg()}
    <span>GenAsset</span>
  `;
  return badge;
}

function isGenAssetNodeText(text) {
  const compact = String(text || "").replace(/\s+/g, " ").trim();
  if (!compact || compact.length > 360) return false;
  return GENASSET_NODE_LABELS.some((label) => compact.includes(label));
}

function rowTextWithoutBadges(row) {
  const clone = row.cloneNode(true);
  clone.querySelectorAll?.(".genasset-node-badge").forEach((badge) => badge.remove());
  return clone.textContent || "";
}

function candidateNodeSearchRows() {
  return [
    ...document.querySelectorAll(
      'button, [role="button"], [role="option"], [role="treeitem"], [cmd], [data-node-type], li, .comfyui-node-search-result, .comfy-node-search-result'
    ),
  ];
}

function badgeAnchorForRow(row) {
  const children = [...row.querySelectorAll("div, span")].filter((child) => {
    if (child.classList.contains("genasset-node-badge")) return false;
    const text = child.textContent || "";
    return isGenAssetNodeText(text) && text.length < 140;
  });
  return children[0] || row;
}

function decorateGenAssetNodeBadges() {
  ensureStyle();
  for (const row of candidateNodeSearchRows()) {
    if (!(row instanceof HTMLElement)) continue;
    if (row.closest(".genasset-manager-backdrop") || row.closest(".genasset-toolbar-button")) continue;
    if (!isVisibleElement(row)) continue;
    const isGenAssetRow = isGenAssetNodeText(rowTextWithoutBadges(row));
    const existingBadges = row.querySelectorAll(".genasset-node-badge");
    if (!isGenAssetRow) {
      existingBadges.forEach((badge) => badge.remove());
      delete row.dataset.genassetBadgeApplied;
      continue;
    }
    if (existingBadges.length) {
      row.dataset.genassetBadgeApplied = "1";
      continue;
    }
    const anchor = badgeAnchorForRow(row);
    anchor.appendChild(createGenAssetNodeBadge());
    row.dataset.genassetBadgeApplied = "1";
  }
}

function installToolbarButton() {
  ensureStyle();
  if (!document.body) return false;
  const existingButtons = [...document.querySelectorAll(".genasset-toolbar-button")];
  if (existingButtons.some(isVisibleElement)) return true;
  for (const existing of existingButtons) existing.remove();
  const managerButton = managerButtonCandidates()[0];
  if (!managerButton || !managerButton.parentElement) return false;
  const button = createToolbarButton();
  managerButton.parentElement.insertBefore(button, managerButton);
  return true;
}

function watchForToolbar() {
  if (!document.body) return;
  installToolbarButton();
  decorateGenAssetNodeBadges();
  const observer = new MutationObserver(() => {
    installToolbarButton();
    decorateGenAssetNodeBadges();
  });
  observer.observe(document.body, { childList: true, subtree: true });
  const interval = window.setInterval(() => {
    installToolbarButton();
    decorateGenAssetNodeBadges();
  }, 1000);
  window.setTimeout(() => {
    observer.disconnect();
    window.clearInterval(interval);
  }, 60000);
}

function installGenAssetManagerUi() {
  if (!document.body) return false;
  watchForToolbar();
  return true;
}

function runWhenReady() {
  if (installGenAssetManagerUi()) return;
  window.addEventListener("DOMContentLoaded", installGenAssetManagerUi, { once: true });
  window.setTimeout(installGenAssetManagerUi, 1000);
  window.setTimeout(installGenAssetManagerUi, 3000);
}

function registerExtensionWhenReady() {
  const comfyApp = getComfyApp();
  if (!comfyApp?.registerExtension) return false;
  comfyApp.registerExtension({
    name: "genasset.manager",
    setup() {
      runWhenReady();
    },
    commands: [
      { id: "genasset.openManager", label: "GenAsset", function: openManager },
      { id: "genasset.importWorkflow", label: "Import from GenAsset", function: openManager },
    ],
    getCanvasMenuItems() {
      return [null, { content: "GenAsset", callback: openManager }];
    },
  });
  return true;
}

function bootGenAssetManager() {
  runWhenReady();
  if (registerExtensionWhenReady()) return;
  window.setTimeout(registerExtensionWhenReady, 1000);
  window.setTimeout(registerExtensionWhenReady, 3000);
}

console.info("[GenAsset] manager extension loaded");
bootGenAssetManager();

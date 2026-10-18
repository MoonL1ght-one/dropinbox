/* DropInbox 前端逻辑：没有框架、没有构建步骤，改完刷新即可 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const COND_KEYS = [
  { key: "ext", label: "扩展名", type: "list", hint: "pdf, docx" },
  { key: "categories", label: "类别", type: "list", hint: "images, documents" },
  { key: "name_regex", label: "文件名正则", type: "text", hint: "(?i)invoice|发票" },
  { key: "name_contains", label: "文件名包含", type: "list", hint: "对账单, 报销" },
  { key: "name_glob", label: "文件名通配", type: "list", hint: "*.bak" },
  { key: "content_keywords", label: "内容关键词", type: "list", hint: "发票, invoice" },
  { key: "source_domains", label: "来源域名", type: "list", hint: "github.com" },
  { key: "size_min", label: "最小大小", type: "text", hint: "10KB" },
  { key: "size_max", label: "最大大小", type: "text", hint: "2GB" },
  { key: "older_than", label: "至少存在", type: "text", hint: "2h" },
  { key: "newer_than", label: "最多存在", type: "text", hint: "30m" },
];

const LIST_KEYS = new Set(COND_KEYS.filter((c) => c.type === "list").map((c) => c.key));

const VIEWS = {
  inbox: { title: "收件箱", sub: "下载目录里还没归位的文件" },
  rules: { title: "规则", sub: "决定每个文件去哪；优先级小的先匹配" },
  history: { title: "历史", sub: "每次归档都可整批撤回" },
  settings: { title: "设置", sub: "监听哪里、归档到哪、多久检查一次" },
};

const S = {
  data: null,
  view: "inbox",
  selected: new Set(),
  rulesDirty: false,
  pendingSig: "",
  busy: false,
};

/* ---------------------------------------------------------------- 工具 */

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function humanSize(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return i === 0 ? `${Math.round(n)} B` : `${n.toFixed(1)} ${units[i]}`;
}

let HOME = "";

function shortPath(path) {
  const text = String(path || "");
  if (HOME && text.toLowerCase().startsWith(HOME.toLowerCase())) {
    return "~" + text.slice(HOME.length).replace(/\//g, "\\");
  }
  return text.replace(/\//g, "\\");
}

function baseName(path) {
  const parts = String(path).split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return sameDay ? `今天 ${time}` : `${d.getMonth() + 1}/${d.getDate()} ${time}`;
}

function toast(message, kind = "ok") {
  const el = document.createElement("div");
  el.className = `toast is-${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity 200ms ease, transform 200ms ease";
    el.style.opacity = "0";
    el.style.transform = "translateY(6px)";
    setTimeout(() => el.remove(), 220);
  }, 3200);
}

function confirmDialog(title, body, okLabel = "确定") {
  return new Promise((resolve) => {
    const modal = $("#modal");
    $("#modal-title").textContent = title;
  $("#modal-body").textContent = body;
  $("#modal-ok").textContent = okLabel;
  modal.hidden = false;
  const close = (value) => {
    modal.hidden = true;
    $("#modal-ok").onclick = null;
    $("#modal-cancel").onclick = null;
    modal.onclick = null;
    document.removeEventListener("keydown", onKey);
    resolve(value);
  };
  const onKey = (e) => { if (e.key === "Escape") close(false); };
  document.addEventListener("keydown", onKey);
  $("#modal-ok").onclick = () => close(true);
  $("#modal-cancel").onclick = () => close(false);
  modal.onclick = (e) => { if (e.target === modal) close(false); };
  });
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let payload = null;
  try {
    payload = await res.json();
  } catch (err) {
    payload = null;
  }
  if (!res.ok) {
    const message = (payload && payload.error) || `请求失败（${res.status}）`;
    throw new Error(message);
  }
  return payload;
}

/* ---------------------------------------------------------------- 渲染 */

function render() {
  syncViewChrome();
  renderTopbar();
  renderStats();
  renderPending();
  renderSkipped();
  renderRules();
  renderHistory();
  renderSettings();
  renderWatch();
}

function syncViewChrome() {
  $$(".nav-item").forEach((btn) => btn.classList.toggle("is-active", btn.dataset.view === S.view));
  $$(".view").forEach((section) => section.classList.toggle("is-active", section.id === `view-${S.view}`));
}

function renderTopbar() {
  const meta = VIEWS[S.view];
  $("#view-title").textContent = meta.title;
  $("#view-sub").textContent = meta.sub;

  const actions = $("#topbar-actions");
  const data = S.data;
  let html = "";

  if (S.view === "inbox") {
    const count = data ? data.counts.pending : 0;
    html = `
      <button class="btn" id="act-rescan">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/></svg>
        重新扫描
      </button>
      <button class="btn btn-primary" id="act-apply" ${count ? "" : "disabled"}>
        ${S.selected.size ? `归档选中的 ${S.selected.size} 个` : `全部归档${count ? ` (${count})` : ""}`}
      </button>`;
  } else if (S.view === "rules") {
    html = `
      <button class="btn" id="act-rule-add">+ 新增规则</button>
      <button class="btn btn-primary" id="act-rules-save" ${S.rulesDirty ? "" : "disabled"}>保存改动</button>`;
  } else if (S.view === "history") {
    const undoable = data ? data.history.some((b) => b.status !== "undone") : false;
    html = `<button class="btn btn-danger" id="act-undo-last" ${undoable ? "" : "disabled"}>撤销最近一批</button>`;
  }

  actions.innerHTML = html;

  const rescan = $("#act-rescan");
  if (rescan) rescan.onclick = () => refreshPreview(true);
  const apply = $("#act-apply");
  if (apply) apply.onclick = () => applyNow();
  const addRule = $("#act-rule-add");
  if (addRule) addRule.onclick = () => addRule();
  const saveRules = $("#act-rules-save");
  if (saveRules) saveRules.onclick = () => saveRules();
  const undoLast = $("#act-undo-last");
  if (undoLast) undoLast.onclick = () => undoBatch("last");
}

function renderStats() {
  const data = S.data;
  if (!data) return;
  const pendingBytes = data.preview
    .filter((d) => d.action === "move")
    .reduce((sum, d) => sum + (d.size || 0), 0);

  const cards = [
    { label: "待归档", value: data.counts.pending, suffix: "个" },
    { label: "待处理体积", value: humanSize(pendingBytes) },
    { label: "累计归档", value: data.counts.files_sorted, suffix: "个" },
    { label: "可撤销批次", value: data.history.filter((b) => b.status !== "undone").length, suffix: "批" },
  ];

  $("#stats").innerHTML = cards
    .map((c) => `
      <div class="stat">
        <span class="stat-label">${c.label}</span>
        <div class="stat-value">${escapeHtml(String(c.value))}${c.suffix ? `<small>${c.suffix}</small>` : ""}</div>
      </div>`)
    .join("");
}

function renderPending() {
  const data = S.data;
  if (!data) return;
  const rows = data.preview.filter((d) => d.action === "move");
  const container = $("#pending-rows");
  const signature = rows.map((d) => `${d.path}|${d.target}`).join("::");

  $("#pending-hint").textContent = rows.length
    ? `${rows.length} 个文件等待归位，共 ${humanSize(rows.reduce((s, d) => s + d.size, 0))}`
    : "没有待归档文件";

  $("#pill-inbox").textContent = String(rows.length);

  const available = new Set(rows.map((d) => d.path));
  Array.from(S.selected).forEach((p) => { if (!available.has(p)) S.selected.delete(p); });

  if (!rows.length) {
    container.innerHTML = `
      <div class="empty">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>
        <p>收件箱是空的</p>
        <small>${data.watcher.running ? "正在监听，新下载的文件会自动出现在这里" : "点击左下角「开始监听」，下载完成即自动归档"}</small>
      </div>`;
  } else {
    container.innerHTML = rows
      .map((d) => {
        const checked = S.selected.has(d.path) ? "checked" : "";
        return `
        <div class="row" data-path="${escapeHtml(d.path)}">
          <input class="row-check" type="checkbox" ${checked} data-path="${escapeHtml(d.path)}" />
          <div class="file-main">
            <div class="file-name" title="${escapeHtml(d.path)}">${escapeHtml(d.name)}</div>
            <div class="file-reason"><b>${escapeHtml(d.rule_name || "规则")}</b> · ${escapeHtml(d.reason || "")}</div>
          </div>
          <div class="file-target" title="${escapeHtml(d.target)}"><span class="arrow">→</span><span>${escapeHtml(d.target_rel || shortPath(d.target))}</span></div>
          <div class="file-size">${humanSize(d.size)}</div>
        </div>`;
      })
      .join("");
  }

  $$(".row-check", container).forEach((box) => {
    box.onchange = () => {
      const path = box.dataset.path;
      if (box.checked) S.selected.add(path);
      else S.selected.delete(path);
      renderTopbar();
      updateSelectAll(rows.length);
    };
  });

  updateSelectAll(rows.length);
  S.pendingSig = signature;
}

function updateSelectAll(total) {
  const box = $("#select-all");
  const wrap = $("#select-all-wrap");
  wrap.style.display = total ? "inline-flex" : "none";
  box.checked = total > 0 && S.selected.size === total;
  box.indeterminate = S.selected.size > 0 && S.selected.size < total;
}

function renderSkipped() {
  const data = S.data;
  if (!data) return;
  const rows = data.preview.filter((d) => d.action !== "move");
  $("#skipped-count").textContent = String(rows.length);
  $("#skipped-panel").style.display = rows.length ? "block" : "none";
  $("#skipped-rows").innerHTML = rows
    .map((d) => `
      <div class="row row-quiet is-skip">
        <span></span>
        <div class="file-main">
          <div class="file-name">${escapeHtml(d.name)}</div>
          <div class="file-reason">${escapeHtml(d.reason)}</div>
        </div>
        <span class="rule-tag is-muted">跳过</span>
        <div class="file-size">${humanSize(d.size)}</div>
      </div>`)
    .join("");
}

function ruleToConditionText(key, value) {
  if (Array.isArray(value)) return value.join(", ");
  if (value === null || value === undefined) return "";
  return String(value);
}

function renderRules() {
  const data = S.data;
  if (!data || S.view !== "rules" || S.rulesDirty) return;
  const rules = (data.rules || []).slice().sort((a, b) => (a.priority || 0) - (b.priority || 0));
  $("#pill-rules").textContent = String(rules.filter((r) => r.enabled !== false).length);

  const editor = $("#rules-editor");
  if (!rules.length) {
    editor.innerHTML = `<div class="empty"><p>还没有规则</p><small>新增一条，或切到 JSON 模式手写</small></div>`;
    return;
  }

  editor.innerHTML = rules
    .map((rule, index) => {
      const match = rule.match || {};
      const condRows = Object.keys(match)
        .map((key) => {
          const meta = COND_KEYS.find((c) => c.key === key);
          const label = meta ? meta.label : key;
          const hint = meta ? meta.hint : "";
          const value = ruleToConditionText(key, match[key]);
          return `
            <div class="cond-row" data-key="${escapeHtml(key)}">
              <select class="cond-key" title="条件类型">
                ${COND_KEYS.map((c) => `<option value="${c.key}" ${c.key === key ? "selected" : ""}>${c.label}</option>`).join("")}
              </select>
              <input class="cond-val" value="${escapeHtml(value)}" placeholder="${escapeHtml(hint)}" title="${escapeHtml(hint)}" spellcheck="false" />
              <button class="icon-btn" data-act="del-cond" title="删除条件">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>
              </button>
            </div>`;
        })
        .join("");

      return `
      <div class="rule-card ${rule.enabled === false ? "is-off" : ""}" data-index="${index}">
        <div class="rule-top">
          <label class="toggle" title="启用 / 停用">
            <input type="checkbox" ${rule.enabled === false ? "" : "checked"} data-act="toggle" />
            <span></span>
          </label>
          <input class="rule-name" value="${escapeHtml(rule.name || "")}" data-act="name" spellcheck="false" />
          <span class="cond-hint">优先级</span>
          <input class="rule-prio" type="number" value="${Number(rule.priority ?? 100)}" data-act="priority" />
          <span class="rule-tag">${escapeHtml(rule.match_mode === "any" ? "任一条件" : "全部条件")}</span>
          <div class="rule-actions">
            <button class="icon-btn" data-act="mode" title="切换 全部/任一">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h9M4 12h13M4 17h7"/></svg>
            </button>
            <button class="icon-btn" data-act="add-cond" title="添加条件">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
            </button>
            <button class="icon-btn" data-act="del-rule" title="删除规则">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/></svg>
            </button>
          </div>
        </div>
        <div class="rule-body">
          ${condRows || `<div class="cond-hint">无条件 —— 这条规则会匹配所有文件（兜底）</div>`}
          <div class="rule-target">
            <span>归档到</span>
            <input value="${escapeHtml(rule.target || "")}" data-act="target" spellcheck="false" />
            <span class="cond-hint">{year} {month} {ext} {source}</span>
          </div>
        </div>
      </div>`;
    })
    .join("");

  bindRuleEvents();
}

function bindRuleEvents() {
  $$("#rules-editor [data-act]").forEach((el) => {
    const act = el.dataset.act;
    const card = el.closest(".rule-card");
    if (!card) return;
    const index = Number(card.dataset.index);

    if (act === "del-rule") {
      el.onclick = () => {
        const rule = currentRules()[index];
        if (!rule) return;
        S.data.rules = S.data.rules.filter((r) => r !== rule);
        S.rulesDirty = false;
        saveRules(true);
      };
    } else if (act === "add-cond") {
      el.onclick = () => {
        const rule = currentRules()[index];
        if (!rule) return;
        rule.match = rule.match || {};
        if (!("name_contains" in rule.match)) rule.match.name_contains = [];
        S.rulesDirty = false;
        renderRules();
        card.querySelectorAll(".cond-val").forEach((input, i, arr) => {
          if (i === arr.length - 1) input.focus();
        });
      };
    } else if (act === "mode") {
      el.onclick = () => {
        const rule = currentRules()[index];
        if (!rule) return;
        rule.match_mode = rule.match_mode === "any" ? "all" : "any";
        S.rulesDirty = true;
        renderRules();
      };
    } else if (act === "toggle") {
      el.onchange = () => {
        const rule = currentRules()[index];
        if (!rule) return;
        rule.enabled = el.checked;
        S.rulesDirty = true;
        renderTopbar();
        card.classList.toggle("is-off", !el.checked);
      };
    } else if (act === "name") {
      el.oninput = () => {
        const rule = currentRules()[index];
        if (rule) rule.name = el.value;
        S.rulesDirty = true;
        renderTopbar();
      };
    } else if (act === "priority") {
      el.onchange = () => {
        const rule = currentRules()[index];
        if (rule) rule.priority = Number(el.value) || 100;
        S.rulesDirty = true;
        renderTopbar();
      };
    } else if (act === "target") {
      el.oninput = () => {
        const rule = currentRules()[index];
        if (rule) rule.target = el.value;
        S.rulesDirty = true;
        renderTopbar();
      };
    } else if (act === "del-cond") {
      el.onclick = () => {
        const rule = currentRules()[index];
        const row = el.closest(".cond-row");
        if (!rule || !row) return;
        delete rule.match[row.dataset.key];
        S.rulesDirty = true;
        renderRules();
      };
    }
  });

  $$("#rules-editor .cond-row").forEach((row) => {
    const card = row.closest(".rule-card");
    const index = Number(card.dataset.index);
    const keySelect = $(".cond-key", row);
    const valueInput = $(".cond-val", row);

    const commit = () => {
      const rule = currentRules()[index];
      if (!rule) return;
      rule.match = rule.match || {};
      const oldKey = row.dataset.key;
      const newKey = keySelect.value;
      const raw = valueInput.value;
      const value = LIST_KEYS.has(newKey)
        ? raw.split(",").map((s) => s.trim()).filter(Boolean)
        : raw.trim();
      if (oldKey !== newKey) delete rule.match[oldKey];
      rule.match[newKey] = value;
      S.rulesDirty = true;
      renderTopbar();
    };

    valueInput.oninput = commit;
    keySelect.onchange = () => {
      row.dataset.key = keySelect.value;
      const meta = COND_KEYS.find((c) => c.key === keySelect.value);
      valueInput.placeholder = meta ? meta.hint : "";
      commit();
    };
  });
}

function currentRules() {
  if (!S.data) return [];
  return (S.data.rules || []).slice().sort((a, b) => (a.priority || 0) - (b.priority || 0));
}

async function saveRules(silent = false) {
  try {
    const payload = await api("/api/rules", {
      method: "PUT",
      body: JSON.stringify({ rules: S.data.rules }),
    });
    S.data.rules = payload.rules;
    S.rulesDirty = false;
    renderTopbar();
    renderRules();
    if (!silent) toast("规则已保存", "ok");
    refreshPreview(false);
  } catch (err) {
    toast(err.message, "err");
  }
}

function addRule() {
  if (!S.data) return;
  const rule = {
    id: `rule-${Date.now()}`,
    name: "新规则",
    enabled: false,
    priority: 200,
    match_mode: "all",
    match: { name_contains: ["关键词"] },
    target: "未分类",
  };
  S.data.rules = (S.data.rules || []).concat([rule]);
  S.rulesDirty = false;
  saveRules(true).then(() => {
    const cards = $$(".rule-card");
    const last = cards[cards.length - 1];
    if (last) {
      const name = $(".rule-name", last);
      if (name) { name.focus(); name.select(); }
    }
  });
  toast("新规则默认停用，配置好条件后再打开开关", "warn");
}

function renderHistory() {
  const data = S.data;
  if (!data) return;
  const rows = data.history || [];
  $("#pill-history") && ($("#pill-history").textContent = String(rows.length));

  if (!rows.length) {
    $("#history-rows").innerHTML = `<div class="empty"><p>还没有归档记录</p><small>每次归档都会留下可撤销的记录</small></div>`;
    return;
  }

  $("#history-rows").innerHTML = rows
    .map((b) => {
      const statusMap = {
        applied: { text: "可撤销", cls: "rule-tag is-ok" },
        undone: { text: "已撤销", cls: "rule-tag is-muted" },
        partial: { text: "部分撤销", cls: "rule-tag is-muted" },
      };
      const status = statusMap[b.status] || { text: b.status, cls: "rule-tag is-muted" };
      const disabled = b.status === "undone" ? "disabled" : "";
      return `
      <div class="row row-history">
        <span></span>
        <div class="file-main">
          <div class="file-name">批次 #${b.id} · ${b.count} 个文件</div>
          <div class="file-reason">${fmtTime(b.created_at)} · ${escapeHtml(b.kind || "")}${b.note ? " · " + escapeHtml(b.note) : ""}</div>
        </div>
        <span class="${status.cls}">${status.text}</span>
        <div style="display:flex;align-items:center;gap:10px">
          <span class="file-size">${humanSize(b.bytes)}</span>
          <button class="btn btn-sm" data-batch="${b.id}" ${disabled}>撤销</button>
        </div>
      </div>`;
    })
    .join("");

  $$("#history-rows [data-batch]").forEach((btn) => {
    btn.onclick = () => undoBatch(Number(btn.dataset.batch));
  });
}

function renderSettings() {
  const data = S.data;
  if (!data) return;
  HOME = data.home || "";
  if (document.activeElement && ["cfg-watch", "cfg-root", "cfg-poll", "cfg-settle"].includes(document.activeElement.id)) {
    return;
  }
  const cfg = data.config;
  $("#cfg-watch").value = (cfg.watch_dirs || []).join("\n");
  $("#cfg-root").value = shortPath(cfg.archive_root || "");
  $("#cfg-poll").value = cfg.poll_interval ?? 2;
  $("#cfg-settle").value = cfg.settle_seconds ?? 1.5;
  $("#cfg-recursive").checked = !!cfg.recursive;

  const items = [
    { label: "配置文件", value: data.paths.config },
    { label: "规则文件", value: data.paths.rules },
    { label: "历史数据库（SQLite）", value: data.paths.db },
  ];
  $("#path-list").innerHTML = items
    .map((it) => `<div class="meta-item"><b>${it.label}</b><code>${escapeHtml(shortPath(it.value))}</code></div>`)
    .join("");
}

function renderWatch() {
  const data = S.data;
  if (!data) return;
  const running = data.watcher.running;
  const box = $("#watch-state");
  box.classList.toggle("is-live", running);
  $("#watch-label").textContent = running ? "正在监听" : "未监听";
  $("#watch-sub").textContent = running
    ? `已归档 ${data.watcher.total_moved} 个文件`
    : "空闲中";
  $("#btn-watch").textContent = running ? "停止监听" : "开始监听";
  $("#btn-watch").className = running ? "btn btn-watch btn-danger" : "btn btn-watch";
  $("#version").textContent = `v${data.version}`;
}

/* ---------------------------------------------------------------- 动作 */

async function loadState() {
  S.data = await api("/api/state");
  render();
}

async function refreshPreview(verbose = false) {
  try {
    S.data = await api("/api/preview", { method: "POST", body: "{}" });
    render();
    if (verbose) toast(`扫描完成，${S.data.counts.pending} 个文件待归档`, "ok");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function applyNow() {
  if (S.busy) return;
  const paths = Array.from(S.selected);
  S.busy = true;
  try {
    const payload = await api("/api/apply", {
      method: "POST",
      body: JSON.stringify(paths.length ? { paths } : {}),
    });
    S.data = payload;
    S.selected.clear();
    const result = payload.result || {};
    render();
    if (result.moved) {
      toast(`已归档 ${result.moved} 个文件${result.failed ? `，${result.failed} 个失败` : ""}`, "ok");
    } else if (result.failed) {
      toast(`${result.failed} 个文件移动失败`, "err");
    } else {
      toast("没有可归档的文件", "warn");
    }
  } catch (err) {
    toast(err.message, "err");
  } finally {
    S.busy = false;
  }
}

async function applyAll() {
  try {
    const payload = await api("/api/apply", { method: "POST", body: "{}" });
    S.data = payload;
    S.selected.clear();
    render();
    const result = payload.result || {};
    toast(result.moved ? `已归档 ${result.moved} 个文件` : "没有可归档的文件", result.moved ? "ok" : "warn");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function undoBatch(batchId) {
  const ok = await confirmDialog(
    "确认撤销？",
    "会把这一批文件搬回它们原来的位置。如果原位置已有同名文件，不会覆盖，而是自动加 (1)。",
    "撤销"
  );
  if (!ok) return;
  try {
    const payload = await api("/api/undo", {
      method: "POST",
      body: JSON.stringify({ batch_id: batchId }),
    });
    S.data = payload;
    render();
    const result = payload.result || {};
    if (result.error) {
      toast(result.error, "warn");
    } else {
      toast(`已还原 ${result.restored || 0} 个文件`, "ok");
    }
  } catch (err) {
    toast(err.message, "err");
  }
}

async function saveConfig() {
  const config = {
    watch_dirs: $("#cfg-watch").value.split("\n").map((s) => s.trim()).filter(Boolean),
    archive_root: $("#cfg-root").value.trim(),
    poll_interval: Number($("#cfg-poll").value) || 2,
    settle_seconds: Number($("#cfg-settle").value) || 1.5,
    recursive: $("#cfg-recursive").checked,
  };
  try {
    const payload = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({ config }),
    });
    S.data = payload.state;
    render();
    toast("设置已保存", "ok");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function toggleWatch() {
  const action = S.data && S.data.watcher.running ? "stop" : "start";
  try {
    await api("/api/watch", { method: "POST", body: JSON.stringify({ action }) });
    await loadState();
    if (action === "start") {
      toast("开始监听，下载完成的文件会被自动归档", "ok");
    } else {
      toast("已停止监听", "warn");
    }
  } catch (err) {
    toast(err.message, "err");
  }
}

/* ---------------------------------------------------------------- 事件 */

function switchView(view) {
  if (!VIEWS[view]) return;
  S.view = view;
  if (location.hash !== `#${view}`) {
    history.replaceState(null, "", `#${view}`);
  }
  render();
  if (view === "inbox") refreshPreview(false);
}

function bindGlobal() {
  $$(".nav-item").forEach((btn) => {
    btn.onclick = () => switchView(btn.dataset.view);
  });

  $("#btn-watch").onclick = toggleWatch;
  $("#btn-save-config").onclick = saveConfig;

  $("#select-all").onchange = (e) => {
    const rows = (S.data ? S.data.preview : []).filter((d) => d.action === "move");
    S.selected.clear();
    if (e.target.checked) rows.forEach((d) => S.selected.add(d.path));
    renderPending();
    renderTopbar();
  };

  $("#rules-json-toggle").onchange = (e) => {
    const on = e.target.checked;
    $("#json-editor").hidden = !on;
    $("#rules-editor").style.display = on ? "none" : "";
    if (on) {
      $("#json-text").value = JSON.stringify(S.data.rules, null, 2);
    }
  };

  $("#btn-json-save").onclick = async () => {
    try {
      const parsed = JSON.parse($("#json-text").value);
      if (!Array.isArray(parsed)) throw new Error("规则必须是数组");
      S.data.rules = parsed;
      await saveRules(true);
      toast("JSON 已保存", "ok");
    } catch (err) {
      toast(`JSON 有误：${err.message}`, "err");
    }
  };

  $("#btn-json-revert").onclick = () => {
    $("#json-text").value = JSON.stringify(S.data.rules, null, 2);
    toast("已恢复为当前规则", "warn");
  };

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#modal").hidden) {
      $("#modal").hidden = true;
    }
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && S.view === "inbox") {
      e.preventDefault();
      applyAll();
    }
  });

  const nav = { "1": "inbox", "2": "rules", "3": "history", "4": "settings" };
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select")) return;
    if (nav[e.key]) switchView(nav[e.key]);
  });
}

/* ---------------------------------------------------------------- 启动 */

async function boot() {
  bindGlobal();
  try {
    await loadState();
    await refreshPreview(false);
  } catch (err) {
    toast(`无法连接本地服务：${err.message}`, "err");
    return;
  }
  const initial = location.hash.replace("#", "");
  if (VIEWS[initial]) S.view = initial;
  render();
  // 监听状态下定期刷新，让新下载的文件自动冒出来
  setInterval(async () => {
    if (!S.data || !S.data.watcher.running) return;
    try {
      const payload = await api("/api/state");
      const changed = JSON.stringify(payload.preview) !== JSON.stringify(S.data.preview);
      S.data = payload;
      if (changed) render();
      else { renderStats(); renderWatch(); }
    } catch (err) { /* 服务关掉了就安静退出 */ }
  }, 3000);
}

document.addEventListener("DOMContentLoaded", boot);

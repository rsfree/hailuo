/* hailuo 控制台 —— 原生 JS，无依赖。
   安全约定：所有来自服务端的数据一律走 textContent，不用 innerHTML 拼接。 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const PAGE = 25;

const state = {
  view: "accounts",
  version: "",
  overview: null,
  accounts: [],
  accFilter: "",
  accSearch: "",
  tasks: { items: [], total: 0, offset: 0, limit: PAGE, status: "", model: "", q: "", days: 7, facets: null },
  system: null,
  models: null,
  modelQ: "",
  timer: null,
};

/* ---------------------------------------------------------------- 基础 */

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  try { body = await res.json(); } catch (_e) { body = null; }
  if (!res.ok) {
    if (res.status === 401 && !path.endsWith("/login")) showLogin("会话已过期，请重新登录");
    const msg = body?.detail?.error?.message || body?.error?.message ||
      (typeof body?.detail === "string" ? body.detail : `HTTP ${res.status}`);
    throw new Error(msg);
  }
  return body;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}

function pad(n) { return String(n).padStart(2, "0"); }

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtAgo(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)} 秒前`;
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.round(s / 3600)} 小时前`;
  return `${Math.round(s / 86400)} 天前`;
}

function fmtDur(sec) {
  if (sec === null || sec === undefined) return "—";
  if (sec < 60) return `${sec.toFixed(1)}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m${pad(Math.round(sec % 60))}s`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h${pad(Math.floor((sec % 3600) / 60))}m`;
  return `${Math.floor(sec / 86400)}d${Math.floor((sec % 86400) / 3600)}h`;
}

function setText(sel, value) {
  const n = $(sel);
  if (n) n.textContent = value === undefined || value === null ? "—" : String(value);
}

/* ---------------------------------------------------------------- 会话 */

function showLogin(message) {
  stopPolling();
  $("#app").hidden = true;
  $("#login-view").hidden = false;
  const err = $("#login-error");
  err.textContent = message || "";
  err.hidden = !message;
  const p = $("#navPulse");
  if (p) { p.className = "pulse off"; }
}

function showApp() {
  $("#login-view").hidden = true;
  $("#app").hidden = false;
}

async function logout() {
  try { await api("/panel/api/logout", { method: "POST" }); } catch (_e) { /* 忽略 */ }
  showLogin("");
}

/* ---------------------------------------------------------------- 路由 */

const TITLES = { accounts: "账号池", tasks: "任务", usage: "用量", models: "模型", system: "系统" };

function switchView(name, { silent = false } = {}) {
  if (!TITLES[name]) name = "accounts";
  state.view = name;
  for (const a of $$(".nav a")) a.classList.toggle("on", a.dataset.view === name);
  for (const s of $$(".view")) s.hidden = s.id !== `view-${name}`;
  setText("#ttl", TITLES[name]);
  if (!silent) {
    const hash = `#${name}`;
    if (location.hash !== hash) history.replaceState(null, "", hash);
  }
  refresh().catch((e) => flash(e.message));
}

function flash(msg) {
  const n = $("#navState");
  if (!n) return;
  n.textContent = msg;
  n.style.color = "var(--bad)";
  setTimeout(() => { n.style.color = ""; }, 2500);
}

/* ---------------------------------------------------------------- 账号池 */

function accBadge(a) {
  if (a.status === "dead") return ["已失效", "bad"];
  if (a.status === "disabled") return ["已停用", "mute"];
  if (a.expired) return ["已过期", "warn"];
  if (a.cooling) return ["冷却中", "warn"];
  return ["可用", "ok"];
}

function renderAccountStats() {
  const a = state.overview?.accounts || {};
  const busy = (a.total ?? 0) - (a.usable ?? 0);
  const cards = [
    ["账号总数", a.total ?? 0, `上限 ${a.max_entries ?? "—"}`, ""],
    ["可用", a.usable ?? 0, `冷却/停用 ${Math.max(0, busy)}`, "good"],
    ["在途任务", state.overview?.active ?? 0, `每号并发 ${state.overview?.concurrency_limit ?? "—"}`, ""],
    ["排队", state.overview?.queue ?? 0, "等待协调器提交", (state.overview?.queue ?? 0) > 0 ? "warn" : ""],
    ["今日受理", state.overview?.today_requests ?? 0, "按服务 Key 计次", ""],
    ["任务总数", state.overview?.tasks?.total ?? 0, "图片任务表", ""],
  ];
  const box = $("#stats");
  box.textContent = "";
  for (const [k, v, s, cls] of cards) {
    const card = el("div", `stat ${cls}`.trim());
    card.append(el("div", "k", k), el("div", "v", v), el("div", "s", s));
    box.appendChild(card);
  }
}

function renderAccountChips() {
  const chips = $("#accChips");
  chips.textContent = "";
  const counts = state.overview?.accounts || {};
  const opts = [["", "全部", counts.total ?? 0], ["ok", "可用", counts.usable ?? 0],
                ["cooling", "冷却中", counts.cooling ?? 0], ["disabled", "停用", counts.disabled ?? 0],
                ["dead", "失效", counts.dead ?? 0]];
  for (const [key, label, n] of opts) {
    const b = el("button", `chip ${state.accFilter === key ? "on" : ""}`.trim());
    b.type = "button";
    b.append(document.createTextNode(label), el("span", "n", n));
    b.addEventListener("click", () => {
      state.accFilter = key;
      renderAccountChips();
      renderAccountTable();
    });
    chips.appendChild(b);
  }
}

function accMatches(a) {
  const f = state.accFilter;
  if (f === "ok" && !a.usable) return false;
  if (f === "cooling" && !a.cooling) return false;
  if (f === "disabled" && a.status !== "disabled") return false;
  if (f === "dead" && a.status !== "dead") return false;
  if (f === "expired" && !a.expired) return false;
  const q = state.accSearch.trim().toLowerCase();
  if (!q) return true;
  return `${a.label || ""} ${a.subject || ""} ${a.fp || ""} ${a.id || ""}`
    .toLowerCase().includes(q);
}

function mkBtn(text, onClick, extra = "") {
  const b = el("button", `btn sm ${extra}`.trim(), text);
  b.type = "button";
  b.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    b.disabled = true;
    try { await onClick(); } catch (e) { flash(e.message); } finally { b.disabled = false; }
  });
  return b;
}

function renderAccountTable() {
  const tbody = $("#accTable tbody");
  tbody.textContent = "";
  const rows = state.accounts.filter(accMatches);
  $("#accEmpty").hidden = rows.length > 0;

  for (const a of rows) {
    const tr = document.createElement("tr");

    const tdName = el("td");
    tdName.append(el("strong", "", a.label || "（未命名）"));
    tdName.append(el("span", "sub mono", (a.subject || "—") + " · " + (a.fp || "").slice(0, 10) + "…"));

    const [txt, cls] = accBadge(a);
    const tdStatus = el("td");
    tdStatus.appendChild(el("span", `tag ${cls}`, txt));

    const tdExp = el("td");
    tdExp.append(el("span", "num", a.exp ? fmtTime(a.exp) : "无 exp"));
    if (a.exp && !a.expired) tdExp.append(el("span", "sub", `剩 ${Math.max(0, Math.round((a.exp - Date.now() / 1000) / 86400))} 天`));

    const tdWeight = el("td", "right num", a.weight);
    const tdUse = el("td", "right");
    tdUse.append(el("span", "num", `${a.use_count ?? 0} / ${a.generations ?? 0}`));
    tdUse.title = "取号次数 / 出图成功张数";

    const tdCheck = el("td");
    tdCheck.append(el("span", "", a.last_checked_at ? fmtAgo(a.last_checked_at) : "未巡检"));
    if (a.last_error) tdCheck.append(el("span", "sub ellip", a.last_error));

    const tdOps = el("td", "right");
    const wrap = el("div", "actions");
    wrap.append(
      mkBtn("巡检", () => checkOne(a.id)),
      a.status === "disabled"
        ? mkBtn("启用", () => setStatus(a.id, "active"))
        : mkBtn("停用", () => setStatus(a.id, "disabled")),
      mkBtn("删除", () => removeOne(a), "danger"),
    );
    tdOps.appendChild(wrap);

    tr.append(tdName, tdStatus, tdExp, tdWeight, tdUse, tdCheck, tdOps);
    tbody.appendChild(tr);
  }
}

async function checkOne(id) {
  const r = await api(`/panel/api/accounts/${id}/check`, { method: "POST" });
  await refresh();
  if (!r.ok) flash(`巡检未通过：${r.detail || "未知原因"}`);
}

async function setStatus(id, status) {
  await api(`/panel/api/accounts/${id}/status`, { method: "POST", body: JSON.stringify({ status }) });
  await refresh();
}

async function removeOne(a) {
  if (!confirm(`删除账号「${a.label || a.id}」？删除后密文不可恢复。`)) return;
  await api(`/panel/api/accounts/${a.id}`, { method: "DELETE" });
  await refresh();
}

/* ---------------------------------------------------------------- 任务 */

const TASK_STATUS = [
  ["", "全部"], ["queued", "排队"], ["in_progress", "进行中"],
  ["succeeded", "成功"], ["failure", "失败"],
];

function taskBadge(s) {
  if (s === "succeeded") return ["成功", "ok"];
  if (s === "failure") return ["失败", "bad"];
  if (s === "in_progress") return ["进行中", "warn"];
  if (s === "queued") return ["排队", "mute"];
  return [s || "—", "mute"];
}

function renderTaskChips() {
  const box = $("#taskChips");
  box.textContent = "";
  const counts = state.tasks.facets?.status || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  for (const [key, label] of TASK_STATUS) {
    const n = key ? (counts[key] ?? 0) : total;
    const b = el("button", `chip ${state.tasks.status === key ? "on" : ""}`.trim());
    b.type = "button";
    b.append(document.createTextNode(label), el("span", "n", n));
    b.addEventListener("click", () => {
      state.tasks.status = key;
      state.tasks.offset = 0;
      refresh();
    });
    box.appendChild(b);
  }
}

function renderTaskModels() {
  const sel = $("#taskModel");
  const cur = state.tasks.model;
  sel.textContent = "";
  sel.appendChild(el("option", "", "全部模型")).value = "";
  for (const m of state.tasks.facets?.model || []) {
    const o = el("option", "", `${m.model}（${m.count}）`);
    o.value = m.model;
    sel.appendChild(o);
  }
  sel.value = cur;
}

function renderTaskTable() {
  const tbody = $("#taskTable tbody");
  tbody.textContent = "";
  const items = state.tasks.items || [];
  $("#taskEmpty").hidden = items.length > 0;

  for (const t of items) {
    const tr = el("tr", "clickable");
    const tdId = el("td");
    tdId.append(el("span", "mono", (t.task_id || "").replace(/^hailuo_/, "").slice(0, 12)));
    if (t.upstream_batch_id) tdId.append(el("span", "sub mono ellip", t.upstream_batch_id));
    tr.appendChild(tdId);

    const [txt, cls] = taskBadge(t.status);
    const tdSt = el("td");
    tdSt.appendChild(el("span", `tag ${cls}`, txt));
    tr.appendChild(tdSt);

    tr.appendChild(el("td", "ellip", t.model || "—"));
    tr.appendChild(el("td", "mono ellip", t.owner_id || "—"));
    tr.appendChild(el("td", "right num", fmtDur(t.duration)));
    tr.appendChild(el("td", "right num", t.attempts ?? 0));
    tr.appendChild(el("td", "num nowrap", fmtTime(t.created_at)));

    const tdErr = el("td", "ellip");
    tdErr.textContent = t.error_brief || "";
    tdErr.title = t.error_brief || "";
    tr.appendChild(tdErr);

    tr.addEventListener("click", () => openDrawer(t.task_id));
    tbody.appendChild(tr);
  }

  const shown = items.length;
  const from = state.tasks.total === 0 ? 0 : state.tasks.offset + 1;
  setText("#taskInfo", `${from}–${state.tasks.offset + shown} / 共 ${state.tasks.total} 条`);
  $("#taskPrev").disabled = state.tasks.offset <= 0;
  $("#taskNext").disabled = state.tasks.offset + state.tasks.limit >= state.tasks.total;
}

function renderTaskAll() {
  renderTaskChips();
  renderTaskModels();
  renderTaskTable();
}

/* ---------------------------------------------------------------- 抽屉 */

function kvRow(k, v) {
  const a = el("div", "", k);
  const b = el("div", "mono", v);
  return [a, b];
}

function openDrawer(taskId) {
  const mask = $("#drawerMask");
  mask.hidden = false;
  setText("#drawerTitle", `任务 ${taskId}`);
  const body = $("#drawerBody");
  body.textContent = "";
  body.appendChild(el("p", "hint", "加载中…"));
  api(`/panel/api/tasks/${encodeURIComponent(taskId)}`).then(({ task }) => {
    body.textContent = "";
    const grid = el("div", "kv");
    const rows = [
      ["task_id", task.task_id], ["状态", task.status], ["模型", task.model],
      ["上游模型", task.upstream_model], ["归属", task.owner_id],
      ["凭据", task.credential_id], ["批次", task.upstream_batch_id || "—"],
      ["尝试", task.attempts], ["创建", fmtTime(task.created_at)],
      ["提交", task.submitted_at ? fmtTime(task.submitted_at) : "—"],
      ["完成", task.finished_at ? fmtTime(task.finished_at) : "—"],
      ["降级", (task.degradations || []).join("; ") || "无"],
    ];
    for (const [k, v] of rows) grid.append(...kvRow(k, v));
    body.appendChild(grid);

    for (const [label, val] of [["request", task.request], ["plan", task.plan],
                                ["result", task.result], ["error", task.error]]) {
      if (val === null || val === undefined) continue;
      body.appendChild(el("h4", "", label));
      const pre = el("pre", "json", JSON.stringify(val, null, 2));
      body.appendChild(pre);
    }
  }).catch((e) => {
    body.textContent = "";
    body.appendChild(el("p", "err", e.message));
  });
}

function closeDrawer() { $("#drawerMask").hidden = true; }

/* ---------------------------------------------------------------- 用量 */

function renderUsage(data) {
  const { usage, daily, latency } = data;
  const tot = daily.reduce((a, d) => a + d.total, 0);
  const ok = daily.reduce((a, d) => a + d.succeeded, 0);
  const rate = tot ? Math.round((ok / tot) * 100) : 0;

  const cards = [
    ["近 7 日受理", usage.total ?? 0, `${usage.by_owner?.length ?? 0} 把 Key`, ""],
    ["成功率", `${rate}%`, `${ok} / ${tot} 任务`, rate >= 90 ? "good" : rate >= 70 ? "warn" : "bad"],
    ["P50 耗时", latency.p50 === null ? "—" : fmtDur(latency.p50), `${latency.count ?? 0} 个样本`, ""],
    ["P95 耗时", latency.p95 === null ? "—" : fmtDur(latency.p95), "尾部延迟", ""],
  ];
  const box = $("#usageStats");
  box.textContent = "";
  for (const [k, v, s, cls] of cards) {
    const c = el("div", `stat ${cls}`.trim());
    c.append(el("div", "k", k), el("div", "v", v), el("div", "s", s));
    box.appendChild(c);
  }

  // 柱状图：按天堆叠，高度按当日最大 total 归一
  const bars = $("#dailyBars");
  bars.textContent = "";
  const max = Math.max(1, ...daily.map((d) => d.total));
  for (const d of daily) {
    const col = el("div", "col");
    const stack = el("div", "stack");
    stack.title = `${d.day}：成功 ${d.succeeded} / 失败 ${d.failure} / 在途 ${d.in_progress} / 排队 ${d.queued}`;
    const parts = [["queued", d.queued], ["in_progress", d.in_progress],
                   ["failure", d.failure], ["succeeded", d.succeeded]];
    const clsMap = { queued: "q", in_progress: "run", failure: "bad", succeeded: "ok" };
    for (const [k, v] of parts) {
      if (!v) continue;
      const seg = el("div", `seg ${clsMap[k]}`);
      seg.style.height = `${Math.max(2, (v / max) * 100)}%`;
      stack.appendChild(seg);
    }
    col.append(stack, el("div", "lb", d.day.slice(5)));
    bars.appendChild(col);
  }

  const ut = $("#usageTable tbody");
  ut.textContent = "";
  $("#usageEmpty").hidden = (usage.by_owner || []).length > 0;
  for (const o of usage.by_owner || []) {
    const tr = document.createElement("tr");
    tr.append(el("td", "mono ellip", o.owner_id), el("td", "right num", o.requests),
              el("td", "right num", o.days), el("td", "num", o.last_day || "—"));
    ut.appendChild(tr);
  }

  const ft = $("#failTable tbody");
  ft.textContent = "";
  $("#failEmpty").hidden = (data.failed_reasons || []).length > 0;
  for (const f of data.failed_reasons || []) {
    const tr = document.createElement("tr");
    const td = el("td", "ellip", f.reason);
    td.title = f.reason;
    tr.append(td, el("td", "right num", f.count));
    ft.appendChild(tr);
  }
}

/* ---------------------------------------------------------------- 模型 */

function renderModels(data) {
  state.models = data;
  const models = data.models || [];

  const cards = [
    ["模型数", data.model_count ?? models.length, data.source === "runtime" ? "运行期实读" : "冻结快照", ""],
    ["快照日期", data.snapshot_date || "—", "能力表取证日", ""],
    ["默认模型", data.default_model || "—", "未指定 model 时用它", ""],
    ["能力名", (data.capabilities || []).length, (data.capabilities || []).join(" / "), ""],
  ];
  const box = $("#modelStats");
  box.textContent = "";
  for (const [k, v, sub, cls] of cards) {
    const c = el("div", `stat ${cls}`.trim());
    c.append(el("div", "k", k), el("div", "v", String(v)), el("div", "s", sub));
    box.appendChild(c);
  }

  const q = state.modelQ.trim().toLowerCase();
  const rows = models.filter((m) => !q || `${m.id} ${m.name} ${(m.tags || []).join(" ")}`.toLowerCase().includes(q));
  const tbody = $("#modelTable tbody");
  tbody.textContent = "";
  $("#modelEmpty").hidden = rows.length > 0;

  for (const m of rows) {
    const tr = document.createElement("tr");
    tr.appendChild(el("td", "mono", m.id));
    tr.appendChild(el("td", "ellip", m.name || "—"));
    tr.appendChild(el("td", "right num", m.max_images ?? "—"));
    tr.appendChild(el("td", "right num", m.max_prompt_length ?? "—"));
    const tdRes = el("td", "ellip mono", (m.resolutions || []).join(" / ") || "—");
    tdRes.title = tdRes.textContent;
    const tdAr = el("td", "ellip mono", (m.aspect_ratios || []).join(" ") || "—");
    tdAr.title = tdAr.textContent;
    tr.append(tdRes, tdAr);
    tr.appendChild(el("td", "right num", m.default_cost ?? "—"));
    const tdTags = el("td");
    for (const t of m.tags || []) tdTags.appendChild(el("span", "tag mute", t));
    tr.appendChild(tdTags);
    tbody.appendChild(tr);
  }

  const ab = $("#absentTable tbody");
  ab.textContent = "";
  const absences = data.deliberate_absences || {};
  for (const [k, why] of Object.entries(absences)) {
    const tr = document.createElement("tr");
    tr.append(el("td", "mono nowrap", k), el("td", "hint", why));
    ab.appendChild(tr);
  }

  const deg = $("#modelDegradations");
  deg.textContent = "";
  const list = data.degradations || [];
  if (!list.length) {
    deg.appendChild(el("p", "hint", "本轮没有降级项。"));
  } else {
    const ul = el("ul");
    ul.style.paddingLeft = "18px";
    for (const d of list) ul.appendChild(el("li", "", d));
    deg.appendChild(ul);
  }
}

/* ---------------------------------------------------------------- 系统 */

function fillKv(sel, pairs) {
  const box = $(sel);
  box.textContent = "";
  for (const [k, v] of pairs) box.append(...kvRow(k, v));
}

function renderSystem(data) {
  const s = data.status || {};
  const mode = s.upstream_configured ? "嵌入 Token"
    : (state.overview?.pool_mode ? "服务 Key 取号" : "透传（调用方自带 JWT）");
  fillKv("#sysService", [
    ["版本", data.version],
    ["认证", s.auth || "—"],
    ["出号模式", mode],
    ["内存凭据池", `${s.credentials?.alive ?? 0} 可用 / ${s.credentials?.entries ?? 0} 条目`],
    ["库中账号", state.overview?.accounts?.total ?? 0],
    ["服务 Key", state.overview?.service_keys ?? 0],
    ["并发上限", `${s.concurrency_limit ?? "—"}（每号）`],
  ]);
  fillKv("#sysStore", [
    ["后端", s.store?.backend || "—"],
    ["任务总数", s.store?.total ?? 0],
    ...Object.entries(s.store?.counts || {}).map(([k, v]) => [`　${k}`, v]),
    ["在途", data.active ?? 0],
    ["排队", data.queue ?? 0],
  ]);
  const g = s.gate || {};
  fillKv("#sysGate", [
    ["最小间隔", `${g.min_interval ?? 0} s`],
    ["每分钟上限", g.per_minute ?? 0],
    ["最近一分钟提交", g.submits_last_minute ?? 0],
    ["冷却中", g.in_cooldown ? `是（剩 ${Math.round(g.cooldown_remaining ?? 0)}s）` : "否"],
    ["冷却原因", g.cooldown_reason || "—"],
  ]);
  const c = data.coordinator || {};
  fillKv("#sysCoord", [
    ["启用", c.enabled ? "是" : "否"],
    ["运行中", c.running ? "是" : "否"],
    ["owner", c.owner || "—"],
    ["轮次", c.ticks ?? 0],
    ["已提交", c.submitted ?? 0],
    ["跳过提交", c.submit_skipped ?? 0],
    ["提交失败", c.submit_failed ?? 0],
    ["推进", c.advanced ?? 0],
    ["超时收起", c.expired ?? 0],
    ["最近 tick", c.last_tick_at ? fmtAgo(c.last_tick_at) : "—"],
    ["最近错误", c.last_error || "无"],
  ]);
  const cap = s.capabilities || {};
  fillKv("#sysCaps", [
    ["来源", cap.source || "—"],
    ["快照日期", cap.snapshot_date || "—"],
    ["模型数", cap.model_count ?? 0],
    ["默认模型", cap.default_model || "—"],
    ["能力", (cap.capabilities || []).join(", ") || "—"],
    ["降级项", (cap.degradations || []).join("; ") || "无"],
  ]);
  fillKv("#sysAccounts", [
    ["库中账号", state.overview?.accounts?.total ?? 0],
    ["库中可用", state.overview?.accounts?.usable ?? 0],
    ["内存池条目", s.credentials?.entries ?? 0],
    ["内存池可用", s.credentials?.alive ?? 0],
    ["池容量上限", s.credentials?.max_entries ?? "—"],
    ["加密", state.overview?.encryption ? "AES-256-GCM" : "未启用"],
    ["服务 Key 数", state.overview?.service_keys ?? 0],
  ]);
}

/* ---------------------------------------------------------------- 刷新 */

async function refresh() {
  const v = state.view;
  if (v === "accounts") {
    const [ov, list] = await Promise.all([api("/panel/api/overview"), api("/panel/api/accounts")]);
    state.overview = ov;
    state.accounts = list.accounts || [];
    renderAccountStats();
    renderAccountChips();
    renderAccountTable();
    updateModeTag();
  } else if (v === "tasks") {
    const t = state.tasks;
    const qs = new URLSearchParams({
      limit: t.limit, offset: t.offset, days: t.days,
      status: t.status, model: t.model, q: t.q,
    });
    const r = await api(`/panel/api/tasks?${qs}`);
    t.items = r.items || [];
    t.total = r.total || 0;
    t.facets = r.facets || null;
    renderTaskAll();
    if (!state.overview) state.overview = await api("/panel/api/overview");
    updateModeTag();
  } else if (v === "usage") {
    const r = await api("/panel/api/usage?days=7");
    renderUsage(r);
    if (!state.overview) state.overview = await api("/panel/api/overview");
    updateModeTag();
  } else if (v === "models") {
    const [r, ov] = await Promise.all([api("/panel/api/models"), api("/panel/api/overview")]);
    state.overview = ov;
    renderModels(r);
    updateModeTag();
  } else if (v === "system") {
    const [r, ov] = await Promise.all([api("/panel/api/system"), api("/panel/api/overview")]);
    state.system = r;
    state.overview = ov;
    renderSystem(r);
    updateModeTag();
  }
  const p = $("#navPulse");
  if (p) p.className = "pulse on";
  setText("#navState", "已连接");
  setText("#navVer", state.version ? `v${state.version}` : "v—");
}

function updateModeTag() {
  const ov = state.overview;
  const t = $("#modeTag");
  if (!ov) return;
  t.textContent = ov.pool_mode ? "池化出号" : "透传模式";
  t.className = `tag ${ov.pool_mode ? "ok" : "mute"}`;
}

function startPolling() {
  stopPolling();
  state.timer = setInterval(() => {
    if (document.visibilityState === "visible") refresh().catch(() => {});
  }, 15000);
}

function stopPolling() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
}

/* ---------------------------------------------------------------- 事件 */

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("/panel/api/login", { method: "POST", body: JSON.stringify({ token: $("#login-token").value }) });
    $("#login-token").value = "";
    showApp();
    switchView(location.hash.replace("#", "") || "accounts", { silent: true });
    startPolling();
  } catch (e) {
    showLogin(e.message);
  }
});

$("#btnLogout").addEventListener("click", logout);

$("#btnTheme").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("hl_theme", next);
});

$("#btnRefresh").addEventListener("click", () => refresh().catch((e) => flash(e.message)));

for (const a of $$(".nav a")) {
  a.addEventListener("click", (ev) => {
    ev.preventDefault();
    switchView(a.dataset.view);
  });
}

window.addEventListener("hashchange", () => {
  const name = location.hash.replace("#", "") || "accounts";
  if (name !== state.view) switchView(name, { silent: true });
});

$("#acc-search").addEventListener("input", (ev) => {
  state.accSearch = ev.target.value;
  renderAccountTable();
});

$("#add-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const label = $("#add-label").value.trim();
  const token = $("#add-token").value.trim();
  try {
    await api("/panel/api/accounts", { method: "POST", body: JSON.stringify({ token, label }) });
    $("#add-token").value = "";
    $("#add-label").value = "";
    await refresh();
  } catch (e) { flash(e.message); }
});

$("#btnImport").addEventListener("click", async () => {
  const text = $("#import-text").value;
  if (!text.trim()) return;
  try {
    const r = await api("/panel/api/accounts/import", { method: "POST", body: JSON.stringify({ text }) });
    const out = $("#import-result");
    out.hidden = false;
    out.textContent = r.results
      .map((x) => (x.ok ? `✅ ${x.label || x.subject} (${x.id})` : `❌ ${x.line} — ${x.error}`))
      .join("\n") + `\n\n成功 ${r.ok_count} / 共 ${r.total}`;
    await refresh();
  } catch (e) { flash(e.message); }
});

$("#btnCheckAll").addEventListener("click", async (ev) => {
  const b = ev.currentTarget;
  b.disabled = true;
  const old = b.textContent;
  b.textContent = "巡检中…";
  try {
    const r = await api("/panel/api/accounts/check_all", { method: "POST" });
    await refresh();
    flash(`巡检完成：通过 ${r.ok_count} / 共 ${r.total}`);
  } catch (e) { flash(e.message); } finally {
    b.disabled = false;
    b.textContent = old;
  }
});

$("#btnReconcile").addEventListener("click", async (ev) => {
  if (!confirm("对账只读上游批次并回写本地，不会新建任务。继续？")) return;
  const b = ev.currentTarget;
  b.disabled = true;
  try {
    const r = await api("/panel/api/reconcile", { method: "POST" });
    flash(`对账完成：扫描 ${r.stats?.scanned ?? 0}，认领 ${r.stats?.adopted ?? 0}`);
    await refresh();
  } catch (e) { flash(e.message); } finally { b.disabled = false; }
});

$("#taskModel").addEventListener("change", (ev) => {
  state.tasks.model = ev.target.value;
  state.tasks.offset = 0;
  refresh().catch((e) => flash(e.message));
});

$("#taskDays").addEventListener("change", (ev) => {
  state.tasks.days = Number(ev.target.value);
  state.tasks.offset = 0;
  refresh().catch((e) => flash(e.message));
});

let qTimer = null;
$("#taskQ").addEventListener("input", (ev) => {
  clearTimeout(qTimer);
  const v = ev.target.value;
  qTimer = setTimeout(() => {
    state.tasks.q = v.trim();
    state.tasks.offset = 0;
    refresh().catch((e) => flash(e.message));
  }, 350);
});

$("#taskPrev").addEventListener("click", () => {
  state.tasks.offset = Math.max(0, state.tasks.offset - state.tasks.limit);
  refresh().catch((e) => flash(e.message));
});

$("#taskNext").addEventListener("click", () => {
  state.tasks.offset += state.tasks.limit;
  refresh().catch((e) => flash(e.message));
});

$("#modelQ").addEventListener("input", (ev) => {
  state.modelQ = ev.target.value;
  if (state.models) renderModels(state.models);
});

$("#drawerClose").addEventListener("click", closeDrawer);
$("#drawerMask").addEventListener("click", (ev) => {
  if (ev.target.id === "drawerMask") closeDrawer();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closeDrawer();
});

/* ---------------------------------------------------------------- 启动 */

(async function boot() {
  try {
    const me = await api("/panel/api/me");
    if (me.ok) {
      state.version = me.version || "";
      setText("#navVer", state.version ? `v${state.version}` : "v—");
      showApp();
      switchView(location.hash.replace("#", "") || "accounts", { silent: true });
      startPolling();
      return;
    }
  } catch (_e) { /* 未登录 */ }
  showLogin("");
})();

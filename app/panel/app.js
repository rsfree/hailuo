/* hailuo 号池面板 —— 原生 JS，无依赖。所有账号数据都用 textContent 渲染。 */
"use strict";

const $ = (sel) => document.querySelector(sel);

const state = { accounts: [], overview: null };

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  try { body = await res.json(); } catch (_e) { body = null; }
  if (!res.ok) {
    if (res.status === 401 && !path.endsWith("/login")) {
      showLogin("会话已过期，请重新登录");
    }
    const msg = body?.detail?.error?.message || body?.error?.message ||
      (typeof body?.detail === "string" ? body.detail : `HTTP ${res.status}`);
    throw new Error(msg);
  }
  return body;
}

/* ---------------------------------------------------------------- 视图切换 */

function showLogin(message) {
  $("#dash").hidden = true;
  $("#login-view").hidden = false;
  $("#btn-logout").hidden = true;
  $("#mode-badge").textContent = "未登录";
  $("#mode-badge").className = "badge badge-muted";
  const err = $("#login-error");
  err.textContent = message || "";
  err.hidden = !message;
}

function showDash() {
  $("#login-view").hidden = true;
  $("#dash").hidden = false;
  $("#btn-logout").hidden = false;
}

/* ---------------------------------------------------------------- 数据渲染 */

const fmtTime = (ts) => {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

function statusBadge(acc) {
  if (acc.status === "disabled") return ["已停用", "badge-muted"];
  if (acc.status === "dead") return ["已失效", "badge-bad"];
  if (acc.expired) return ["已过期", "badge-warn"];
  if (acc.cooling) return ["冷却中", "badge-warn"];
  return ["可用", "badge-ok"];
}

function renderStats() {
  const a = state.overview?.accounts || {};
  const cards = [
    ["账号总数", a.total ?? 0, `可用 ${a.usable ?? 0}`],
    ["可用", a.usable ?? 0, `停用 ${a.disabled ?? 0} · 失效 ${a.dead ?? 0}`],
    ["已过期", a.expired ?? 0, `3 天内到期 ${a.expiring_soon ?? 0}`],
    ["出号模式", state.overview?.pool_mode ? "池化" : "透传", state.overview?.pool_mode ? "服务 Key 取号" : "调用方自带 JWT"],
  ];
  const box = $("#stats");
  box.textContent = "";
  for (const [k, v, s] of cards) {
    const el = document.createElement("div");
    el.className = "stat";
    const kk = document.createElement("div"); kk.className = "k"; kk.textContent = k;
    const vv = document.createElement("div"); vv.className = "v"; vv.textContent = String(v);
    const ss = document.createElement("div"); ss.className = "s"; ss.textContent = s;
    el.append(kk, vv, ss);
    box.appendChild(el);
  }
}

function renderTable() {
  const tbody = $("#accounts tbody");
  tbody.textContent = "";
  $("#empty-hint").hidden = state.accounts.length > 0;

  for (const acc of state.accounts) {
    const tr = document.createElement("tr");

    const tdLabel = document.createElement("td");
    const lbl = document.createElement("strong");
    lbl.textContent = acc.label || "（未命名）";
    tdLabel.appendChild(lbl);
    if (acc.note) {
      const n = document.createElement("span");
      n.className = "sub"; n.textContent = acc.note;
      tdLabel.appendChild(n);
    }

    const tdSubject = document.createElement("td");
    const s = document.createElement("span");
    s.textContent = acc.subject || "—";
    const fp = document.createElement("span");
    fp.className = "sub mono"; fp.textContent = (acc.fp || "").slice(0, 12) + "…";
    tdSubject.append(s, fp);

    const [text, cls] = statusBadge(acc);
    const tdStatus = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge ${cls}`; badge.textContent = text;
    tdStatus.appendChild(badge);

    const tdExp = document.createElement("td");
    tdExp.textContent = acc.exp ? fmtTime(acc.exp) : "无 exp";

    const tdWeight = document.createElement("td");
    tdWeight.textContent = String(acc.weight);

    const tdUse = document.createElement("td");
    // 取号次数 / 出图张数 —— 两个口径，不能混
    tdUse.textContent = `${acc.use_count ?? 0} / ${acc.generations ?? 0}`;
    tdUse.title = "取号次数 / 出图成功张数";

    const tdCheck = document.createElement("td");
    const t = document.createElement("span");
    t.textContent = acc.last_checked_at ? fmtTime(acc.last_checked_at) : "未巡检";
    tdCheck.appendChild(t);
    if (acc.last_error) {
      const e = document.createElement("span");
      e.className = "sub"; e.textContent = acc.last_error.slice(0, 42);
      tdCheck.appendChild(e);
    }

    const tdOps = document.createElement("td");
    tdOps.className = "right";
    const wrap = document.createElement("div");
    wrap.className = "actions";

    const btnCheck = mkBtn("巡检", () => checkOne(acc.id));
    const toggle = acc.status === "disabled"
      ? mkBtn("启用", () => setStatus(acc.id, "active"))
      : mkBtn("停用", () => setStatus(acc.id, "disabled"));
    const btnDel = mkBtn("删除", () => removeOne(acc), "btn-danger");
    wrap.append(btnCheck, toggle, btnDel);
    tdOps.appendChild(wrap);

    tr.append(tdLabel, tdSubject, tdStatus, tdExp, tdWeight, tdUse, tdCheck, tdOps);
    tbody.appendChild(tr);
  }
}

function mkBtn(text, onClick, extra = "") {
  const b = document.createElement("button");
  b.className = `btn ${extra}`.trim();
  b.type = "button";
  b.textContent = text;
  b.addEventListener("click", async () => {
    b.disabled = true;
    try { await onClick(); } catch (e) { alert(e.message); } finally { b.disabled = false; }
  });
  return b;
}

/* ---------------------------------------------------------------- 动作 */

async function refresh() {
  const [ov, list] = await Promise.all([
    api("/panel/api/overview"),
    api("/panel/api/accounts"),
  ]);
  state.overview = ov;
  state.accounts = list.accounts || [];
  renderStats();
  renderTable();
  const mode = $("#mode-badge");
  mode.textContent = ov.pool_mode ? "池化出号" : "透传模式";
  mode.className = `badge ${ov.pool_mode ? "badge-ok" : "badge-muted"}`;
}

async function checkOne(id) {
  const r = await api(`/panel/api/accounts/${id}/check`, { method: "POST" });
  await refresh();
  if (!r.ok) alert(`巡检未通过：${r.detail || "未知原因"}`);
}

async function setStatus(id, status) {
  await api(`/panel/api/accounts/${id}/status`, {
    method: "POST", body: JSON.stringify({ status }),
  });
  await refresh();
}

async function removeOne(acc) {
  if (!confirm(`删除账号「${acc.label || acc.id}」？删除后无法恢复密文。`)) return;
  await api(`/panel/api/accounts/${acc.id}`, { method: "DELETE" });
  await refresh();
}

/* ---------------------------------------------------------------- 事件 */

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const token = $("#login-token").value;
  try {
    await api("/panel/api/login", { method: "POST", body: JSON.stringify({ token }) });
    $("#login-token").value = "";
    showDash();
    await refresh();
  } catch (e) {
    showLogin(e.message);
  }
});

$("#btn-logout").addEventListener("click", async () => {
  try { await api("/panel/api/logout", { method: "POST" }); } catch (_e) { /* 忽略 */ }
  showLogin("");
});

$("#add-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const label = $("#add-label").value.trim();
  const token = $("#add-token").value.trim();
  try {
    await api("/panel/api/accounts", {
      method: "POST", body: JSON.stringify({ token, label }),
    });
    $("#add-token").value = ""; $("#add-label").value = "";
    await refresh();
  } catch (e) { alert(e.message); }
});

$("#btn-import").addEventListener("click", async () => {
  const text = $("#import-text").value;
  if (!text.trim()) return;
  try {
    const r = await api("/panel/api/accounts/import", {
      method: "POST", body: JSON.stringify({ text }),
    });
    const out = $("#import-result");
    out.hidden = false;
    out.textContent = r.results
      .map((x) => x.ok ? `✅ ${x.label || x.subject} (${x.id})` : `❌ ${x.line} — ${x.error}`)
      .join("\n") + `\n\n成功 ${r.ok_count} / 共 ${r.total}`;
    await refresh();
  } catch (e) { alert(e.message); }
});

$("#btn-check-all").addEventListener("click", async (ev) => {
  const btn = ev.currentTarget;
  btn.disabled = true;
  btn.textContent = "巡检中…";
  try {
    const r = await api("/panel/api/accounts/check_all", { method: "POST" });
    await refresh();
    alert(`巡检完成：通过 ${r.ok_count} / 共 ${r.total}`);
  } catch (e) { alert(e.message); } finally {
    btn.disabled = false;
    btn.textContent = "全部巡检（零成本）";
  }
});

$("#btn-refresh").addEventListener("click", () => refresh().catch((e) => alert(e.message)));

/* ---------------------------------------------------------------- 启动 */

(async function boot() {
  try {
    const me = await api("/panel/api/me");
    if (me.ok) { showDash(); await refresh(); return; }
  } catch (_e) { /* 未登录 */ }
  showLogin("");
})();

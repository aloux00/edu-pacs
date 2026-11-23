/* ============ EduPACS 教学病例库 前端 SPA ============ */
"use strict";

const state = {
  token: localStorage.getItem("edupacs_token") || "",
  user: JSON.parse(localStorage.getItem("edupacs_user") || "null"),
  config: { orthanc_origin: "http://127.0.0.1:8042" },
};

/* ---------------- 工具 ---------------- */
const $app = document.getElementById("app");

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function toast(msg, isErr) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => (t.className = "toast"), 2600);
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (r.status === 401) {
    logout();
    throw new Error("请先登录");
  }
  let data = null;
  try { data = await r.json(); } catch (e) { /* 非JSON */ }
  if (!r.ok) {
    throw new Error((data && (data.detail || data.message)) || ("请求失败 " + r.status));
  }
  return data;
}

function saveAuth(token, user) {
  state.token = token;
  state.user = user;
  localStorage.setItem("edupacs_token", token);
  localStorage.setItem("edupacs_user", JSON.stringify(user));
}

function logout() {
  state.token = "";
  state.user = null;
  localStorage.removeItem("edupacs_token");
  localStorage.removeItem("edupacs_user");
  location.hash = "#/login";
}

function stars(n) {
  return "★".repeat(n) + "☆".repeat(Math.max(0, 3 - n));
}

let diffLabels = { 1: "基础", 2: "进阶", 3: "疑难" };

function fmtDiff(n) {
  return diffLabels[n] || "未知";
}

function barChart(rows, unit) {
  if (!rows.length) return '<div class="empty">暂无数据</div>';
  const max = Math.max(...rows.map(r => r.value), 1);
  return rows.map(r => `
    <div class="bar-row">
      <div class="bar-label">${esc(r.name)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${(r.value / max) * 100}%"></div></div>
      <div class="bar-value">${r.value}${unit || ""}</div>
    </div>`).join("");
}

/* 强制改密：首次登录/重置后 must_change_pwd=1 时拦截使用 */
function showForceChangePwd() {
  const wrap = document.createElement("div");
  wrap.style.cssText = "position:fixed;inset:0;background:rgba(15,23,42,.55);z-index:99;display:flex;align-items:center;justify-content:center;padding:20px";
  wrap.innerHTML = `
    <div class="card" style="max-width:420px;width:100%;margin:0">
      <div class="card-title">🔒 首次登录，请修改密码</div>
      <div class="muted" style="margin-bottom:10px">当前使用默认/重置密码，为保障账号安全请设置新密码（至少 8 位）。修改后需重新登录。</div>
      <div class="form-grid">
        <div class="field full"><label>原密码</label><input id="fcOld" type="password"></div>
        <div class="field full"><label>新密码（至少 8 位）</label><input id="fcNew" type="password"></div>
        <div class="field full"><label>确认新密码</label><input id="fcNew2" type="password"></div>
        <div class="field full" style="align-self:end"><button class="btn primary" id="fcOk" style="width:100%">确认修改并重新登录</button></div>
      </div>
    </div>`;
  document.body.appendChild(wrap);
  document.getElementById("fcOk").onclick = async () => {
    const n1 = document.getElementById("fcNew").value;
    if (n1 !== document.getElementById("fcNew2").value) { toast("两次新密码不一致", true); return; }
    try {
      await api("/api/auth/change-password", {
        method: "POST",
        json: { old_password: document.getElementById("fcOld").value, new_password: n1 },
      });
      toast("密码已修改，请用新密码重新登录");
      setTimeout(() => logout(), 800);
    } catch (e) { toast(e.message, true); }
  };
}

/* ---------------- 布局 ---------------- */
function navItems() {
  const role = state.user ? state.user.role : null;
  if (!role) return [];
  const items = [
    { hash: "#/dashboard", label: "看板", roles: ["admin", "teacher"] },
    { hash: "#/cases", label: "病例库", roles: ["admin", "teacher", "student"] },
    { hash: "#/upload", label: "上传病例", roles: ["admin", "teacher"] },
    { hash: "#/nodes", label: "远程节点", roles: ["admin", "teacher"] },
    { hash: "#/dicts", label: "字典管理", roles: ["admin", "teacher"] },
    { hash: "#/students", label: "学情统计", roles: ["admin", "teacher"] },
    { hash: "#/manage", label: "用户管理", roles: ["admin"] },
    { hash: "#/backup", label: "备份恢复", roles: ["admin"] },
    { hash: "#/my", label: "我的学习", roles: ["student"] },
  ];
  return items.filter(i => i.roles.includes(role));
}

function layout(contentHtml, activeHash) {
  const nav = navItems().map(i =>
    `<a href="${i.hash}" class="${i.hash === activeHash ? "active" : ""}">${i.label}</a>`).join("");
  const roleLabel = { admin: "管理员", teacher: "教师", student: "学生" }[state.user.role];
  const name = state.user.full_name || state.user.username;
  $app.innerHTML = `
    <header class="topbar">
      <div class="topbar-inner">
        <div class="brand"><div class="brand-logo">教</div>教学病例库系统</div>
        <nav class="nav">${nav}</nav>
        <div class="user-box">
          <div class="avatar">${esc(name.slice(0, 1))}</div>
          <span>${esc(name)}<br><span style="font-size:11px;opacity:.8">${roleLabel}</span></span>
          <a href="javascript:void(0)" id="logoutBtn">退出</a>
        </div>
      </div>
    </header>
    <main class="container">${contentHtml}</main>`;
  document.getElementById("logoutBtn").onclick = () => { logout(); toast("已退出登录"); };
  if (state.user && state.user.must_change_pwd) showForceChangePwd();
}

/* ---------------- 登录 ---------------- */
async function viewLogin() {
  if (state.token) { location.hash = "#/dashboard"; return; }
  $app.innerHTML = `
    <div class="login-wrap">
      <div class="login-card">
        <div class="login-logo"><div class="circle">教</div></div>
        <div class="login-title">教学病例库系统</div>
        <div class="login-sub">Orthanc + OHIF 医学影像教学平台</div>
        <div class="field"><label>用户名</label><input id="lgUser" autocomplete="username" placeholder="请输入用户名"></div>
        <div class="field"><label>密码</label><input id="lgPass" type="password" autocomplete="current-password" placeholder="请输入密码"></div>
        <button class="btn primary" id="lgBtn">登 录</button>
        <div class="login-tip">默认管理员：admin / admin123<br>演示教师：teacher01 / teach123　演示学生：st001 / st001@pass</div>
      </div>
    </div>`;
  const doLogin = async () => {
    const btn = document.getElementById("lgBtn");
    btn.disabled = true;
    try {
      const r = await api("/api/auth/login", {
        method: "POST",
        json: { username: document.getElementById("lgUser").value.trim(),
                password: document.getElementById("lgPass").value },
      });
      saveAuth(r.token, r.user);
      state.config = await api("/api/config");
      toast("登录成功");
      location.hash = "#/dashboard";
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
    }
  };
  document.getElementById("lgBtn").onclick = doLogin;
  $app.querySelectorAll("input").forEach(inp =>
    inp.addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); }));
}

/* ---------------- 看板 ---------------- */
async function viewDashboard() {
  const [ov, health] = await Promise.all([
    api("/api/stats/overview"),
    api("/api/orthanc/health"),
  ]);
  const cards = `
    <div class="stats-row">
      <div class="stat-card"><div class="stat-value">${ov.published}<span style="font-size:14px;color:var(--text-3)"> / ${ov.case_total}</span></div><div class="stat-label">已发布病例 / 总数</div></div>
      <div class="stat-card c-blue"><div class="stat-value">${ov.student_total}</div><div class="stat-label">在册学生</div></div>
      <div class="stat-card c-blue"><div class="stat-value">${ov.learned_pairs}</div><div class="stat-label">累计学习人次</div></div>
      <div class="stat-card c-amber"><div class="stat-value">${ov.pending_grade}</div><div class="stat-label">待批阅答卷</div></div>
      <div class="stat-card ${ov.avg_score == null ? "" : "c-rose"}"><div class="stat-value">${ov.avg_score == null ? "-" : ov.avg_score}</div><div class="stat-label">平均批阅得分</div></div>
    </div>`;
  const healthHtml = health.ok
    ? `<span class="badge green">Orthanc 正常 · v${esc(health.version || "")}</span>`
    : `<span class="badge rose">Orthanc 未连接：${esc(health.error || "")}</span>`;
  layout(`
    <div class="page-head">
      <div><div class="page-title">教学看板</div><div class="page-sub">病例库运行总览</div></div>
      <div>${healthHtml}</div>
    </div>
    ${cards}
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr))">
      <div class="card"><div class="card-title">科室分布</div>${barChart(ov.by_department)}</div>
      <div class="card"><div class="card-title">检查模态分布</div>${barChart(ov.by_modality)}</div>
      <div class="card"><div class="card-title">难度分布</div>
        ${barChart(ov.by_difficulty.map(d => ({ name: diffLabels[d.name] || d.name, value: d.value })))}
      </div>
    </div>
    <div class="card">
      <div class="card-title">学生学习排行（前10）</div>
      <div class="table-wrap"><table class="tbl">
        <thead><tr><th>名次</th><th>姓名</th><th>学号</th><th>班级</th><th>已学病例数</th></tr></thead>
        <tbody>${ov.top_students.map((s, i) => `
          <tr><td>${i + 1}</td><td>${esc(s.full_name || s.username)}</td><td>${esc(s.username)}</td>
              <td>${esc(s.class_name)}</td><td>${s.learned}</td></tr>`).join("") || '<tr><td colspan="5" class="empty">暂无数据</td></tr>'}
        </tbody></table></div>
    </div>`, "#/dashboard");
}

/* ---------------- 病例库 ---------------- */
const caseFilter = { q: "", department: "", system: "", modality: "", disease: "", difficulty: 0, status: "", page: 1 };

async function viewCases() {
  const meta = await loadMeta();
  layout(`
    <div class="page-head">
      <div><div class="page-title">病例库</div><div class="page-sub">按条件检索教学病例，点击卡片进入详情与阅片</div></div>
    </div>
    <div class="filter-bar">
      <input id="fq" placeholder="搜索标题 / 病名 / 标签" value="${esc(caseFilter.q)}">
      <select id="fdept"><option value="">全部科室</option></select>
      <select id="fsys"><option value="">全部系统</option></select>
      <select id="fmod"><option value="">全部模态</option></select>
      <input id="fdis" placeholder="病名（可输入）" list="fdisList" style="width:150px" value="${esc(caseFilter.disease)}">
        <datalist id="fdisList">${meta.diseases.map(v => `<option value="${esc(v)}">`).join("")}</datalist>
      <select id="fdiff">
        <option value="0">全部难度</option>${[1, 2, 3].map(v => `<option value="${v}">${esc(diffLabels[v])}</option>`).join("")}
      </select>
      ${state.user.role !== "student" ? `<select id="fstatus">
        <option value="">全部状态</option><option value="published">已发布</option><option value="draft">草稿</option>
      </select>` : ""}
      <button class="btn primary sm" id="fbtn">查询</button>
      <button class="btn sm" id="freset">重置</button>
    </div>
    <div id="caseGrid" class="grid grid-cards"><div class="empty">加载中...</div></div>
    <div class="pager" id="pager"></div>`, "#/cases");

  const fill = (id, arr) => {
    const el = document.getElementById(id);
    arr.forEach(v => {
      const o = document.createElement("option");
      o.value = v; o.textContent = v;
      el.appendChild(o);
    });
  };
  fill("fdept", meta.departments);
  fill("fsys", meta.systems);
  fill("fmod", meta.modalities);
  document.getElementById("fdept").value = caseFilter.department;
  document.getElementById("fsys").value = caseFilter.system;
  document.getElementById("fmod").value = caseFilter.modality;
  document.getElementById("fdiff").value = caseFilter.difficulty;
  if (document.getElementById("fstatus")) document.getElementById("fstatus").value = caseFilter.status;

  const apply = () => {
    caseFilter.q = document.getElementById("fq").value.trim();
    caseFilter.department = document.getElementById("fdept").value;
    caseFilter.system = document.getElementById("fsys").value;
    caseFilter.modality = document.getElementById("fmod").value;
    caseFilter.disease = document.getElementById("fdis").value.trim();
    caseFilter.difficulty = parseInt(document.getElementById("fdiff").value) || 0;
    if (document.getElementById("fstatus")) caseFilter.status = document.getElementById("fstatus").value;
    caseFilter.page = 1;
    loadCases();
  };
  document.getElementById("fbtn").onclick = apply;
  document.getElementById("fq").addEventListener("keydown", e => { if (e.key === "Enter") apply(); });
  document.getElementById("fdis").addEventListener("keydown", e => { if (e.key === "Enter") apply(); });
  document.getElementById("freset").onclick = () => {
    Object.assign(caseFilter, { q: "", department: "", system: "", modality: "", disease: "", difficulty: 0, status: "", page: 1 });
    viewCases();
  };
  loadCases();
}

async function loadCases() {
  const params = new URLSearchParams();
  for (const k of ["q", "department", "system", "modality", "disease", "status"]) {
    if (caseFilter[k]) params.set(k, caseFilter[k]);
  }
  if (caseFilter.difficulty) params.set("difficulty", caseFilter.difficulty);
  params.set("page", caseFilter.page);
  const grid = document.getElementById("caseGrid");
  const pager = document.getElementById("pager");
  try {
    const r = await api("/api/cases?" + params.toString());
    if (!r.items.length) {
      grid.innerHTML = '<div class="empty">没有符合条件的病例</div>';
      pager.innerHTML = "";
      return;
    }
    grid.innerHTML = r.items.map(c => `
      <div class="case-card" onclick="location.hash='#/case/${c.id}'">
        <div class="case-thumb"><div class="modality">${esc(c.modality || "DCM")}</div>
          <div class="img-count">${c.image_count} 幅图像</div></div>
        <div class="case-body">
          <div class="case-title">${esc(c.title)}</div>
          <div class="case-meta">
            <span class="badge">${esc(c.department)}</span>
            ${c.system ? `<span class="badge blue">${esc(c.system)}</span>` : ""}
            ${c.disease ? `<span class="badge gray">${esc(c.disease)}</span>` : ""}
            ${c.status === "draft" ? '<span class="badge amber">草稿</span>' : ""}
            ${c.learned ? '<span class="badge green">已学习</span>' : ""}
          </div>
          <div class="case-foot">
            <span class="difficulty">${stars(c.difficulty)} <span style="color:var(--text-3)">${fmtDiff(c.difficulty)}</span></span>
            <span>${esc((c.created_at || "").slice(0, 10))}</span>
          </div>
        </div>
      </div>`).join("");
    const totalPage = Math.ceil(r.total / r.page_size);
    pager.innerHTML = `
      <button class="btn sm" ${caseFilter.page <= 1 ? "disabled" : ""} id="pgPrev">上一页</button>
      <span>第 ${r.page} / ${totalPage} 页 · 共 ${r.total} 例</span>
      <button class="btn sm" ${caseFilter.page >= totalPage ? "disabled" : ""} id="pgNext">下一页</button>`;
    document.getElementById("pgPrev").onclick = () => { caseFilter.page--; loadCases(); };
    document.getElementById("pgNext").onclick = () => { caseFilter.page++; loadCases(); };
  } catch (e) {
    grid.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

/* ---------------- 病例详情 ---------------- */
let editingCase = false;

async function viewCase(id) {
  let c;
  try {
    c = await api("/api/cases/" + id);
  } catch (e) {
    layout(`<div class="card"><div class="empty">${esc(e.message)}</div>
      <div style="text-align:center"><a href="#/cases">返回病例库</a></div></div>`, "#/cases");
    return;
  }
  const isTeacher = state.user.role === "teacher" || state.user.role === "admin";
  const viewerUrl = `/ohif/viewer?StudyInstanceUIDs=${encodeURIComponent(c.study_uid)}`;

  const metaHtml = `
    <div class="kv-grid">
      <div class="kv"><div class="k">患者编号</div><div class="v">${esc(c.patient_id)}</div></div>
      <div class="kv"><div class="k">患者姓名</div><div class="v">${esc(c.patient_name)}</div></div>
      <div class="kv"><div class="k">模态</div><div class="v">${esc(c.modality)}</div></div>
      <div class="kv"><div class="k">检查部位</div><div class="v">${esc(c.body_part || "-")}</div></div>
      <div class="kv"><div class="k">系统</div><div class="v">${esc(c.system || "-")}</div></div>
      <div class="kv"><div class="k">科室</div><div class="v">${esc(c.department)}</div></div>
      <div class="kv"><div class="k">序列 / 图像</div><div class="v">${c.series_count} 序列 / ${c.image_count} 幅</div></div>
      <div class="kv"><div class="k">难度</div><div class="v">${stars(c.difficulty)} ${fmtDiff(c.difficulty)}</div></div>
    </div>`;

  const headActions = isTeacher ? `
    <div class="btn-row">
      <button class="btn sm" id="pubBtn">${c.status === "published" ? "下架为草稿" : "发布病例"}</button>
      <button class="btn sm" id="editBtn">编辑信息</button>
      <button class="btn sm" id="originBtn">溯源信息</button>
      <button class="btn sm danger" id="delBtn">删除病例</button>
    </div>` : "";

  const blindOn = c.blind_mode === 1 || c.blind_mode === true;
  const lockCard = (state.user.role === "student" && c.locked) ? `
    <div class="card" style="text-align:center;padding:26px">
      <div style="font-size:32px;margin-bottom:8px">🔒</div>
      <div style="font-weight:700;font-size:15px;color:var(--warn)">盲读模式</div>
      <div class="muted" style="margin-top:6px">${esc(c.locked_reason || "完成作答后解锁教学要点与参考诊断")}</div>
    </div>` : "";

  const teachHtml = c.teaching_points ? `
    <div class="card"><div class="card-title">教学要点</div>
      <div style="font-size:13.5px;color:var(--text-2);white-space:pre-wrap">${esc(c.teaching_points)}</div></div>` : lockCard;
  const diagHtml = c.diagnosis ? `
    <div class="card"><div class="card-title">参考诊断</div>
      <div style="font-size:13.5px;white-space:pre-wrap">${esc(c.diagnosis)}</div></div>` : "";

  const questionsHtml = await renderQuestions(c, isTeacher);

  layout(`
    <div class="page-head">
      <div>
        <div class="page-title">${esc(c.title)}</div>
        <div class="page-sub" style="display:flex;gap:8px;margin-top:6px;flex-wrap:wrap">
          <span class="badge">${esc(c.modality)}</span>
          <span class="badge blue">${esc(c.system)}</span>
          <span class="badge gray">${esc(c.department)}</span>
          ${c.status === "published" ? '<span class="badge green">已发布</span>' : '<span class="badge amber">草稿</span>'}
          ${isTeacher ? (blindOn ? '<span class="badge gray">盲读模式</span>' : '<span class="badge amber">要点直接可见</span>') : ""}
        </div>
      </div>
      ${headActions}
    </div>
    <div class="card">
      <div class="card-title">病例信息</div>
      ${metaHtml}
      <div class="muted" style="margin-top:10px">StudyInstanceUID: ${esc(c.study_uid)}</div>
      <div class="btn-row" style="margin-top:14px">
        <button class="btn primary" id="viewerBtn">打开阅片器（OHIF）</button>
        ${state.user.role === "student" ? `
          <textarea id="noteInput" placeholder="学习笔记（可选）：写下你的阅片发现..." style="flex:1;min-width:240px;border:1px solid var(--border);border-radius:8px;padding:8px 11px;font-family:inherit;min-height:38px">${esc((c.note || ""))}</textarea>
          <button class="btn" id="noteBtn">保存笔记</button>` : ""}
      </div>
      <div class="viewer-box" id="viewerBox" style="display:none">
        <div class="muted" style="margin-bottom:6px">OHIF Viewer 内嵌于本页，工具栏支持窗宽窗位、缩放、测量与标注。</div>
        <iframe class="viewer-frame" id="viewerFrame" data-src="${esc(viewerUrl)}"></iframe>
      </div>
    </div>
    ${teachHtml}
    ${diagHtml}
    <div class="card"><div class="card-title">教学问答</div>
      ${(state.user.role === "student" && c.locked) ? '<div class="muted" style="margin-bottom:10px;color:var(--warn)">🔒 作答完成本病例全部题目后，将解锁教学要点、参考诊断与各题参考答案</div>' : ""}
      ${questionsHtml}</div>
    ${isTeacher ? `<div class="card"><div class="card-title">新增题目</div>${questionFormHtml(c.id)}</div>` : ""}
    <div id="editArea"></div>`, "#/case/" + id);

  document.getElementById("viewerBtn").onclick = async () => {
    const box = document.getElementById("viewerBox");
    const frame = document.getElementById("viewerFrame");
    const opening = box.style.display === "none";
    box.style.display = opening ? "block" : "none";
    if (opening && !frame.getAttribute("src")) {
      frame.setAttribute("src", frame.getAttribute("data-src"));
    }
    if (state.user.role === "student" && opening) {
      api("/api/progress", { method: "POST", json: { case_id: c.id, note: "" } })
        .then(() => toast("已记录学习打卡"))
        .catch(() => {});
    }
  };
  if (document.getElementById("noteBtn")) {
    document.getElementById("noteBtn").onclick = async () => {
      try {
        await api("/api/progress", {
          method: "POST",
          json: { case_id: c.id, note: document.getElementById("noteInput").value },
        });
        toast("笔记已保存");
      } catch (e) { toast(e.message, true); }
    };
  }
  if (isTeacher) {
    document.getElementById("originBtn").onclick = async () => {
      const area = document.getElementById("editArea");
      try {
        const o = await api(`/api/cases/${c.id}/origin`);
        area.innerHTML = o.exists ? `
          <div class="card"><div class="card-title">脱敏溯源（仅教师/管理员可见）</div>
            <div class="kv-grid">
              <div class="kv"><div class="k">教学编号（脱敏后）</div><div class="v">${esc(o.teaching_code || "-")}</div></div>
              <div class="kv"><div class="k">原患者编号</div><div class="v">${esc(o.source_patient_id || "-")}</div></div>
              <div class="kv"><div class="k">原患者姓名</div><div class="v">${esc(o.source_patient_name || "-")}</div></div>
              <div class="kv"><div class="k">原出生日期</div><div class="v">${esc(o.source_patient_birth || "-")}</div></div>
              <div class="kv"><div class="k">原检查号</div><div class="v">${esc(o.source_accession || "-")}</div></div>
              <div class="kv"><div class="k">原医疗机构</div><div class="v">${esc(o.source_institution || "-")}</div></div>
              <div class="kv full" style="grid-column:1/-1"><div class="k">原 StudyInstanceUID</div><div class="v" style="font-size:12px">${esc(o.source_study_uid || "-")}</div></div>
            </div>
            <div class="muted" style="margin-top:10px">${esc(o.deid_note || "")} · ${esc((o.deid_at || "").slice(0, 19))}</div>
            <div class="btn-row" style="margin-top:10px"><button class="btn sm" id="oClose">关闭</button></div>
          </div>` : `
          <div class="card"><div class="muted">${esc(o.note || "无溯源记录")}</div>
            <div class="btn-row" style="margin-top:10px"><button class="btn sm" id="oClose">关闭</button></div></div>`;
        document.getElementById("oClose").onclick = () => (area.innerHTML = "");
        area.scrollIntoView({ behavior: "smooth" });
      } catch (e) { toast(e.message, true); }
    };
    document.getElementById("pubBtn").onclick = async () => {
      try {
        await api("/api/cases/" + c.id, {
          method: "PUT",
          json: { status: c.status === "published" ? "draft" : "published" },
        });
        toast(c.status === "published" ? "已下架为草稿" : "已发布");
        viewCase(c.id);
      } catch (e) { toast(e.message, true); }
    };
    document.getElementById("delBtn").onclick = () => {
      if (!confirm("确认删除该病例？Orthanc 中对应影像数据将一并删除。")) return;
      api("/api/cases/" + c.id, { method: "DELETE" })
        .then(() => { toast("已删除"); location.hash = "#/cases"; })
        .catch(e => toast(e.message, true));
    };
    document.getElementById("editBtn").onclick = () => renderCaseEdit(c);
  }
}

function questionFormHtml(caseId) {
  return `
    <div class="form-grid">
      <div class="field full"><label>题目</label><input id="qQuestion" placeholder="例如：请描述主要影像学表现"></div>
      <div class="field full"><label>参考答案（学生作答完成后可见）</label><textarea id="qRef"></textarea></div>
      <div class="field"><label>分值</label><input id="qScore" type="number" value="10" min="1" max="100"></div>
      <div class="field" style="align-self:end"><button class="btn primary" id="qAdd">添加题目</button></div>
    </div>`;
}

async function renderQuestions(c, isTeacher) {
  const qs = c.questions || [];
  if (!qs.length) {
    return '<div class="empty">本病例暂无题目' + (isTeacher ? "，可在下方添加" : "") + "</div>";
  }
  if (!isTeacher) {
    const rows = await Promise.all(qs.map(async (q) => {
      const sub = q.my_submission;
      const graded = sub && sub.status === "graded";
      return `
        <div class="q-item">
          <div class="q-title"><span>${esc(q.question)}</span><span class="badge gray">${q.score} 分</span></div>
          <div class="q-ans">
            <textarea id="ans-${q.id}" placeholder="输入你的答案...">${esc(sub ? sub.answer : "")}</textarea>
            <div class="grade-row">
              <button class="btn primary sm" data-submit="${q.id}">${sub ? "更新答案" : "提交答案"}</button>
              ${sub ? `<span class="muted">已于 ${esc(sub.submitted_at)} 提交</span>` : ""}
              ${graded ? `<span class="badge green">得分 ${sub.score} / ${q.score}</span>` :
                 (sub ? '<span class="badge amber">待批阅</span>' : "")}
            </div>
            ${graded && sub.feedback ? `<div class="q-ref"><b>教师评语：</b>${esc(sub.feedback)}</div>` : ""}
            ${q.reference_answer ? `<div class="q-ref"><b>参考答案：</b>${esc(q.reference_answer)}</div>` : ""}
          </div>
        </div>`;
    }));
    setTimeout(() => bindStudentAnswers(c.id), 0);
    return rows.join("");
  }
  // 教师视角：题目 + 收到的答卷
  let subs = [];
  try {
    subs = await api(`/api/cases/${c.id}/submissions`);
  } catch (e) { subs = []; }
  const qRows = qs.map(q => {
    const mySubs = subs.filter(s => s.question_id === q.id);
    const subRows = mySubs.map(s => `
      <tr>
        <td>${esc(s.full_name || s.username)}<div class="muted">${esc(s.class_name || "")}</div></td>
        <td style="max-width:340px">${esc(s.answer) || '<span class="muted">（空）</span>'}</td>
        <td>
          ${s.status === "graded"
            ? `<span class="badge green">已批 ${s.score} / ${q.score}</span><div class="muted">${esc(s.feedback)}</div>`
            : `<div class="grade-row">
                 <input type="number" id="g-${s.id}" placeholder="分数" min="0" max="${q.score}" value="${q.score}">
                 <input type="text" id="f-${s.id}" placeholder="评语" style="width:150px;border:1px solid var(--border);border-radius:8px;padding:7px 10px">
                 <button class="btn primary sm" data-grade="${s.id}" data-max="${q.score}">批阅</button>
               </div>`}
        </td>
        <td>
          <button class="btn sm danger" data-delq="${q.id}">删题</button>
          <div class="muted" style="margin-top:6px">参考答案：${esc(q.reference_answer || "-")}</div>
        </td>
      </tr>`).join("");
    return `
      <div class="q-item">
        <div class="q-title"><span>${esc(q.question)}</span>
          <span><span class="badge gray">${q.score} 分</span>
          <span class="badge ${mySubs.length ? "blue" : "gray"}">${mySubs.length} 份答卷</span></span>
        </div>
        ${mySubs.length ? `<div class="table-wrap" style="margin-top:10px"><table class="tbl">
          <thead><tr><th>学生</th><th>答案</th><th>批阅</th><th>操作</th></tr></thead>
          <tbody>${subRows}</tbody></table></div>`
          : '<div class="muted" style="margin-top:6px">暂无学生作答</div>'}
      </div>`;
  }).join("");
  setTimeout(() => bindTeacherGrading(c.id), 0);
  return qRows;
}

function bindStudentAnswers(caseId) {
  document.querySelectorAll("[data-submit]").forEach(btn => {
    btn.onclick = async () => {
      const qid = btn.getAttribute("data-submit");
      try {
        await api(`/api/questions/${qid}/submit`, {
          method: "POST",
          json: { answer: document.getElementById("ans-" + qid).value },
        });
        btn.textContent = "更新答案";
        toast("答案已提交，等待教师批阅");
        if (caseId) {
          // 重新加载详情：若本题是最后一题，将解锁教学要点/参考诊断
          setTimeout(() => viewCase(caseId), 600);
        }
      } catch (e) { toast(e.message, true); }
    };
  });
}

function bindTeacherGrading(caseId) {
  document.querySelectorAll("[data-grade]").forEach(btn => {
    btn.onclick = async () => {
      const sid = btn.getAttribute("data-grade");
      const score = parseFloat(document.getElementById("g-" + sid).value);
      if (isNaN(score)) { toast("请输入分数", true); return; }
      try {
        await api(`/api/submissions/${sid}/grade`, {
          method: "POST",
          json: { score, feedback: document.getElementById("f-" + sid).value },
        });
        toast("批阅完成");
        viewCase(caseId);
      } catch (e) { toast(e.message, true); }
    };
  });
  document.querySelectorAll("[data-delq]").forEach(btn => {
    btn.onclick = () => {
      if (!confirm("确认删除该题目及其全部答卷？")) return;
      api(`/api/questions/${btn.getAttribute("data-delq")}`, { method: "DELETE" })
        .then(() => { toast("已删除题目"); viewCase(caseId); })
        .catch(e => toast(e.message, true));
    };
  });
}

async function renderCaseEdit(c) {
  const meta = await loadMeta();
  const blindOn = c.blind_mode === 1 || c.blind_mode === true;
  const area = document.getElementById("editArea");
  area.innerHTML = `
    <div class="card"><div class="card-title">编辑病例信息</div>
      <div class="form-grid">
        <div class="field full"><label>标题</label><input id="eTitle" value="${esc(c.title)}"></div>
        <div class="field"><label>科室</label><input id="eDept" value="${esc(c.department)}"></div>
        <div class="field"><label>系统</label>
          <select id="eSys"><option value="">未选择</option>${meta.systems.map(v => `<option ${v === c.system ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></div>
        <div class="field"><label>模态</label>
          <select id="eMod"><option value="">未选择</option>${meta.modalities.map(v => `<option ${v === c.modality ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></div>
        <div class="field"><label>病名</label><input id="eDisease" list="eDisList" value="${esc(c.disease)}">
          <datalist id="eDisList">${meta.diseases.map(v => `<option value="${esc(v)}">`).join("")}</datalist></div>
        <div class="field"><label>难度</label><select id="eDiff">${[1, 2, 3].map(v => `<option value="${v}" ${v === c.difficulty ? "selected" : ""}>${esc(diffLabels[v])}</option>`).join("")}</select></div>
        <div class="field"><label>标签（逗号分隔）</label><input id="eTags" value="${esc(c.tags)}"></div>
        <div class="field full"><label>教学要点</label><textarea id="eTeach">${esc(c.teaching_points)}</textarea></div>
        <div class="field full"><label>参考诊断</label><textarea id="eDiag">${esc(c.diagnosis)}</textarea></div>
        <div class="field"><label>盲读模式（学生作答后才可见要点/诊断/答案）</label>
          <label style="display:flex;align-items:center;gap:8px;font-size:13.5px">
            <input type="checkbox" id="eBlind" ${blindOn ? "checked" : ""}> 开启盲读
          </label></div>
        <div class="field" style="align-self:end">
          <div class="btn-row">
            <button class="btn primary" id="eSave">保存</button>
            <button class="btn" id="eCancel">取消</button>
          </div>
        </div>
      </div>
    </div>`;
  area.scrollIntoView({ behavior: "smooth" });
  document.getElementById("eCancel").onclick = () => (area.innerHTML = "");
  document.getElementById("eSave").onclick = async () => {
    try {
      await api("/api/cases/" + c.id, {
        method: "PUT",
        json: {
          title: document.getElementById("eTitle").value.trim(),
          department: document.getElementById("eDept").value.trim(),
          system: document.getElementById("eSys").value,
          modality: document.getElementById("eMod").value,
          disease: document.getElementById("eDisease").value.trim(),
          difficulty: parseInt(document.getElementById("eDiff").value) || 1,
          tags: document.getElementById("eTags").value.trim(),
          teaching_points: document.getElementById("eTeach").value,
          diagnosis: document.getElementById("eDiag").value,
          blind_mode: document.getElementById("eBlind").checked,
        },
      });
      toast("已保存");
      viewCase(c.id);
    } catch (e) { toast(e.message, true); }
  };
}

/* ---------------- 字典管理（系统/模态/难度/疾病名称） ---------------- */
let metaCache = null;

async function loadMeta(force) {
  if (!metaCache || force) {
    metaCache = await api("/api/meta");
    if (metaCache.difficulty_labels) diffLabels = metaCache.difficulty_labels;
  }
  return metaCache;
}

const dictState = { tab: "system" };
const DICT_CATS = [
  { key: "system", label: "系统", codeHint: "", labelHint: "如：呼吸系统" },
  { key: "modality", label: "模态", codeHint: "标准缩写，如 CT / MR / OT", labelHint: "显示名称，如 CT" },
  { key: "difficulty", label: "难度", codeHint: "必须为 1 / 2 / 3", labelHint: "如：基础" },
  { key: "disease", label: "疾病名称", codeHint: "可留空", labelHint: "如：肺结节" },
];

async function viewDicts() {
  await loadMeta(true);
  const tabs = DICT_CATS.map(c =>
    `<button class="btn sm ${dictState.tab === c.key ? "primary" : ""}" data-tab="${c.key}">${c.label}</button>`).join("");
  layout(`
    <div class="page-head">
      <div><div class="page-title">字典管理</div>
      <div class="page-sub">维护「系统 / 模态 / 难度 / 疾病名称」选项，供病例上传、编辑与筛选下拉使用</div></div>
      <div class="btn-row">
        <button class="btn sm" id="dTplBtn">下载 xlsx 模板</button>
        <button class="btn sm" id="dExport">导出当前字典</button>
        <label class="btn sm primary" for="dImportFile" style="cursor:pointer">导入 xlsx</label>
        <input type="file" id="dImportFile" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" style="display:none">
        <button class="btn sm" id="dPasteBtn">粘贴导入</button>
      </div>
    </div>
    <div class="filter-bar">${tabs}
      <button class="btn sm primary" id="dAddBtn" style="margin-left:auto">+ 新增${catLabel()}</button>
    </div>
    <div class="card" id="dAddCard" style="display:none"></div>
    <div class="card"><div class="table-wrap" id="dTable"><div class="empty">加载中...</div></div>
      <div class="muted" style="margin-top:8px">说明：被病例引用的项只能「停用」不能删除；停用后不再出现在下拉中。模板导入时同名同类别的行自动跳过（不覆盖）。</div>
    </div>
    <div id="dImportHint"></div>`, "#/dicts");
  document.getElementById("dTplBtn").onclick = () => {
    toast("正在生成模板，浏览器将开始下载…");
    window.location.href = `/api/dicts/template?token=${encodeURIComponent(state.token)}`;
  };
  document.getElementById("dExport").onclick = () => {
    toast("正在导出字典，浏览器将开始下载…");
    window.location.href = `/api/dicts/export?token=${encodeURIComponent(state.token)}`;
  };
  document.getElementById("dPasteBtn").onclick = () => {
    const wrap = document.createElement("div");
    wrap.style.cssText = "position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:99;display:flex;align-items:flex-start;justify-content:center;padding:40px 12px;overflow:auto";
    wrap.innerHTML = `
      <div class="card" style="max-width:620px;width:100%;margin:0">
        <div class="card-title">粘贴导入字典（无需选择文件）</div>
        <div class="muted" style="margin-bottom:8px">每行一条，列间用英文逗号分隔：<b>类别,代码,名称,排序,是否启用</b>；后两列可省略。
          类别可填中文（系统 / 模态 / 难度 / 疾病名称）。同名同类别自动跳过。</div>
        <textarea id="pasteTxt" style="width:100%;min-height:180px;border:1px solid var(--border);border-radius:8px;padding:10px;font-family:inherit;font-size:13px" placeholder="系统,,骨关节系统,16&#10;模态,XA,血管造影(DSA),11&#10;疾病名称,,骨肉瘤,3&#10;难度,1,基础,1"></textarea>
        <div class="btn-row" style="margin-top:10px">
          <button class="btn primary" id="pasteOk">导入</button>
          <button class="btn" id="pasteCancel">取消</button>
        </div>
      </div>`;
    document.body.appendChild(wrap);
    document.getElementById("pasteCancel").onclick = () => wrap.remove();
    document.getElementById("pasteOk").onclick = async () => {
      const btn = document.getElementById("pasteOk");
      try {
        const r = await api("/api/dicts/import-text", {
          method: "POST",
          json: { content: document.getElementById("pasteTxt").value },
        });
        toast(`粘贴导入完成：新增 ${r.imported}、跳过 ${r.skipped}`);
        wrap.remove();
        loadDictTable();
        loadMeta(true);
      } catch (e) {
        toast(e.message, true);
        btn.disabled = false;
      }
    };
  };
  document.getElementById("dImportFile").onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f, f.name);
    try {
      const r = await api("/api/dicts/import", { method: "POST", body: fd });
      const hint = document.getElementById("dImportHint");
      hint.innerHTML = `<div class="card"><div class="card-title">导入结果</div>
        <div>新增 ${r.imported} 项，跳过（同名）${r.skipped} 项${r.failed.length ? `，失败 ${r.failed.length} 行` : ""}</div>
        ${r.failed.length ? `<pre style="white-space:pre-wrap;font-size:12px;color:var(--warn)">${esc(JSON.stringify(r.failed, null, 1))}</pre>` : ""}</div>`;
      toast(`导入完成：新增 ${r.imported}、跳过 ${r.skipped}`);
      loadDictTable();
    } catch (err) { toast(err.message, true); }
    e.target.value = "";
  };
  document.querySelectorAll("[data-tab]").forEach(b => {
    b.onclick = () => { dictState.tab = b.getAttribute("data-tab"); viewDicts(); };
  });
  document.getElementById("dAddBtn").onclick = () => renderDictAdd();
  loadDictTable();
}

function catLabel() {
  return (DICT_CATS.find(c => c.key === dictState.tab) || {}).label || "字典项";
}

async function loadDictTable() {
  const cat = DICT_CATS.find(c => c.key === dictState.tab);
  try {
    const r = await api(`/api/dicts?category=${dictState.tab}`);
    const rows = r.items;
    document.getElementById("dTable").innerHTML = rows.length ? `
      <table class="tbl">
        <thead><tr><th>代码</th><th>名称</th><th>排序</th><th>状态</th><th style="width:220px">操作</th></tr></thead>
        <tbody>
          ${rows.map(d => `
            <tr data-row="${d.id}">
              <td>${esc(d.code || "-")}</td>
              <td><b>${esc(d.label)}</b></td>
              <td>${d.sort_no || "-"}</td>
              <td>${d.active ? '<span class="badge green">启用</span>' : '<span class="badge gray">停用</span>'}</td>
              <td>
                <button class="btn sm" data-edit="${d.id}" data-json='${esc(JSON.stringify(d))}'>编辑</button>
                <button class="btn sm" data-toggle="${d.id}" data-active="${d.active}">${d.active ? "停用" : "启用"}</button>
                <button class="btn sm danger" data-del="${d.id}" data-label="${esc(d.label)}">删除</button>
              </td>
            </tr>`).join("")}
        </tbody>
      </table>` : `<div class="empty">「${cat.label}」暂无字典项，点击右上角新增或导入 xlsx</div>`;
    bindDictRowOps();
  } catch (e) {
    document.getElementById("dTable").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

function bindDictRowOps() {
  document.querySelectorAll("[data-toggle]").forEach(b => {
    b.onclick = async () => {
      try {
        await api("/api/dicts/" + b.getAttribute("data-toggle"), {
          method: "PUT", json: { active: b.getAttribute("data-active") !== "1" },
        });
        loadDictTable(); loadMeta(true);
      } catch (e) { toast(e.message, true); }
    };
  });
  document.querySelectorAll("[data-del]").forEach(b => {
    b.onclick = async () => {
      if (!confirm(`确认删除字典项「${b.getAttribute("data-label")}」？（被病例引用时无法删除，只能停用）`)) return;
      try {
        await api("/api/dicts/" + b.getAttribute("data-del"), { method: "DELETE" });
        toast("已删除"); loadDictTable(); loadMeta(true);
      } catch (e) { toast(e.message, true); }
    };
  });
  document.querySelectorAll("[data-edit]").forEach(b => {
    b.onclick = () => renderDictEdit(JSON.parse(b.getAttribute("data-json")));
  });
}

function dictFormHtml(prefix, d) {
  const cat = DICT_CATS.find(c => c.key === dictState.tab);
  return `
    <div class="form-grid">
      <div class="field"><label>类别</label><input value="${cat.label}（${dictState.tab}）" disabled></div>
      <div class="field"><label>代码 ${cat.codeHint ? `（${cat.codeHint}）` : "（可留空）"}</label>
        <input id="${prefix}Code" value="${esc(d ? d.code : "")}"></div>
      <div class="field"><label>名称（必填，${cat.labelHint}）</label>
        <input id="${prefix}Label" value="${esc(d ? d.label : "")}"></div>
      <div class="field"><label>排序（数字，越小越靠前）</label>
        <input id="${prefix}Sort" type="number" value="${d ? (d.sort_no || 0) : 0}"></div>
      <div class="field" style="align-self:end"><button class="btn primary" id="${prefix}Save">${d ? "保存修改" : "新增"}</button>
        <button class="btn" id="${prefix}Cancel">取消</button></div>
    </div>`;
}

function renderDictAdd() {
  const card = document.getElementById("dAddCard");
  card.style.display = "block";
  card.innerHTML = `<div class="card-title">新增${catLabel()}</div>${dictFormHtml("da", null)}`;
  card.scrollIntoView({ behavior: "smooth" });
  document.getElementById("daCancel").onclick = () => { card.style.display = "none"; };
  document.getElementById("daSave").onclick = async () => {
    try {
      await api("/api/dicts", {
        method: "POST",
        json: {
          category: dictState.tab,
          code: document.getElementById("daCode").value.trim(),
          label: document.getElementById("daLabel").value.trim(),
          sort_no: parseInt(document.getElementById("daSort").value) || 0,
          active: true,
        },
      });
      toast("已新增"); card.style.display = "none";
      loadDictTable(); loadMeta(true);
    } catch (e) { toast(e.message, true); }
  };
}

function renderDictEdit(d) {
  const card = document.getElementById("dAddCard");
  card.style.display = "block";
  card.innerHTML = `<div class="card-title">编辑${catLabel()}：${esc(d.label)}</div>${dictFormHtml("de", d)}`;
  card.scrollIntoView({ behavior: "smooth" });
  document.getElementById("deCancel").onclick = () => { card.style.display = "none"; };
  document.getElementById("deSave").onclick = async () => {
    try {
      await api("/api/dicts/" + d.id, {
        method: "PUT",
        json: {
          code: document.getElementById("deCode").value.trim(),
          label: document.getElementById("deLabel").value.trim(),
          sort_no: parseInt(document.getElementById("deSort").value) || 0,
        },
      });
      toast("已保存"); card.style.display = "none";
      loadDictTable(); loadMeta(true);
    } catch (e) { toast(e.message, true); }
  };
}

/* ---------------- 远程节点（Orthanc Modalities / Peers） ---------------- */
async function viewNodes() {
  const [local, mods] = await Promise.all([
    api("/api/orthanc/local-aet"),
    api("/api/orthanc/modalities"),
  ]);
  layout(`
    <div class="page-head">
      <div><div class="page-title">远程 PACS / 设备节点</div>
      <div class="page-sub">维护 Orthanc 的 DICOM 节点与 Orthanc 节点；支持远程查询、拉取导入（自动脱敏）与本地未入库检查导入</div></div>
    </div>
    <div class="card">
      <div class="card-title">本机接收端（影像设备 / 院内 PACS 推送目标）</div>
      <div class="kv-grid">
        <div class="kv"><div class="k">本机 AET</div><div class="v">${esc(local.aet || "-")}</div></div>
        <div class="kv"><div class="k">DICOM 端口</div><div class="v">${local.port || "-"}</div></div>
        <div class="kv full"><div class="k">说明</div><div class="v">影像设备可直推本端口；直推数据未脱敏，请在下方「本地未入库检查」中导入（导入时自动脱敏重建）</div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">DICOM 节点（Modalities · 远程 PACS / 影像设备）
        <button class="btn sm primary" id="mAddBtn" style="float:right">+ 新增节点</button></div>
      <div class="table-wrap" id="mTable"><div class="empty">加载中...</div></div>
      <div class="card" id="mAddCard" style="display:none;margin-top:10px;box-shadow:none;background:var(--bg-1)"></div>
    </div>
    <div class="card">
      <div class="card-title">Orthanc 节点（Peers · HTTP 对接）
        <button class="btn sm primary" id="pAddBtn" style="float:right">+ 新增节点</button></div>
      <div class="table-wrap" id="pTable"><div class="empty">加载中...</div></div>
      <div class="card" id="pAddCard" style="display:none;margin-top:10px;box-shadow:none;background:var(--bg-1)"></div>
    </div>
    <div class="card">
      <div class="card-title">远程查询（C-FIND）→ 拉取导入为教学病例</div>
      <div class="filter-bar">
        <select id="qNode"><option value="">选择 DICOM 节点</option></select>
        <input id="qPid" placeholder="患者ID（可空）">
        <input id="qPname" placeholder="患者姓名（可空）">
        <input id="qDate" placeholder="检查日期 yyyymmdd（可空）">
        <button class="btn primary sm" id="qBtn">查询</button>
      </div>
      <div class="table-wrap" id="qResult"></div>
    </div>
    <div class="card">
      <div class="card-title">本地未入库检查（含设备直推数据 · 导入时自动脱敏）
        <button class="btn sm" id="orRefresh">刷新</button></div>
      <div class="table-wrap" id="orTable"><div class="empty">加载中...</div></div>
    </div>
    <div id="nodeEditArea"></div>`, "#/nodes");

  // DICOM 节点表
  const renderMods = (items) => {
    document.getElementById("mTable").innerHTML = items.length ? `
      <table class="tbl">
        <thead><tr><th>名称</th><th>AET</th><th>主机</th><th>端口</th><th>支持</th><th style="width:250px">操作</th></tr></thead>
        <tbody>${items.map(m => `
          <tr>
            <td><b>${esc(m.name)}</b></td><td>${esc(m.aet)}</td><td>${esc(m.host)}</td><td>${m.port}</td>
            <td>${[["E", "echo", "C-ECHO"], ["F", "find", "C-FIND"], ["M", "move", "C-MOVE"], ["S", "store", "C-STORE"]]
              .map(([k, cap, t]) => `<span class="badge ${(m.capabilities || []).includes(cap) ? "green" : "gray"}" title="${t}">${k}</span>`).join(" ")}</td>
            <td>
              <button class="btn sm" data-echo="${esc(m.name)}">测试连通</button>
              <button class="btn sm" data-medit="${esc(m.name)}" data-json='${esc(JSON.stringify(m))}'>编辑</button>
              <button class="btn sm danger" data-mdel="${esc(m.name)}">删除</button>
            </td>
          </tr>`).join("")}</tbody>
      </table>` : '<div class="empty">暂无 DICOM 节点，点击「+ 新增节点」添加（如院内 PACS / CT / MR 设备）</div>';
    items.forEach(m => {
      const o = document.createElement("option");
      o.value = m.name; o.textContent = `${m.name} (${m.aet}@${m.host}:${m.port})`;
      document.getElementById("qNode").appendChild(o);
    });
    document.querySelectorAll("[data-echo]").forEach(b => {
      b.onclick = async () => {
        b.disabled = true; b.textContent = "测试中...";
        try {
          const r = await api(`/api/orthanc/modalities/${encodeURIComponent(b.getAttribute("data-echo"))}/echo`, { method: "POST" });
          toast(r.ok ? "C-ECHO 连通正常 ✓" : r.error, !r.ok);
        } catch (e) { toast(e.message, true); }
        b.disabled = false; b.textContent = "测试连通";
      };
    });
    document.querySelectorAll("[data-mdel]").forEach(b => {
      b.onclick = async () => {
        if (!confirm(`确认删除节点「${b.getAttribute("data-mdel")}」？`)) return;
        try {
          await api(`/api/orthanc/modalities/${encodeURIComponent(b.getAttribute("data-mdel"))}`, { method: "DELETE" });
          toast("已删除（已同步配置文件）"); viewNodes();
        } catch (e) { toast(e.message, true); }
      };
    });
    document.querySelectorAll("[data-medit]").forEach(b => {
      b.onclick = () => renderModalityForm(JSON.parse(b.getAttribute("data-json")));
    });
  };
  renderMods(mods.items);

  document.getElementById("mAddBtn").onclick = () => renderModalityForm(null);
  function renderModalityForm(m) {
    const card = document.getElementById("mAddCard");
    card.style.display = "block";
    card.innerHTML = `
      <div class="card-title">${m ? "编辑节点：" + esc(m.name) : "新增 DICOM 节点"}</div>
      <div class="form-grid">
        ${m ? "" : `<div class="field"><label>节点名称（字母/数字，如 hospital-pacs）</label><input id="mfName"></div>`}
        <div class="field"><label>对方 AET（Application Entity Title）</label><input id="mfAet" value="${m ? esc(m.aet) : ""}"></div>
        <div class="field"><label>主机 IP / 域名</label><input id="mfHost" value="${m ? esc(m.host) : ""}"></div>
        <div class="field"><label>DICOM 端口</label><input id="mfPort" type="number" value="${m ? m.port : 104}"></div>
        <div class="field" style="align-self:end"><div class="btn-row">
          <button class="btn primary" id="mfSave">${m ? "保存" : "新增"}</button>
          <button class="btn" id="mfCancel">取消</button></div></div>
      </div>
      <div class="muted">保存后即时生效（无需重启），并同步写入 orthanc.json，重启后仍然有效。</div>`;
    card.scrollIntoView({ behavior: "smooth" });
    document.getElementById("mfCancel").onclick = () => (card.style.display = "none");
    document.getElementById("mfSave").onclick = async () => {
      const name = m ? m.name : document.getElementById("mfName").value.trim();
      try {
        await api(`/api/orthanc/modalities/${encodeURIComponent(name)}`, {
          method: "PUT",
          json: {
            aet: document.getElementById("mfAet").value.trim(),
            host: document.getElementById("mfHost").value.trim(),
            port: parseInt(document.getElementById("mfPort").value) || 104,
          },
        });
        toast(m ? "节点已更新" : "节点已新增"); viewNodes();
      } catch (e) { toast(e.message, true); }
    };
  }

  // Peers
  const renderPeers = async () => {
    try {
      const r = await api("/api/orthanc/peers");
      document.getElementById("pTable").innerHTML = r.items.length ? `
        <table class="tbl">
          <thead><tr><th>名称</th><th>URL</th><th>用户名</th><th style="width:210px">操作</th></tr></thead>
          <tbody>${r.items.map(p => `
            <tr><td><b>${esc(p.name)}</b></td><td>${esc(p.url)}</td><td>${esc(p.username || "-")}${p.has_password ? "（有密码）" : ""}</td>
            <td>
              <button class="btn sm" data-pst="${esc(p.name)}">测试连通</button>
              <button class="btn sm danger" data-pdel="${esc(p.name)}">删除</button>
            </td></tr>`).join("")}</tbody>
        </table>` : '<div class="empty">暂无 Orthanc 节点（Peers 为 HTTP 对接方式，院内平台互联用）</div>';
      document.querySelectorAll("[data-pst]").forEach(b => {
        b.onclick = async () => {
          b.disabled = true; b.textContent = "测试中...";
          try {
            const r = await api(`/api/orthanc/peers/${encodeURIComponent(b.getAttribute("data-pst"))}/system`, { method: "POST" });
            toast(r.ok ? `连通正常 · 对方 Orthanc ${r.version || ""}` : r.error, !r.ok);
          } catch (e) { toast(e.message, true); }
          b.disabled = false; b.textContent = "测试连通";
        };
      });
      document.querySelectorAll("[data-pdel]").forEach(b => {
        b.onclick = async () => {
          if (!confirm(`确认删除 Orthanc 节点「${b.getAttribute("data-pdel")}」？`)) return;
          try {
            await api(`/api/orthanc/peers/${encodeURIComponent(b.getAttribute("data-pdel"))}`, { method: "DELETE" });
            toast("已删除"); renderPeers();
          } catch (e) { toast(e.message, true); }
        };
      });
    } catch (e) {
      document.getElementById("pTable").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    }
  };
  renderPeers();

  document.getElementById("pAddBtn").onclick = () => {
    const card = document.getElementById("pAddCard");
    card.style.display = "block";
    card.innerHTML = `
      <div class="card-title">新增 Orthanc 节点（Peer）</div>
      <div class="form-grid">
        <div class="field"><label>节点名称（字母/数字）</label><input id="pfName"></div>
        <div class="field"><label>URL（如 http://192.168.1.10:8042/）</label><input id="pfUrl"></div>
        <div class="field"><label>用户名（可空）</label><input id="pfUser"></div>
        <div class="field"><label>密码（可空）</label><input id="pfPass" type="password"></div>
        <div class="field" style="align-self:end"><div class="btn-row">
          <button class="btn primary" id="pfSave">新增</button>
          <button class="btn" id="pfCancel">取消</button></div></div>
      </div>`;
    document.getElementById("pfCancel").onclick = () => (card.style.display = "none");
    document.getElementById("pfSave").onclick = async () => {
      try {
        await api(`/api/orthanc/peers/${encodeURIComponent(document.getElementById("pfName").value.trim())}`, {
          method: "PUT",
          json: {
            url: document.getElementById("pfUrl").value.trim(),
            username: document.getElementById("pfUser").value.trim(),
            password: document.getElementById("pfPass").value,
          },
        });
        toast("已新增"); card.style.display = "none"; renderPeers();
      } catch (e) { toast(e.message, true); }
    };
  };

  // 远程查询（C-FIND）
  document.getElementById("qBtn").onclick = async () => {
    const node = document.getElementById("qNode").value;
    if (!node) { toast("请先选择 DICOM 节点", true); return; }
    const box = document.getElementById("qResult");
    box.innerHTML = '<div class="empty">C-FIND 查询中...</div>';
    try {
      const r = await api(`/api/orthanc/modalities/${encodeURIComponent(node)}/query`, {
        method: "POST",
        json: {
          PatientID: document.getElementById("qPid").value.trim(),
          PatientName: document.getElementById("qPname").value.trim(),
          StudyDate: document.getElementById("qDate").value.trim(),
        },
      });
      box.innerHTML = r.items.length ? `
        <table class="tbl">
          <thead><tr><th>患者</th><th>日期</th><th>描述</th><th>模态</th><th>Study UID</th><th style="width:150px">操作</th></tr></thead>
          <tbody>${r.items.map(s => `
            <tr>
              <td>${esc(s.patient_name)}<div class="muted">${esc(s.patient_id)}</div></td>
              <td>${esc(s.study_date)}</td>
              <td>${esc(s.study_desc || "-")}</td><td>${esc(s.modality || "-")}</td>
              <td style="font-size:11px">${esc(s.study_uid.slice(0, 26))}…</td>
              <td><button class="btn sm primary" data-fetch="${esc(s.study_uid)}"
                data-pid="${esc(s.patient_id)}" data-pname="${esc(s.patient_name)}">拉取并导入</button></td>
            </tr>`).join("")}</tbody>
        </table>` : '<div class="empty">远程未查到符合条件的检查</div>';
      bindFetchButtons(node);
    } catch (e) {
      box.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    }
  };

  function bindFetchButtons(node) {
    document.querySelectorAll("[data-fetch]").forEach(b => {
      b.onclick = () => {
        const studyUid = b.getAttribute("data-fetch");
        showImportMetaForm(`拉取远程检查（${esc(b.getAttribute("data-pid"))} / ${esc(b.getAttribute("data-pname"))}）并导入为教学病例`,
          async (meta) => {
            b.disabled = true; b.textContent = "拉取中...";
            try {
              const r = await api(`/api/orthanc/modalities/${encodeURIComponent(node)}/fetch-case`, {
                method: "POST",
                json: {
                  study_uid: studyUid,
                  patient_id: b.getAttribute("data-pid"),
                  patient_name: b.getAttribute("data-pname"),
                  meta,
                },
              });
              toast(`拉取导入成功（${r.image_count} 幅图像，已自动脱敏）`);
              location.hash = "#/case/" + r.id;
            } catch (e) { toast(e.message, true); b.disabled = false; b.textContent = "拉取并导入"; }
          });
      };
    });
  }

  // 本地未入库检查
  const loadOrphans = async () => {
    try {
      const r = await api("/api/orthanc/orphan-studies");
      document.getElementById("orTable").innerHTML = r.items.length ? `
        <table class="tbl">
          <thead><tr><th>患者</th><th>日期</th><th>描述</th><th>序列数</th><th style="width:150px">操作</th></tr></thead>
          <tbody>${r.items.map(s => `
            <tr>
              <td>${esc(s.patient_name || "(无名)")}<div class="muted">${esc(s.patient_id || "-")}</div></td>
              <td>${esc(s.study_date || "-")}</td><td>${esc(s.study_desc || "-")}</td><td>${s.series_count}</td>
              <td><button class="btn sm primary" data-imp="${esc(s.study_id)}">导入为病例</button></td>
            </tr>`).join("")}</tbody>
        </table>` : '<div class="empty">没有未入库的检查（Orthanc 中数据均已关联教学病例）</div>';
      document.querySelectorAll("[data-imp]").forEach(b => {
        b.onclick = () => showImportMetaForm("导入本地检查为教学病例（导入时自动脱敏重建）", async (meta) => {
          b.disabled = true; b.textContent = "导入中...";
          try {
            const r = await api("/api/cases/import-from-orthanc", {
              method: "POST",
              json: { study_id: b.getAttribute("data-imp"), meta },
            });
            toast(`导入成功（${r.image_count} 幅图像，已自动脱敏）`);
            location.hash = "#/case/" + r.id;
          } catch (e) { toast(e.message, true); b.disabled = false; b.textContent = "导入为病例"; }
        });
      });
    } catch (e) {
      document.getElementById("orTable").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    }
  };
  loadOrphans();
  document.getElementById("orRefresh").onclick = loadOrphans;
}

/* 拉取/导入共用的病例元信息表单 */
async function showImportMetaForm(title, onOk) {
  const meta = await loadMeta();
  const area = document.getElementById("nodeEditArea") || document.body;
  const wrap = document.createElement("div");
  wrap.style.cssText = "position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:99;display:flex;align-items:flex-start;justify-content:center;padding:40px 12px;overflow:auto";
  wrap.innerHTML = `
    <div class="card" style="max-width:640px;width:100%;margin:0">
      <div class="card-title">${esc(title)}</div>
      <div class="form-grid">
        <div class="field full"><label>病例标题</label><input id="imTitle" placeholder="留空自动取检查描述"></div>
        <div class="field"><label>科室</label><input id="imDept" value="放射科"></div>
        <div class="field"><label>系统</label><select id="imSys"><option value="">未选择</option>${meta.systems.map(v => `<option>${esc(v)}</option>`).join("")}</select></div>
        <div class="field"><label>模态</label><select id="imMod"><option value="">未选择</option>${meta.modalities.map(v => `<option>${esc(v)}</option>`).join("")}</select></div>
        <div class="field"><label>病名</label><input id="imDis" list="imDisList"><datalist id="imDisList">${meta.diseases.map(v => `<option value="${esc(v)}">`).join("")}</datalist></div>
        <div class="field"><label>难度</label><select id="imDiff">${[1, 2, 3].map(v => `<option value="${v}">${esc(diffLabels[v])}</option>`).join("")}</select></div>
        <div class="field"><label>标签（逗号分隔）</label><input id="imTags"></div>
        <div class="field"><label>状态</label><select id="imStatus"><option value="draft">草稿</option><option value="published">直接发布</option></select></div>
        <div class="field"><label>盲读模式</label><label style="display:flex;align-items:center;gap:8px;font-size:13.5px"><input type="checkbox" id="imBlind" checked> 开启</label></div>
        <div class="field full"><label>教学要点</label><textarea id="imTeach"></textarea></div>
        <div class="field full"><label>参考诊断</label><textarea id="imDiag"></textarea></div>
        <div class="field" style="align-self:end"><div class="btn-row">
          <button class="btn primary" id="imOk">确认导入</button>
          <button class="btn" id="imCancel">取消</button></div></div>
      </div>
    </div>`;
  document.body.appendChild(wrap);
  const collect = () => ({
    title: document.getElementById("imTitle").value.trim(),
    department: document.getElementById("imDept").value.trim(),
    system: document.getElementById("imSys").value,
    modality: document.getElementById("imMod").value,
    disease: document.getElementById("imDis").value.trim(),
    difficulty: parseInt(document.getElementById("imDiff").value) || 1,
    tags: document.getElementById("imTags").value.trim(),
    teaching_points: document.getElementById("imTeach").value,
    diagnosis: document.getElementById("imDiag").value,
    status: document.getElementById("imStatus").value,
    blind_mode: document.getElementById("imBlind").checked,
  });
  document.getElementById("imCancel").onclick = () => wrap.remove();
  document.getElementById("imOk").onclick = async () => {
    const btn = document.getElementById("imOk");
    btn.disabled = true; btn.textContent = "处理中...";
    await onOk(collect());
    wrap.remove();
  };
}

/* ---------------- 上传病例 ---------------- */
async function viewUpload() {
  const meta = await loadMeta();
  layout(`
    <div class="page-head">
      <div><div class="page-title">上传病例</div>
      <div class="page-sub">支持 DICOM（.dcm / .zip）与 JPG·PNG 等图片——论文/网络的少见病例截图可直接导入；上传即自动脱敏并转存 Orthanc 关联 OHIF 阅片</div></div>
    </div>
    <div class="card">
      <div class="form-grid">
        <div class="field full"><label>DICOM 文件或图片（可多选 .dcm / .zip / .jpg / .png / .bmp / .webp，一次一个检查）</label>
          <input type="file" id="upFiles" multiple accept=".dcm,.dicom,.zip,.jpg,.jpeg,.png,.bmp,.webp,.gif,.tif,.tiff">
          <span class="hint">DICOM 多序列请打包 zip；图片将自动合成 DICOM 教学序列（模态 OT），多图合成一个检查</span>
        </div>
        <div class="field full"><label>病例标题</label><input id="upTitle" placeholder="留空则自动从 DICOM 描述生成"></div>
        <div class="field"><label>科室</label><input id="upDept" value="放射科"></div>
        <div class="field"><label>系统</label><select id="upSys"><option value="">未选择</option>${meta.systems.map(v => `<option>${esc(v)}</option>`).join("")}</select></div>
        <div class="field"><label>模态</label><select id="upMod"><option value="">自动识别（图片默认 OT）</option>${meta.modalities.map(v => `<option>${esc(v)}</option>`).join("")}</select></div>
        <div class="field"><label>病名</label><input id="upDisease" list="upDisList"><datalist id="upDisList">${meta.diseases.map(v => `<option value="${esc(v)}">`).join("")}</datalist></div>
        <div class="field"><label>难度</label><select id="upDiff">${[1, 2, 3].map(v => `<option value="${v}">${esc(diffLabels[v])}</option>`).join("")}</select></div>
        <div class="field"><label>标签（逗号分隔）</label><input id="upTags"></div>
        <div class="field full"><label>教学要点</label><textarea id="upTeach"></textarea></div>
        <div class="field full"><label>参考诊断</label><textarea id="upDiag"></textarea></div>
        <div class="field"><label>状态</label><select id="upStatus"><option value="draft">草稿（仅教师可见）</option><option value="published">直接发布</option></select></div>
        <div class="field"><label>盲读模式（学生作答后才可见要点/诊断）</label>
          <label style="display:flex;align-items:center;gap:8px;font-size:13.5px">
            <input type="checkbox" id="upBlind" checked> 开启盲读
          </label></div>
        <div class="field full"><label>图片来源 PHI 确认（含图片时必勾）</label>
          <label style="display:flex;align-items:center;gap:8px;font-size:13.5px;color:var(--warn)">
            <input type="checkbox" id="upNoPhi"> 我确认所选图片内**无患者可识别信息**（论文/网络截图可能把患者姓名、ID 烧录在图像内，请先自行遮蔽）
          </label></div>
        <div class="field" style="align-self:end"><button class="btn primary" id="upBtn">开始上传</button></div>
      </div>
    </div>`, "#/upload");
  document.getElementById("upBtn").onclick = async () => {
    const btn = document.getElementById("upBtn");
    const files = document.getElementById("upFiles").files;
    if (!files.length) { toast("请选择 DICOM 文件、zip 或图片", true); return; }
    const meta = {
      title: document.getElementById("upTitle").value.trim(),
      department: document.getElementById("upDept").value.trim(),
      system: document.getElementById("upSys").value,
      modality: document.getElementById("upMod").value,
      disease: document.getElementById("upDisease").value.trim(),
      difficulty: parseInt(document.getElementById("upDiff").value),
      tags: document.getElementById("upTags").value.trim(),
      teaching_points: document.getElementById("upTeach").value,
      diagnosis: document.getElementById("upDiag").value,
      status: document.getElementById("upStatus").value,
      blind_mode: document.getElementById("upBlind").checked,
      confirm_no_phi: document.getElementById("upNoPhi").checked,
    };
    const fd = new FormData();
    fd.append("meta", JSON.stringify(meta));
    for (const f of files) fd.append("files", f, f.name);
    btn.disabled = true;
    btn.textContent = "上传中，请稍候...";
    try {
      const r = await api("/api/cases/upload", { method: "POST", body: fd });
      toast(`上传成功：${r.uploaded} 个文件 / ${r.image_count} 幅图像${r.image_import ? "（图片合成 DICOM 序列）" : ""}`);
      location.hash = "#/case/" + r.id;
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
      btn.textContent = "开始上传";
    }
  };
}

/* ---------------- 用户管理（管理员） ---------------- */
async function viewManage() {
  layout(`
    <div class="page-head">
      <div><div class="page-title">用户管理</div><div class="page-sub">账号增删、批量导入、密码重置</div></div>
      <button class="btn primary" id="addUserBtn">+ 新增用户</button>
    </div>
    <div class="card" id="addUserCard" style="display:none">
      <div class="card-title">新增用户</div>
      <div class="form-grid">
        <div class="field"><label>用户名</label><input id="nuUser"></div>
        <div class="field"><label>初始密码</label><input id="nuPass"></div>
        <div class="field"><label>角色</label><select id="nuRole"><option value="student">学生</option><option value="teacher">教师</option><option value="admin">管理员</option></select></div>
        <div class="field"><label>姓名</label><input id="nuName"></div>
        <div class="field"><label>科室/系</label><input id="nuDept"></div>
        <div class="field"><label>班级</label><input id="nuClass"></div>
        <div class="field" style="align-self:end"><button class="btn primary" id="nuSave">保存</button></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">批量导入（每行：用户名,密码,角色,姓名,科室,班级；密码留空则自动生成随机密码）</div>
      <textarea id="csvText" style="width:100%;min-height:110px;border:1px solid var(--border);border-radius:8px;padding:10px;font-family:inherit" placeholder="st101,st101@pass,student,张三,医学影像系,影像2401班&#10;st102,st102@pass,student,李四,医学影像系,影像2401班"></textarea>
      <div class="btn-row" style="margin-top:10px">
        <button class="btn primary" id="csvBtn">导入文本</button>
        <button class="btn" id="csvTpl">填入 100 人示例</button>
        <label class="btn" for="uXlsxFile" style="cursor:pointer">导入 xlsx 文件</label>
        <input type="file" id="uXlsxFile" accept=".xlsx" style="display:none">
        <button class="btn" id="uTplBtn">下载 xlsx 模板</button>
      </div>
      <span class="hint" id="csvResult" style="display:block;margin-top:8px"></span>
    </div>
    <div class="card">
      <div class="card-title">用户列表</div>
      <div class="filter-bar" style="box-shadow:none;padding:0;margin-bottom:10px">
        <input id="uq" placeholder="搜索用户名/姓名">
        <select id="urole"><option value="">全部角色</option><option value="admin">管理员</option><option value="teacher">教师</option><option value="student">学生</option></select>
        <button class="btn primary sm" id="uSearch">查询</button>
        <button class="btn sm danger" id="uBatchDel" disabled style="margin-left:auto">批量删除</button>
      </div>
      <div class="muted" style="margin-bottom:8px">勾选要删除的账号后点「批量删除」（不能删除自己，系统始终保留至少一名管理员）；<span id="uSelCount">已选 0 个</span></div>
      <div class="table-wrap" id="userTable"><div class="empty">加载中...</div></div>
    </div>`, "#/manage");

  document.getElementById("addUserBtn").onclick = () => {
    const c = document.getElementById("addUserCard");
    c.style.display = c.style.display === "none" ? "block" : "none";
  };
  document.getElementById("nuSave").onclick = async () => {
    try {
      await api("/api/users", {
        method: "POST",
        json: {
          username: document.getElementById("nuUser").value.trim(),
          password: document.getElementById("nuPass").value,
          role: document.getElementById("nuRole").value,
          full_name: document.getElementById("nuName").value.trim(),
          dept: document.getElementById("nuDept").value.trim(),
          class_name: document.getElementById("nuClass").value.trim(),
        },
      });
      toast("已创建");
      loadUsers();
    } catch (e) { toast(e.message, true); }
  };
  document.getElementById("csvTpl").onclick = () => {
    const rows = [];
    for (let i = 101; i <= 200; i++) {
      rows.push(`st${i},st${i}@pass,student,学生${i},医学影像系,影像2401班`);
    }
    document.getElementById("csvText").value = rows.join("\n");
  };
  document.getElementById("csvBtn").onclick = async () => {
    try {
      const r = await api("/api/users/import", {
        method: "POST",
        body: new URLSearchParams({ content: document.getElementById("csvText").value }).toString(),
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
      });
      document.getElementById("csvResult").innerHTML =
        `成功 ${r.imported} 人` + (r.failed.length ? `，失败 ${r.failed.length} 人（用户名重复或格式错误）` : "");
      if (r.generated && r.generated.length) {
        const lines = r.generated.map(g => `${g.username},${g.password}`);
        document.getElementById("csvResult").innerHTML += `
          <div class="card" style="margin-top:10px">
            <div class="card-title">自动生成的随机密码（仅本次展示，请复制保存后发放）</div>
            <textarea readonly style="width:100%;min-height:80px;border:1px solid var(--border);border-radius:8px;padding:8px;font-family:monospace">${lines.join("\n")}</textarea>
          </div>`;
      }
      toast("导入完成");
      loadUsers();
    } catch (e) { toast(e.message, true); }
  };
  document.getElementById("uTplBtn").onclick = () => {
    toast("正在生成模板，浏览器将开始下载…");
    window.location.href = `/api/users/template?token=${encodeURIComponent(state.token)}`;
  };
  document.getElementById("uXlsxFile").onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f, f.name);
    try {
      const r = await api("/api/users/import-xlsx", { method: "POST", body: fd });
      showImportResult(r);
      toast(`xlsx 导入完成：成功 ${r.imported} 人` + (r.failed.length ? `，失败 ${r.failed.length} 行` : ""));
      loadUsers();
    } catch (err) { toast(err.message, true); }
    e.target.value = "";
  };
  document.getElementById("uBatchDel").onclick = async () => {
    const ids = [...document.querySelectorAll(".uChk:checked")].map(c => parseInt(c.value));
    if (!ids.length) return;
    if (!confirm(`确定删除选中的 ${ids.length} 个账号？其学习记录与答卷将一并删除，不可恢复。`)) return;
    try {
      const r = await api("/api/users/batch-delete", { method: "POST", json: { ids } });
      toast(`已删除 ${r.deleted} 个账号` + (r.skipped.length ? `，跳过 ${r.skipped.length} 个` : ""));
      if (r.skipped.length) {
        document.getElementById("csvResult").textContent =
          "跳过明细：" + r.skipped.map(s => `${s.username || s.id}（${s.reason}）`).join("；");
      }
      loadUsers();
    } catch (e) { toast(e.message, true); }
  };
  document.getElementById("uSearch").onclick = loadUsers;
  loadUsers();
}

function showImportResult(r) {
  const hint = document.getElementById("csvResult");
  hint.textContent = `成功 ${r.imported} 人` + (r.failed.length ? `，失败 ${r.failed.length} 人（用户名重复或格式错误）` : "");
  hint.innerHTML += (r.generated && r.generated.length) ? `
    <div class="card" style="margin-top:10px">
      <div class="card-title">自动生成的随机密码（仅本次展示，请复制保存后发放）</div>
      <textarea readonly style="width:100%;min-height:80px;border:1px solid var(--border);border-radius:8px;padding:8px;font-family:monospace">${r.generated.map(g => `${g.username},${g.password}`).join("\n")}</textarea>
    </div>` : "";
}

async function loadUsers() {
  const q = document.getElementById("uq").value.trim();
  const role = document.getElementById("urole").value;
  const users = await api(`/api/users?q=${encodeURIComponent(q)}&role=${role}`);
  const roleBadge = { admin: '<span class="badge rose">管理员</span>', teacher: '<span class="badge blue">教师</span>', student: '<span class="badge gray">学生</span>' };
  document.getElementById("userTable").innerHTML = `
    <table class="tbl">
      <thead><tr><th style="width:34px"><input type="checkbox" id="uSelAll" title="全选/全不选"></th><th>ID</th><th>用户名</th><th>姓名</th><th>角色</th><th>科室/系</th><th>班级</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>
        ${users.map(u => `
          <tr>
            <td><input type="checkbox" class="uChk" value="${u.id}" data-name="${esc(u.username)}"></td>
            <td>${u.id}</td><td>${esc(u.username)}</td><td>${esc(u.full_name)}</td>
            <td>${roleBadge[u.role] || ""}</td><td>${esc(u.dept)}</td><td>${esc(u.class_name)}</td>
            <td>${u.active ? '<span class="ok-icon">正常</span>' : '<span class="muted">已停用</span>'}</td>
            <td>
              <button class="btn sm" data-reset="${u.id}" data-name="${esc(u.username)}">重置密码</button>
              <button class="btn sm" data-toggle="${u.id}" data-active="${u.active}">${u.active ? "停用" : "启用"}</button>
              <button class="btn sm danger" data-del="${u.id}" data-name="${esc(u.username)}">删除</button>
            </td>
          </tr>`).join("")}
      </tbody>
    </table>`;
  const updateSel = () => {
    const all = [...document.querySelectorAll(".uChk")];
    const sel = all.filter(c => c.checked);
    document.getElementById("uSelCount").textContent = `已选 ${sel.length} 个`;
    document.getElementById("uBatchDel").disabled = sel.length === 0;
    const sa = document.getElementById("uSelAll");
    if (sa) sa.checked = all.length > 0 && sel.length === all.length;
  };
  document.querySelectorAll(".uChk").forEach(c => { c.onchange = updateSel; });
  const sa = document.getElementById("uSelAll");
  if (sa) sa.onchange = () => {
    document.querySelectorAll(".uChk").forEach(c => { c.checked = sa.checked; });
    updateSel();
  };
  updateSel();
  document.querySelectorAll("[data-reset]").forEach(b => {
    b.onclick = () => {
      const pw = prompt("为用户 " + b.getAttribute("data-name") + " 设置新密码：");
      if (!pw) return;
      api("/api/users/" + b.getAttribute("data-reset"), {
        method: "PUT", json: { password: pw },
      }).then(() => toast("密码已重置")).catch(e => toast(e.message, true));
    };
  });
  document.querySelectorAll("[data-toggle]").forEach(b => {
    b.onclick = () => {
      const active = b.getAttribute("data-active") === "1";
      api("/api/users/" + b.getAttribute("data-toggle"), {
        method: "PUT", json: { active: !active },
      }).then(() => { toast(active ? "已停用" : "已启用"); loadUsers(); }).catch(e => toast(e.message, true));
    };
  });
  document.querySelectorAll("[data-del]").forEach(b => {
    b.onclick = () => {
      if (!confirm("确认删除用户 " + b.getAttribute("data-name") + "？其学习记录与答卷将一并删除。")) return;
      api("/api/users/" + b.getAttribute("data-del"), { method: "DELETE" })
        .then(() => { toast("已删除"); loadUsers(); })
        .catch(e => toast(e.message, true));
    };
  });
}

/* ---------------- 学情统计（教师） ---------------- */
async function viewStudents() {
  const r = await api("/api/stats/students");
  layout(`
    <div class="page-head">
      <div><div class="page-title">学情统计</div>
      <div class="page-sub">已发布病例 ${r.total_cases} 例 · 学习进度按已发布病例计算</div></div>
    </div>
    <div class="card">
      <div class="table-wrap"><table class="tbl">
        <thead><tr><th>学号</th><th>姓名</th><th>班级</th><th>已学病例</th><th>学习进度</th><th>答题数</th><th>平均得分</th></tr></thead>
        <tbody>
          ${r.items.map(s => `
            <tr>
              <td>${esc(s.username)}</td><td>${esc(s.full_name)}</td><td>${esc(s.class_name)}</td>
              <td>${s.learned} / ${r.total_cases}</td>
              <td><div style="display:flex;align-items:center;gap:8px">
                <div class="progress-bar"><div style="width:${s.progress_pct}%"></div></div>
                <span class="muted">${s.progress_pct}%</span></div></td>
              <td>${s.answers}</td>
              <td>${s.avg_score == null ? "-" : s.avg_score}</td>
            </tr>`).join("") || '<tr><td colspan="7" class="empty">暂无学生</td></tr>'}
        </tbody>
      </table></div>
    </div>`, "#/students");
}

/* ---------------- 我的学习（学生） ---------------- */
async function viewMy() {
  const r = await api("/api/my/progress");
  const pct = r.total ? Math.round((100 * r.learned) / r.total) : 0;
  layout(`
    <div class="page-head">
      <div><div class="page-title">我的学习</div>
      <div class="page-sub">已学 ${r.learned} / ${r.total} 例 · 完成度 ${pct}%</div></div>
    </div>
    <div class="card"><div class="progress-bar" style="width:100%;height:12px"><div style="width:${pct}%"></div></div></div>
    <div class="card">
      <div class="card-title">学习记录</div>
      <div class="table-wrap"><table class="tbl">
        <thead><tr><th>病例</th><th>系统</th><th>难度</th><th>学习次数</th><th>最近学习</th><th>我的笔记</th><th></th></tr></thead>
        <tbody>
          ${r.items.map(p => `
            <tr>
              <td>${esc(p.title)}</td><td>${esc(p.system)}</td><td>${fmtDiff(p.difficulty)}</td>
              <td>${p.view_count}</td><td>${esc(p.last_viewed)}</td>
              <td style="max-width:260px">${esc(p.note) || '<span class="muted">-</span>'}</td>
              <td><a href="#/case/${p.case_id}">继续学习</a></td>
            </tr>`).join("") || '<tr><td colspan="7" class="empty">还没有学习记录，去病例库开始学习吧</td></tr>'}
        </tbody>
      </table></div>
    </div>`, "#/my");
}

/* ---------------- 备份恢复（admin） ---------------- */
const bk = { state: null, dest: "local", archives: [], pollTimer: null, verifyPwd: {} };

function bkFmtSize(n) {
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + " GB";
  if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + " MB";
  return Math.max(1, Math.round(n / 1024)) + " KB";
}

function bkJobText(j) {
  if (!j || !j.stage) return "";
  const names = {
    init: "准备中", snapshot: "快照教学数据库", orthanc: "Orthanc 一致性窗口",
    storage: "拷贝影像存储", pack: "打包压缩", encrypt: "AES-256-GCM 加密",
    verify: "写后校验", apply: "写入恢复数据", done: "完成",
  };
  return names[j.stage] || j.stage;
}

async function bkPoll() {
  try {
    const p = await api("/api/backup/progress");
    bkRenderJob(p.job);
    if (p.job && p.job.running) {
      bk.pollTimer = setTimeout(bkPoll, 1200);
    } else if (p.job && p.job.stage === "done") {
      clearTimeout(bk.pollTimer); bk.pollTimer = null;
      toast("备份完成：" + p.job.result.file);
      bkLoadArchives();
      bkUpdateRunBox(false, "");
    } else if (p.job && p.job.stage === "error") {
      clearTimeout(bk.pollTimer); bk.pollTimer = null;
      toast("备份失败：" + (p.job.error || ""), true);
      bkUpdateRunBox(false, p.job.error || "");
    }
  } catch (e) { /* 网络闪断：下轮重试 */ }
}

function bkRenderJob(j) {
  const box = document.getElementById("bkJobBox");
  if (!box || !j || !j.stage) return;
  const pct = Math.max(0, Math.min(100, j.pct || 0));
  box.style.display = "block";
  box.innerHTML = `
    <div class="bk-job-head">
      <span>${esc(bkJobText(j))}</span>
      <span>${pct.toFixed(0)}%</span>
    </div>
    <div class="bk-progress"><div class="bk-progress-bar" style="width:${pct}%"></div></div>
    <div class="muted" style="margin-top:6px;font-size:12px">${esc(j.msg || "")}</div>
    ${j.running && j.stage !== "done" ? '<button class="btn sm danger" id="bkCancelBtn" style="margin-top:8px">取消本次备份</button>' : ""}`;
  const cb = document.getElementById("bkCancelBtn");
  if (cb) cb.onclick = async () => {
    try { await api("/api/backup/cancel", { method: "POST", json: {} }); toast("已请求取消"); }
    catch (e) { toast(e.message, true); }
  };
}

function bkUpdateRunBox(showing, msg) {
  const box = document.getElementById("bkRunResult");
  if (!box) return;
  if (!showing) { if (msg !== "") box.innerHTML = `<div class="empty">${esc(msg || "")}</div>`; }
}

async function viewBackup() {
  let st;
  try { st = await api("/api/backup/state"); bk.state = st; }
  catch (e) {
    layout(`<div class="card"><div class="empty">${esc(e.message)}</div></div>`, "#/backup");
    return;
  }
  if (!st.available) {
    layout(`
      <div class="page-head"><div><div class="page-title">备份恢复</div><div class="page-sub">加密备份与恢复管理</div></div></div>
      <div class="card"><div class="empty">备份模块不可用：服务器缺少 cryptography 依赖。<br>
        请管理员执行 <code>.venv\\Scripts\\pip install -r requirements.lock.txt</code> 后重启服务。</div></div>`, "#/backup");
    return;
  }
  const dests = st.config.destinations || [];
  layout(`
    <div class="page-head">
      <div><div class="page-title">备份恢复</div>
      <div class="page-sub">AES-256-GCM 加密 · 同机/异机目的地 · 网页一键操作（命令行工具见 tools\\backup.py）</div></div>
    </div>

    <div class="card" id="bkRunCard">
      <div class="card-title">立即备份</div>
      <div class="form-grid">
        <div class="field"><label>备份目的地</label>
          <select id="bkDest">${dests.map(d =>
            `<option value="${esc(d.id)}" ${d.enabled ? "" : 'data-off="1"'}>${esc(d.label || d.id)}${d.enabled ? "" : "（未启用）"} · 保留 ${d.keep} 份</option>`).join("")}
          </select>
        </div>
        <div class="field"><label>备份密码（≥8 位，AES-256-GCM 加密密钥，务必牢记）</label>
          <input id="bkPwd" type="password" autocomplete="new-password" placeholder="备份密码">
        </div>
        <div class="field"><label>备注（明文存于归档头，勿写敏感信息）</label>
          <input id="bkNote" placeholder="如：每日例行 / 升级前快照">
        </div>
        <div class="field" style="align-self:end">
          <button class="btn primary" id="bkRunBtn">开始备份</button>
        </div>
      </div>
      ${st.capability.can_orchestrate
        ? '<div class="hint">备份期间将<b>优雅停止并自动重启</b>本项目 Orthanc（约数秒~数十秒），保证索引与影像完全一致（Orthanc 官方推荐做法）。教学系统本身不停。</div>'
        : '<div class="hint">当前部署无法编排 Orthanc 进程，将采用<b>热备份</b>模式（拷贝索引+重放 WAL+完整性校验，失败自动重试）。</div>'}
      <div id="bkJobBox" style="display:none"></div>
      <div id="bkRunResult" style="margin-top:8px"></div>
      ${st.last && st.last.ok ? `<div class="muted" style="margin-top:6px;font-size:12px">上次备份：${esc(st.last.file)} · ${bkFmtSize(st.last.size)} · ${esc(st.last.duration_s)}s · 模式 ${esc(st.last.mode)}</div>` : ""}
    </div>

    <div class="card">
      <div class="card-title">归档列表 <select id="bkListDest" style="float:right">${dests.map(d =>
        `<option value="${esc(d.id)}">${esc(d.label || d.id)}</option>`).join("")}</select></div>
      <div class="table-wrap" id="bkTable"><div class="empty">加载中...</div></div>
      <div class="btn-row" style="margin-top:10px">
        <button class="btn sm" id="bkPruneBtn">立即执行保留策略</button>
        <button class="btn sm" id="bkReloadBtn">刷新</button>
      </div>
      <div id="bkPruneResult" class="hint" style="display:block;margin-top:8px"></div>
    </div>

    <div class="card" id="bkRestoreCard" style="display:none">
      <div class="card-title">恢复向导</div>
      <div id="bkRestoreBody"></div>
    </div>

    <div class="card">
      <div class="card-title">备份目的地配置 <button class="btn sm" id="bkCfgToggle" style="float:right">编辑</button></div>
      <div id="bkCfgView"></div>
      <div id="bkCfgEdit" style="display:none"></div>
    </div>`, "#/backup");

  document.getElementById("bkRunBtn").onclick = async () => {
    const destSel = document.getElementById("bkDest");
    const opt = destSel.selectedOptions[0];
    if (opt && opt.dataset.off) { toast("该目的地未启用（可在下方配置中启用）", true); return; }
    const pwd = document.getElementById("bkPwd").value;
    if (pwd.length < 8) { toast("备份密码至少 8 位", true); return; }
    try {
      await api("/api/backup/run", { method: "POST", json: {
        dest: destSel.value, password: pwd,
        note: document.getElementById("bkNote").value, mode: "auto" } });
      toast("备份任务已启动");
      bkRenderJob({ stage: "init", pct: 0, msg: "准备备份……", running: true });
      clearTimeout(bk.pollTimer);
      bk.pollTimer = setTimeout(bkPoll, 800);
    } catch (e) { toast(e.message, true); }
  };

  document.getElementById("bkListDest").onchange = () => { bk.dest = document.getElementById("bkListDest").value; bkLoadArchives(); };
  document.getElementById("bkReloadBtn").onclick = () => bkLoadArchives();
  document.getElementById("bkPruneBtn").onclick = async () => {
    try {
      const r = await api("/api/backup/prune", { method: "POST", json: { dest: bk.dest } });
      document.getElementById("bkPruneResult").textContent =
        r.removed.length ? `已清理 ${r.removed.length} 个旧归档（保留最近 ${r.keep} 份）` : `无需清理（当前不超过 ${r.keep} 份）`;
      bkLoadArchives();
    } catch (e) { toast(e.message, true); }
  };

  document.getElementById("bkCfgToggle").onclick = () => bkCfgRender();

  bkLoadArchives();
  bkCfgViewRender();
}

async function bkLoadArchives() {
  const el = document.getElementById("bkTable");
  if (!el) return;
  try {
    const r = await api("/api/backup/archives?dest=" + encodeURIComponent(bk.dest));
    bk.archives = r.archives || [];
  } catch (e) { el.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  if (!bk.archives.length) { el.innerHTML = `<div class="empty">该目的地暂无归档。点上方「开始备份」创建第一个加密备份。</div>`; return; }
  el.innerHTML = `
    <table class="table">
      <thead><tr><th>归档文件</th><th>大小</th><th>创建时间</th><th>内容</th><th>备注</th><th style="width:190px">操作</th></tr></thead>
      <tbody>${bk.archives.map((a, i) => `
        <tr>
          <td style="font-family:monospace;font-size:12px;word-break:break-all">${esc(a.name)}</td>
          <td>${bkFmtSize(a.size)}</td>
          <td>${esc(a.mtime)}</td>
          <td class="muted" style="font-size:12px">${(a.counts && a.counts.studies) || "?"} study / ${(a.counts && a.counts.instances) || "?"} 实例</td>
          <td class="muted" style="font-size:12px">${esc(a.note || "")}</td>
          <td>
            <button class="btn sm" onclick="bkVerifyIdx(${i})">校验</button>
            <button class="btn sm danger" onclick="bkRestoreIdx(${i})">恢复…</button>
          </td>
        </tr>`).join("")}
      </tbody></table>`;
}

function bkPwdPrompt(kind, name, extra) {
  const saved = bk.verifyPwd[kind + name];
  const input = `<div class="field" style="max-width:420px"><label>归档密码（${esc(name)}）</label>
    <input id="bkPromptPwd" type="password" value="${esc(saved || "")}" placeholder="备份时设置的密码"></div>`;
  return { input, saved };
}

window.bkVerifyIdx = async (i) => {
  const a = bk.archives[i];
  const card = document.getElementById("bkTable");
  const row = card.querySelector(`tbody tr:nth-child(${i + 1})`);
  let pwdCell = row.querySelector(".bk-verify-cell");
  if (pwdCell) { pwdCell.remove(); return; }
  const td = document.createElement("td");
  td.colSpan = 6; td.className = "bk-verify-cell";
  const { input } = bkPwdPrompt("verify", a.name);
  td.innerHTML = `<div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">${input}
    <button class="btn sm primary" id="bkVerifyGo">开始校验</button>
    <span class="muted" id="bkVerifyMsg" style="font-size:12px">GCM tag + 分区哈希全量校验</span></div>`;
  row.after(document.createElement("tr"));
  const tr = row.parentNode.lastElementChild || row.nextSibling;
  const newTr = document.createElement("tr");
  newTr.innerHTML = `<td colspan="6" style="padding:4px 12px">${td.innerHTML}</td>`;
  row.parentNode.insertBefore(newTr, row.nextSibling);
  row.querySelector(".btn").focus();
  newTr.querySelector("#bkVerifyGo").onclick = async () => {
    const msg = newTr.querySelector("#bkVerifyMsg");
    msg.textContent = "校验中……（读取并解密全量数据，约数秒）";
    try {
      const r = await api("/api/backup/verify", { method: "POST", json: {
        dest: bk.dest, name: a.name, password: newTr.querySelector("#bkPromptPwd").value } });
      if (r.ok) {
        msg.innerHTML = `<span style="color:#16a34a">✓ 校验通过：AES-256-GCM 完整性、manifest、分区哈希（app.db / secret.key / orthanc.json / index / ${r.checks.storage_files ? "存储文件数" : "存储"}）全部一致</span>`;
        bk.verifyPwd["verify" + a.name] = newTr.querySelector("#bkPromptPwd").value;
        toast("归档校验通过");
      } else {
        msg.innerHTML = `<span style="color:#dc2626">✗ ${esc(r.error || "校验失败")}</span>`;
      }
    } catch (e) { msg.innerHTML = `<span style="color:#dc2626">✗ ${esc(e.message)}</span>`; }
  };
};

window.bkRestoreIdx = async (i) => {
  const a = bk.archives[i];
  const card = document.getElementById("bkRestoreCard");
  const body = document.getElementById("bkRestoreBody");
  card.style.display = "block";
  card.scrollIntoView({ behavior: "smooth" });
  body.innerHTML = `
    <div class="hint" style="display:block">目标归档：<b>${esc(a.name)}</b>（${bkFmtSize(a.size)} · ${esc(a.mtime)}）。第一步先干跑分析归档内容。</div>
    <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;margin:10px 0">
      <div class="field" style="max-width:360px"><label>归档密码</label><input id="bkRstPwd" type="password" placeholder="备份时设置的密码"></div>
      <button class="btn primary" id="bkRstAnalyze">① 干跑分析（不写入）</button>
    </div>
    <div id="bkRstResult"></div>`;
  document.getElementById("bkRstAnalyze").onclick = async () => {
    const btn = document.getElementById("bkRstAnalyze");
    const pwd = document.getElementById("bkRstPwd").value;
    const out = document.getElementById("bkRstResult");
    btn.disabled = true; out.innerHTML = `<div class="empty">分析中……</div>`;
    try {
      const r = await api("/api/backup/analyze", { method: "POST", json: {
        dest: bk.dest, name: a.name, password: pwd } });
      if (!r.ok) { out.innerHTML = `<div class="empty" style="color:#dc2626">✗ ${esc(r.error || "分析失败")}</div>`; return; }
      const m = r.manifest, idx = (m.sections && m.sections.orthanc_index) || {}, stt = (m.sections && m.sections.storage) || {};
      out.innerHTML = `
        <table class="table" style="margin-bottom:10px">
          <tr><th>创建时间</th><td>${esc(m.created)}（主机 ${esc(m.host)}）</td></tr>
          <tr><th>备注</th><td>${esc(m.note || "-")}</td></tr>
          <tr><th>备份模式</th><td>${m.mode === "hot" ? "热备份（WAL 重放）" : "停机一致性窗口"}</td></tr>
          <tr><th>教学库 app.db</th><td>${r.planned["app/data/app.db"] ? "✓ 将恢复" : "✗ 不含"}${pwd.length ? "" : ""}</td></tr>
          <tr><th>登录密钥 secret.key</th><td>${r.planned["app/data/secret.key"] ? "✓ 将恢复（恢复后旧登录态保留）" : "✗ 不含（恢复后需全员重新登录）"}</td></tr>
          <tr><th>Orthanc 索引</th><td>${r.planned["orthanc/storage/index"] ? "✓ " : "✗ "}${idx.instances || "?"} 实例 / ${idx.studies || "?"} study</td></tr>
          <tr><th>影像存储</th><td>${r.storage_files} 个文件（${bkFmtSize(stt.bytes || 0)}）</td></tr>
          <tr><th>orthanc.json</th><td>${r.planned["orthanc/orthanc.json"] ? "✓ 将恢复（远程节点/服务账号一并还原）" : "✗ 不含"}</td></tr>
        </table>
        <div class="hint" style="display:block">② 确认无误后执行正式恢复。<b>恢复前必须停止服务</b>（EduPACS启动器「一键停止」或 scripts\\stop_all.ps1）；
          恢复会先自动把当前数据移入回滚保障目录（.temp\\pre-restore-*），完成后建议跑一次冒烟测试。</div>
        <div style="margin:10px 0">
          <button class="btn danger" id="bkRstApply">③ 正式恢复（覆盖当前数据）</button>
        </div>`;
      const applyBtn = document.getElementById("bkRstApply");
      applyBtn.onclick = async () => {
        if (!confirm("确认恢复？当前 app.db / 影像存储 / orthanc.json 将被归档内容覆盖（原数据自动移入回滚保障目录）。恢复前请确认两个服务均已停止。")) return;
        applyBtn.disabled = true;
        out.innerHTML = `<div class="empty">恢复流程为管理员命令行操作（防止网页误触）：请在服务器上执行<br>
          <code style="word-break:break-all">.venv\\Scripts\\python tools\\backup.py restore "${esc(a.name.replace(/"/g, ""))}" --password <密码> --apply</code><br>
          或带沙箱演练：<code>--root .temp\\restore_test</code></div>`;
      };
    } catch (e) {
      out.innerHTML = `<div class="empty" style="color:#dc2626">${esc(e.message)}</div>`;
    } finally { btn.disabled = false; }
  };
};

function bkCfgViewRender() {
  const el = document.getElementById("bkCfgView");
  if (!el || !bk.state) return;
  const ds = bk.state.config.destinations || [];
  el.innerHTML = `<table class="table">
    <thead><tr><th>ID</th><th>名称</th><th>路径</th><th>保留</th><th>启用</th></tr></thead>
    <tbody>${ds.map(d => `<tr>
      <td>${esc(d.id)}</td><td>${esc(d.label || "")}</td>
      <td style="font-family:monospace;font-size:12px;word-break:break-all">${esc(d.path)}</td>
      <td>${d.keep} 份</td><td>${d.enabled ? "✓" : "—"}</td></tr>`).join("")}
    </tbody></table>
    <div class="hint" style="display:block;margin-top:8px">异机备份：把 path 改成 UNC 网络路径（如 \\FILESERVER\EduPACS备份）或挂载的 NFS/SSHFS 目录，与本地目的地并存。每次备份只写入当前选中的一个目的地；需要同机 + 异机两份时，请分别选择目的地各执行一次。</div>`;
}

function bkCfgRender() {
  const view = document.getElementById("bkCfgView");
  const edit = document.getElementById("bkCfgEdit");
  const ds = JSON.parse(JSON.stringify((bk.state && bk.state.config.destinations) || []));
  edit.style.display = "block";
  edit.innerHTML = `
    ${ds.map((d, i) => `
      <div class="form-grid" data-i="${i}" style="margin-bottom:8px;padding:10px;border:1px dashed var(--border);border-radius:8px">
        <div class="field"><label>ID（字母数字-_）</label><input data-k="id" value="${esc(d.id)}"></div>
        <div class="field"><label>名称</label><input data-k="label" value="${esc(d.label || "")}"></div>
        <div class="field"><label>路径（相对=项目内；异机填 UNC/NFS 挂载路径）</label><input data-k="path" value="${esc(d.path)}" style="font-family:monospace"></div>
        <div class="field"><label>保留份数（1-365）</label><input data-k="keep" type="number" min="1" max="365" value="${esc(String(d.keep))}"></div>
        <div class="field" style="align-self:end;display:flex;gap:8px;align-items:center">
          <label style="display:flex;gap:4px;align-items:center;margin:0"><input data-k="enabled" type="checkbox" ${d.enabled ? "checked" : ""}>启用</label>
          <button class="btn sm danger" data-del="${i}">删除</button>
        </div>
      </div>`).join("")}
    <div class="btn-row">
      <button class="btn sm" id="bkCfgAdd">+ 新增目的地</button>
      <button class="btn primary sm" id="bkCfgSave">保存配置</button>
      <button class="btn sm" id="bkCfgCancel">取消</button>
    </div>
    <div id="bkCfgMsg" class="hint" style="display:block"></div>`;
  view.style.display = "none";

  const collect = () => {
    const out = [];
    edit.querySelectorAll(".form-grid[data-i]").forEach(g => {
      if (g.dataset.dead === "1") return;
      const get = k => g.querySelector(`[data-k="${k}"]`);
      out.push({ id: get("id").value.trim(), label: get("label").value.trim(),
        path: get("path").value.trim(), keep: parseInt(get("keep").value || "7", 10),
        enabled: get("enabled").checked });
    });
    return out;
  };
  edit.querySelectorAll("[data-del]").forEach(b => {
    b.onclick = () => { b.closest(".form-grid").dataset.dead = "1"; b.closest(".form-grid").style.opacity = .35; };
  });
  document.getElementById("bkCfgAdd").onclick = () => {
    const n = document.createElement("div");
    n.className = "form-grid";
    n.dataset.i = "new";
    n.style.cssText = "margin-bottom:8px;padding:10px;border:1px dashed var(--border);border-radius:8px";
    n.innerHTML = `
      <div class="field"><label>ID（字母数字-_）</label><input data-k="id" value="dest${Math.floor(Math.random() * 90 + 10)}"></div>
      <div class="field"><label>名称</label><input data-k="label" value="新目的地"></div>
      <div class="field"><label>路径</label><input data-k="path" value="D:\\backups\\edupacs" style="font-family:monospace"></div>
      <div class="field"><label>保留份数</label><input data-k="keep" type="number" min="1" max="365" value="7"></div>
      <div class="field" style="align-self:end;display:flex;gap:8px;align-items:center">
        <label style="display:flex;gap:4px;align-items:center;margin:0"><input data-k="enabled" type="checkbox" checked>启用</label>
        <button class="btn sm danger" data-del>删除</button>
      </div>`;
    edit.insertBefore(n, document.getElementById("bkCfgAdd").parentNode);
    n.querySelector("[data-del]").onclick = () => { n.dataset.dead = "1"; n.style.opacity = .35; };
  };
  document.getElementById("bkCfgCancel").onclick = () => { edit.style.display = "none"; view.style.display = "block"; };
  document.getElementById("bkCfgSave").onclick = async () => {
    const msg = document.getElementById("bkCfgMsg");
    try {
      const r = await api("/api/backup/config", { method: "POST", json: { destinations: collect() } });
      bk.state = Object.assign(bk.state, { config: r.config });
      toast("备份配置已保存");
      edit.style.display = "none"; view.style.display = "block";
      bkCfgViewRender();
      location.hash = "#/backup"; viewBackup();
    } catch (e) { msg.textContent = e.message; }
  };
}

/* ---------------- 路由 ---------------- */
async function router() {
  const hash = location.hash || "#/dashboard";
  if (!state.user || hash === "#/login") { viewLogin(); return; }
  try {
    if (!state.config._loaded) {
      state.config = Object.assign(await api("/api/config"), { _loaded: true });
    }
  } catch (e) { /* ignore */ }
  try {
    if (hash.startsWith("#/case/")) await viewCase(parseInt(hash.split("/")[2]));
    else if (hash === "#/cases") await viewCases();
    else if (hash === "#/upload") await viewUpload();
    else if (hash === "#/nodes") await viewNodes();
    else if (hash === "#/dicts") await viewDicts();
    else if (hash === "#/manage") await viewManage();
    else if (hash === "#/backup") await viewBackup();
    else if (hash === "#/students") await viewStudents();
    else if (hash === "#/my") await viewMy();
    else if (hash === "#/dashboard") await viewDashboard();
    else await viewDashboard();
  } catch (e) {
    layout(`<div class="card"><div class="empty">${esc(e.message)}</div></div>`, "");
  }
}

window.addEventListener("hashchange", () => { editingCase = false; router(); });
router();

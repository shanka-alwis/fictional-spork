"use strict";

// ---------- helpers ----------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

async function api(path, opts = {}) {
  const init = { headers: {}, ...opts };
  if (opts.body && typeof opts.body !== "string") {
    init.body = JSON.stringify(opts.body);
    init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch("/api" + path, init);
  if (res.status === 401 && path !== "/login") { showLogin(); throw new Error("login required"); }
  const type = res.headers.get("content-type") || "";
  const data = type.includes("json") ? await res.json() : await res.blob();
  if (!res.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => `${d.loc.slice(-1)[0]}: ${d.msg}`).join("; ");
    throw new Error(msg || res.statusText);
  }
  return data;
}

function toast(msg, bad = false) {
  const el = document.createElement("div");
  el.textContent = msg;
  if (bad) el.className = "bad";
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), bad ? 7000 : 3500);
}

function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return (n / 1024 ** i).toFixed(i ? 1 : 0) + " " + u[i];
}
const fmtTime = (iso) => iso ? new Date(iso).toLocaleString() : "—";
const fmtClock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}
function stateBadge(st) {
  const cls = { recording: "recording", error: "error", starting: "ok" }[st.state] || "";
  const title = st.error ? ` title="${esc(st.error)}"` : "";
  return `<span class="badge ${cls}"${title}>${esc(st.state)}</span>`;
}

function openModal(html, onMount) {
  const dlg = $("#modal");
  $("#modal-body").innerHTML = html;
  dlg.showModal();
  onMount && onMount($("#modal-body"));
}
const closeModal = () => $("#modal").close();

// ---------- auth ----------
function showLogin() {
  stopTimers();
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } });
    $("#login").classList.add("hidden");
    boot();
  } catch (err) { $("#login-error").textContent = err.message; }
});

$("#logout").addEventListener("click", async () => { await api("/logout", { method: "POST" }); showLogin(); });

// ---------- routing ----------
let timers = [];
function every(ms, fn) { timers.push(setInterval(fn, ms)); }
let liveToken = 0; // bumped on navigation so live-view frame loops stop
function stopTimers() { timers.forEach(clearInterval); timers = []; liveToken++; }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const views = {};
function route() {
  stopTimers();
  const name = (location.hash.slice(1) || "live").split("?")[0];
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
  $("#view").innerHTML = "";
  (views[name] || views.live)($("#view"));
}
$$("#nav a").forEach((a) => a.addEventListener("click", () => { location.hash = a.dataset.view; }));
window.addEventListener("hashchange", route);

async function refreshStatus() {
  try {
    const s = await api("/status");
    $("#status-bar").textContent =
      `${s.recording}/${s.cameras} recording · ${fmtBytes(s.recordings_bytes)} stored · ${fmtBytes(s.disk_free_bytes)} free`;
  } catch { /* ignore */ }
}

let statusTimer = null;
async function boot() {
  const sess = await api("/session");
  if (sess.auth_enabled && !sess.user) return showLogin();
  $("#logout").classList.toggle("hidden", !sess.auth_enabled);
  $("#app").classList.remove("hidden");
  refreshStatus();
  if (!statusTimer) statusTimer = setInterval(refreshStatus, 15000);
  route();
}

// ---------- Live ----------
views.live = async (el) => {
  const token = liveToken; // captured before any await, so a quick navigation away stops the loops
  const cols = localStorage.getItem("gridCols") || "2";
  el.innerHTML = `<h2>Live view <span class="spacer"></span>
      <span class="grid-controls">${[1, 2, 3, 4].map((n) =>
        `<button data-cols="${n}" class="${String(n) === cols ? "primary" : ""}">${n}×</button>`).join("")}</span></h2>
    <div class="grid" style="--cols:${cols}"></div>`;
  $$("[data-cols]", el).forEach((b) => b.addEventListener("click", () => {
    try { localStorage.setItem("gridCols", b.dataset.cols); } catch { /* private mode */ }
    route();
  }));
  const [all, settings] = await Promise.all([api("/cameras"), api("/settings")]);
  const cams = all.filter((c) => c.enabled);
  const grid = $(".grid", el);
  if (!cams.length) {
    grid.outerHTML = `<div class="card empty">No cameras yet. <a href="#cameras">Add a camera</a> to get started.</div>`;
    return;
  }
  for (const c of cams) {
    const tile = document.createElement("div");
    tile.className = "tile";
    tile.innerHTML = `<div class="msg">Connecting…</div><img alt="${esc(c.name)}">
      <div class="overlay"><span class="name">${esc(c.name)}</span><span class="st">${stateBadge(c.status)}</span></div>`;
    tile.addEventListener("dblclick", () => tile.classList.toggle("fullscreen"));
    tile.dataset.id = c.id;
    grid.appendChild(tile);
    pumpFrames(tile, c.id, token, settings.live_fps);
  }
  every(5000, async () => {
    const fresh = await api("/cameras");
    for (const c of fresh) {
      const t = $(`.tile[data-id="${c.id}"] .st`, el);
      if (t) t.innerHTML = stateBadge(c.status);
    }
  });
};

// Fetch one frame at a time per camera, until the user navigates away.
async function pumpFrames(tile, camId, token, fps) {
  const img = $("img", tile), msg = $(".msg", tile);
  let shown = null;
  while (token === liveToken) {
    const t0 = performance.now();
    try {
      const res = await fetch(`/api/cameras/${camId}/frame`, { cache: "no-store" });
      if (res.status === 401) return showLogin();
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      const url = URL.createObjectURL(await res.blob());
      img.src = url;
      if (shown) URL.revokeObjectURL(shown);
      shown = url;
      msg.classList.add("hidden");
    } catch (err) {
      msg.textContent = `Stream unavailable: ${err.message}. Retrying…`;
      msg.classList.remove("hidden");
      await sleep(5000);
    }
    await sleep(Math.max(0, 1000 / fps - (performance.now() - t0)));
  }
  if (shown) URL.revokeObjectURL(shown);
}

// ---------- Recordings ----------
views.recordings = async (el) => {
  const cams = await api("/cameras");
  const camName = Object.fromEntries(cams.map((c) => [c.id, c.name]));
  const dests = await api("/destinations");
  const destName = Object.fromEntries(dests.map((d) => [d.id, d.name]));
  el.innerHTML = `<h2>Recordings</h2>
    <div class="filters">
      <select id="f-cam"><option value="">All cameras</option>
        ${cams.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}</select>
      <select id="f-date"></select>
    </div>
    <div class="rec-layout">
      <div class="card player hidden"><video controls autoplay playsinline></video>
        <div class="muted" id="now-playing" style="margin-top:8px"></div></div>
      <div class="card">
        <div class="timeline"><div class="hours">${Array.from({ length: 24 }, (_, h) => `<span>${h}</span>`).join("")}</div></div>
        <div class="table-wrap"><table><thead><tr><th>Start</th><th>Camera</th><th>Length</th><th>Size</th>
          <th>Uploaded</th><th></th></tr></thead><tbody></tbody></table></div>
      </div>
    </div>`;
  const video = $("video", el);
  let list = [];

  async function loadDates() {
    const cam = $("#f-cam", el).value;
    const dates = await api("/recordings/dates" + (cam ? `?camera_id=${cam}` : ""));
    const cur = $("#f-date", el).value;
    $("#f-date", el).innerHTML = dates.length
      ? dates.map((d) => `<option ${d === cur ? "selected" : ""}>${d}</option>`).join("")
      : `<option value="">No recordings</option>`;
    await loadList();
  }

  function play(r) {
    $(".player", el).classList.remove("hidden");
    video.src = `/api/recordings/${r.camera_id}/${r.filename}`;
    $("#now-playing", el).textContent = `${camName[r.camera_id] || "Camera " + r.camera_id} — ${fmtTime(r.start)}`;
    $$("tr", el).forEach((tr) => tr.classList.toggle("playing", tr.dataset.rel === r.rel_path));
    video.onended = () => { // continue with the next segment of the same camera
      const next = list.filter((x) => x.camera_id === r.camera_id && x.start > r.start)
        .sort((a, b) => a.start.localeCompare(b.start))[0];
      if (next) play(next);
    };
  }

  async function loadList() {
    const cam = $("#f-cam", el).value, date = $("#f-date", el).value;
    const tbody = $("tbody", el);
    if (!date) { list = []; tbody.innerHTML = `<tr><td colspan="6" class="empty">No recordings yet.</td></tr>`; drawTimeline(); return; }
    const q = new URLSearchParams({ date, limit: 2000 });
    if (cam) q.set("camera_id", cam);
    list = await api("/recordings?" + q);
    tbody.innerHTML = list.map((r) => {
      const len = (new Date(r.end) - new Date(r.start)) / 1000;
      const up = r.uploaded_to.length
        ? r.uploaded_to.map((id) => `<span class="badge ok">${esc(destName[id] || "#" + id)}</span>`).join(" ")
        : `<span class="muted">${r.ready ? "pending" : "recording…"}</span>`;
      return `<tr data-rel="${esc(r.rel_path)}"><td>${fmtClock(r.start)}</td><td>${esc(camName[r.camera_id] || r.camera_id)}</td>
        <td>${fmtDur(len)}</td><td>${fmtBytes(r.size)}</td><td>${up}</td>
        <td class="actions"><button data-act="play">Play</button>
          <a href="/api/recordings/${r.camera_id}/${r.filename}?download=true"><button>Download</button></a>
          <button data-act="del" class="danger">Delete</button></td></tr>`;
    }).join("") || `<tr><td colspan="6" class="empty">Nothing on this day.</td></tr>`;
    $$("tbody tr[data-rel]", el).forEach((tr) => {
      const r = list.find((x) => x.rel_path === tr.dataset.rel);
      $("[data-act=play]", tr).addEventListener("click", () => play(r));
      $("[data-act=del]", tr).addEventListener("click", async () => {
        if (!confirm(`Delete recording ${r.filename}?`)) return;
        await api(`/recordings/${r.camera_id}/${r.filename}`, { method: "DELETE" });
        loadList();
      });
    });
    drawTimeline();
  }

  function drawTimeline() {
    const tl = $(".timeline", el);
    $$(".seg", tl).forEach((s) => s.remove());
    for (const r of list) {
      const s = new Date(r.start), e = new Date(r.end);
      const day0 = new Date(s); day0.setHours(0, 0, 0, 0);
      const left = (s - day0) / 864e5 * 100;
      const width = Math.max(0.15, (e - s) / 864e5 * 100);
      const seg = document.createElement("div");
      seg.className = "seg";
      seg.style.left = left + "%";
      seg.style.width = Math.min(width, 100 - left) + "%";
      seg.title = `${camName[r.camera_id] || ""} ${fmtClock(r.start)}`;
      seg.addEventListener("click", () => play(r));
      tl.appendChild(seg);
    }
  }

  $("#f-cam", el).addEventListener("change", loadDates);
  $("#f-date", el).addEventListener("change", loadList);
  await loadDates();
};

// ---------- Cameras ----------
function scheduleRow(w = { days: [0, 1, 2, 3, 4, 5, 6], start: "22:00", end: "06:00" }) {
  return `<div class="sched-row"><span class="days">${DAYS.map((d, i) =>
      `<label>${d[0]}<input type="checkbox" value="${i}" ${w.days.includes(i) ? "checked" : ""}></label>`).join("")}</span>
    <input type="time" class="s-start" value="${esc(w.start)}"> to <input type="time" class="s-end" value="${esc(w.end)}">
    <button type="button" class="ghost danger s-del">✕</button></div>`;
}

// Tapo cameras: rtsp://<account>:<password>@<ip>:554/stream1 (full quality) and /stream2 (low res).
const TAPO_RE = /^rtsp:\/\/([^:@\/]+):([^@\/]*)@([^:\/]+)(?::554)?\/stream1$/;
function tapoUrls(ip, user, pass) {
  const auth = `${encodeURIComponent(user)}:${encodeURIComponent(pass)}@${ip.trim()}:554`;
  return { url: `rtsp://${auth}/stream1`, live_url: `rtsp://${auth}/stream2` };
}
function parseTapo(url) {
  const m = TAPO_RE.exec(url || "");
  return m ? { user: decodeURIComponent(m[1]), pass: decodeURIComponent(m[2]), ip: m[3] } : null;
}

function cameraForm(cam) {
  const c = cam || { name: "", url: "", live_url: null, enabled: true, record_mode: "continuous", schedule: [],
    segment_seconds: 300, audio: true };
  const tapo = cam ? parseTapo(cam.url) : { user: "", pass: "", ip: "" };
  openModal(`<h3>${cam ? "Edit camera" : "Add camera"}</h3>
    <form id="cam-form">
      <label>Name <input name="name" required value="${esc(c.name)}" placeholder="Front door"></label>
      <label>Camera type <select name="kind">
        <option value="tapo">TP-Link Tapo (C200, C210, C310, …)</option>
        <option value="other">Other RTSP / HTTP camera</option></select></label>
      <div id="kind-tapo">
        <label>IP address <input name="ip" value="${esc(tapo ? tapo.ip : "")}" placeholder="192.168.1.20">
          <div class="hint">Shown in the Tapo app under Camera settings → Device info. Reserve it in your router so it never changes.</div></label>
        <div class="row">
          <label>Camera account username <input name="tuser" autocomplete="off" value="${esc(tapo ? tapo.user : "")}"></label>
          <label>Camera account password <input name="tpass" type="password" autocomplete="new-password" value="${esc(tapo ? tapo.pass : "")}"></label>
        </div>
        <div class="hint" style="margin:-8px 0 12px">Not your Tapo login: create it in the Tapo app under
          Camera settings → Advanced settings → Camera account.</div>
      </div>
      <div id="kind-other">
        <label>Recording stream URL <input name="url" value="${esc(c.url)}" placeholder="rtsp://user:pass@192.168.1.20:554/stream1">
          <div class="hint">Full-quality stream. HTTP streams and local video files also work.</div></label>
        <label>Live view stream URL (optional) <input name="live_url" value="${esc(c.live_url || "")}" placeholder="rtsp://…/sub-stream">
          <div class="hint">A low-resolution sub-stream keeps live view light. Leave empty to use the recording stream.</div></label>
      </div>
      <div class="row">
        <label>Recording
          <select name="record_mode">
            <option value="continuous">Continuous (24/7)</option>
            <option value="schedule">On a schedule</option>
            <option value="off">Off (live view only)</option>
          </select></label>
        <label>Segment length
          <select name="segment_seconds">${[60, 120, 300, 600, 900, 1800, 3600].map((s) =>
            `<option value="${s}">${s < 3600 ? s / 60 + " min" : "1 hour"}</option>`).join("")}</select></label>
      </div>
      <div id="sched" class="card" style="margin-bottom:12px">
        <div class="muted" style="margin-bottom:8px">Record during these times</div>
        <div id="sched-rows">${(c.schedule.length ? c.schedule : [undefined]).map(scheduleRow).join("")}</div>
        <button type="button" id="sched-add">+ Add time window</button>
      </div>
      <label class="check"><input type="checkbox" name="audio" ${c.audio ? "checked" : ""}> Record audio</label>
      <label class="check"><input type="checkbox" name="enabled" ${c.enabled ? "checked" : ""}> Enabled</label>
      <div class="preview"></div>
      <div class="modal-actions">
        ${cam ? `<button type="button" class="danger left" id="cam-del">Delete</button>` : ""}
        <button type="button" id="cam-test">Test connection</button>
        <button type="button" onclick="closeModal()">Cancel</button>
        <button class="primary" type="submit">Save</button>
      </div>
    </form>`, (root) => {
    const f = $("#cam-form", root);
    f.kind.value = tapo ? "tapo" : "other";
    const syncKind = () => {
      const isTapo = f.kind.value === "tapo";
      $("#kind-tapo", root).classList.toggle("hidden", !isTapo);
      $("#kind-other", root).classList.toggle("hidden", isTapo);
      f.ip.required = f.tuser.required = isTapo;
      f.url.required = !isTapo;
    };
    f.kind.addEventListener("change", syncKind); syncKind();
    const urls = () => f.kind.value === "tapo" ? tapoUrls(f.ip.value, f.tuser.value, f.tpass.value)
      : { url: f.url.value.trim(), live_url: f.live_url.value.trim() || null };
    f.record_mode.value = c.record_mode;
    f.segment_seconds.value = String(c.segment_seconds);
    const syncSched = () => $("#sched", root).classList.toggle("hidden", f.record_mode.value !== "schedule");
    f.record_mode.addEventListener("change", syncSched); syncSched();
    $("#sched-add", root).addEventListener("click", () => $("#sched-rows", root).insertAdjacentHTML("beforeend", scheduleRow()));
    $("#sched-rows", root).addEventListener("click", (e) => { if (e.target.classList.contains("s-del")) e.target.parentElement.remove(); });

    $("#cam-test", root).addEventListener("click", async (e) => {
      const btn = e.target; btn.disabled = true; btn.textContent = "Testing…";
      const prev = $(".preview", root);
      try {
        const u = urls();
        const blob = await api("/cameras/test", { method: "POST", body: { url: u.url } });
        prev.innerHTML = `<img src="${URL.createObjectURL(blob)}">`;
        if (u.live_url) await api("/cameras/test", { method: "POST", body: { url: u.live_url } });
        prev.insertAdjacentHTML("beforeend", `<p class="badge ok">Connected${u.live_url ? " (both streams)" : ""}</p>`);
      } catch (err) { prev.innerHTML = `<p class="error">${esc(err.message)}</p>`; }
      btn.disabled = false; btn.textContent = "Test connection";
    });
    if (cam) $("#cam-del", root).addEventListener("click", async () => {
      if (!confirm(`Delete camera "${cam.name}"? Existing recordings stay on disk.`)) return;
      await api(`/cameras/${cam.id}`, { method: "DELETE" });
      closeModal(); route();
    });
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      const schedule = $$(".sched-row", root).map((r) => ({
        days: $$(".days input:checked", r).map((i) => +i.value),
        start: $(".s-start", r).value, end: $(".s-end", r).value,
      }));
      const body = { name: f.name.value, ...urls(), record_mode: f.record_mode.value,
        segment_seconds: +f.segment_seconds.value, audio: f.audio.checked, enabled: f.enabled.checked,
        schedule: f.record_mode.value === "schedule" ? schedule : c.schedule };
      try {
        await api(cam ? `/cameras/${cam.id}` : "/cameras", { method: cam ? "PUT" : "POST", body });
        closeModal(); toast("Camera saved"); route();
      } catch (err) { toast(err.message, true); }
    });
  });
}

views.cameras = async (el) => {
  el.innerHTML = `<h2>Cameras <span class="spacer"></span><button class="primary" id="add">+ Add camera</button></h2>
    <div class="card table-wrap"><table><thead><tr><th>Name</th><th>Status</th><th>Recording</th><th>Segments</th><th></th></tr></thead>
    <tbody></tbody></table></div>`;
  $("#add", el).addEventListener("click", () => cameraForm());
  async function load() {
    const cams = await api("/cameras");
    const mode = (c) => c.record_mode === "schedule"
      ? "Schedule: " + c.schedule.map((w) => `${w.start}–${w.end}`).join(", ")
      : { continuous: "Continuous", off: "Off" }[c.record_mode];
    $("tbody", el).innerHTML = cams.map((c) => `<tr data-id="${c.id}">
      <td><b>${esc(c.name)}</b>${c.enabled ? "" : ` <span class="muted">(disabled)</span>`}</td>
      <td>${stateBadge(c.status)}${c.status.error ? `<div class="hint">${esc(c.status.error)}</div>` : ""}</td>
      <td>${esc(mode(c))}</td><td>${c.segment_seconds / 60} min</td>
      <td class="actions"><button data-act="edit">Edit</button></td></tr>`).join("")
      || `<tr><td colspan="5" class="empty">No cameras yet.</td></tr>`;
    $$("tr[data-id]", el).forEach((tr) => $("[data-act=edit]", tr).addEventListener("click", () =>
      cameraForm(cams.find((c) => c.id === +tr.dataset.id))));
  }
  await load();
  every(5000, () => { if (!$("#modal").open) load(); });
};

// ---------- Uploads ----------
const CRON_PRESETS = [
  ["0 1 * * *", "Daily at 01:00"], ["0 2 * * *", "Daily at 02:00"], ["0 * * * *", "Every hour"],
  ["0 */6 * * *", "Every 6 hours"], ["*/15 * * * *", "Every 15 minutes"], ["0 3 * * 0", "Weekly (Sunday 03:00)"],
];

async function destForm(dest) {
  const [cams, remotes] = await Promise.all([api("/cameras"), api("/rclone/remotes").catch(() => [])]);
  const d = dest || { name: "Google Drive", type: "rclone", target: remotes[0] ? remotes[0] + "CCTV" : "gdrive:CCTV",
    cron: "0 1 * * *", camera_ids: null, delete_after_upload: false, remote_keep_days: 30, enabled: true };
  const isPreset = CRON_PRESETS.some(([c]) => c === d.cron);
  openModal(`<h3>${dest ? "Edit upload destination" : "Add upload destination"}</h3>
    <form id="dest-form">
      <label>Name <input name="name" required value="${esc(d.name)}"></label>
      <label>Type <select name="type">
        <option value="rclone">Cloud drive via rclone (Google Drive, OneDrive, Dropbox, S3…)</option>
        <option value="local">Folder on this machine / mounted network share</option></select></label>
      <label>Target <input name="target" required value="${esc(d.target)}" list="remotes">
        <datalist id="remotes">${remotes.map((r) => `<option value="${esc(r)}CCTV">`).join("")}</datalist>
        <div class="hint" id="target-hint"></div></label>
      <div class="row">
        <label>Schedule <select name="preset">${CRON_PRESETS.map(([c, l]) => `<option value="${c}">${l}</option>`).join("")}
          <option value="custom">Custom (cron)…</option></select></label>
        <label>Cron expression <input name="cron" value="${esc(d.cron)}" placeholder="m h dom mon dow"></label>
      </div>
      <label>Keep footage on the destination for (days)
        <input type="number" min="0" name="remote_keep_days" value="${d.remote_keep_days}">
        <div class="hint">Older footage is deleted from the destination after each upload run. 0 = keep forever.
          Only files this app uploaded are ever deleted.</div></label>
      <div class="muted" style="margin-bottom:6px">Cameras</div>
      <label class="check"><input type="checkbox" name="allcams" ${d.camera_ids ? "" : "checked"}> All cameras</label>
      <div id="camlist" style="margin:-6px 0 12px 24px">${cams.map((c) => `<label class="check">
        <input type="checkbox" value="${c.id}" ${d.camera_ids && d.camera_ids.includes(c.id) ? "checked" : ""}> ${esc(c.name)}</label>`).join("")}</div>
      <label class="check"><input type="checkbox" name="delete_after_upload" ${d.delete_after_upload ? "checked" : ""}>
        Delete local copy after a successful upload</label>
      <div class="hint" style="margin:-8px 0 12px 24px">Only tick this on your last/only destination, otherwise other destinations may miss files.</div>
      <label class="check"><input type="checkbox" name="enabled" ${d.enabled ? "checked" : ""}> Enabled</label>
      <p id="dest-msg"></p>
      <div class="modal-actions">
        ${dest ? `<button type="button" class="danger left" id="dest-del">Delete</button>` : ""}
        <button type="button" id="dest-test">Test</button>
        <button type="button" onclick="closeModal()">Cancel</button>
        <button class="primary" type="submit">Save</button>
      </div>
    </form>`, (root) => {
    const f = $("#dest-form", root);
    f.type.value = d.type;
    f.preset.value = isPreset ? d.cron : "custom";
    const hint = () => {
      $("#target-hint", root).innerHTML = f.type.value === "rclone"
        ? (remotes.length ? `Configured remotes: ${remotes.map(esc).join(", ")}. Format <code>remote:folder</code>.`
          : `No rclone remotes found. Run <code>rclone config</code> first (see README), then use <code>remote:folder</code>.`)
        : "Absolute path, e.g. /mnt/nas/cctv";
    };
    f.type.addEventListener("change", hint); hint();
    f.preset.addEventListener("change", () => { if (f.preset.value !== "custom") f.cron.value = f.preset.value; });
    f.cron.addEventListener("input", () => { f.preset.value = CRON_PRESETS.some(([c]) => c === f.cron.value) ? f.cron.value : "custom"; });
    const syncCams = () => $("#camlist", root).classList.toggle("hidden", f.allcams.checked);
    f.allcams.addEventListener("change", syncCams); syncCams();
    const body = () => ({ name: f.name.value, type: f.type.value, target: f.target.value, cron: f.cron.value,
      camera_ids: f.allcams.checked ? null : $$("#camlist input:checked", root).map((i) => +i.value),
      delete_after_upload: f.delete_after_upload.checked, remote_keep_days: +f.remote_keep_days.value,
      enabled: f.enabled.checked });
    $("#dest-test", root).addEventListener("click", async (e) => {
      const msg = $("#dest-msg", root); e.target.disabled = true; msg.className = "muted"; msg.textContent = "Testing…";
      try { const r = await api("/destinations/test", { method: "POST", body: body() }); msg.className = "badge ok"; msg.textContent = r.message; }
      catch (err) { msg.className = "error"; msg.textContent = err.message; }
      e.target.disabled = false;
    });
    if (dest) $("#dest-del", root).addEventListener("click", async () => {
      if (!confirm(`Delete destination "${dest.name}"? Files already uploaded are not touched.`)) return;
      await api(`/destinations/${dest.id}`, { method: "DELETE" }); closeModal(); route();
    });
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await api(dest ? `/destinations/${dest.id}` : "/destinations", { method: dest ? "PUT" : "POST", body: body() });
        closeModal(); toast("Destination saved"); route();
      } catch (err) { toast(err.message, true); }
    });
  });
}

views.uploads = async (el) => {
  el.innerHTML = `<h2>Scheduled uploads <span class="spacer"></span><button class="primary" id="add">+ Add destination</button></h2>
    <div class="card table-wrap" style="margin-bottom:16px"><table><thead><tr><th>Name</th><th>Target</th><th>Schedule</th>
      <th>Next run</th><th>Last run</th><th>Waiting</th><th>Space needed</th><th></th></tr></thead><tbody id="dests"></tbody></table></div>
    <h2>Recent uploads</h2>
    <div class="card table-wrap"><table><thead><tr><th>Time</th><th>Destination</th><th>File</th><th>Size</th><th>Status</th></tr></thead>
      <tbody id="hist"></tbody></table></div>`;
  $("#add", el).addEventListener("click", () => destForm());
  const cronLabel = (c) => (CRON_PRESETS.find(([x]) => x === c) || [c, c])[1];
  async function load() {
    const [dests, hist] = await Promise.all([api("/destinations"), api("/uploads?limit=100")]);
    $("#dests", el).innerHTML = dests.map((d) => `<tr data-id="${d.id}">
      <td><b>${esc(d.name)}</b>${d.enabled ? "" : ` <span class="muted">(disabled)</span>`}</td>
      <td><code>${esc(d.target)}</code></td><td>${esc(cronLabel(d.cron))}</td>
      <td>${d.enabled ? fmtTime(d.next_run) : "—"}</td>
      <td>${d.running ? `<span class="badge recording">uploading…</span>` : d.last_run
        ? `${fmtTime(d.last_run)}<div class="hint">${esc(d.last_status)}</div>` : "never"}</td>
      <td>${d.pending} files<div class="hint">${fmtBytes(d.pending_bytes)}</div></td>
      <td>${d.estimated_remote_bytes ? `~${fmtBytes(d.estimated_remote_bytes)}<div class="hint">${d.remote_keep_days} days at
        ${fmtBytes(d.bytes_per_day)}/day</div>` : d.remote_keep_days ? `<span class="muted">measuring…</span>` : "keeps forever"}</td>
      <td class="actions"><button data-act="run" ${d.running ? "disabled" : ""}>Upload now</button><button data-act="edit">Edit</button></td></tr>`).join("")
      || `<tr><td colspan="8" class="empty">No destinations yet. Add one to start uploading footage on a schedule.</td></tr>`;
    $$("#dests tr[data-id]", el).forEach((tr) => {
      const d = dests.find((x) => x.id === +tr.dataset.id);
      $("[data-act=edit]", tr).addEventListener("click", () => destForm(d));
      $("[data-act=run]", tr).addEventListener("click", async () => {
        await api(`/destinations/${d.id}/run`, { method: "POST" }); toast(`Upload to ${d.name} started`); load();
      });
    });
    $("#hist", el).innerHTML = hist.map((u) => `<tr><td>${fmtTime(u.updated_at)}</td><td>${esc(u.destination_name || "—")}</td>
      <td title="${esc(u.remote_path || "")}">${esc(u.rel_path)}</td><td>${fmtBytes(u.size)}</td>
      <td>${u.status === "done" ? `<span class="badge ok">done</span>` : `<span class="badge error" title="${esc(u.error)}">failed</span>
        <div class="hint">${esc(u.error)}</div>`}</td></tr>`).join("")
      || `<tr><td colspan="5" class="empty">Nothing uploaded yet.</td></tr>`;
  }
  await load();
  every(5000, () => { if (!$("#modal").open) load(); });
};

// ---------- Settings ----------
views.settings = async (el) => {
  const [s, st] = await Promise.all([api("/settings"), api("/status")]);
  const usedPct = st.disk_total_bytes ? (1 - st.disk_free_bytes / st.disk_total_bytes) * 100 : 0;
  const health = st.disk_health;
  const failing = health && /FAIL/.test(health);
  const healthy = health && !failing && /PASSED|\bOK\b/.test(health);
  const rates = Object.entries(st.bytes_per_day);
  el.innerHTML = `<h2>Settings</h2>
    <div class="stats">
      <div class="card stat"><div class="v">${fmtBytes(st.recordings_bytes)}</div><div class="k">Recordings on this computer</div></div>
      <div class="card stat"><div class="v">${fmtBytes(st.disk_free_bytes)}</div><div class="k">Free of ${fmtBytes(st.disk_total_bytes)}</div>
        <div class="bar"><div style="width:${usedPct.toFixed(1)}%"></div></div></div>
      <div class="card stat"><div class="v">${st.total_bytes_per_day ? fmtBytes(st.total_bytes_per_day) : "—"}</div>
        <div class="k">Recorded per day (all cameras)</div></div>
      <div class="card stat"><div class="v">${health ? `<span class="badge ${healthy ? "ok" : failing ? "error" : ""}">${
        healthy ? "healthy" : failing ? "check disk" : "unknown"}</span>` : "—"}</div>
        <div class="k">Disk health (SMART)${health ? ` · <a id="smart-more">details</a>` : " · not available"}</div></div>
    </div>
    ${rates.length ? `<div class="card table-wrap" style="margin-bottom:16px;max-width:640px"><table>
      <thead><tr><th>Camera</th><th>Per day</th><th>Per 30 days</th></tr></thead><tbody>
      ${rates.map(([name, b]) => `<tr><td>${esc(name)}</td><td>${b ? fmtBytes(b) : `<span class="muted">measuring…</span>`}</td>
        <td>${b ? fmtBytes(b * 30) : ""}</td></tr>`).join("")}</tbody></table></div>` : ""}
    <form id="settings" class="card" style="max-width:640px">
      <h3 style="margin-top:0">Footage on this computer</h3>
      <div class="row">
        <label>Delete after upload (days) <input type="number" min="0" step="0.5" name="local_keep_days_after_upload"
          value="${s.local_keep_days_after_upload}">
          <div class="hint">How long a clip stays here once it has been uploaded.</div></label>
        <label>Free space safeguard (% full) <input type="number" min="0" max="99" name="max_disk_percent" value="${s.max_disk_percent}">
          <div class="hint">Above this, the oldest <em>uploaded</em> clips are removed early. 0 = off.</div></label>
      </div>
      <label>Delete even if not uploaded after (days) <input type="number" min="0" name="max_age_days" value="${s.max_age_days}">
        <div class="hint">Last resort if uploads keep failing. 0 = never delete footage that hasn't been uploaded.</div></label>
      <h3>Live view</h3>
      <div class="row">
        <label>Frames per second <input type="number" min="1" max="30" name="live_fps" value="${s.live_fps}"></label>
        <label>Width (px) <input type="number" min="160" max="3840" name="live_width" value="${s.live_width}"></label>
      </div>
      <div class="modal-actions"><button type="button" id="ret-run">Run cleanup now</button>
        <button class="primary" type="submit">Save</button></div>
    </form>`;
  const f = $("#settings", el);
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/settings", { method: "PUT", body: {
        local_keep_days_after_upload: +f.local_keep_days_after_upload.value, max_age_days: +f.max_age_days.value,
        max_disk_percent: +f.max_disk_percent.value, live_fps: +f.live_fps.value, live_width: +f.live_width.value } });
      toast("Settings saved");
    } catch (err) { toast(err.message, true); }
  });
  $("#ret-run", el).addEventListener("click", async () => {
    const r = await api("/retention/run", { method: "POST" }); toast(`Removed ${r.removed} file(s)`);
  });
  if (health) $("#smart-more", el).addEventListener("click", () =>
    openModal(`<h3>Disk health</h3><pre class="log" style="white-space:pre-wrap">${esc(health)}</pre>
      <div class="modal-actions"><button onclick="closeModal()">Close</button></div>`));
};

// ---------- Logs ----------
views.logs = async (el) => {
  el.innerHTML = `<h2>Logs</h2><div class="card table-wrap"><table class="log"><tbody></tbody></table></div>`;
  async function load() {
    const rows = await api("/logs");
    $("tbody", el).innerHTML = rows.map((r) => `<tr class="lvl-${r.level}"><td>${fmtTime(r.time)}</td>
      <td>${esc(r.level)}</td><td>${esc(r.source)}</td><td>${esc(r.message)}</td></tr>`).join("")
      || `<tr><td class="empty">No events.</td></tr>`;
  }
  await load();
  every(5000, load);
};

boot();

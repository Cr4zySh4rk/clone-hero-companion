/* Clone Hero Companion — library manager UI (no framework, no build step). */
(() => {
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtLen = (s) => (s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}` : "");
const MIN_P = 640;

async function api(path, body, method) {
  const opt = body !== undefined ? { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}
function toast(msg, ms = 2600) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("on"), ms);
}
function ask({ title, text = "", input = null, select = null, ok = "OK", danger = false }) {
  return new Promise((resolve) => {
    const dlg = $("#dlg"), f = $("#dlgForm");
    f.innerHTML = `<h2>${esc(title)}</h2>${text ? `<p class="hint">${esc(text)}</p>` : ""}
      ${select ? `<select id="dlgSel">${select.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("")}</select>` : ""}
      ${input !== null ? `<input type="text" id="dlgIn" value="${esc(input)}" placeholder="${esc(title)}">` : ""}
      <div class="btns"><button value="cancel" class="ghost">Cancel</button><button value="ok" class="${danger ? "danger" : "primary"}">${esc(ok)}</button></div>`;
    dlg.onclose = () => {
      if (dlg.returnValue !== "ok") return resolve(null);
      resolve({ input: $("#dlgIn")?.value ?? null, select: $("#dlgSel")?.value ?? null });
    };
    dlg.showModal();
    ($("#dlgIn") || $("#dlgSel"))?.focus();
  });
}

/* ================================================================ navigation */
let currentView = "library";
function setFilter(f) {
  st.filter = f;
  $$("#libFilters button").forEach((b) => b.classList.toggle("on", b.dataset.f === f));
  applyFilters();
}
function show(view) {
  if (view === "favs") { show("library"); setFilter("fav"); $$("#nav button").forEach((b) => b.classList.toggle("on", b.dataset.view === "favs")); return; }
  currentView = view;
  $$("#nav button").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("on", v.id === "view-" + view));
  $("#detail").hidden = !(view === "library" && activeId);
  $("#shell").classList.toggle("detail-open", !$("#detail").hidden);
  if (view === "now" && $("#nowFrame").src === "about:blank") $("#nowFrame").src = "/now";
  if (view === "setlists") loadSetlists();
  if (view === "dupes") loadDupes();
  if (view === "settings") loadSettings();
  if (view === "setup") loadSetup();
  if (view === "jobs") renderJobs();
  if (view === "charts" && !charts.loaded) searchCharts(1);
}
$("#nav").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.view === "library" && st.filter === "fav") setFilter("all");
  show(b.dataset.view);
});
$("#launchGame").onclick = async () => { try { await api("/api/game/launch", {}); toast("Starting Clone Hero…"); } catch (e) { toast(e.message, 4000); } };

/* ================================================================ library */
const lib = { songs: [], byId: new Map(), version: -1 };
const st = { filter: "all", q: "", group: "", sort: "artist", dir: 1, sel: new Set(), anchor: null };
let rows = [];
let activeId = null;

async function loadLibrary() {
  const d = await api("/api/library");
  lib.songs = d.songs; lib.version = d.version;
  lib.byId = new Map(d.songs.map((s) => [s.id, s]));
  for (const id of [...st.sel]) if (!lib.byId.has(id)) st.sel.delete(id);
  const groups = [...new Set(d.songs.map((s) => s.group).filter(Boolean))].sort();
  const gsel = $("#libGroup"), cur = gsel.value;
  gsel.innerHTML = `<option value="">All folders</option>` + groups.map((g) => `<option${g === cur ? " selected" : ""}>${esc(g)}</option>`).join("");
  $("#navLibCount").textContent = d.songs.filter((s) => !s.disabled).length || "";
  updateFavCount();
  $("#sideStatus").textContent = d.enriching ? `Checking songs… ${d.enrich[0]}/${d.enrich[1]}` : "";
  applyFilters();
  if (activeId && lib.byId.has(activeId) && currentView === "library") refreshDetail();
}
async function pollLibrary() {
  try {
    const v = await api("/api/library/version");
    if (v.version !== lib.version) await loadLibrary();
    else $("#sideStatus").textContent = v.enriching ? `Checking songs… ${v.enrich[0]}/${v.enrich[1]}` : "";
  } catch (e) { /* app closing */ }
  setTimeout(pollLibrary, 2500);
}

function videoState(s) {
  if (!s.hasVideo) return s.videoFailed ? "failed" : "none";
  if (s.video && s.video < MIN_P) return "low";
  return "ok";
}
function applyFilters() {
  const q = st.q.trim().toLowerCase();
  const terms = q ? q.split(/\s+/) : [];
  const now = Date.now() / 1000;
  rows = lib.songs.filter((s) => {
    if (st.filter === "disabled") { if (!s.disabled) return false; }
    else if (s.disabled && st.filter !== "all") return false;
    if (st.group && s.group !== st.group) return false;
    switch (st.filter) {
      case "novideo": if (s.hasVideo) return false; break;
      case "lowres": if (videoState(s) !== "low") return false; break;
      case "nolyrics": if (s.lyrics !== "none") return false; break;
      case "fav": if (!s.fav) return false; break;
      case "recent": if (now - s.added > 30 * 86400) return false; break;
    }
    if (terms.length) {
      const hay = `${s.name} ${s.artist} ${s.album} ${s.charter} ${s.genre} ${s.year}`.toLowerCase();
      return terms.every((t) => hay.includes(t));
    }
    return true;
  });
  const k = st.sort, d = st.dir;
  const val = (s) => {
    switch (k) {
      case "video": return s.hasVideo ? (s.video || 1) : -1;
      case "lyrics": return { chart: 2, added: 2, none: 0 }[s.lyrics] ?? 1;
      case "fav": return (s.fav ? 10 : 0) + (s.rating || 0);
      case "length": return s.length || 0;
      case "year": return parseInt(s.year) || 0;
      case "name": return s.name.toLowerCase();
      case "album": return (s.album || "").toLowerCase();
      case "charter": return (s.charter || "").toLowerCase();
      default: return `${s.artist.toLowerCase()}\u0000${s.name.toLowerCase()}`;
    }
  };
  rows.sort((a, b) => { const x = val(a), y = val(b); return x < y ? -d : x > y ? d : 0; });
  if (st.filter === "recent") rows.sort((a, b) => b.added - a.added);
  $$("#libHead .sortable").forEach((h) => { h.classList.toggle("asc", h.dataset.k === k && d > 0); h.classList.toggle("desc", h.dataset.k === k && d < 0); });
  const empty = $("#libEmpty");
  empty.hidden = rows.length > 0;
  empty.textContent = st.filter === "fav" && !lib.songs.some((s) => s.fav) ? "No favourites yet. Click the ☆ on any song, select songs and choose “Add to favourites”, or press F."
    : lib.songs.length ? "No songs match this view." : "No songs found yet. Check the Clone Hero folder in Settings.";
  renderTable(true);
  updateSelbar();
}

const body = $("#libBody"), spacer = $("#libSpacer");
const ROW = 46;
let rendered = new Map();
function renderTable(reset) {
  spacer.style.height = rows.length * ROW + "px";
  if (reset) { rendered.forEach((el) => el.remove()); rendered.clear(); }
  const top = body.scrollTop, h = body.clientHeight;
  const from = Math.max(0, Math.floor(top / ROW) - 8), to = Math.min(rows.length, Math.ceil((top + h) / ROW) + 8);
  const keep = new Set();
  for (let i = from; i < to; i++) {
    const s = rows[i];
    const key = s.id;
    keep.add(key);
    let el = rendered.get(key);
    if (!el) { el = rowEl(s); rendered.set(key, el); spacer.appendChild(el); }
    el.style.top = i * ROW + "px";
    el._i = i;
    el.classList.toggle("sel", st.sel.has(s.id));
    el.classList.toggle("active", s.id === activeId);
    el.querySelector("input").checked = st.sel.has(s.id);
  }
  rendered.forEach((el, key) => { if (!keep.has(key)) { el.remove(); rendered.delete(key); } });
}
body.addEventListener("scroll", () => renderTable(false), { passive: true });
window.addEventListener("resize", () => renderTable(false));

function pills(s) {
  const v = videoState(s);
  const vid = v === "ok" ? `<span class="pill ok">${s.video ? s.video + "p" : "video"}</span>`
    : v === "low" ? `<span class="pill low" title="Below 640p — Run with “Replace videos” to upgrade">${s.video}p</span>`
    : v === "failed" ? `<span class="pill" title="No HD music video found">none found</span>` : `<span class="pill">—</span>`;
  const ly = s.lyrics === "chart" ? `<span class="pill ok">chart</span>` : s.lyrics === "added" ? `<span class="pill ok">added</span>`
    : s.lyrics === "none" ? `<span class="pill">—</span>` : `<span class="pill">…</span>`;
  return [vid, ly];
}
function instHtml(d) {
  const map = [["guitar", "G"], ["bass", "B"], ["rhythm", "R"], ["drums", "D"], ["keys", "K"], ["vocals", "V"]];
  return `<div class="inst">${map.filter(([k]) => k in d).map(([k, l]) => `<i class="${l}" title="${k} ${d[k]}">${l}</i>`).join("")}</div>`;
}
function rowEl(s) {
  const el = document.createElement("div");
  el.className = "row" + (s.disabled ? " disabled" : "");
  el.dataset.id = s.id;
  const [vid, ly] = pills(s);
  el.innerHTML = `<div><input type="checkbox" tabindex="-1"></div>
    <div class="art"${s.art ? ` style="background-image:url('/media/art/${s.id}?v=${lib.version}')"` : ""}></div>
    <div class="t"><b>${esc(s.name)}</b><span>${esc(s.artist)}${s.sng ? " · .sng" : ""}</span></div>
    <div class="cell h1">${esc(s.album)}</div><div class="cell h1">${esc(s.year)}</div>
    <div class="cell">${fmtLen(s.length)}</div><div class="cell h1">${esc(s.charter)}</div>
    <div>${instHtml(s.diffs)}</div><div>${vid}</div><div>${ly}</div>
    <div class="fav${s.fav ? " on" : ""}" title="Favourite">${s.fav ? "★" : "☆"}</div>`;
  return el;
}
spacer.addEventListener("click", async (e) => {
  const el = e.target.closest(".row"); if (!el) return;
  const id = el.dataset.id, i = el._i;
  if (e.target.closest(".fav")) {
    const s = lib.byId.get(id);
    await setFav([id], !s.fav); return;
  }
  if (e.target.matches("input[type=checkbox]") || e.metaKey || e.ctrlKey) {
    st.sel.has(id) ? st.sel.delete(id) : st.sel.add(id); st.anchor = i;
  } else if (e.shiftKey && st.anchor !== null) {
    const [a, b] = [Math.min(st.anchor, i), Math.max(st.anchor, i)];
    for (let k = a; k <= b; k++) st.sel.add(rows[k].id);
  } else {
    st.anchor = i; openDetail(id);
  }
  renderTable(false); updateSelbar();
});
spacer.addEventListener("dblclick", (e) => { const el = e.target.closest(".row"); if (el) playSong(el.dataset.id); });
$("#selAll").addEventListener("change", (e) => {
  if (e.target.checked) rows.forEach((s) => st.sel.add(s.id)); else st.sel.clear();
  renderTable(false); updateSelbar();
});
$("#libHead").addEventListener("click", (e) => {
  const h = e.target.closest(".sortable"); if (!h) return;
  st.dir = st.sort === h.dataset.k ? -st.dir : (["video", "lyrics", "fav", "year"].includes(h.dataset.k) ? -1 : 1);
  st.sort = h.dataset.k; applyFilters();
});
$("#libSearch").addEventListener("input", (e) => { st.q = e.target.value; clearTimeout(applyFilters.t); applyFilters.t = setTimeout(applyFilters, 120); });
$("#libFilters").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  st.filter = b.dataset.f; $$("#libFilters button").forEach((x) => x.classList.toggle("on", x === b)); applyFilters();
});
$("#libGroup").addEventListener("change", (e) => { st.group = e.target.value; applyFilters(); });

function updateSelbar() {
  const n = st.sel.size;
  $("#selbar").classList.toggle("on", n > 0);
  $("#selCount").textContent = `${n} selected`;
  $("#selAll").checked = n > 0 && rows.length > 0 && rows.every((s) => st.sel.has(s.id));
  const anyDisabled = [...st.sel].some((id) => lib.byId.get(id)?.disabled);
  $("#disableBtn").textContent = anyDisabled ? "Enable" : "Disable";
  $("#disableBtn").dataset.act = anyDisabled ? "enable" : "disable";
  const allFav = n > 0 && [...st.sel].every((id) => lib.byId.get(id)?.fav);
  $("#favBtn").textContent = allFav ? "☆ Remove from favourites" : "★ Add to favourites";
}

/* run batch */
$("#runBtn").addEventListener("click", (e) => {
  const o = $("#runOpts");
  if (!o.classList.contains("on")) { o.classList.add("on"); e.target.textContent = `Start ${st.sel.size}`; return; }
  startRun(); o.classList.remove("on"); e.target.textContent = "Run";
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#runOpts") && !e.target.closest("#runBtn")) { $("#runOpts").classList.remove("on"); $("#runBtn").textContent = "Run"; }
});
async function startRun(ids = [...st.sel]) {
  const video = $("#optVideo").checked, lyrics = $("#optLyrics").checked, meta = $("#optMeta").checked, force = $("#optForce").checked;
  if (!video && !lyrics && !meta) return toast("Pick at least one thing to get");
  const kind = (video || lyrics) && meta ? "full" : meta ? "metadata" : "media";
  const r = await api("/api/jobs", { ids, kind, options: { video, lyrics, force } });
  toast(`Queued ${r.queued} song${r.queued === 1 ? "" : "s"} — see Jobs`);
  pollJobs(true);
}
$("#selbar").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const ids = [...st.sel], act = b.dataset.act;
  try {
    if (act === "clear") { st.sel.clear(); renderTable(false); updateSelbar(); return; }
    if (act === "favorite") return setFav(ids, !ids.every((i) => lib.byId.get(i)?.fav));
    if (act === "setlist") return addToSetlist(ids);
    if (act === "pack") {
      const r = await ask({ title: "Export chart pack", text: `${ids.length} songs into one .zip (saved in the “Chart Packs” folder).`, input: "My Chart Pack", ok: "Export" });
      if (!r) return; toast("Packing…"); const d = await api("/api/songs/action", { ids, action: "pack", name: r.input }); return toast("Saved " + d.path, 5000);
    }
    if (act === "rename") {
      const r = await ask({ title: "Rename song folders", text: "Use {artist}, {name}, {charter}, {album}, {year}.", input: "{artist} - {name}", ok: "Rename" });
      if (!r) return; const d = await api("/api/songs/action", { ids, action: "rename", template: r.input }); return toast(`Renamed ${d.count}`);
    }
    if (act === "restore") {
      if (!await ask({ title: "Undo changes?", text: `Removes the videos and lyrics this app added to ${ids.length} songs and restores their original files.`, ok: "Undo changes", danger: true })) return;
      const d = await api("/api/songs/action", { ids, action: "restore" }); return toast(`Restored ${d.count} songs`);
    }
    if (act === "disable" || act === "enable") {
      const d = await api("/api/songs/action", { ids, action: act });
      toast(act === "disable" ? `Disabled ${d.count} — moved to “Disabled Songs”, so Clone Hero skips them` : `Enabled ${d.count}`);
      return;
    }
    if (act === "delete") {
      if (!await ask({ title: `Move ${ids.length} song${ids.length === 1 ? "" : "s"} to the Trash?`, text: "You can restore them from the Trash.", ok: "Move to Trash", danger: true })) return;
      const d = await api("/api/songs/action", { ids, action: "delete" }); st.sel.clear(); toast(`Moved ${d.count} to the Trash`);
    }
  } catch (err) { toast(err.message, 4500); }
});

/* ================================================================ favourites */
function updateFavCount() {
  const n = lib.songs.filter((s) => s.fav).length;
  $("#navFavCount").textContent = n || "";
}
async function setFav(ids, value) {
  ids = ids.filter((i) => lib.byId.has(i));
  if (!ids.length) return;
  await api("/api/songs/action", { ids, action: "favorite", value });
  const keys = new Set(ids.map((i) => lib.byId.get(i).key));   // duplicate charts of a song share the star
  lib.songs.forEach((x) => { if (keys.has(x.key)) x.fav = value; });
  updateFavCount(); renderTable(true); updateSelbar();
  if (detail && ids.includes(detail.id)) { detail.fav = value; paintDetailFav(); }
  toast(value ? `Added ${ids.length === 1 ? "to" : ids.length + " songs to"} favourites ★` : `Removed ${ids.length === 1 ? "from" : ids.length + " songs from"} favourites`);
}
function paintDetailFav() {
  const b = $("#dFav"); if (!b || !detail) return;
  const on = lib.byId.get(detail.id)?.fav;
  b.textContent = on ? "★ Favourite" : "☆ Add to favourites"; b.classList.toggle("fav-on", !!on);
}

/* ================================================================ detail */
async function openDetail(id) {
  activeId = id; $("#detail").hidden = false; $("#shell").classList.add("detail-open"); renderTable(false);
  $("#detailBody").innerHTML = `<div class="d-art"></div><div class="d-head"><h3>…</h3></div>`;
  await refreshDetail();
}
$("#detailClose").onclick = () => { activeId = null; $("#detail").hidden = true; $("#shell").classList.remove("detail-open"); stopPreview(); renderTable(false); };
let detail = null;
async function refreshDetail() {
  if (!activeId) return;
  let d;
  try { d = await api(`/api/song/${activeId}`); } catch (e) { return; }
  if (d.id !== activeId) return;
  const keepPreview = detail && detail.id === d.id && previewing;
  detail = d;
  if (keepPreview) return updateDetailStatus();
  const [vid, ly] = pills(d);
  const meta = [d.album, d.year, d.genre].filter(Boolean).join(" · ");
  const vOff = Number(d.videoOffset || 0), lOff = Number(d.lyricsOffset || 0);
  $("#detailBody").innerHTML = `
    <div class="d-art"${d.art ? ` style="background-image:url('/media/art/${d.id}?v=${lib.version}')"` : ""}></div>
    <div class="d-head"><h3>${esc(d.name)}</h3><p>${esc(d.artist)}</p></div>
    <div class="d-sec">
      <div class="d-row">${vid}${ly}${d.sng ? '<span class="pill">.sng</span>' : ""}${d.disabled ? '<span class="pill low">disabled</span>' : ""}</div>
      <div class="kv"><span>Release</span><span>${esc(meta) || "—"}</span><span>Charter</span><span>${esc(d.charter) || "—"}</span>
        <span>Length</span><span>${fmtLen(d.length) || "—"}</span><span>Folder</span><span title="${esc(d.path)}">${esc(d.rel)}</span></div>
      <div class="status-line" id="dStatus">${esc(d.status || "")}</div>
      <div class="d-row">
        <button id="dFav" class="favbtn"></button>
        <button class="primary" id="dRun">Get video, lyrics &amp; metadata</button>
        <button id="dMeta">Metadata only</button>
        <button id="dPlay">Play song</button>
        <button id="dReveal">Show in folder</button>
      </div>
      <div class="d-row"><span class="hint">Rating</span>${[1, 2, 3, 4, 5].map((n) => `<span class="fav${(d.rating || 0) >= n ? " on" : ""}" data-r="${n}">★</span>`).join("")}</div>
    </div>
    <div class="d-sec">
      <h4>Background video and sync</h4>
      <div class="preview"><video id="dVideo" muted playsinline preload="metadata"${d.hasVideo ? ` src="/media/video/${d.id}?v=${lib.version}"` : ""}></video>${d.hasVideo ? "" : '<div class="none">No video yet</div>'}</div>
      <audio id="dAudio" preload="none"></audio>
      <div class="slider"><span>Position</span><input type="range" id="dPos" min="0" max="${Math.round(d.length || 300)}" step="1" value="0"><b id="dPosV">0:00</b></div>
      <div class="slider"><span>Video</span><input type="range" id="dVOff" min="-15" max="90" step="0.05" value="${vOff}"><b id="dVOffV">${vOff.toFixed(2)}s</b></div>
      <div class="slider"><span>Lyrics</span><input type="range" id="dLOff" min="-10" max="10" step="0.05" value="${lOff}"><b id="dLOffV">${lOff.toFixed(2)}s</b></div>
      <div class="d-row"><button id="dPrev">Preview</button><button class="primary" id="dApply">Save sync to game</button></div>
      <p class="hint">Video: seconds into the music video at the song's first second. Positive skips an intro.</p>
    </div>
    <div class="d-sec">
      <h4>Pick a different video (HD only)</h4>
      <div class="d-row"><input type="url" id="dUrl" placeholder="Paste a YouTube link" style="flex:1"><button id="dUse">Use</button></div>
      <button id="dFind">Show YouTube matches</button>
      <div class="cands" id="dCands"></div>
    </div>
    <div class="d-sec">
      <div class="d-row">
        <button id="dSkip">${d.skip ? "Allow auto-download" : "Never auto-download for this song"}</button>
        <button id="dUndo" class="danger">Undo changes</button>
      </div>
    </div>`;
  bindDetail(d);
}
function updateDetailStatus() { const s = $("#dStatus"); if (s && detail) s.textContent = detail.status || ""; }
function bindDetail(d) {
  $("#dRun").onclick = () => startRunOne(d.id, "full");
  paintDetailFav();
  $("#dFav").onclick = () => setFav([d.id], !lib.byId.get(d.id)?.fav);
  $("#dMeta").onclick = () => startRunOne(d.id, "metadata");
  $("#dPlay").onclick = () => playSong(d.id);
  $("#dReveal").onclick = () => api("/api/songs/action", { ids: [d.id], action: "reveal" });
  $("#dUndo").onclick = async () => { await api("/api/songs/action", { ids: [d.id], action: "restore" }); toast("Restored the original files"); };
  $("#dSkip").onclick = async () => { await api("/api/songs/action", { ids: [d.id], action: "skip", value: !d.skip }); d.skip = !d.skip; $("#dSkip").textContent = d.skip ? "Allow auto-download" : "Never auto-download for this song"; };
  $$("#detailBody .fav[data-r]").forEach((el) => el.onclick = async () => {
    const r = Number(el.dataset.r) === d.rating ? 0 : Number(el.dataset.r);
    await api("/api/songs/action", { ids: [d.id], action: "rate", value: r }); d.rating = r;
    $$("#detailBody .fav[data-r]").forEach((x) => x.classList.toggle("on", Number(x.dataset.r) <= r));
  });
  const vOff = $("#dVOff"), lOff = $("#dLOff"), pos = $("#dPos");
  vOff.oninput = () => { $("#dVOffV").textContent = Number(vOff.value).toFixed(2) + "s"; syncPreview(true); };
  lOff.oninput = () => { $("#dLOffV").textContent = Number(lOff.value).toFixed(2) + "s"; };
  pos.oninput = () => { $("#dPosV").textContent = fmtLen(Number(pos.value)) || "0:00"; };
  pos.onchange = () => { if (previewing) startPreview(Number(pos.value)); };
  $("#dPrev").onclick = () => previewing ? stopPreview() : startPreview(Number(pos.value));
  $("#dApply").onclick = async () => {
    await api(`/api/song/${d.id}/offsets`, { video: Number(vOff.value), lyrics: Number(lOff.value), apply: true });
    toast("Saved. The song is rewritten with this sync as soon as it isn't playing.", 4000);
  };
  $("#dUse").onclick = async () => {
    const v = $("#dUrl").value.trim(); if (!v) return;
    try { const r = await api(`/api/song/${d.id}/use-video`, { video: v }); toast(`Using that video (${r.res}p) — downloading`); pollJobs(true); }
    catch (e) { toast(e.message, 4500); }
  };
  $("#dFind").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Searching YouTube…";
    try {
      const r = await api(`/api/song/${d.id}/candidates`, {});
      $("#dCands").innerHTML = r.candidates.map((c) => `<div class="cand${c.id === d.videoId ? " current" : ""}">
          <a href="https://youtu.be/${c.id}" target="_blank" title="${esc(c.title)}">${esc(c.title)}</a>
          <span class="pill ${c.res >= MIN_P ? "ok" : "low"}">${c.res ? c.res + "p" : "no HD"}</span>
          <button data-v="${c.id}" ${c.res >= MIN_P ? "" : "disabled"}>Use</button></div>`).join("") || '<p class="hint">Nothing found.</p>';
      $$("#dCands button").forEach((b) => b.onclick = async () => {
        try { await api(`/api/song/${d.id}/use-video`, { video: b.dataset.v }); toast("Downloading that video"); pollJobs(true); } catch (err) { toast(err.message, 4500); }
      });
    } catch (err) { toast(err.message, 4500); }
    e.target.disabled = false; e.target.textContent = "Show YouTube matches";
  };
}
async function startRunOne(id, kind) {
  await api("/api/jobs", { ids: [id], kind, options: { video: true, lyrics: true, force: kind !== "metadata" && !!detail?.hasVideo && videoState(detail) === "low" } });
  toast("Queued — progress shows here and in Jobs"); pollJobs(true);
}

/* preview: audio from the chart, video offset applied live */
let previewing = false, previewStart = 0;
function startPreview(t) {
  const a = $("#dAudio"); if (!a || !detail) return;
  previewing = true; previewStart = t;
  a.src = `/media/audio/${detail.id}?t=${t}`; a.play().catch(() => {});
  $("#dPrev").textContent = "Stop";
  const v = $("#dVideo"); if (v && v.src) { syncPreview(true); v.play().catch(() => {}); }
  clearInterval(startPreview.iv); startPreview.iv = setInterval(() => syncPreview(false), 250);
}
function stopPreview() {
  previewing = false; clearInterval(startPreview.iv);
  const a = $("#dAudio"), v = $("#dVideo"); if (a) { a.pause(); a.removeAttribute("src"); } if (v) v.pause();
  if ($("#dPrev")) $("#dPrev").textContent = "Preview";
}
function syncPreview(hard) {
  const a = $("#dAudio"), v = $("#dVideo"); if (!a || !v || !detail) return;
  const songT = previewStart + (a.currentTime || 0);
  const pos = $("#dPos"); if (pos && !hard) { pos.value = Math.floor(songT); $("#dPosV").textContent = fmtLen(songT) || "0:00"; }
  if (!v.src) return;
  // the file already contains the offset that was written to the game; apply only the change
  const target = songT + (detail.videoStart || 0) + (Number($("#dVOff").value) - (detail.videoOffsetWritten || 0));
  if (target < 0) { v.pause(); v.currentTime = 0; return; }
  if (Math.abs(v.currentTime - target) > (hard ? 0.05 : 0.35)) v.currentTime = target;
  if (v.paused && !a.paused) v.play().catch(() => {});
}

/* mini player */
function playSong(id) {
  const s = lib.byId.get(id); if (!s) return;
  const a = $("#plAudio"); a.src = `/media/audio/${id}?t=0`; a.play().catch(() => {});
  $("#player").hidden = false; $("#plTitle").textContent = `${s.artist} — ${s.name}`; $("#plToggle").textContent = "Pause";
}
$("#plToggle").onclick = () => { const a = $("#plAudio"); if (a.paused) { a.play(); $("#plToggle").textContent = "Pause"; } else { a.pause(); $("#plToggle").textContent = "Play"; } };

/* ================================================================ now playing window */
async function openNowWindow(fullscreen) {
  try {
    const r = await api("/api/window", { page: "now", fullscreen });
    toast(r.kind === "window" ? "Opened. Drag it to your second screen (fullscreen: ⌃⌘F on Mac, F11 on Windows)."
      : "Opened in a separate window", 5000);
    $("#nowFrame").src = "about:blank";   // don't run two copies of the video
  } catch (e) { toast(e.message, 4000); }
}
$("#nowWin").onclick = () => openNowWindow(false);
$("#nowFull").onclick = () => openNowWindow(true);

/* ================================================================ setlists */
let setlists = [], slActive = null;
async function loadSetlists() {
  try { setlists = (await api("/api/setlists")).setlists; } catch (e) { return toast(e.message); }
  $("#slList").innerHTML = setlists.map((s) => `<li data-n="${esc(s.name)}" class="${s.name === slActive ? "on" : ""}"><span>${esc(s.name)}</span><span>${s.songs.length}</span></li>`).join("") || '<li class="hint">No setlists yet</li>';
  renderSetlist();
}
function renderSetlist() {
  const sl = setlists.find((s) => s.name === slActive);
  if (!sl) { $("#slBody").innerHTML = '<p class="hint">Pick a setlist.</p>'; return; }
  $("#slBody").innerHTML = `<div class="pane-head"><h2>${esc(sl.name)}</h2><button class="danger" id="slDel">Delete setlist</button></div>` +
    sl.songs.map((x, i) => {
      const s = x.id && lib.byId.get(x.id);
      return `<div class="line"><span class="hint">${i + 1}</span><div class="art"${s && s.art ? ` style="background-image:url('/media/art/${s.id}')"` : ""}></div>
        <div><b>${s ? esc(s.name) : "Song not in library"}</b><span>${s ? esc(s.artist) : x.hash}</span></div>
        <span class="pill">${x.speed}%</span>${s ? `<button data-rm="${s.id}">Remove</button>` : "<span></span>"}</div>`;
    }).join("");
  $("#slDel").onclick = async () => {
    if (!await ask({ title: `Delete “${sl.name}”?`, ok: "Delete", danger: true })) return;
    await api("/api/setlists", { action: "delete", name: sl.name }); slActive = null; loadSetlists();
  };
  $$("#slBody [data-rm]").forEach((b) => b.onclick = async () => { await api("/api/setlists", { action: "remove", name: sl.name, ids: [b.dataset.rm] }); loadSetlists(); });
}
$("#slList").addEventListener("click", (e) => { const li = e.target.closest("li[data-n]"); if (li) { slActive = li.dataset.n; loadSetlists(); } });
$("#slNew").onclick = async () => {
  const r = await ask({ title: "New setlist", input: "", ok: "Create" }); if (!r || !r.input) return;
  try { await api("/api/setlists", { action: "create", name: r.input }); slActive = r.input; loadSetlists(); } catch (e) { toast(e.message); }
};
async function addToSetlist(ids) {
  try { setlists = (await api("/api/setlists")).setlists; } catch (e) { /* ignore */ }
  const r = await ask({ title: `Add ${ids.length} song${ids.length === 1 ? "" : "s"} to a setlist`, text: "Pick one, or type a new name.",
    select: [{ value: "", label: "New setlist…" }, ...setlists.map((s) => ({ value: s.name, label: s.name }))], input: "", ok: "Add" });
  if (!r) return;
  const name = r.input?.trim() || r.select; if (!name) return toast("Give the setlist a name");
  const d = await api("/api/setlists", { action: "add", name, ids });
  toast(`Added ${d.added} to “${name}”. Clone Hero shows setlist changes after a restart.`, 4500);
}

/* ================================================================ duplicates */
async function loadDupes() {
  $("#dupList").innerHTML = '<p class="hint">Looking for duplicates…</p>';
  const d = await api("/api/duplicates");
  $("#dupList").innerHTML = d.groups.map((g) => `<div class="dupe"><h3>${esc(g[0].artist)} — ${esc(g[0].name)}</h3>` + g.map((s) => {
    const [vid, ly] = pills(s);
    return `<div class="copy"><span title="${esc(s.path)}">${esc(s.rel)}<br><small>${esc(s.charter || "unknown charter")}${s.sng ? " · .sng" : ""}</small></span>${vid}${ly}${instHtml(s.diffs)}
      <span><button data-keep="${s.id}">Keep only this</button></span></div>`;
  }).join("") + "</div>").join("") || '<p class="hint">No duplicates in your library.</p>';
  $$("#dupList [data-keep]").forEach((b) => b.onclick = async () => {
    const group = d.groups.find((g) => g.some((s) => s.id === b.dataset.keep));
    const others = group.filter((s) => s.id !== b.dataset.keep).map((s) => s.id);
    if (!await ask({ title: `Move ${others.length} other cop${others.length === 1 ? "y" : "ies"} to the Trash?`, ok: "Move to Trash", danger: true })) return;
    await api("/api/songs/action", { ids: others, action: "delete" }); toast("Done"); loadDupes();
  });
}
$("#dupRefresh").onclick = loadDupes;

/* ================================================================ get charts */
const charts = { db: "encore", page: 1, total: 0, items: [], sel: new Map(), loaded: false };
async function searchCharts(page) {
  charts.loaded = true; charts.page = page;
  const params = new URLSearchParams({ db: charts.db, q: $("#chartSearch").value, page, per: 48,
    instrument: $("#chartInst").value, difficulty: $("#chartDiff").value, sort: $("#chartSort").value,
    dir: $("#chartSort").value === "newest" ? "desc" : "asc" });
  $("#chartResults").innerHTML = '<p class="hint">Searching…</p>';
  try {
    const d = await api("/api/search?" + params);
    charts.items = d.songs; charts.total = d.total;
    renderCharts();
  } catch (e) { $("#chartResults").innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
}
function renderCharts() {
  $("#chartResults").innerHTML = charts.items.map((c, i) => `<div class="card${charts.sel.has(c.id) ? " sel" : ""}" data-i="${i}">
      <input type="checkbox" class="chk"${charts.sel.has(c.id) ? " checked" : ""}>
      ${c.owned ? '<span class="pill ok owned">In library</span>' : ""}
      <div class="art"${c.artUrl ? ` style="background-image:url('${esc(c.artUrl)}')"` : ""}></div>
      ${c.source === "encore" && c.downloadUrl ? '<button class="pv" title="Listen before downloading">▶ Preview</button>' : ""}
      <div class="body"><b>${esc(c.name)}</b><span>${esc(c.artist)}</span><span>${esc(c.charter)}${c.length ? " · " + fmtLen(c.length) : ""}</span>
      <div class="tags">${instHtml(c.difficulties || {})}${c.hasVideo ? '<span class="pill ok">video</span>' : ""}${c.needsConversion ? '<span class="pill low">Rock Band format</span>' : ""}${!c.downloadUrl && !c.needsConversion ? '<span class="pill">manual download</span>' : ""}</div></div></div>`).join("")
    || '<p class="hint">No charts found.</p>';
  const pages = Math.max(1, Math.ceil(charts.total / 48));
  $("#chartPager").innerHTML = `<button ${charts.page <= 1 ? "disabled" : ""} id="pgPrev">Previous</button> Page ${charts.page} of ${pages} · ${charts.total} charts <button ${charts.page >= pages ? "disabled" : ""} id="pgNext">Next</button>`;
  $("#pgPrev").onclick = () => searchCharts(charts.page - 1); $("#pgNext").onclick = () => searchCharts(charts.page + 1);
  updateChartSel();
}
$("#chartResults").addEventListener("click", (e) => {
  const card = e.target.closest(".card"); if (!card) return;
  const c = charts.items[Number(card.dataset.i)];
  if (e.target.closest(".pv")) {
    const a = $("#plAudio");
    a.src = `/media/chart-preview?url=${encodeURIComponent(c.downloadUrl)}&t=${c.previewStart ?? Math.round((c.length || 120) * 0.35)}`;
    a.play().catch(() => {});
    $("#player").hidden = false; $("#plTitle").textContent = `Preview: ${c.artist} — ${c.name}`; $("#plToggle").textContent = "Pause";
    return toast("Loading preview…", 1500);
  }
  if (!c.downloadUrl) { if (c.pageUrl) window.open(c.pageUrl, "_blank"); return toast(c.needsConversion ? "Rock Band charts need conversion, which isn't supported here" : "This one has to be downloaded from its page", 3500); }
  charts.sel.has(c.id) ? charts.sel.delete(c.id) : charts.sel.set(c.id, c);
  card.classList.toggle("sel"); card.querySelector(".chk").checked = charts.sel.has(c.id); updateChartSel();
});
function updateChartSel() { $("#chartSel").classList.toggle("on", charts.sel.size > 0); $("#chartSelCount").textContent = `${charts.sel.size} selected`; }
$("#chartDl").onclick = async () => {
  const r = await api("/api/download", { items: [...charts.sel.values()], process: $("#chartProcess").checked });
  toast(`Downloading ${r.queued} chart${r.queued === 1 ? "" : "s"} — see Jobs`); charts.sel.clear(); renderCharts(); pollJobs(true);
};
$("#chartClear").onclick = () => { charts.sel.clear(); renderCharts(); };
$("#chartDb").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; charts.db = b.dataset.db; $$("#chartDb button").forEach((x) => x.classList.toggle("on", x === b)); searchCharts(1); });
$("#chartSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") searchCharts(1); });
["#chartInst", "#chartDiff", "#chartSort"].forEach((s) => $(s).addEventListener("change", () => searchCharts(1)));

/* ================================================================ spotify */
let spTracks = [];
$("#spGo").onclick = async () => {
  const url = $("#spUrl").value.trim(); if (!url) return;
  $("#spResults").innerHTML = '<p class="hint">Reading the playlist and matching charts…</p>';
  try {
    const d = await api("/api/spotify", { url });
    spTracks = d.tracks.map((t) => ({ ...t, pick: !!(t.match && !t.owned && t.match.downloadUrl) }));
    renderSpotify(d);
  } catch (e) { $("#spResults").innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
};
function renderSpotify(d) {
  const found = spTracks.filter((t) => t.match).length, owned = spTracks.filter((t) => t.owned).length;
  $("#spResults").innerHTML = `<p class="hint"><b>${esc(d.name)}</b> — ${spTracks.length} tracks: ${owned} in your library, ${found} found on Chorus Encore${d.truncated ? " (Spotify shows the first 100 tracks)" : ""}.</p>` +
    spTracks.map((t, i) => `<div class="line"><input type="checkbox" data-i="${i}" ${t.pick ? "checked" : ""} ${t.match && !t.owned ? "" : "disabled"}>
      <div class="art"${t.match && t.match.artUrl ? ` style="background-image:url('${esc(t.match.artUrl)}')"` : ""}></div>
      <div><b>${esc(t.title)}</b><span>${esc(t.artists)}</span></div>
      <span>${t.owned ? '<span class="pill ok">In library</span>' : t.match ? `<span class="pill mid">${esc(t.match.charter || "chart found")}</span>` : '<span class="pill">no chart</span>'}</span><span></span></div>`).join("");
  $$("#spResults input[data-i]").forEach((c) => c.onchange = () => { spTracks[c.dataset.i].pick = c.checked; updSp(); });
  updSp();
}
function updSp() { const n = spTracks.filter((t) => t.pick).length; $("#spSel").classList.toggle("on", n > 0); $("#spSelCount").textContent = `${n} to download`; }
$("#spDl").onclick = async () => {
  const items = spTracks.filter((t) => t.pick).map((t) => t.match);
  const r = await api("/api/download", { items, process: $("#spProcess").checked });
  toast(`Downloading ${r.queued} charts — see Jobs`); pollJobs(true);
};

/* ================================================================ jobs */
let jobsSnap = null;
async function pollJobs(once) {
  try {
    jobsSnap = await api("/api/jobs");
    renderDock(); if (currentView === "jobs") renderJobs();
    if (activeId && detail) {
      const j = jobsSnap.jobs.find((x) => x.songId === activeId);
      if (j) { const s = j.status === "done" ? j.result : j.status === "failed" ? "Failed: " + j.error : (j.step || j.status); if (detail.status !== s) { detail.status = s; updateDetailStatus(); } }
    }
    $("#navNow").classList.toggle("on", jobsSnap.now && ["playing", "paused"].includes(jobsSnap.now.status));
  } catch (e) { /* ignore */ }
  if (!once) setTimeout(pollJobs, 1500);
}
function renderDock() {
  const c = jobsSnap.counts || {};
  const active = (c.running || 0) + (c.queued || 0) + (c.waiting || 0);
  const total = active + (c.done || 0) + (c.failed || 0) + (c.cancelled || 0);
  $("#navJobs").textContent = active || "";
  const running = jobsSnap.jobs.filter((j) => j.status === "running").slice(0, 2);
  const pct = total ? Math.round(100 * (total - active) / total) : 0;
  $("#dockJobs").innerHTML = active ? `<div class="meter"><i style="width:${pct}%"></i></div>
     <span>${total - active}/${total} done${c.failed ? ` · ${c.failed} failed` : ""}</span>
     <span>${running.map((j) => `${esc(j.title)}: ${esc(j.step)}`).join(" · ")}</span>
     ${jobsSnap.youtube?.pausedFor ? `<span class="pill low">YouTube pause ${jobsSnap.youtube.pausedFor}s</span>` : ""}`
    : (total ? `<span>All jobs finished${c.failed ? ` (${c.failed} failed)` : ""}</span>` : "<span>Ready</span>");
}
function renderJobs() {
  if (!jobsSnap) return;
  const c = jobsSnap.counts || {};
  $("#jobCounts").textContent = ["running", "queued", "waiting", "done", "failed"].filter((k) => c[k]).map((k) => `${c[k]} ${k}`).join(" · ") +
    (jobsSnap.avgSeconds ? ` · ~${jobsSnap.avgSeconds}s per song` : "");
  const w = $("#jobWorkers"); if (document.activeElement !== w) { w.value = jobsSnap.workers; $("#jobWorkersVal").textContent = jobsSnap.workers; }
  const yt = jobsSnap.youtube || {};
  $("#ytWarn").hidden = !yt.pausedFor;
  $("#ytWarn").textContent = `YouTube is rate-limiting. YouTube requests resume in ${yt.pausedFor}s; nothing is lost.`;
  $("#jobList").innerHTML = jobsSnap.jobs.map((j) => `<div class="job"><span class="st ${j.status}">${j.status}</span>
      <span>${esc(j.title)}</span><span class="msg" title="${esc(j.error || j.result || j.step)}">${esc(j.error || j.result || j.step)}</span>
      <span>${["queued", "running", "waiting"].includes(j.status) ? `<button data-cancel="${j.id}">Cancel</button>` : ""}</span></div>`).join("") || '<p class="hint">Nothing running. Select songs in the Library and press Run.</p>';
}
$("#jobList").addEventListener("click", async (e) => { const b = e.target.closest("[data-cancel]"); if (b) { await api("/api/jobs/cancel", { id: b.dataset.cancel }); pollJobs(true); } });
$("#jobRetry").onclick = async () => { await api("/api/jobs/retry", {}); pollJobs(true); };
$("#jobClear").onclick = async () => { await api("/api/jobs/clear", {}); pollJobs(true); };
$("#jobCancel").onclick = async () => { await api("/api/jobs/cancel", {}); pollJobs(true); };
$("#jobWorkers").addEventListener("input", (e) => { $("#jobWorkersVal").textContent = e.target.value; });
$("#jobWorkers").addEventListener("change", async (e) => { await api("/api/settings", { settings: { workers: Number(e.target.value) } }); toast(`${e.target.value} workers`); });

/* ================================================================ settings */
async function loadSettings() {
  const d = await api("/api/settings"); const s = d.settings;
  const chk = (k, label, help) => `<div class="row2"><span>${label}</span><label class="inline"><input type="checkbox" data-k="${k}" ${s[k] ? "checked" : ""}> ${help}</label></div>`;
  $("#settingsForm").innerHTML = `
    <section><h3>Videos</h3>
      <div class="row2"><span>Preferred quality</span><select data-k="pref_p"><option value="720"${s.pref_p == 720 ? " selected" : ""}>720p</option><option value="1080"${s.pref_p == 1080 ? " selected" : ""}>1080p</option></select>
        <small>Videos below ${d.minP}p are never downloaded or accepted. Widescreen videos count by their YouTube quality (1280×526 is 720p).</small></div>
      ${chk("replace_low_res", "Low-resolution videos", "Replace existing videos below 640p when a song is processed")}
    </section>
    <section><h3>In the game</h3>
      ${chk("in_game", "Add videos and lyrics", "Put videos and lyrics into your songs so Clone Hero shows them")}
      ${chk("prefetch", "Prepare while browsing", "Start on a song as soon as it's highlighted in Clone Hero's song list")}
      ${chk("game_lyrics", "Lyrics", "Write lyrics into charts that have none (gives that chart a fresh high-score list)")}
    </section>
    <section><h3>Processing</h3>
      <div class="row2"><span>Workers</span><select data-k="workers">${[0, 1, 2, 3, 4, 6, 8].map((n) => `<option value="${n}"${Number(s.workers) === n ? " selected" : ""}>${n ? n : "Automatic"}</option>`).join("")}</select>
        <small>Songs processed at the same time. YouTube requests are paced across all workers.</small></div>
      ${chk("metadata_overwrite", "Metadata", "Overwrite album, year and genre that are already filled in")}
      ${chk("fav_setlist", "Favourites in the game", "Keep a “Favourites” setlist in Clone Hero with your favourite songs (Clone Hero loads setlists at start-up)")}
      ${chk("auto_process_downloads", "New charts", "Add video, lyrics and metadata to charts right after downloading")}
      <div class="row2"><span>Download into</span><input type="text" data-k="download_subdir" value="${esc(s.download_subdir)}" placeholder="Songs folder (or a sub-folder name, e.g. Downloads)"></div>
    </section>
    <section><h3>Folders</h3>
      <div class="row2"><span>Clone Hero</span><span>${esc(d.game || "Not found")}</span></div>
      <div class="row2"><span>Data folder</span><span>${esc(d.dataDir)}</span></div>
      <div class="row2"><span>Song folders</span><span>${d.songDirs.map(esc).join("<br>") || "None"}</span></div>
      <div class="row2"><span></span><span><button id="setChange" class="primary">Change game &amp; folders…</button> <button id="setOpen">Open data folder</button> <button id="setRescan">Rescan library</button></span></div>
    </section>
    <section><h3>About</h3>
      <div class="row2"><span>Version</span><span>${esc(d.version)} · yt-dlp ${esc(d.ytdlp)} (updates itself) · ffmpeg ${d.ffmpeg ? "included" : "missing"}</span></div>
    </section>`;
  $$("#settingsForm [data-k]").forEach((el) => el.addEventListener("change", async () => {
    const k = el.dataset.k; let v = el.type === "checkbox" ? el.checked : el.value;
    if (["pref_p", "workers"].includes(k)) v = Number(v);
    try { await api("/api/settings", { settings: { [k]: v } }); toast("Saved"); } catch (e) { toast(e.message); }
  }));
  $("#setChange").onclick = () => show("setup");
  api("/api/macperm").then((m) => {
    if (!m.applicable) return;
    const row = document.createElement("div"); row.className = "row2";
    row.innerHTML = `<span>Full Disk Access</span><span>${m.fda ? "✓ On" : `Off: macOS asks folder by folder. <button id="setFda">Grant Full Disk Access…</button>`}</span>`;
    $("#setChange").closest(".row2").before(row);
    const btn = $("#setFda"); if (btn) btn.onclick = () => show("setup");
  }).catch(() => {});
  $("#setOpen").onclick = () => api("/api/open", {});
  $("#setRescan").onclick = async () => { await api("/api/library/rescan", {}); toast("Rescanning…"); };
}

/* ================================================================ setup (game & folders) */
const setup = { songs: [], detected: null };
const PLAT_HINT = {
  darwin: { game: "Usually /Applications/Clone Hero.app, but it can be anywhere.",
            data: "Usually the “Clone Hero” folder in your home folder (~/Clone Hero) or in Documents." },
  win32: { game: "The folder you unzipped Clone Hero into, e.g. C:\\Games\\Clone Hero\\Clone Hero.exe.",
           data: "Usually Documents\\Clone Hero (it can be inside OneDrive\\Documents)." },
  linux: { game: "The clonehero program in the folder you extracted Clone Hero into.",
           data: "Usually ~/.clonehero (or the folder next to the game for portable installs)." },
};
async function checkPath(kind, path, statusSel) {
  const el = $(statusSel);
  if (!path) { el.textContent = ""; el.className = "status"; return null; }
  const d = await api("/api/setup/check", { kind, path });
  let msg;
  if (kind === "game") msg = d.ok ? "✓ Clone Hero found" : "✗ This is not the Clone Hero game";
  else if (kind === "data") msg = d.ok ? `✓ ${d.settingsIni ? "settings.ini" : ""}${d.settingsIni && d.logs ? " and " : ""}${d.logs ? "Logs" : ""} found`
    : d.exists ? "✗ No settings.ini or Logs here. Start Clone Hero once, or pick the folder it writes to." : "✗ Folder not found";
  el.textContent = msg; el.className = "status " + (d.ok ? "ok" : "bad");
  return d;
}
function renderSongDirs() {
  $("#suSongs").innerHTML = setup.songs.length ? setup.songs.map((s, i) => `<li data-i="${i}"><span class="p">${esc(s.path)}</span>
    <span class="status ${s.ok ? (s.songs ? "ok" : "") : "bad"}">${s.ok ? `${s.songs}${s.songs >= 5000 ? "+" : ""} songs` : "not found"}</span>
    <button class="ghost" data-rm="${i}" title="Remove">×</button></li>`).join("") : `<li class="hint">No song folders yet.</li>`;
  const det = (setup.detected?.songDirs || []).filter((p) => !setup.songs.some((s) => s.path === p));
  $("#suSongsFound").innerHTML = det.length ? `<span class="hint">Found:</span>` + det.map((p) => `<button data-addsong="${esc(p)}">+ ${esc(p)}</button>`).join("") : "";
}
async function addSongDir(p) {
  p = (p || "").trim();
  if (!p || setup.songs.some((s) => s.path === p)) return;
  const d = await api("/api/setup/check", { kind: "songs", path: p });
  setup.songs.push(d); renderSongDirs();
}
function foundChips(sel, list, target) {
  $(sel).innerHTML = list.length > 0 ? `<span class="hint">Found:</span>` + list.map((p) => `<button data-use="${esc(p)}" data-target="${target}">${esc(p)}</button>`).join("") : `<span class="hint">Nothing found automatically. Use Browse.</span>`;
}
async function loadFda() {
  let m; try { m = await api("/api/macperm"); } catch (e) { return; }
  $("#fdaCard").hidden = !m.applicable;
  if (!m.applicable) return;
  $("#fdaStatus").textContent = m.fda ? "✓ Full Disk Access is on" : "Not granted yet: macOS will ask about protected folders one by one.";
  $("#fdaStatus").className = "status " + (m.fda ? "ok" : "");
  $("#fdaOpen").hidden = !!m.fda;
  if (m.fda) { $("#fdaSteps").hidden = true; $("#fdaRestart").hidden = true; }
}
$("#fdaOpen").onclick = async () => {
  await api("/api/macperm/open", {});
  $("#fdaSteps").hidden = false; $("#fdaRestart").hidden = false;
  toast("System Settings opened at Full Disk Access", 4000);
};
$("#fdaRestart").onclick = async () => {
  const r = await api("/api/restart", {});
  toast(r.ok ? "Restarting…" : "Quit and open the app again to finish", 5000);
};
async function loadSetup() {
  loadFda();
  const d = await api("/api/setup");
  setup.detected = d.detected;
  const h = PLAT_HINT[d.detected.platform] || PLAT_HINT.linux;
  $("#suGameHint").textContent = h.game; $("#suDataHint").textContent = h.data;
  $("#suGame").value = d.current.game || ""; $("#suData").value = d.current.dataDir || "";
  foundChips("#suGameFound", d.detected.games, "suGame");
  foundChips("#suDataFound", d.detected.dataDirs, "suData");
  setup.songs = d.check.songs;
  renderSongDirs();
  checkPath("game", $("#suGame").value, "#suGameStatus");
  checkPath("data", $("#suData").value, "#suDataStatus");
  $("#navSetup").classList.toggle("on", !d.setupDone || !d.dataOk);
  return d;
}
$("#setupForm").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.browse) {
    const input = $("#" + b.dataset.target);
    const r = await api("/api/browse", { kind: b.dataset.browse, start: input.value });
    if (r.path === null) { toast("Paste the path into the box (no file picker in this window)", 4000); input.focus(); return; }
    if (!r.path) return;
    input.value = r.path; input.dispatchEvent(new Event("change"));
    if (b.dataset.target === "suSongAdd") { await addSongDir(r.path); input.value = ""; }
  } else if (b.dataset.use) {
    const input = $("#" + b.dataset.target); input.value = b.dataset.use; input.dispatchEvent(new Event("change"));
  } else if (b.dataset.addsong) {
    await addSongDir(b.dataset.addsong);
  } else if (b.dataset.rm !== undefined) {
    setup.songs.splice(Number(b.dataset.rm), 1); renderSongDirs();
  } else if (b.id === "suSongAddBtn") {
    await addSongDir($("#suSongAdd").value); $("#suSongAdd").value = "";
  } else if (b.id === "suSave") {
    const st2 = $("#suSaveStatus"); st2.textContent = "Saving…"; st2.className = "status";
    try {
      const r = await api("/api/setup", { game: $("#suGame").value, dataDir: $("#suData").value,
                                          songDirs: setup.songs.filter((s) => s.ok).map((s) => s.path), createData: true });
      st2.textContent = `✓ Saved. Reading ${r.songDirs.length} song folder${r.songDirs.length === 1 ? "" : "s"}…`; st2.className = "status ok";
      $("#navSetup").classList.remove("on");
      lib.version = -1; setTimeout(() => { loadLibrary(); show("library"); }, 1500);
    } catch (err) { st2.textContent = "✗ " + err.message; st2.className = "status bad"; }
  }
});
$("#suGame").addEventListener("change", async () => {
  const d = await checkPath("game", $("#suGame").value.trim(), "#suGameStatus");
  if (d && d.ok) {   // a portable install: its data and Songs live next to it
    if (d.dataDir && !$("#suData").value) { $("#suData").value = d.dataDir; checkPath("data", d.dataDir, "#suDataStatus"); }
    if (d.songsDir) addSongDir(d.songsDir);
  }
});
$("#suData").addEventListener("change", async () => {
  const d = await checkPath("data", $("#suData").value.trim(), "#suDataStatus");
  if (d && d.songDirs) for (const p of d.songDirs) await addSongDir(p);
});
$("#suSongAdd").addEventListener("keydown", async (e) => { if (e.key === "Enter") { await addSongDir(e.target.value); e.target.value = ""; } });

/* ================================================================ keyboard */
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea")) return;
  if (currentView !== "library") return;
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "a") { e.preventDefault(); rows.forEach((s) => st.sel.add(s.id)); renderTable(false); updateSelbar(); }
  if (e.key === "Escape") { st.sel.clear(); renderTable(false); updateSelbar(); }
  if (e.key.toLowerCase() === "f" && !e.metaKey && !e.ctrlKey && !e.altKey) {
    const ids = st.sel.size ? [...st.sel] : activeId ? [activeId] : [];
    if (ids.length) { e.preventDefault(); setFav(ids, !ids.every((i) => lib.byId.get(i)?.fav)); }
  }
  if (e.key === "/") { e.preventDefault(); $("#libSearch").focus(); }
});

loadLibrary().then(() => { pollLibrary(); pollJobs(); });
// First run (or the data folder vanished): open the setup screen.
api("/api/setup").then((d) => {
  const need = !d.setupDone || !d.dataOk;   // ask once, even when everything was found
  $("#navSetup").classList.toggle("on", !d.setupDone || !d.dataOk);
  if (need) show("setup");
}).catch(() => {});
})();

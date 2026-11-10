/* Clone Hero Companion — library manager UI (no framework, no build step). */
(() => {
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtLen = (s) => (s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}` : "");
const MIN_P = 640;
const tr = (k, v) => I18N.t(k, v);
const nf = (n) => I18N.nf(n);
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");
const dn = (d) => (I18N.has("diff." + d) ? tr("diff." + d) : cap(d));      // difficulty / instrument names from the data
const inn = (i) => (I18N.has("inst." + i) ? tr("inst." + i) : i);

async function api(path, body, method) {
  const opt = body !== undefined ? { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (r.status === 426 && window.CHCUpdate) window.CHCUpdate.refresh();      // an update is required: show the update screen
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}
function toast(msg, ms = 2600, onclick = null) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("on");
  t.onclick = onclick; t.style.cursor = onclick ? "pointer" : ""; t.style.pointerEvents = onclick ? "auto" : "";
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("on"), ms);
}
function ask({ title, text = "", input = null, select = null, ok = null, danger = false }) {
  return new Promise((resolve) => {
    const dlg = $("#dlg"), f = $("#dlgForm");
    f.innerHTML = `<h2>${esc(title)}</h2>${text ? `<p class="hint" style="white-space:pre-line;max-height:45vh;overflow:auto">${esc(text)}</p>` : ""}
      ${select ? `<select id="dlgSel">${select.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("")}</select>` : ""}
      ${input !== null ? `<input type="text" id="dlgIn" value="${esc(input)}" placeholder="${esc(title)}">` : ""}
      <div class="btns"><button value="cancel" class="ghost">${esc(tr("common.cancel"))}</button><button value="ok" class="${danger ? "danger" : "primary"}">${esc(ok || tr("common.ok"))}</button></div>`;
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
  renderCloudFavs();
  updateFavCount();
}
/* Which library filter a sidebar entry implies: "★ Favourites" -> fav, "Library" -> All, anything else keeps it. */
function viewFilter(view, cur) { return view === "favs" ? "fav" : view === "library" ? "all" : cur; }
function syncChips() { $$("#libFilters button").forEach((b) => b.classList.toggle("on", b.dataset.f === st.filter)); }
function show(view) {
  if (view === "favs") { show("library"); setFilter(viewFilter("favs")); $$("#nav button").forEach((b) => b.classList.toggle("on", b.dataset.view === "favs")); return; }
  currentView = view;
  $$("#nav button").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("on", v.id === "view-" + view));
  $("#detail").hidden = !(view === "library" && activeId);
  $("#shell").classList.toggle("detail-open", !$("#detail").hidden);
  if (view === "now" && $("#nowFrame").src === "about:blank") $("#nowFrame").src = "/now";
  if (view === "library") syncChips();
  renderCloudFavs();
  if (view === "setlists") loadSetlists();
  if (view === "dupes") loadDupes();
  if (view === "settings") loadSettings();
  if (view === "setup") loadSetup();
  if (view === "jobs") renderJobs();
  if (view === "charts" && !charts.loaded) searchCharts(1);
  if (view === "profile") { const o = profNext; profNext = null; loadProfile(o || {}); }
  if (view === "friends") { fr.page = null; loadFriends(); }
  if (view === "leaderboards") loadLeaderboards();
}
$("#nav").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.view === "library" && st.filter !== viewFilter("library")) setFilter(viewFilter("library"));
  show(b.dataset.view);
});
$("#launchGame").onclick = async () => { try { await api("/api/game/launch", {}); toast(tr("toast.starting_game")); } catch (e) { toast(e.message, 4000); } };

/* ================================================================ library */
const lib = { songs: [], byId: new Map(), version: -1 };
const st = { filter: "all", q: "", group: "", sort: "artist", dir: 1, sel: new Set(), anchor: null };
let rows = [];
let activeId = null;

let libGen = 0, cloudGen = 0;      // a late response for an older request must not overwrite newer state
async function loadLibrary() {
  const gen = ++libGen;
  const d = await api("/api/library");
  if (gen !== libGen) return;
  lib.songs = d.songs; lib.version = d.version;
  lib.byId = new Map(d.songs.map((s) => [s.id, s]));
  for (const id of [...st.sel]) if (!lib.byId.has(id)) st.sel.delete(id);
  const groups = [...new Set(d.songs.map((s) => s.group).filter(Boolean))].sort();
  const gsel = $("#libGroup"), cur = gsel.value;
  gsel.innerHTML = `<option value="">${esc(tr("lib.all_folders"))}</option>` + groups.map((g) => `<option${g === cur ? " selected" : ""}>${esc(g)}</option>`).join("");
  $("#navLibCount").textContent = d.songs.filter((s) => !s.disabled).length || "";
  updateFavCount();
  $("#sideStatus").textContent = d.enriching ? tr("lib.checking", { done: d.enrich[0], total: d.enrich[1] }) : "";
  applyFilters();
  if (st.filter === "fav" && currentView === "library") loadCloud();     // recovered favourites appear in "not on this machine"
  if (activeId && lib.byId.has(activeId) && currentView === "library") refreshDetail();
}
async function pollLibrary() {
  try {
    const v = await api("/api/library/version");
    if (v.version !== lib.version) await loadLibrary();
    else $("#sideStatus").textContent = v.enriching ? tr("lib.checking", { done: v.enrich[0], total: v.enrich[1] }) : "";
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
  empty.textContent = st.filter === "fav" && !lib.songs.some((s) => s.fav) ? tr("lib.empty.nofav")
    : lib.songs.length ? tr("lib.empty.nomatch") : tr("lib.empty.nosongs");
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
  const vid = v === "ok" ? `<span class="pill ok">${s.video ? s.video + "p" : esc(tr("pill.video"))}</span>`
    : v === "low" ? `<span class="pill low" title="${esc(tr("pill.low_tip", { p: MIN_P }))}">${s.video}p</span>`
    : v === "failed" ? `<span class="pill" title="${esc(tr("pill.none_found_tip"))}">${esc(tr("pill.none_found"))}</span>` : `<span class="pill">—</span>`;
  const ly = s.lyrics === "chart" ? `<span class="pill ok">${esc(tr("pill.lyrics_chart"))}</span>` : s.lyrics === "added" ? `<span class="pill ok">${esc(tr("pill.lyrics_added"))}</span>`
    : s.lyrics === "none" ? `<span class="pill">—</span>` : `<span class="pill">…</span>`;
  return [vid, ly];
}
function levelsHtml(lv) {
  // which difficulty levels a downloadable chart has: for the chosen instrument, else any instrument
  if (!lv || !Object.keys(lv).length) return "";
  const inst = $("#chartInst").value;
  const have = new Set(inst ? (lv[inst] || []) : Object.values(lv).flat());
  const want = $("#chartDiff").value;
  const L = [["easy", tr("diff.letter.easy")], ["medium", tr("diff.letter.medium")], ["hard", tr("diff.letter.hard")], ["expert", tr("diff.letter.expert")]];
  const list = L.filter(([k]) => have.has(k)).map(([k]) => dn(k)).join(", ") || tr("common.none");
  return `<div class="lv" title="${esc(inst ? tr("chart.levels_tip_inst", { inst: inn(inst), list }) : tr("chart.levels_tip", { list }))}">`
    + L.map(([k, l]) => `<i class="${have.has(k) ? "on" : ""}${k === want ? " want" : ""}">${l}</i>`).join("") + "</div>";
}
function instHtml(d) {
  const map = [["guitar", "G"], ["bass", "B"], ["rhythm", "R"], ["drums", "D"], ["keys", "K"], ["vocals", "V"]];
  return `<div class="inst">${map.filter(([k]) => k in d).map(([k, l]) => `<i class="${l}" title="${esc(inn(k))} ${esc(d[k])}">${esc(tr("inst.letter." + k))}</i>`).join("")}</div>`;
}
// A song with several charts shows which chart each row is; the favourite copy is marked with a star.
function chartTag(s) {
  if (!(s.copies > 1 || st.filter === "fav")) return "";
  const who = s.charter || tr("lib.unknown_charter");
  return ` · <em class="chart-tag${s.fav ? " on" : ""}">${s.fav ? "★ " : ""}${esc(who)}</em>`;
}
function rowEl(s) {
  const el = document.createElement("div");
  el.className = "row" + (s.disabled ? " disabled" : "");
  el.dataset.id = s.id;
  const [vid, ly] = pills(s);
  el.innerHTML = `<div><input type="checkbox" tabindex="-1"></div>
    <div class="art"${s.art ? ` style="background-image:url('/media/art/${s.id}?v=${lib.version}')"` : ""}></div>
    <div class="t"><b>${esc(s.name)}</b><span>${esc(s.artist)}${s.sng ? " · .sng" : ""}${chartTag(s)}</span></div>
    <div class="cell h1">${esc(s.album)}</div><div class="cell h1">${esc(s.year)}</div>
    <div class="cell">${fmtLen(s.length)}</div><div class="cell h1">${esc(s.charter)}</div>
    <div>${instHtml(s.diffs)}</div><div>${vid}</div><div>${ly}</div>
    <div class="fav${s.fav ? " on" : ""}" title="${esc(s.fav ? tr("lib.fav_chart") : tr("lib.fav_this"))}${s.copies > 1 && s.charter ? " (" + esc(s.charter) + ")" : ""}">${s.fav ? "★" : "☆"}</div>`;
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
  setFilter(b.dataset.f);
});
$("#libGroup").addEventListener("change", (e) => { st.group = e.target.value; applyFilters(); });

function updateSelbar() {
  const n = st.sel.size;
  $("#selbar").classList.toggle("on", n > 0);
  $("#selCount").textContent = tr("common.selected", { count: n });
  $("#selAll").checked = n > 0 && rows.length > 0 && rows.every((s) => st.sel.has(s.id));
  const anyDisabled = [...st.sel].some((id) => lib.byId.get(id)?.disabled);
  $("#disableBtn").textContent = anyDisabled ? tr("lib.act.enable") : tr("lib.act.disable");
  $("#disableBtn").dataset.act = anyDisabled ? "enable" : "disable";
  const allFav = n > 0 && [...st.sel].every((id) => lib.byId.get(id)?.fav);
  $("#favBtn").textContent = allFav ? tr("lib.act.fav_remove") : tr("lib.act.fav_add");
}

/* run batch */
$("#runBtn").addEventListener("click", (e) => {
  const o = $("#runOpts");
  if (!o.classList.contains("on")) { o.classList.add("on"); e.target.textContent = tr("lib.start", { count: st.sel.size }); return; }
  startRun(); o.classList.remove("on"); e.target.textContent = tr("lib.run");
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#runOpts") && !e.target.closest("#runBtn")) { $("#runOpts").classList.remove("on"); $("#runBtn").textContent = tr("lib.run"); }
});
async function startRun(ids = [...st.sel]) {
  const video = $("#optVideo").checked, lyrics = $("#optLyrics").checked, meta = $("#optMeta").checked, force = $("#optForce").checked;
  if (!video && !lyrics && !meta) return toast(tr("toast.pick_one"));
  const kind = (video || lyrics) && meta ? "full" : meta ? "metadata" : "media";
  const fav = st.filter === "fav" && cloud.favs.length;
  const r = await api("/api/jobs", { ids, kind, options: { video, lyrics, force }, installMissing: !!fav });
  if (r.installing?.queued) toast(tr("toast.installing_missing", { count: r.installing.queued }) + (r.installing.notFound.length ? " · " + tr("toast.not_found_online", { count: r.installing.notFound.length }) : ""), 5000);
  else toast(tr("toast.queued", { count: r.queued }) + (r.installing?.notFound.length ? " · " + tr("toast.fav_not_found_online", { count: r.installing.notFound.length }) : ""));
  if (r.installing) loadCloud();
  pollJobs(true);
}
/* Trash in the background (one Trash call per chart); poll progress, then report what could not be moved. */
async function trashSongs(ids, keepSel) {
  const { op } = await api("/api/songs/action", { ids, action: "delete" });
  let s;
  for (;;) {
    s = await api("/api/songs/op", { id: op });
    if (s.status !== "running") break;
    toast(tr("toast.trashing", { done: s.done, total: s.total }), 4000);
    await new Promise((r) => setTimeout(r, 700));
  }
  if (s.status === "error") throw new Error(s.error || tr("toast.trash_failed"));
  await loadLibrary();
  const bad = s.failed || [];
  bad.forEach((f) => keepSel.add(f.id));
  if (!bad.length) return toast(tr("toast.moved", { count: s.deleted }));
  toast(tr("toast.moved", { count: s.deleted }) + " · " + tr("toast.cant_move", { count: bad.length }), 30000, () => showTrashFailures(bad));
  showTrashFailures(bad);
}
async function showTrashFailures(bad) {
  const list = bad.slice(0, 200).map((f) => `• ${f.name}: ${f.reason}`).join("\n") + (bad.length > 200 ? "\n" + tr("toast.and_more", { count: bad.length - 200 }) : "");
  const r = await ask({ title: tr("dlg.cant_trash", { count: bad.length }), text: list, ok: tr("dlg.retry"), danger: true });
  if (!r) return;
  const keep = new Set(); await trashSongs(bad.map((f) => f.id), keep); st.sel.clear(); keep.forEach((i) => st.sel.add(i)); renderTable(false); updateSelbar();
}
$("#selbar").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const ids = [...st.sel], act = b.dataset.act;
  try {
    if (act === "clear") { st.sel.clear(); renderTable(false); updateSelbar(); return; }
    if (act === "favorite") return setFav(ids, !ids.every((i) => lib.byId.get(i)?.fav));
    if (act === "setlist") return addToSetlist(ids);
    if (act === "pack") {
      const r = await ask({ title: tr("dlg.pack_title"), text: tr("dlg.pack_text", { count: ids.length }), input: tr("dlg.pack_default"), ok: tr("dlg.export") });
      if (!r) return; toast(tr("toast.packing")); const d = await api("/api/songs/action", { ids, action: "pack", name: r.input }); return toast(tr("toast.saved_path", { path: d.path }), 5000);
    }
    if (act === "rename") {
      const r = await ask({ title: tr("dlg.rename_title"), text: tr("dlg.rename_text"), input: "{artist} - {name}", ok: tr("dlg.rename") });
      if (!r) return; const d = await api("/api/songs/action", { ids, action: "rename", template: r.input }); return toast(tr("toast.renamed", { count: d.count }));
    }
    if (act === "restore") {
      if (!await ask({ title: tr("dlg.restore_title"), text: tr("dlg.restore_text", { count: ids.length }), ok: tr("lib.act.restore"), danger: true })) return;
      const d = await api("/api/songs/action", { ids, action: "restore" }); return toast(tr("toast.restored", { count: d.count }));
    }
    if (act === "disable" || act === "enable") {
      const d = await api("/api/songs/action", { ids, action: act });
      toast(act === "disable" ? tr("toast.disabled", { count: d.count }) : tr("toast.enabled", { count: d.count }));
      return;
    }
    if (act === "delete") {
      if (!await ask({ title: tr("dlg.trash_title", { count: ids.length }), text: tr("dlg.trash_text"), ok: tr("lib.act.delete"), danger: true })) return;
      const keep = new Set(); await trashSongs(ids, keep); st.sel.clear(); keep.forEach((i) => st.sel.add(i)); renderTable(false); updateSelbar(); loadCloud();
    }
  } catch (err) { toast(err.message, 4500); }
});

/* ================================================================ favourites */
/* One number everywhere: distinct favourited charts in the collection (on disk + not on this machine), the
   same on every synced machine. Before the server has answered, fall back to the favourites on disk. */
function favTotals() {
  const c = cloud.counts;
  if (c) return c;
  const n = lib.songs.filter((s) => s.fav).length;
  return { total: n, onDisk: n, notHere: 0 };
}
function updateFavCount() {
  const ft = favTotals();
  $("#navFavCount").textContent = ft.total || "";
  const sum = $("#favSummary");
  sum.hidden = !(st.filter === "fav" && currentView === "library");
  sum.textContent = tr("fav.summary", { count: ft.total }) + (ft.notHere ? " · " + tr("fav.not_here", { count: ft.notHere }) : "");
}
/* The one sharing switch (Friends view and Settings) and its status line. */
function shareStatusText() {
  const on = !!(fr.status && fr.status.sharing && fr.status.sharing.lists), n = (fr.status && fr.status.friends) || 0;
  return on ? (n ? tr("share.status_on", { count: n }) : tr("share.status_nofriends")) : tr("share.status_off");
}
function paintShare() {
  const on = !!(fr.status && fr.status.sharing && fr.status.sharing.lists);
  $$(".shareSw").forEach((cb) => { cb.checked = on; cb.disabled = false; });
  const sw = $("#statsSw"); if (sw) sw.checked = !(fr.status && fr.status.sharing && fr.status.sharing.stats === false);
  $$(".shareStatus").forEach((el) => { el.textContent = shareStatusText(); });
}
function bindShare(root) {
  const sw = $("#statsSw", root);
  if (sw) sw.onchange = async (e) => {
    const on = e.target.checked; sw.disabled = true;
    try { const r = await api("/api/friends/share", { stats_shared: on }); fr.status = { ...(fr.status || {}), sharing: r }; toast(tr(on ? "share.stats_on" : "share.stats_off")); }
    catch (err) { toast(err.message, 4500); }
    sw.disabled = false; await pollFriends();
  };
  $$(".shareSw", root).forEach((cb) => cb.onchange = async (e) => {
    const on = e.target.checked;
    $$(".shareSw").forEach((x) => x.disabled = true);
    try { const r = await api("/api/friends/share", { lists_shared: on }); fr.status = { ...(fr.status || {}), sharing: r }; toast(tr(on ? "share.on" : "share.off")); }
    catch (err) { toast(err.message, 4500); }
    await pollFriends();
  });
}
async function setFav(ids, value) {
  ids = ids.filter((i) => lib.byId.has(i));
  if (!ids.length) return;
  await api("/api/songs/action", { ids, action: "favorite", value });
  const want = new Set(ids);                                   // favourites are per chart: other copies are untouched
  lib.songs.forEach((x) => { if (want.has(x.id)) x.fav = value; });
  updateFavCount(); renderTable(true); updateSelbar(); loadCloud();
  if (detail && ids.includes(detail.id)) { detail.fav = value; paintDetailFav(); }
  if (!value && ids.length > 1) toast(tr("toast.fav_removed_many", { count: ids.length }), 6000);
  else toast(value ? (ids.length === 1 ? tr("toast.fav_added") : tr("toast.fav_added_many", { count: ids.length })) : tr("toast.fav_removed"));
}
function paintDetailFav() {
  const b = $("#dFav"); if (!b || !detail) return;
  const on = lib.byId.get(detail.id)?.fav;
  b.textContent = on ? tr("detail.fav_on") : tr("lib.act.fav_add"); b.classList.toggle("fav-on", !!on);
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
      <div class="d-row">${vid}${ly}${d.sng ? '<span class="pill">.sng</span>' : ""}${d.disabled ? `<span class="pill low">${esc(tr("pill.disabled"))}</span>` : ""}</div>
      <div class="kv"><span>${esc(tr("detail.release"))}</span><span>${esc(meta) || "—"}</span><span>${esc(tr("detail.charter"))}</span><span>${esc(d.charter) || "—"}</span>
        <span>${esc(tr("detail.length"))}</span><span>${fmtLen(d.length) || "—"}</span><span>${esc(tr("detail.folder"))}</span><span title="${esc(d.path)}">${esc(d.rel)}</span></div>
      <div class="status-line" id="dStatus">${esc(d.status || "")}</div>
      <div class="d-row">
        <button id="dFav" class="favbtn"></button>
        <button class="primary" id="dRun">${esc(tr("detail.run"))}</button>
        <button id="dMeta">${esc(tr("detail.meta_only"))}</button>
        <button id="dPlay">${esc(tr("detail.play"))}</button>
        <button id="dReveal">${esc(tr("detail.reveal"))}</button>
      </div>
      <div class="d-row"><span class="hint">${esc(tr("detail.rating"))}</span>${[1, 2, 3, 4, 5].map((n) => `<span class="fav${(d.rating || 0) >= n ? " on" : ""}" data-r="${n}">★</span>`).join("")}</div>
    </div>
    <div class="d-sec">
      <h4>${esc(tr("detail.sync_title"))}</h4>
      <div class="preview"><video id="dVideo" muted playsinline preload="metadata"${d.hasVideo ? ` src="/media/video/${d.id}?v=${lib.version}"` : ""}></video>${d.hasVideo ? "" : `<div class="none">${esc(tr("detail.no_video"))}</div>`}</div>
      <audio id="dAudio" preload="none"></audio>
      <div class="slider"><span>${esc(tr("detail.position"))}</span><input type="range" id="dPos" min="0" max="${Math.round(d.length || 300)}" step="1" value="0"><b id="dPosV">0:00</b></div>
      <div class="slider"><span>${esc(tr("detail.video"))}</span><input type="range" id="dVOff" min="-15" max="90" step="0.05" value="${vOff}"><b id="dVOffV">${vOff.toFixed(2)}s</b></div>
      <div class="slider"><span>${esc(tr("detail.lyrics"))}</span><input type="range" id="dLOff" min="-10" max="10" step="0.05" value="${lOff}"><b id="dLOffV">${lOff.toFixed(2)}s</b></div>
      <div class="d-row"><button id="dPrev">${esc(tr("detail.preview"))}</button><button class="primary" id="dApply">${esc(tr("detail.apply"))}</button></div>
      <p class="hint">${esc(tr("detail.sync_hint"))}</p>
    </div>
    <div class="d-sec">
      <h4>${esc(tr("detail.pick_title"))}</h4>
      <div class="d-row"><input type="url" id="dUrl" placeholder="${esc(tr("detail.paste_ph"))}" style="flex:1"><button id="dUse">${esc(tr("common.use"))}</button></div>
      <button id="dFind">${esc(tr("detail.find"))}</button>
      <div class="cands" id="dCands"></div>
    </div>
    <div class="d-sec">
      <h4>${esc(tr("detail.top_title"))}</h4>
      <div id="dLb"><p class="hint">${esc(tr("common.loading"))}</p></div>
    </div>
    <div class="d-sec">
      <div class="d-row">
        <button id="dSkip">${esc(d.skip ? tr("detail.skip_on") : tr("detail.skip_off"))}</button>
        <button id="dUndo" class="danger">${esc(tr("detail.undo"))}</button>
      </div>
    </div>`;
  bindDetail(d);
  loadDetailLb(d);
}
function updateDetailStatus() { const s = $("#dStatus"); if (s && detail) s.textContent = detail.status || ""; }
function bindDetail(d) {
  $("#dRun").onclick = () => startRunOne(d.id, "full");
  paintDetailFav();
  $("#dFav").onclick = () => setFav([d.id], !lib.byId.get(d.id)?.fav);
  $("#dMeta").onclick = () => startRunOne(d.id, "metadata");
  $("#dPlay").onclick = () => playSong(d.id);
  $("#dReveal").onclick = () => api("/api/songs/action", { ids: [d.id], action: "reveal" });
  $("#dUndo").onclick = async () => { await api("/api/songs/action", { ids: [d.id], action: "restore" }); toast(tr("toast.restored_files")); };
  $("#dSkip").onclick = async () => { await api("/api/songs/action", { ids: [d.id], action: "skip", value: !d.skip }); d.skip = !d.skip; $("#dSkip").textContent = d.skip ? tr("detail.skip_on") : tr("detail.skip_off"); };
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
    toast(tr("toast.sync_saved"), 4000);
  };
  $("#dUse").onclick = async () => {
    const v = $("#dUrl").value.trim(); if (!v) return;
    try { const r = await api(`/api/song/${d.id}/use-video`, { video: v }); toast(tr("toast.using_video", { res: r.res })); pollJobs(true); }
    catch (e) { toast(e.message, 4500); }
  };
  $("#dFind").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = tr("detail.searching");
    try {
      const r = await api(`/api/song/${d.id}/candidates`, {});
      $("#dCands").innerHTML = r.candidates.map((c) => `<div class="cand${c.id === d.videoId ? " current" : ""}">
          <a href="https://youtu.be/${c.id}" target="_blank" title="${esc(c.title)}">${esc(c.title)}</a>
          <span class="pill ${c.res >= MIN_P ? "ok" : "low"}">${c.res ? c.res + "p" : esc(tr("pill.no_hd"))}</span>
          <button data-v="${c.id}" ${c.res >= MIN_P ? "" : "disabled"}>${esc(tr("common.use"))}</button></div>`).join("") || `<p class="hint">${esc(tr("detail.nothing_found"))}</p>`;
      $$("#dCands button").forEach((b) => b.onclick = async () => {
        try { await api(`/api/song/${d.id}/use-video`, { video: b.dataset.v }); toast(tr("toast.downloading_video")); pollJobs(true); } catch (err) { toast(err.message, 4500); }
      });
    } catch (err) { toast(err.message, 4500); }
    e.target.disabled = false; e.target.textContent = tr("detail.find");
  };
}
async function startRunOne(id, kind) {
  await api("/api/jobs", { ids: [id], kind, options: { video: true, lyrics: true, force: kind !== "metadata" && !!detail?.hasVideo && videoState(detail) === "low" } });
  toast(tr("toast.queued_one")); pollJobs(true);
}

/* preview: audio from the chart, video offset applied live */
let previewing = false, previewStart = 0;
function startPreview(t) {
  const a = $("#dAudio"); if (!a || !detail) return;
  previewing = true; previewStart = t;
  a.src = `/media/audio/${detail.id}?t=${t}`; a.play().catch(() => {});
  $("#dPrev").textContent = tr("detail.stop");
  const v = $("#dVideo"); if (v && v.src) { syncPreview(true); v.play().catch(() => {}); }
  clearInterval(startPreview.iv); startPreview.iv = setInterval(() => syncPreview(false), 250);
}
function stopPreview() {
  previewing = false; clearInterval(startPreview.iv);
  const a = $("#dAudio"), v = $("#dVideo"); if (a) { a.pause(); a.removeAttribute("src"); } if (v) v.pause();
  if ($("#dPrev")) $("#dPrev").textContent = tr("detail.preview");
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
  $("#player").hidden = false; $("#plTitle").textContent = `${s.artist} — ${s.name}`; $("#plToggle").textContent = tr("player.pause");
}
$("#plToggle").onclick = () => { const a = $("#plAudio"); if (a.paused) { a.play(); $("#plToggle").textContent = tr("player.pause"); } else { a.pause(); $("#plToggle").textContent = tr("player.play"); } };

/* ================================================================ now playing window */
async function openNowWindow(fullscreen) {
  try {
    const r = await api("/api/window", { page: "now", fullscreen });
    toast(r.kind === "window" ? tr("toast.now_window") : tr("toast.now_window_plain"), 5000);
    $("#nowFrame").src = "about:blank";   // don't run two copies of the video
  } catch (e) { toast(e.message, 4000); }
}
$("#nowWin").onclick = () => openNowWindow(false);
$("#nowFull").onclick = () => openNowWindow(true);

/* ================================================================ setlists */
let setlists = [], slActive = null;
async function loadSetlists() {
  try { setlists = (await api("/api/setlists")).setlists; } catch (e) { return toast(e.message); }
  $("#slList").innerHTML = setlists.map((s) => `<li data-n="${esc(s.name)}" class="${s.name === slActive ? "on" : ""}"><span>${esc(s.name)}</span><span>${s.songs.length}</span></li>`).join("") || `<li class="hint">${esc(tr("setlists.none"))}</li>`;
  renderSetlist();
}
function renderSetlist() {
  const sl = setlists.find((s) => s.name === slActive);
  if (!sl) { $("#slBody").innerHTML = `<p class="hint">${esc(tr("setlists.pick"))}</p>`; return; }
  $("#slBody").innerHTML = `<div class="pane-head"><h2>${esc(sl.name)}</h2><button class="danger" id="slDel">${esc(tr("setlists.delete"))}</button></div>` +
    sl.songs.map((x, i) => {
      const s = x.id && lib.byId.get(x.id);
      return `<div class="line"><span class="hint">${i + 1}</span><div class="art"${s && s.art ? ` style="background-image:url('/media/art/${s.id}')"` : ""}></div>
        <div><b>${s ? esc(s.name) : esc(tr("setlists.not_in_library"))}</b><span>${s ? esc(s.artist) : x.hash}</span></div>
        <span class="pill">${x.speed}%</span>${s ? `<button data-rm="${s.id}">${esc(tr("setlists.remove"))}</button>` : "<span></span>"}</div>`;
    }).join("");
  $("#slDel").onclick = async () => {
    if (!await ask({ title: tr("setlists.delete_title", { name: sl.name }), ok: tr("setlists.delete_ok"), danger: true })) return;
    await api("/api/setlists", { action: "delete", name: sl.name }); slActive = null; loadSetlists();
  };
  $$("#slBody [data-rm]").forEach((b) => b.onclick = async () => { await api("/api/setlists", { action: "remove", name: sl.name, ids: [b.dataset.rm] }); loadSetlists(); });
}
$("#slList").addEventListener("click", (e) => { const li = e.target.closest("li[data-n]"); if (li) { slActive = li.dataset.n; loadSetlists(); } });
$("#slNew").onclick = async () => {
  const r = await ask({ title: tr("setlists.new_title"), input: "", ok: tr("setlists.create") }); if (!r || !r.input) return;
  try { await api("/api/setlists", { action: "create", name: r.input }); slActive = r.input; loadSetlists(); } catch (e) { toast(e.message); }
};
async function addToSetlist(ids) {
  try { setlists = (await api("/api/setlists")).setlists; } catch (e) { /* ignore */ }
  const r = await ask({ title: tr("setlists.add_title", { count: ids.length }), text: tr("setlists.add_text"),
    select: [{ value: "", label: tr("setlists.new_option") }, ...setlists.map((s) => ({ value: s.name, label: s.name }))], input: "", ok: tr("setlists.add") });
  if (!r) return;
  const name = r.input?.trim() || r.select; if (!name) return toast(tr("setlists.need_name"));
  const d = await api("/api/setlists", { action: "add", name, ids });
  toast(tr("setlists.added", { count: d.added, name }), 4500);
}

/* ================================================================ duplicates */
async function loadDupes() {
  $("#dupList").innerHTML = `<p class="hint">${esc(tr("dupes.looking"))}</p>`;
  const d = await api("/api/duplicates");
  $("#dupList").innerHTML = d.groups.map((g) => `<div class="dupe"><h3>${esc(g[0].artist)} — ${esc(g[0].name)}</h3>` + g.map((s) => {
    const [vid, ly] = pills(s);
    return `<div class="copy"><span title="${esc(s.path)}">${esc(s.rel)}<br><small>${esc(s.charter || tr("lib.unknown_charter"))}${s.sng ? " · .sng" : ""}</small></span>${vid}${ly}${instHtml(s.diffs)}
      <span><button data-keep="${s.id}">${esc(tr("dupes.keep"))}</button></span></div>`;
  }).join("") + "</div>").join("") || `<p class="hint">${esc(tr("dupes.none"))}</p>`;
  $$("#dupList [data-keep]").forEach((b) => b.onclick = async () => {
    const group = d.groups.find((g) => g.some((s) => s.id === b.dataset.keep));
    const others = group.filter((s) => s.id !== b.dataset.keep).map((s) => s.id);
    if (!await ask({ title: tr("dupes.trash_title", { count: others.length }), ok: tr("lib.act.delete"), danger: true })) return;
    await trashSongs(others, new Set()); loadDupes();
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
  $("#chartResults").innerHTML = `<p class="hint">${esc(tr("charts.searching"))}</p>`;
  try {
    const d = await api("/api/search?" + params);
    charts.items = d.songs; charts.total = d.total;
    renderCharts();
  } catch (e) { $("#chartResults").innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
}
function renderCharts() {
  $("#chartResults").innerHTML = charts.items.map((c, i) => `<div class="card${charts.sel.has(c.id) ? " sel" : ""}${c.owned ? " owned" : ""}" data-i="${i}">
      <input type="checkbox" class="chk"${charts.sel.has(c.id) ? " checked" : ""}>
      <div class="art"${c.artUrl ? ` style="background-image:url('${esc(c.artUrl)}')"` : ""}></div>
      ${c.source === "encore" && c.downloadUrl ? `<button class="pv" title="${esc(tr("charts.preview_tip"))}">${esc(tr("charts.preview"))}</button>` : ""}
      <div class="body"><b>${esc(c.name)}</b><span>${esc(c.artist)}</span><span>${esc(c.charter)}${c.length ? " · " + fmtLen(c.length) : ""}</span>
      <div class="tags">${instHtml(c.difficulties || {})}${levelsHtml(c.levels)}${c.hasVideo ? `<span class="pill ok">${esc(tr("charts.has_video"))}</span>` : ""}${c.needsConversion ? `<span class="pill low">${esc(tr("charts.rb_format"))}</span>` : ""}${!c.downloadUrl && !c.needsConversion ? `<span class="pill">${esc(tr("charts.manual"))}</span>` : ""}</div>
      ${c.owned ? `<span class="inlib">${esc(tr("charts.in_library"))}${!c.ownedSameChart && c.ownedCharters && c.ownedCharters.length ? ` <em>${esc(tr("charts.yours", { charters: c.ownedCharters.join(", ") }))}</em>` : ""}</span>` : ""}</div></div>`).join("")
    || `<p class="hint">${esc(tr("charts.none"))}</p>`;
  const pages = Math.max(1, Math.ceil(charts.total / 48));
  $("#chartPager").innerHTML = `<button ${charts.page <= 1 ? "disabled" : ""} id="pgPrev">${esc(tr("charts.prev"))}</button> <span class="pginfo">${esc(tr("charts.page_info", { page: charts.page, pages, count: charts.total }))}</span> <button ${charts.page >= pages ? "disabled" : ""} id="pgNext">${esc(tr("charts.next"))}</button>`;
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
    $("#player").hidden = false; $("#plTitle").textContent = tr("player.preview", { artist: c.artist, name: c.name }); $("#plToggle").textContent = tr("player.pause");
    return toast(tr("toast.loading_preview"), 1500);
  }
  if (!c.downloadUrl) { if (c.pageUrl) window.open(c.pageUrl, "_blank"); return toast(c.needsConversion ? tr("toast.rb_unsupported") : tr("toast.manual_page"), 3500); }
  charts.sel.has(c.id) ? charts.sel.delete(c.id) : charts.sel.set(c.id, c);
  card.classList.toggle("sel"); card.querySelector(".chk").checked = charts.sel.has(c.id); updateChartSel();
});
function updateChartSel() { $("#chartSel").classList.toggle("on", charts.sel.size > 0); $("#chartSelCount").textContent = tr("common.selected", { count: charts.sel.size }); }
$("#chartDl").onclick = async () => {
  const all = [...charts.sel.values()];
  const items = all.filter((c) => !c.ownedSameChart), have = all.length - items.length;
  if (!items.length) { toast(have === 1 ? tr("toast.already_one") : tr("toast.already_all", { count: have }), 4000); return; }
  const r = await api("/api/download", { items, process: $("#chartProcess").checked });
  toast(tr("toast.downloading", { count: r.queued }) + (have ? " " + tr("toast.skipped_have", { count: have }) : "") + " — " + tr("toast.see_jobs"), 4500);
  charts.sel.clear(); renderCharts(); pollJobs(true);
};
$("#chartClear").onclick = () => { charts.sel.clear(); renderCharts(); };
$("#chartDb").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; charts.db = b.dataset.db; $$("#chartDb button").forEach((x) => x.classList.toggle("on", x === b)); searchCharts(1); });
$("#chartSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") searchCharts(1); });
["#chartInst", "#chartDiff", "#chartSort"].forEach((s) => $(s).addEventListener("change", () => searchCharts(1)));

/* ================================================================ spotify */
let spTracks = [], spData = null;
$("#spGo").onclick = async () => {
  const url = $("#spUrl").value.trim(); if (!url) return;
  $("#spResults").innerHTML = `<p class="hint">${esc(tr("spotify.reading"))}</p>`;
  try {
    const d = await api("/api/spotify", { url });
    spData = d;
    spTracks = d.tracks.map((t) => ({ ...t, pick: !!(t.match && !t.owned && t.match.downloadUrl) }));
    renderSpotify(d);
  } catch (e) { $("#spResults").innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
};
function renderSpotify(d) {
  const found = spTracks.filter((t) => t.match).length, owned = spTracks.filter((t) => t.owned).length;
  $("#spResults").innerHTML = `<p class="hint"><b>${esc(d.name)}</b> — ${esc(tr("spotify.summary", { count: spTracks.length, owned, found }))}${d.truncated ? " " + esc(tr("spotify.truncated")) : ""}.</p>` +
    spTracks.map((t, i) => `<div class="line"><input type="checkbox" data-i="${i}" ${t.pick ? "checked" : ""} ${t.match && !t.owned ? "" : "disabled"}>
      <div class="art"${t.match && t.match.artUrl ? ` style="background-image:url('${esc(t.match.artUrl)}')"` : ""}></div>
      <div><b>${esc(t.title)}</b><span>${esc(t.artists)}</span></div>
      <span>${t.owned ? `<span class="pill ok">${esc(tr("spotify.in_library"))}</span>` : t.match ? `<span class="pill mid">${esc(t.match.charter || tr("spotify.chart_found"))}</span>` : `<span class="pill">${esc(tr("spotify.no_chart"))}</span>`}</span><span></span></div>`).join("");
  $$("#spResults input[data-i]").forEach((c) => c.onchange = () => { spTracks[c.dataset.i].pick = c.checked; updSp(); });
  updSp();
}
function updSp() { const n = spTracks.filter((t) => t.pick).length; $("#spSel").classList.toggle("on", n > 0); $("#spSelCount").textContent = tr("spotify.to_download", { count: n }); }
$("#spDl").onclick = async () => {
  const items = spTracks.filter((t) => t.pick).map((t) => t.match);
  const r = await api("/api/download", { items, process: $("#spProcess").checked });
  toast(tr("toast.downloading", { count: r.queued }) + " — " + tr("toast.see_jobs")); pollJobs(true);
};

/* ================================================================ jobs */
let jobsSnap = null, jobsSeen = null;
/* ids of jobs that reached a final state since `seen` (a Set, updated); the first call only seeds it */
function jobsNewlyFinished(seen, snap) {
  const fin = snap.jobs.filter((j) => ["done", "failed", "cancelled"].includes(j.status)).map((j) => j.id);
  const fresh = seen ? fin.filter((id) => !seen.has(id)) : [];
  fin.forEach((id) => seen && seen.add(id));
  return fresh;
}
async function pollJobs(once) {
  try {
    jobsSnap = await api("/api/jobs");
    if (!jobsSeen) jobsSeen = new Set(jobsSnap.jobs.filter((j) => ["done", "failed", "cancelled"].includes(j.status)).map((j) => j.id));
    else if (jobsNewlyFinished(jobsSeen, jobsSnap).length) { loadLibrary(); loadCloud(); }   // charts landed: the "not on this machine" box shrinks
    if (fr.watch) {          // friend downloads were queued: remind about Scan Songs once every job has finished
      if (jobsSnap.jobs.some((j) => ["queued", "waiting", "running"].includes(j.status))) fr.watchBusy = true;
      else if (fr.watchBusy) { fr.watch = fr.watchBusy = false; toast(tr("friends.scan_reminder"), 9000); }
    }
    renderConvert(); renderDock(); if (currentView === "jobs") renderJobs();
    if (activeId && detail) {
      const j = jobsSnap.jobs.find((x) => x.songId === activeId);
      if (j) { const s = j.status === "done" ? j.result : j.status === "failed" ? tr("job.failed", { error: j.error }) : (j.step || tr("job.status." + j.status)); if (detail.status !== s) { detail.status = s; updateDetailStatus(); } }
    }
    $("#navNow").classList.toggle("on", jobsSnap.now && ["playing", "paused"].includes(jobsSnap.now.status));
  } catch (e) { /* ignore */ }
  if (!once) setTimeout(pollJobs, 1500);
}
/* videos that won't play on every system (MP4 from before 2.3.1, or from another computer) */
function renderConvert() {
  const el = $("#convertBanner"); const c = jobsSnap && jobsSnap.convert;
  if (!el || !c) return;
  if (c.active) {
    el.hidden = false;
    el.innerHTML = `<span>${esc(tr("convert.running", { n: nf(c.active) }))}</span> <button data-conv="cancel">${esc(tr("convert.cancel"))}</button>`;
  } else if (c.count) {
    el.hidden = false;
    el.innerHTML = `<span>${esc(c.prompt ? tr("convert.ask") : tr("convert.banner", { n: nf(c.count) }))}</span>
      <button class="primary" data-conv="go">${esc(tr(c.prompt ? "convert.now" : "convert.go"))}</button>${c.prompt ? ` <button data-conv="later">${esc(tr("convert.later"))}</button>` : ""}
      <small class="hint">${esc(tr("convert.note"))}</small>`;
  } else { el.hidden = true; el.innerHTML = ""; }
}
$("#convertBanner").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-conv]"); if (!b) return;
  const url = { go: "/api/videos/convert", later: "/api/videos/convert/later", cancel: "/api/videos/convert/cancel" }[b.dataset.conv];
  await api(url, {}); pollJobs(true);
});
function renderDock() {
  const c = jobsSnap.counts || {};
  const active = (c.running || 0) + (c.queued || 0) + (c.waiting || 0);
  const total = active + (c.done || 0) + (c.failed || 0) + (c.cancelled || 0);
  $("#navJobs").textContent = active || "";
  const running = jobsSnap.jobs.filter((j) => j.status === "running").slice(0, 2);
  const pct = total ? Math.round(100 * (total - active) / total) : 0;
  $("#dockJobs").innerHTML = active ? `<div class="meter"><i style="width:${pct}%"></i></div>
     <span>${esc(tr("jobs.dock_done", { done: total - active, total }))}${c.failed ? ` · ${esc(tr("jobs.n_failed", { count: c.failed }))}` : ""}</span>
     <span>${running.map((j) => `${esc(j.title)}: ${esc(j.step)}`).join(" · ")}</span>
     ${jobsSnap.youtube?.pausedFor ? `<span class="pill low">${esc(tr("jobs.yt_pause", { secs: jobsSnap.youtube.pausedFor }))}</span>` : ""}`
    : (total ? `<span>${esc(c.failed ? tr("jobs.all_finished_failed", { count: c.failed }) : tr("jobs.all_finished"))}</span>`
        + (jobsSnap.jobs.some((j) => j.status === "done" && j.videoAdded)
          ? `<span class="hint">${tr("jobs.scan_hint")}</span>` : "")
      : `<span>${esc(tr("jobs.ready"))}</span>`);
}
function renderJobs() {
  if (!jobsSnap) return;
  const c = jobsSnap.counts || {};
  $("#jobCounts").textContent = ["running", "queued", "waiting", "done", "failed"].filter((k) => c[k]).map((k) => `${nf(c[k])} ${tr("job.status." + k)}`).join(" · ") +
    (jobsSnap.avgSeconds ? " · " + tr("jobs.avg", { secs: jobsSnap.avgSeconds }) : "");
  const w = $("#jobWorkers"); if (document.activeElement !== w) { w.value = jobsSnap.workers; $("#jobWorkersVal").textContent = jobsSnap.workers; }
  const yt = jobsSnap.youtube || {};
  $("#ytWarn").hidden = !yt.pausedFor;
  const ll = yt.lastLimit;
  $("#ytWarn").textContent = (yt.hint ? yt.hint + " " : tr("jobs.yt_limiting") + " ") + tr("jobs.yt_resume", { min: Math.ceil(yt.pausedFor / 60) });
  $("#ytWarn").title = ll ? tr("jobs.yt_tip", { signal: ll.signalLabel || ll.signal, said: ll.message || "", video: ll.videoId || tr("common.na"), r10: ll.requests10, r60: ll.requests60 }) : "";
  $("#jobList").innerHTML = jobsSnap.jobs.map((j) => `<div class="job"><span class="st ${j.status}">${esc(tr("job.status." + j.status))}</span>
      <span>${esc(j.title)}</span><span class="msg" title="${esc(j.error || j.result || j.step)}">${esc(j.error || j.result || j.step)}</span>
      <span>${["queued", "running", "waiting"].includes(j.status) ? `<button data-cancel="${j.id}">${esc(tr("jobs.cancel"))}</button>` : ""}</span></div>`).join("") || `<p class="hint">${esc(tr("jobs.empty"))}</p>`;
}
$("#jobList").addEventListener("click", async (e) => { const b = e.target.closest("[data-cancel]"); if (b) { await api("/api/jobs/cancel", { id: b.dataset.cancel }); pollJobs(true); } });
$("#jobRetry").onclick = async () => { await api("/api/jobs/retry", {}); pollJobs(true); };
$("#jobClear").onclick = async () => { await api("/api/jobs/clear", {}); pollJobs(true); };
$("#jobCancel").onclick = async () => { await api("/api/jobs/cancel", {}); pollJobs(true); };
$("#jobWorkers").addEventListener("input", (e) => { $("#jobWorkersVal").textContent = e.target.value; });
$("#jobWorkers").addEventListener("change", async (e) => { await api("/api/settings", { settings: { workers: Number(e.target.value) } }); toast(tr("toast.workers", { count: Number(e.target.value) })); });

/* ================================================================ settings */
function ytCookieText(y) {
  const c = (y && y.cookies) || {};
  if (c.error) return c.error;
  return c.active ? tr("settings.cookie_using", { source: c.mode === "file" ? "cookies.txt" : cap(c.mode) }) : tr("settings.cookie_none");
}
function ytActivityText(y) {
  if (!y) return "";
  const l = y.lastLimit;
  const when = l ? I18N.time(l.at * 1000) : "";
  const limit = l ? (l.videoId ? tr("settings.yt_last_limit_video", { label: l.signalLabel || l.signal, video: l.videoId, time: when }) : tr("settings.yt_last_limit", { label: l.signalLabel || l.signal, time: when })) : tr("settings.yt_no_limit");
  return tr("settings.yt_activity", { count: y.requests60, budget: y.budget, limit });
}
async function loadSettings() {
  const d = await api("/api/settings"); const s = d.settings;
  const chk = (k, label, help) => `<div class="row2"><span>${esc(label)}</span><label class="inline"><input type="checkbox" data-k="${k}" ${s[k] ? "checked" : ""}> <span>${esc(help)}</span></label></div>`;
  const langOpts = [["auto", tr("settings.language_auto")], ...Object.entries(I18N.LANGS)];
  $("#settingsForm").innerHTML = `
    <section><h3>${esc(tr("settings.language"))}</h3>
      <div class="row2"><span>${esc(tr("settings.language"))}</span><select data-lang-select>${langOpts.map(([v, l]) => `<option value="${v}"${(s.language || "auto") === v ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></div>
    </section>
    <section><h3>${esc(tr("settings.videos"))}</h3>
      <div class="row2"><span>${esc(tr("settings.pref_quality"))}</span><select data-k="pref_p"><option value="720"${s.pref_p == 720 ? " selected" : ""}>720p</option><option value="1080"${s.pref_p == 1080 ? " selected" : ""}>1080p</option></select>
        <small>${esc(tr("settings.min_note", { p: d.minP }))}</small></div>
      ${chk("replace_low_res", tr("settings.low_res"), tr("settings.low_res_help"))}
      <div class="row2"><span>${esc(tr("settings.video_format"))}</span><span>${esc(tr("settings.format_webm"))}</span>
        <small>${esc(tr("settings.format_note"))}</small></div>
    </section>
    ${cloud.status && cloud.status.signedIn ? `<section><h3>${esc(tr("nav.friends"))}</h3>
      <div class="row2"><span>${esc(tr("share.switch"))}</span><label class="inline"><input type="checkbox" class="shareSw"> <span>${esc(tr("share.switch_help"))}</span></label></div>
      <div class="row2"><span></span><span class="hint shareStatus"></span></div></section>` : ""}
    <section><h3>${esc(tr("settings.in_game"))}</h3>
      ${chk("in_game", tr("settings.add_media"), tr("settings.add_media_help"))}
      ${chk("prefetch", tr("settings.prefetch"), tr("settings.prefetch_help"))}
      ${chk("game_lyrics", tr("settings.game_lyrics"), tr("settings.game_lyrics_help"))}
    </section>
    <section><h3>${esc(tr("settings.youtube"))}</h3>
      <div class="row2"><span>${esc(tr("settings.cookies_from"))}</span><select data-k="yt_cookies">${[["none", tr("settings.cookies_none")], ["chrome", "Chrome"], ["edge", "Edge"], ["firefox", "Firefox"], ["safari", "Safari"], ["brave", "Brave"], ["file", tr("settings.cookies_file_opt")]].map(([v, l]) => `<option value="${v}"${(s.yt_cookies || "none") === v ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>
        <small>${esc(tr("settings.cookies_note"))}</small></div>
      ${s.yt_cookies === "file" ? `<div class="row2"><span>cookies.txt</span><input type="text" data-k="yt_cookies_file" value="${esc(s.yt_cookies_file || "")}" placeholder="${esc(tr("settings.cookies_file_ph"))}"></div>` : ""}
      <div class="row2"><span>${esc(tr("settings.signin_status"))}</span><span id="ytCookieState">${esc(ytCookieText(d.youtube))}</span></div>
      <div class="row2"><span>${esc(tr("settings.yt_activity_label"))}</span><span id="ytActivity">${esc(ytActivityText(d.youtube))}</span></div>
    </section>
    <section><h3>${esc(tr("settings.processing"))}</h3>
      <div class="row2"><span>${esc(tr("settings.workers"))}</span><select data-k="workers">${[0, 1, 2, 3, 4, 6, 8].map((n) => `<option value="${n}"${Number(s.workers) === n ? " selected" : ""}>${n ? n : esc(tr("settings.auto"))}</option>`).join("")}</select>
        <small>${esc(tr("settings.workers_note"))}</small></div>
      ${chk("metadata_overwrite", tr("settings.metadata"), tr("settings.metadata_help"))}
      ${chk("fav_setlist", tr("settings.fav_setlist"), tr("settings.fav_setlist_help"))}
      ${chk("auto_process_downloads", tr("settings.new_charts"), tr("settings.new_charts_help"))}
      <div class="row2"><span>${esc(tr("settings.download_into"))}</span><input type="text" data-k="download_subdir" value="${esc(s.download_subdir)}" placeholder="${esc(tr("settings.download_ph"))}"></div>
    </section>
    <section><h3>${esc(tr("settings.folders"))}</h3>
      <div class="row2"><span>${esc(tr("settings.clone_hero"))}</span><span>${esc(d.game || tr("settings.not_found"))}</span></div>
      <div class="row2"><span>${esc(tr("settings.data_folder"))}</span><span>${esc(d.dataDir)}</span></div>
      <div class="row2"><span>${esc(tr("settings.song_folders"))}</span><span>${d.songDirs.map(esc).join("<br>") || esc(tr("common.none"))}</span></div>
      <div class="row2"><span></span><span class="btnrow"><button id="setChange" class="primary">${esc(tr("settings.change_folders"))}</button> <button id="setOpen">${esc(tr("settings.open_data"))}</button> <button id="setRescan">${esc(tr("settings.rescan"))}</button></span></div>
    </section>
    <section><h3>${esc(tr("settings.about"))}</h3>
      <div class="row2"><span>YouTube</span><span>${d.jsRuntime ? esc(tr("settings.js_runtime", { name: d.jsRuntime, ejs: d.ejs })) : tr("settings.js_missing", { help: esc(d.jsHelp) })}</span></div>
      <div class="row2"><span>yt-dlp</span><span>${esc(d.ytdlpInfo.version)} (${esc(tr("about." + d.ytdlpInfo.source))}) <button id="ytdlpCheck">${esc(tr("settings.check_now"))}</button> <span id="ytdlpMsg" class="hint">${esc(d.ytdlpInfo.message || "")}</span></span></div>
      ${d.videoPath ? `<div class="row2"><span>${esc(tr("settings.video_path"))}</span><span>${esc(d.videoPath)}</span></div>` : ""}
      <div class="row2"><span>${esc(tr("settings.updates"))}</span><span><span id="updState"></span> <button id="updCheck">${esc(tr("settings.check_now"))}</button></span></div>
      <div class="row2"><span>${esc(tr("settings.version"))}</span><span>${esc(tr("settings.version_line", { version: d.version, ytdlp: d.ytdlp, ffmpeg: d.ffmpeg ? tr("settings.included") : tr("settings.missing") }))}</span></div>
    </section>`;
  bindLangSelects();
  bindShare($("#settingsForm")); paintShare();
  $$("#settingsForm [data-k]").forEach((el) => el.addEventListener("change", async () => {
    const k = el.dataset.k; let v = el.type === "checkbox" ? el.checked : el.value;
    if (["pref_p", "workers"].includes(k)) v = Number(v);
    try { const r = await api("/api/settings", { settings: { [k]: v } }); toast(tr("settings.saved")); if (k === "yt_cookies") { loadSettings(); setTimeout(async () => { const e = $("#ytCookieState"); if (e) e.textContent = ytCookieText((await api("/api/settings")).youtube); }, 2500); } } catch (e) { toast(e.message); }
  }));
  $("#setChange").onclick = () => show("setup");
  $("#ytdlpCheck").onclick = async () => { $("#ytdlpMsg").textContent = tr("settings.checking"); await api("/api/ytdlp/check", {}); setTimeout(async () => { const i = (await api("/api/settings")).ytdlpInfo; $("#ytdlpMsg").textContent = tr("settings.ytdlp_result", { message: i.message || tr("common.done"), version: i.version, source: tr("about." + i.source) }); }, 4000); };
  const paintUpd = (u) => { $("#updState").textContent = u.checking ? tr("settings.checking") : u.status === "available" ? tr("settings.update_available", { version: u.latest }) : u.status === "current" ? tr("settings.update_ok", { version: u.current }) : u.status === "offline" ? tr("update.offline_banner") : u.status === "disabled" ? tr("settings.update_off", { version: u.current }) : tr("settings.checking"); };
  api("/api/update").then(paintUpd).catch(() => {});
  $("#updCheck").onclick = async () => { $("#updState").textContent = tr("settings.checking"); try { const u = await api("/api/update/check", {}); paintUpd(u); if (window.CHCUpdate) window.CHCUpdate.refresh(); } catch (e) { toast(e.message, 4000); } };
  api("/api/macperm").then((m) => {
    if (!m.applicable) return;
    const row = document.createElement("div"); row.className = "row2";
    row.innerHTML = `<span>${esc(tr("settings.fda"))}</span><span>${m.fda ? esc(tr("settings.fda_on")) : `${esc(tr("settings.fda_off"))} <button id="setFda">${esc(tr("settings.fda_grant"))}</button>`}</span>`;
    $("#setChange").closest(".row2").before(row);
    const btn = $("#setFda"); if (btn) btn.onclick = () => show("setup");
  }).catch(() => {});
  api("/api/desktop-entry").then((m) => {   // Linux AppImage: optional app-menu entry
    if (!m.applicable) return;
    const row = document.createElement("div"); row.className = "row2";
    row.innerHTML = `<span>${esc(tr("settings.desktop_entry"))}</span><span>${m.installed ? esc(tr("settings.desktop_entry_on")) : `<button id="setDeskEntry">${esc(tr("settings.desktop_entry_add"))}</button>`}</span>`;
    $("#setChange").closest(".row2").before(row);
    const btn = $("#setDeskEntry");
    if (btn) btn.onclick = async () => { const r = await api("/api/desktop-entry", {}); if (r.ok) { toast(tr("toast.desktop_entry_done")); btn.replaceWith(tr("settings.desktop_entry_on")); } else toast(tr("toast.desktop_entry_failed", { error: r.error || "" }), 6000); };
  }).catch(() => {});
  $("#setOpen").onclick = () => api("/api/open", {});
  $("#setRescan").onclick = async () => { await api("/api/library/rescan", {}); toast(tr("toast.rescanning")); };
}

/* ================================================================ setup (game & folders) */
const setup = { songs: [], detected: null, platform: "linux" };
const PLAT_KEY = { darwin: "mac", win32: "win", linux: "linux" };
async function checkPath(kind, path, statusSel) {
  const el = $(statusSel);
  if (!path) { el.textContent = ""; el.className = "status"; return null; }
  const d = await api("/api/setup/check", { kind, path });
  let msg;
  if (kind === "game") msg = d.ok ? tr("setup.game_ok") : tr("setup.game_bad");
  else if (kind === "data") msg = d.ok ? tr(d.settingsIni && d.logs ? "setup.data_found_both" : d.settingsIni ? "setup.data_found_ini" : "setup.data_found_logs")
    : d.exists ? tr("setup.data_empty") : tr("setup.data_missing");
  if (d.writable === false) msg = "✗ " + d.writeError;
  else if (d.writable === true) msg += tr("setup.write_ok");
  el.textContent = msg; el.className = "status " + (d.ok && d.writable !== false ? "ok" : "bad");
  return d;
}
function renderSongDirs() {
  $("#suSongs").innerHTML = setup.songs.length ? setup.songs.map((s, i) => `<li data-i="${i}"><span class="p">${esc(s.path)}</span>
    <span class="status ${s.ok && s.writable !== false ? (s.songs ? "ok" : "") : "bad"}">${s.writable === false ? esc(s.writeError) : s.ok ? esc(tr("setup.songs_found", { count: s.songs, n: nf(s.songs) + (s.songs >= 5000 ? "+" : "") }) + (s.writable ? tr("setup.write_ok") : "")) : esc(tr("setup.not_found"))}</span>
    <button class="ghost" data-rm="${i}" title="${esc(tr("setup.remove"))}">×</button></li>`).join("") : `<li class="hint">${esc(tr("setup.no_dirs"))}</li>`;
  const det = (setup.detected?.songDirs || []).filter((p) => !setup.songs.some((s) => s.path === p));
  $("#suSongsFound").innerHTML = det.length ? `<span class="hint">${esc(tr("setup.found"))}</span>` + det.map((p) => `<button data-addsong="${esc(p)}">+ ${esc(p)}</button>`).join("") : "";
}
async function addSongDir(p) {
  p = (p || "").trim();
  if (!p || setup.songs.some((s) => s.path === p)) return;
  const d = await api("/api/setup/check", { kind: "songs", path: p });
  setup.songs.push(d); renderSongDirs();
}
function foundChips(sel, list, target) {
  $(sel).innerHTML = list.length > 0 ? `<span class="hint">${esc(tr("setup.found"))}</span>` + list.map((p) => `<button data-use="${esc(p)}" data-target="${target}">${esc(p)}</button>`).join("") : `<span class="hint">${esc(tr("setup.none_found"))}</span>`;
}
async function loadFda() {
  let m; try { m = await api("/api/macperm"); } catch (e) { return; }
  $("#fdaCard").hidden = !m.applicable;
  if (!m.applicable) return;
  $("#fdaStatus").textContent = m.fda ? tr("setup.fda_on") : tr("setup.fda_off");
  $("#fdaStatus").className = "status " + (m.fda ? "ok" : "");
  $("#fdaOpen").hidden = !!m.fda;
  if (m.fda) { $("#fdaSteps").hidden = true; $("#fdaRestart").hidden = true; }
}
$("#fdaOpen").onclick = async () => {
  await api("/api/macperm/open", {});
  $("#fdaSteps").hidden = false; $("#fdaRestart").hidden = false;
  toast(tr("toast.fda_opened"), 4000);
};
$("#fdaRestart").onclick = async () => {
  const r = await api("/api/restart", {});
  toast(r.ok ? tr("toast.restarting") : tr("toast.restart_manual"), 5000);
};
async function loadSetup() {
  loadFda();
  const d = await api("/api/setup");
  setup.detected = d.detected;
  const pk = PLAT_KEY[d.detected.platform] || "linux";
  setup.platform = pk;
  $("#suGameHint").textContent = tr(`setup.hint.${pk}.game`); $("#suDataHint").textContent = tr(`setup.hint.${pk}.data`);
  $("#suGame").value = d.current.game || ""; $("#suData").value = d.current.dataDir || "";
  foundChips("#suGameFound", d.detected.games, "suGame");
  foundChips("#suDataFound", d.detected.dataDirs, "suData");
  setup.songs = await Promise.all(d.check.songs.map((s) => api("/api/setup/check", { kind: "songs", path: s.path })));
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
    if (r.path === null) { toast(tr("toast.paste_path"), 4000); input.focus(); return; }
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
    const st2 = $("#suSaveStatus"); st2.textContent = tr("setup.saving"); st2.className = "status";
    try {
      const r = await api("/api/setup", { game: $("#suGame").value, dataDir: $("#suData").value,
                                          songDirs: setup.songs.filter((s) => s.ok).map((s) => s.path), createData: true });
      st2.textContent = tr("setup.saved", { count: r.songDirs.length }); st2.className = "status ok";
      $("#navSetup").classList.remove("on");
      lib.version = -1; setTimeout(() => { loadLibrary(); setFilter("all"); show("library"); }, 1500);
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

/* ================================================================ cloud account, favourites in the cloud */
const cloud = { status: null, favs: [], counts: null };
const pct = (a) => (a == null ? "—" : new Intl.NumberFormat(I18N.lang, { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(a));
const ago = (ts) => I18N.ago(ts);
async function loadCloud() {
  const gen = ++cloudGen;
  let status, favs, counts = null;
  try { status = await api("/api/cloud/status"); } catch (e) { status = { signedIn: false }; }
  try { const d = await api("/api/cloud/favorites"); favs = d.items; counts = d.counts || null; } catch (e) { favs = []; }   // signed out too: favourites whose chart was trashed or moved
  if (gen !== cloudGen) return cloud.status;
  cloud.status = status; cloud.favs = favs; cloud.counts = counts;
  updateFavCount();
  renderCloudFavs();
  return cloud.status;
}
function renderCloudFavs() {
  const box = $("#cloudFavs"), items = cloud.favs;
  box.hidden = !(st.filter === "fav" && currentView === "library" && items.length);
  if (box.hidden) return;
  const missing = items.filter((i) => i.notFound).length;
  box.innerHTML = `<header><b>${esc(cloud.status.signedIn ? tr("cloud.in_cloud") : tr("cloud.not_here"))} (${items.length})</b>
      <label class="inline"><input type="checkbox" id="cfProcess" checked> <span>${esc(tr("charts.then_process"))}</span></label>
      <button class="primary" id="cfAll">${esc(tr("cloud.install_all"))}</button></header>
    <div class="cfgrid">${items.map((i) => `<div class="cf"><div><b>${esc(i.name)}</b><span>${esc(i.artist)}${i.artist && i.charter ? " · " : ""}${i.charter ? esc(i.charter) : ""}</span>
        ${i.notFound ? `<em>${esc(tr("cloud.not_found_online"))}</em>` : ""}${i.unknown ? `<em>${esc(tr("cloud.no_details"))}</em>` : ""}</div>
        <div class="cfbtns"><button data-k="${esc(i.key)}"${i.unknown ? " disabled" : ""}>${esc(tr("cloud.install"))}</button>${i.unknown ? `<button class="rm" data-rm="${esc(i.key)}">${esc(tr("cloud.remove"))}</button>` : ""}</div></div>`).join("")}</div>
    ${missing ? `<span class="hint">${esc(tr("cloud.missing_note", { count: missing }))}</span>` : ""}`;
  const go = async (keys, btn) => {
    btn.disabled = true;
    try {
      const r = await api("/api/cloud/favorites/install", { keys, process: $("#cfProcess").checked });
      toast(tr("toast.downloading", { count: r.queued }) + (r.notFound.length ? " · " + tr("toast.not_found_online", { count: r.notFound.length }) : "") + (r.failed ? " · " + tr("cloud.lookup_failed", { count: r.failed }) : ""), 5000);
      pollJobs(true); await loadCloud();
    } catch (e) { toast(e.message, 4500); }
    btn.disabled = false;
  };
  $("#cfAll").onclick = (e) => go([], e.target);
  $$("#cloudFavs .cf button[data-k]").forEach((b) => b.onclick = () => go([b.dataset.k], b));
  $$("#cloudFavs .cf button[data-rm]").forEach((b) => b.onclick = async () => {
    b.disabled = true;
    try { await api("/api/cloud/favorites/remove", { keys: [b.dataset.rm] }); toast(tr("toast.removed_fav")); await loadCloud(); lib.version = -1; }
    catch (e) { toast(e.message, 4500); b.disabled = false; }
  });
}
let cloudPoll = null;
function watchLogin() {
  clearInterval(cloudPoll); let n = 0;
  cloudPoll = setInterval(async () => {
    const s = await loadCloud();
    if (s.signedIn || ++n > 90) { clearInterval(cloudPoll); if (s.signedIn) { toast(tr("toast.signed_in")); if (currentView === "profile") loadProfile(); lib.version = -1; } }
  }, 2000);
}
async function signIn() {
  try { await api("/api/cloud/login", {}); toast(tr("toast.finish_google"), 5000); watchLogin(); } catch (e) { toast(e.message, 4500); }
}
async function signOut() { await api("/api/cloud/logout", {}); await loadCloud(); toast(tr("toast.signed_out")); if (currentView === "profile") loadProfile(); }
async function syncNow() {
  await api("/api/cloud/sync", {}); toast(tr("toast.syncing"));
  setTimeout(async () => { await loadCloud(); if (currentView === "profile") loadProfile(); lib.version = -1; }, 3500);
}


/* ================================================================ friends and sharing */
const fr = { status: null, view: null, page: null, data: null, busy: false };
async function pollFriends() {
  try {
    fr.status = await api("/api/friends/status");
  } catch (e) { fr.status = null; }
  const n = fr.status ? fr.status.incoming : 0;
  const b = $("#navFriends"); b.textContent = n || ""; b.title = n ? tr("friends.requests", { count: n }) : "";
  paintShare();
}
function usernameBox() {
  return `<h3>${esc(tr("friends.username_title"))}</h3><p class="hint">${esc(tr("friends.username_text"))}</p>
    <div class="fr-add"><input type="text" id="unIn" maxlength="20" autocomplete="off" spellcheck="false" placeholder="${esc(tr("friends.username_ph"))}">
    <button class="primary" id="unSave" disabled>${esc(tr("friends.username_save"))}</button></div><p class="hint" id="unMsg"></p>`;
}
function bindUsernameBox(root, done) {
  const inp = $("#unIn", root), save = $("#unSave", root), msg = $("#unMsg", root);
  let timer = null, seq = 0;
  inp.oninput = () => {
    save.disabled = true; msg.textContent = "";
    clearTimeout(timer);
    const v = inp.value.trim();
    if (!v) return;
    timer = setTimeout(async () => {
      const my = ++seq;
      try {
        const r = await api(`/api/friends/username/check?name=${encodeURIComponent(v)}`);
        if (my !== seq) return;
        msg.textContent = tr(!r.valid ? "friends.username_invalid" : r.available ? "friends.username_ok" : "friends.username_taken");
        save.disabled = !(r.valid && r.available);
      } catch (e) { if (my === seq) msg.textContent = e.message; }
    }, 350);
  };
  save.onclick = async () => {
    save.disabled = true;
    try { await api("/api/friends/username", { username: inp.value }); toast(tr("friends.username_saved")); pollFriends(); done(); }
    catch (e) { msg.textContent = e.message; }
  };
}
const avatar = (p) => `<div class="fr-ava"${p.avatar ? ` style="background-image:url('${esc(p.avatar)}')"` : ""}>${p.avatar ? "" : esc((p.name || p.username || "?")[0].toUpperCase())}</div>`;
const who = (p) => `<div class="who"><b>${esc(p.name || p.username || "?")}</b><span>${p.username ? "@" + esc(p.username) : ""}</span></div>`;
async function loadFriends(keepPage) {
  const body = $("#friendsBody");
  if (!keepPage) fr.page = null;
  if (!cloud.status) await loadCloud();
  if (!cloud.status || !cloud.status.signedIn) {
    body.innerHTML = `<h2>${esc(tr("friends.title"))}</h2><div class="fr-card"><h3>${esc(tr("friends.signed_out_title"))}</h3><p class="hint">${esc(tr("friends.signed_out_text"))}</p>
      <button class="primary" id="frGo">${esc(tr("friends.go_profile"))}</button></div>`;
    $("#frGo").onclick = () => show("profile");
    return;
  }
  let v;
  try { v = await api("/api/friends"); } catch (e) { v = await api("/api/friends?cached=1").catch(() => null); if (!v) return toast(e.message, 4500); v.offline = true; }
  fr.view = v;
  if (fr.page) return renderFriendPage();
  const me = v.me || {};
  const offline = v.offline ? `<div class="fr-banner">${esc(tr("friends.offline"))}</div>` : "";
  const dis = v.offline ? " disabled" : "";
  fr.status = { ...(fr.status || {}), sharing: v.sharing, friends: v.friends.length };
  body.innerHTML = `<h2>${esc(tr("friends.title"))}</h2>${offline}
    <div class="fr-card fr-share"><label class="inline"><input type="checkbox" class="shareSw"${dis}> <b>${esc(tr("share.switch"))}</b></label>
      <p class="hint">${esc(tr("share.switch_help"))}</p><p class="hint shareStatus"></p>
      <label class="inline"><input type="checkbox" id="statsSw"${dis}> <b>${esc(tr("share.stats_switch"))}</b></label>
      <p class="hint">${esc(tr("share.stats_help"))}</p></div>
    ${v.needsUsername ? `<div class="fr-card" id="frUserBox">${usernameBox()}</div>` : ""}
    <div class="fr-card"><h3>${esc(tr("friends.your_id"))}</h3><div class="fr-id">
      <div><small>${esc(tr("friends.username"))}</small><b>${me.username ? "@" + esc(me.username) : esc(tr("friends.no_username"))}</b></div>
      <div><small>${esc(tr("friends.friend_code"))}</small><b id="frCode">${esc(me.friend_code || "—")}</b></div>
      <button id="frCopy"${me.friend_code ? "" : " disabled"}>${esc(tr("friends.copy"))}</button></div></div>
    <div class="fr-card"><h3>${esc(tr("friends.add_title"))}</h3><div class="fr-add"><input type="text" id="frQuery" maxlength="40" autocomplete="off" spellcheck="false" placeholder="${esc(tr("friends.add_ph"))}"${dis}>
      <button class="primary" id="frSend"${dis}>${esc(tr("friends.add_btn"))}</button></div></div>
    ${v.incoming.length ? `<div class="fr-card"><h3>${esc(tr("friends.incoming"))}</h3>${v.incoming.map((p) => `<div class="fr-row">${avatar(p)}${who(p)}
      <button class="primary" data-acc="${esc(p.id)}"${dis}>${esc(tr("friends.accept"))}</button><button data-rm="${esc(p.id)}"${dis}>${esc(tr("friends.decline"))}</button></div>`).join("")}</div>` : ""}
    ${v.outgoing.length ? `<div class="fr-card"><h3>${esc(tr("friends.outgoing"))}</h3>${v.outgoing.map((p) => `<div class="fr-row">${avatar(p)}${who(p)}
      <button data-rm="${esc(p.id)}"${dis}>${esc(tr("friends.cancel"))}</button></div>`).join("")}</div>` : ""}
    <div class="fr-card"><h3>${esc(tr("friends.list_title"))}</h3>${v.friends.length ? v.friends.map((p) => `<div class="fr-row click" data-open="${esc(p.id)}">${avatar(p)}${who(p)}
      <button data-prof="${esc(p.id)}">${esc(tr("friends.view_profile"))}</button><button data-unf="${esc(p.id)}" data-nm="${esc(p.name || p.username || "")}"${dis}>${esc(tr("friends.unfriend"))}</button></div>`).join("") : `<p class="hint">${esc(tr("friends.none"))}</p>`}</div>`;
  bindShare(body); paintShare();
  $$("#friendsBody [data-prof]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); profNext = { friendId: b.dataset.prof }; show("profile"); });
  if ($("#frUserBox")) bindUsernameBox($("#frUserBox"), () => loadFriends());
  $("#frCopy").onclick = async () => {
    try { await navigator.clipboard.writeText(me.friend_code); toast(tr("friends.copied")); }
    catch (e) { const r = document.createRange(); r.selectNodeContents($("#frCode")); getSelection().removeAllRanges(); getSelection().addRange(r); }
  };
  const send = async () => {
    const q = $("#frQuery").value.trim(); if (!q) return;
    $("#frSend").disabled = true;
    try { const r = await api("/api/friends/request", { query: q }); toast(tr(r.status === "accepted" ? "friends.now_friends" : "friends.requested")); await pollFriends(); }
    catch (e) { toast(e.message, 4500); }
    loadFriends();
  };
  if (!v.offline) { $("#frSend").onclick = send; $("#frQuery").onkeydown = (e) => { if (e.key === "Enter") send(); }; }
  $$("#friendsBody [data-acc]").forEach((b) => b.onclick = async () => { b.disabled = true; try { await api("/api/friends/respond", { id: b.dataset.acc, accept: true }); } catch (e) { toast(e.message, 4500); } await pollFriends(); loadFriends(); });
  $$("#friendsBody [data-rm]").forEach((b) => b.onclick = async () => { b.disabled = true; try { await api("/api/friends/remove", { id: b.dataset.rm }); } catch (e) { toast(e.message, 4500); } await pollFriends(); loadFriends(); });
  $$("#friendsBody [data-unf]").forEach((b) => b.onclick = async (e) => {
    e.stopPropagation();
    if (!await ask({ title: tr("friends.unfriend_title", { name: b.dataset.nm }), text: tr("friends.unfriend_text"), ok: tr("friends.unfriend_ok"), danger: true })) return;
    try { await api("/api/friends/remove", { id: b.dataset.unf }); } catch (err) { toast(err.message, 4500); }
    await pollFriends(); loadFriends();
  });
  $$("#friendsBody [data-open]").forEach((r) => r.onclick = () => { fr.page = r.dataset.open; renderFriendPage(); });
}
async function renderFriendPage() {
  const body = $("#friendsBody");
  body.innerHTML = `<p class="hint">${esc(tr("common.loading"))}</p>`;
  let d;
  try { d = await api(`/api/friends/list?friend=${encodeURIComponent(fr.page)}`); }
  catch (e) { toast(e.message, 4500); fr.page = null; return loadFriends(); }
  fr.data = d;
  const p = d.friend, ro = !!d.offline, dis = ro ? " disabled" : "", fname = p.name || p.username || "";
  const head = `<div class="fr-tools"><button id="frBack">${esc(tr("friends.back"))}</button></div><div class="fr-row">${avatar(p)}${who(p)}</div>
    ${ro ? `<div class="fr-banner">${esc(tr("friends.offline"))}</div>` : ""}`;
  const back = () => { $("#frBack").onclick = () => { fr.page = null; loadFriends(); }; };
  if (!d.shared || (!d.favorites.length && !d.setlists.length)) {
    body.innerHTML = head + `<p class="hint">${esc(tr("friends.nothing_shared", { name: fname }))}</p>`;
    return back();
  }
  const list = (key, title, items, isFav) => {
    const miss = items.filter((x) => !x.present).length;
    const sum = `${tr("friends.song_count", { count: items.length })}${miss ? " · " + tr("friends.missing_count", { count: miss }) : ""}`;
    return `<details class="fr-card" data-list="${esc(key)}" data-title="${esc(title.toLowerCase())}"><summary><b>${esc(title)}</b> <span class="hint">${esc(sum)}</span></summary>
      <div class="fr-tools"><label class="inline"><input type="checkbox" class="frProc" checked> <span>${esc(tr("charts.then_process"))}</span></label>
        <button data-do="install"${miss && !ro ? "" : " disabled"}>${esc(tr("friends.install_missing"))}</button>
        <button class="primary" data-do="setlist"${ro || !items.length ? " disabled" : ""}>${esc(tr("friends.copy_setlist"))}</button>
        <button data-do="favs"${ro || !items.length ? " disabled" : ""}>${esc(tr("friends.add_favs"))}</button></div>
      ${items.map((x) => `<div class="fr-song" data-q="${esc((x.name + " " + x.artist + " " + x.charter).toLowerCase())}"><div class="t"><b>${esc(x.name)}</b><span>${esc(x.artist)}${x.artist && x.charter ? " · " : ""}${esc(x.charter)}</span></div>
        <span class="${x.present ? "have" : "miss"}">${esc(tr(x.present ? "friends.in_library" : "friends.not_installed"))}</span>
        <button data-dl="${esc(x.song_key)}"${x.present || ro ? " disabled" : ""}>${esc(tr("friends.song_download"))}</button>
        <button data-fav="${esc(x.song_key)}"${dis}>${esc(tr("friends.add_fav"))}</button>
        <button data-sl="${esc(x.song_key)}"${dis}>${esc(tr("lib.act.setlist"))}</button></div>`).join("")}</details>`;
  };
  const cards = (d.favorites.length ? list("fav", tr("friends.fav_title"), d.favorites, true) : "") +
    d.setlists.map((s, i) => list("sl:" + i, s.name, s.items, false)).join("");
  body.innerHTML = head + `<div class="fr-card"><b>${esc(fname)}</b>
      <p class="hint">${esc(tr("friends.fav_count", { count: d.favorites.length }))} · ${esc(tr("friends.sets_count", { count: d.setlists.length }))}</p>
      <input type="search" id="frFilter" placeholder="${esc(tr("friends.filter_ph"))}" autocomplete="off"></div>
    <div id="frLists">${cards}</div><p class="hint" id="frNoMatch" hidden>${esc(tr("friends.no_match"))}</p>`;
  back();
  $("#frFilter").oninput = (e) => {
    const q = e.target.value.trim().toLowerCase(); let any = false;
    $$("#frLists .fr-card").forEach((card) => {
      const t = !q || card.dataset.title.includes(q); let hit = t;
      $$(".fr-song", card).forEach((r) => { const m = t || r.dataset.q.includes(q); r.hidden = !m; hit = hit || m; });
      card.hidden = !hit; if (q && hit) card.open = true; any = any || hit;
    });
    $("#frNoMatch").hidden = any;
  };
  $$("#frLists .fr-card").forEach((card) => {
    const key = card.dataset.list, isFav = key === "fav";
    const spec = isFav ? { kind: "favorites" } : { kind: "setlist", name: d.setlists[Number(key.slice(3))].name };
    const title = isFav ? tr("friends.fav_title") : spec.name;
    const proc = () => card.querySelector(".frProc").checked;
    const go = async (extra, btn) => {
      btn.disabled = true;
      try {
        const r = await api("/api/friends/install", { friend: fr.page, list: spec, process: proc(), ...extra });
        let m = r.queued ? tr("toast.downloading", { count: r.queued }) : "";
        if (r.setlist) m += (m ? " · " : "") + tr("friends.setlist_queued", { name: r.setlist });
        if (r.notFound.length) m += (m ? " · " : "") + tr("toast.not_found_online", { count: r.notFound.length });
        if (r.failed) m += (m ? " · " : "") + tr("cloud.lookup_failed", { count: r.failed });
        const jobs = r.queued || r.setlist;
        toast((m || tr("friends.nothing_to_do")) + (jobs ? " · " + tr("friends.view_jobs") : ""), 6000, jobs ? () => show("jobs") : null);
        if (jobs) { fr.watch = true; fr.watchBusy = true; }
        pollJobs(true); lib.version = -1; loadCloud();
      } catch (e) { toast(e.message, 4500); }
      renderFriendPage();
    };
    $$("[data-do]", card).forEach((b) => b.onclick = () => {
      const act = b.dataset.do;
      if (act === "install") return go({}, b);
      if (act === "favs") return go({ favourite: true }, b);
      go({ setlist: title, favourite: false }, b);
    });
    $$("[data-dl]", card).forEach((b) => b.onclick = () => go({ keys: [b.dataset.dl] }, b));
    $$("[data-fav]", card).forEach((b) => b.onclick = () => go({ keys: [b.dataset.fav], favourite: true }, b));
    $$("[data-sl]", card).forEach((b) => b.onclick = async () => {
      let mine = [];
      try { mine = (await api("/api/setlists")).setlists; } catch (e) { /* new setlist only */ }
      const r = await ask({ title: tr("setlists.add_title", { count: 1 }), text: tr("setlists.add_text"),
        select: [{ value: "", label: tr("setlists.new_option") }, ...mine.map((x) => ({ value: x.name, label: x.name }))], input: "", ok: tr("setlists.add") });
      if (!r) return;
      const typed = r.input?.trim(), name = typed || r.select;
      if (!name) return toast(tr("setlists.need_name"));
      go(mine.some((x) => x.name === name) ? { keys: [b.dataset.sl], addToSetlist: name } : { keys: [b.dataset.sl], setlist: name }, b);
    });
  });
}

/* ================================================================ share your stats (card image + social buttons) */
const SITE = "cloneherocompanion.com";
const CARD_W = 1080, CARD_H = 1350;
/* A highlight from /api/stats (catalog keys + raw vars) as translated { title, detail }. */
function accText(a) {
  const v = { ...(a.vars || {}) };
  if (v.diff != null) v.diff = dn(v.diff);
  if (v.inst != null) v.inst = inn(v.inst);
  return { title: tr(a.title, v), detail: tr(a.detail, v) };
}
/* Text drawn on the canvas always fits its box: shrink the font down to minPx, then cut with an ellipsis. */
function fitText(ctx, text, maxW, px, weight, family, minPx = 0.6) {
  let size = px;
  const set = () => { ctx.font = `${weight} ${size}px ${family}`; };
  set();
  while (size > px * minPx && ctx.measureText(text).width > maxW) { size -= 1; set(); }
  let t = String(text);
  if (ctx.measureText(t).width > maxW) {
    const chars = Array.from(t);
    while (chars.length > 1 && ctx.measureText(chars.join("") + "…").width > maxW) chars.pop();
    t = chars.join("") + "…";
  }
  return t;
}
function loadAvatar(url) {
  return new Promise((resolve) => {
    if (!url) return resolve(null);
    const img = new Image();
    const done = (v) => { clearTimeout(timer); resolve(v); };
    const timer = setTimeout(() => done(null), 3500);
    img.crossOrigin = "anonymous";        // not CORS-safe -> onerror -> initials (a tainted canvas could not be exported)
    img.onload = () => done(img);
    img.onerror = () => done(null);
    img.src = url;
  });
}
function drawStatsCard(canvas, c) {
  const ctx = canvas.getContext("2d"), css = getComputedStyle(document.documentElement);
  const col = (n, d) => css.getPropertyValue(n).trim() || d;
  const stage = col("--stage", "#160d2b"), panel = col("--panel", "#1e1338"), panel3 = col("--panel-3", "#352460"),
    ink = col("--ink", "#f6f1e7"), dim = "rgba(246,241,231,.62)", spot = col("--spot", "#ffb52e"), spotInk = col("--spot-ink", "#241500");
  const UI = col("--ui", "sans-serif"), DISP = col("--display", "sans-serif");
  const M = 72, W = CARD_W, H = CARD_H;
  canvas.width = W; canvas.height = H;
  ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
  ctx.fillStyle = stage; ctx.fillRect(0, 0, W, H);
  const g = ctx.createLinearGradient(0, 0, 0, 420); g.addColorStop(0, panel3); g.addColorStop(1, stage);
  ctx.fillStyle = g; ctx.fillRect(0, 0, W, 420);
  const text = (s, x, y, maxW, px, weight, family, color, align = "left") => {
    const t = fitText(ctx, s, maxW, px, weight, family);
    ctx.fillStyle = color; ctx.textAlign = align; ctx.fillText(t, x, y); ctx.textAlign = "left";
  };
  // brand: the five fret dots of the sidebar logo + the app name
  ["--green", "--red", "--yellow", "--blue", "--orange"].forEach((n, i) => {
    ctx.fillStyle = col(n, ["#3cc85a", "#e8413a", "#f2d23a", "#3a8ee8", "#f08a2a"][i]);
    ctx.beginPath(); ctx.arc(M + 14 + i * 34, M + 8, 14, 0, Math.PI * 2); ctx.fill();
  });
  text("Clone Hero Companion", M + 5 * 34 + 14, M + 22, W - M * 2 - 5 * 34 - 14, 46, 800, DISP, ink);
  // identity
  const R = 84, cx = M + R, cy = 270;
  ctx.save(); ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.closePath(); ctx.clip();
  if (c.avatar) ctx.drawImage(c.avatar, cx - R, cy - R, R * 2, R * 2);
  else { ctx.fillStyle = panel3; ctx.fillRect(cx - R, cy - R, R * 2, R * 2); }
  ctx.restore();
  if (!c.avatar) {
    ctx.fillStyle = spot; ctx.textAlign = "center"; ctx.font = `900 96px ${DISP}`;
    ctx.fillText(Array.from(c.name || "?")[0].toUpperCase(), cx, cy + 32); ctx.textAlign = "left";
  }
  const tx = M + R * 2 + 36, tw = W - M - tx;
  text(c.name, tx, cy + 6, tw, 84, 900, DISP, ink);
  text(c.sub, tx, cy + 56, tw, 34, 500, UI, dim);
  // headline tiles (2 x 2)
  const gap = 24, tw2 = (W - M * 2 - gap) / 2, th = 190, ty0 = 410;
  c.tiles.forEach((t, i) => {
    const x = M + (i % 2) * (tw2 + gap), y = ty0 + Math.floor(i / 2) * (th + gap);
    ctx.fillStyle = panel; ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(x, y, tw2, th, 24); else ctx.rect(x, y, tw2, th); ctx.fill();
    ctx.strokeStyle = "rgba(246,241,231,.09)"; ctx.lineWidth = 2; ctx.stroke();
    text(t.value, x + 32, y + 108, tw2 - 64, 100, 900, DISP, ink);
    text(t.label, x + 32, y + 158, tw2 - 64, 34, 500, UI, dim);
  });
  // highlights
  let y = ty0 + th * 2 + gap + 78;
  text(c.accTitle, M, y, W - M * 2, 38, 800, DISP, spot);
  y += 18;
  const kindColor = { fc: "#3cc85a", five_star: "#f2d23a", pb: "#3a8ee8", streak: "#f08a2a", milestone: "#e8413a" };
  if (!c.acc.length) text(c.accNone, M, y + 60, W - M * 2, 32, 500, UI, dim);
  c.acc.slice(0, 3).forEach((a, i) => {
    const ry = y + i * 108;
    ctx.fillStyle = kindColor[a.kind] || spot; ctx.beginPath(); ctx.arc(M + 14, ry + 54, 14, 0, Math.PI * 2); ctx.fill();
    text(a.title, M + 48, ry + 52, W - M * 2 - 48, 38, 700, UI, ink);
    text(a.detail, M + 48, ry + 92, W - M * 2 - 48, 30, 500, UI, dim);
  });
  // footer
  ctx.fillStyle = spot; ctx.fillRect(M, H - 100, W - M * 2, 3);
  text(SITE, W / 2, H - 42, W - M * 2, 42, 700, UI, spot, "center");
  void spotInk;
}
function shareData(d, cs) {
  const T = d.totals, name = (cs && cs.signedIn && (cs.name || cs.email)) || tr("share.card_player");
  const inst = d.instrument ? inn(d.instrument) : "", diff = d.difficulty ? dn(d.difficulty) : "";
  const acc = (d.accomplishments || []).map((a) => ({ kind: a.kind, ...accText(a) }));
  const latest = acc[0] ? acc[0].title : "";
  const text = [tr("share.text", { plays: nf(T.plays), hours: T.hours, fcs: nf(T.fcs), streak: T.streak }),
    latest ? tr("share.text_latest", { what: latest }) : "", tr("share.text_end")].filter(Boolean).join(" ");
  return {
    name, avatarUrl: cs && cs.signedIn ? cs.avatar : "", acc, text,
    sub: [inst, diff].filter(Boolean).join(" · ") || tr("share.card_sub"),
    tiles: [{ value: nf(T.plays), label: tr("profile.tile.plays") }, { value: String(T.hours), label: tr("profile.tile.hours") },
      { value: nf(T.fcs), label: tr("profile.tile.fcs") }, { value: String(T.streak), label: tr("profile.tile.streak") }],
    accTitle: tr("share.card_highlights"), accNone: tr("share.card_none"),
  };
}
async function openShare(d, cs) {
  let dlg = $("#shareDlg"); const form = $("#shareForm");
  const c = shareData(d, cs);
  form.innerHTML = `<h2>${esc(tr("share.title"))}</h2>
    <div class="shareBox"><div class="sharePrev"><canvas id="shareCanvas" role="img" aria-label="${esc(tr("share.preview_alt"))}"></canvas></div>
      <div class="shareBtns" id="shareBtns"><p class="hint">${esc(tr("common.loading"))}</p></div></div>
    <div class="btns"><button value="close" class="ghost">${esc(tr("common.close"))}</button></div>`;
  dlg.showModal();
  const canvas = $("#shareCanvas");
  c.avatar = await loadAvatar(c.avatarUrl);
  drawStatsCard(canvas, c);
  const blob = await new Promise((r) => canvas.toBlob(r, "image/png"));
  const file = blob ? new File([blob], "CloneHeroCompanion-stats.png", { type: "image/png" }) : null;
  const dataUrl = () => canvas.toDataURL("image/png");
  const saveImage = async (reveal) => { const r = await api("/api/share/save", { png_base64: dataUrl(), reveal }); return r.path; };
  const folderOf = (p) => (p.split(/[\\/]/).slice(-2, -1)[0] || "");
  const copyText = async () => { try { await navigator.clipboard.writeText(c.text); return true; } catch (e) { return false; } };
  const guard = (fn) => async (e) => {
    const b = e.currentTarget; b.disabled = true;
    try { await fn(); } catch (err) { toast(err.message || String(err), 4500); }
    b.disabled = false;
  };
  const open = (url) => api("/api/open", { url });
  const platforms = [
    { id: "whatsapp", label: "WhatsApp", url: () => `https://wa.me/?text=${encodeURIComponent(c.text)}` },
    { id: "reddit", label: "Reddit", url: () => `https://www.reddit.com/submit?title=${encodeURIComponent(tr("share.reddit_title"))}&url=https://${SITE}` },
    { id: "facebook", label: "Facebook", url: () => `https://www.facebook.com/sharer/sharer.php?u=https://${SITE}&quote=${encodeURIComponent(c.text)}` },
    { id: "instagram", label: "Instagram", url: () => "https://www.instagram.com/", story: true },
    { id: "snapchat", label: "Snapchat", url: () => "https://www.snapchat.com/", story: true },
  ];
  const canCopyImg = !!(window.ClipboardItem && navigator.clipboard && navigator.clipboard.write);
  const canSys = !!(file && navigator.canShare && navigator.canShare({ files: [file] }));
  $("#shareBtns").innerHTML = `<button class="primary" data-a="save">${esc(tr("share.save"))}</button>
    ${canCopyImg ? `<button data-a="copy">${esc(tr("share.copy"))}</button>` : ""}
    ${canSys ? `<button data-a="sys">${esc(tr("share.system"))}</button>` : ""}
    <p class="hint">${esc(tr("share.platforms"))}</p>
    ${platforms.map((p) => `<button data-p="${p.id}">${esc(p.label)}</button>`).join("")}`;
  $("[data-a=save]", dlg).onclick = guard(async () => { const p = await saveImage(true); toast(tr("share.saved", { folder: folderOf(p) || p }), 4500); });
  const cp = $("[data-a=copy]", dlg);
  if (cp) cp.onclick = guard(async () => {
    try { await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]); toast(tr("share.copied")); }
    catch (e) { toast(tr("share.copy_unsupported"), 4500); cp.hidden = true; }
  });
  const sys = $("[data-a=sys]", dlg);
  if (sys) sys.onclick = guard(async () => { try { await navigator.share({ files: [file], text: c.text }); } catch (e) { if (e.name !== "AbortError") throw e; } });
  platforms.forEach((p) => {
    $(`[data-p=${p.id}]`, dlg).onclick = guard(async () => {
      const path = await saveImage(!!p.story);       // every platform button saves the image and copies the text first
      await copyText();
      await open(p.url());
      toast(p.story ? tr("share.story_hint", { folder: folderOf(path) || path }) : tr("share.attach_hint", { folder: folderOf(path) || path }), 6000);
    });
  });
}

/* ================================================================ profile */
const prof = { inst: "", diff: "" }, profF = { inst: "", diff: "" };      // own filter, a friend's filter
let profNext = null, profCur = {};        // one-shot options for the next show("profile"): { friendId }
function openSong(it) {
  if (it.songId && lib.byId.has(it.songId)) {
    show("library"); setFilter("all"); st.q = ""; $("#libSearch").value = ""; applyFilters();
    const i = rows.findIndex((s) => s.id === it.songId);
    if (i >= 0) $("#libTable .tbody, #libBody").scrollTop = Math.max(0, i * ROW - 120);
    openDetail(it.songId);
  } else {
    show("library"); setFilter("all"); st.q = it.name || ""; $("#libSearch").value = st.q; applyFilters();
    toast(tr("toast.not_in_library"), 3000);
  }
}
function songList(items, right, empty) {
  if (!items.length) return `<p class="hint">${esc(empty)}</p>`;
  return `<ul class="plist">${items.map((it, i) => `<li data-i="${i}"><div class="t">${esc(it.name)}<small>${esc(it.artist)}</small></div><div class="v">${right(it)}</div></li>`).join("")}</ul>`;
}
/* The Profile view. opts.friendId shows that friend's shared stats read-only (no sign-in, CH account, share or edit controls). */
async function loadProfile(opts = {}) {
  const body = $("#profileBody"), fid = opts.friendId || "", P = fid ? profF : prof;
  profCur = opts;
  const q = `instrument=${encodeURIComponent(P.inst)}&difficulty=${encodeURIComponent(P.diff)}`;
  if (fid) {
    let d;
    try { d = await api(`/api/friends/stats?id=${encodeURIComponent(fid)}&${q}`); }
    catch (e) {
      const fp = (fr.view && fr.view.friends || []).find((x) => x.id === fid) || {};
      body.innerHTML = `<h2>${esc(tr("profile.title"))}</h2><div class="fr-tools"><button id="pBack">${esc(tr("friends.back"))}</button></div>
        <p class="hint">${esc(e.message === "not_shared" ? tr("friends.stats_not_shared", { name: fp.name || fp.username || "?" }) : e.message)}</p>`;
      $("#pBack").onclick = () => show("friends");
      return;
    }
    return renderProfile(body, d, null, null, opts);
  }
  const [cs, d, fv] = await Promise.all([loadCloud(), api(`/api/stats?${q}`), api("/api/friends").catch(() => null)]);
  return renderProfile(body, d, cs, fv, opts);
}
function renderProfile(body, d, cs, fv, opts) {
  const fid = opts.friendId || "", P = fid ? profF : prof, ro = !!fid;
  const reload = () => loadProfile(opts);
  const T = d.totals, f = d.facets, H = d.heatmap;
  const A = d.account && d.account.used ? d.account : null, AR = (A && A.ranks) || {};
  const rk = (k) => (A && AR[k] ? " · #" + nf(AR[k]) : "");
  const max = Math.max(1, ...H.days.map((x) => x.n));
  const lvl = (n) => (n ? Math.min(4, 1 + Math.floor((n / max) * 3.999)) : 0);
  const bars = (arr, labels) => { const m = Math.max(1, ...arr); return `<div class="bars">${arr.map((v, i) => `<div title="${labels[i]}: ${v}"><i style="height:${Math.round((v / m) * 52)}px"></i>${labels[i]}</div>`).join("")}</div>`; };
  const fp = ro ? d.friend || {} : null;
  const head = ro
    ? `<div class="fr-tools"><button id="pBack">${esc(tr("friends.back"))}</button></div>
      <div class="p-head"><div class="ava"${fp.avatar ? ` style="background-image:url('${esc(fp.avatar)}')"` : ""}>${fp.avatar ? "" : esc((fp.name || fp.username || "?")[0].toUpperCase())}</div>
        <div class="who"><b>${esc(fp.name || fp.username || "?")}</b><span>${fp.username ? "@" + esc(fp.username) : ""}</span></div></div>`
    : cs.signedIn
    ? `<div class="p-head"><div class="ava"${cs.avatar ? ` style="background-image:url('${esc(cs.avatar)}')"` : ""}>${cs.avatar ? "" : esc((cs.name || cs.email || "?")[0].toUpperCase())}</div>
        <div class="who"><b>${esc(cs.name || cs.email)}</b><span>${esc(cs.email || "")} · <a href="#" id="pRename">${esc(tr("profile.edit_name"))}</a></span>
        <span>${esc(tr("profile.sync_line", { ago: ago(cs.lastSync) }))}${cs.pending ? ` · ${esc(tr("profile.pending", { count: cs.pending }))}` : ""}${cs.error ? ` · ${esc(tr("profile.offline"))}` : ""}</span></div>
        <div class="actions"><button class="primary" id="pShare">${esc(tr("share.btn"))}</button><button id="pSync">${esc(tr("profile.sync_now"))}</button><button id="pOut">${esc(tr("profile.sign_out"))}</button></div></div>`
    : `<div class="p-head cta"><div class="ava">♪</div><div class="who"><b>${esc(tr("profile.sign_in"))}</b><span>${esc(tr("profile.sign_in_text"))}</span>
        <div class="actions" style="margin-top:8px"><button class="primary" id="pIn">${esc(tr("profile.google"))}</button><button id="pShare">${esc(tr("share.btn"))}</button></div>
        <span style="margin-top:8px">${esc(tr("profile.or_email"))}</span>
        <div class="d-row"><input type="email" id="pEmail" placeholder="${esc(tr("profile.email_ph"))}" autocomplete="email"><button id="pSend">${esc(tr("profile.send_code"))}</button></div>
        <div class="d-row" id="pCodeRow" hidden><input type="text" id="pCode" inputmode="numeric" maxlength="6" placeholder="${esc(tr("profile.code_ph"))}" autocomplete="one-time-code"><button class="primary" id="pVerify">${esc(tr("profile.verify"))}</button></div>
        <span id="pMsg" class="hint"></span></div></div>`;
  const sel = (id, vals, cur, any, label) => `<select id="${id}"><option value="">${esc(any)}</option>${vals.map((v) => `<option value="${esc(v)}"${v === cur ? " selected" : ""}>${esc(label(v))}</option>`).join("")}</select>`;
  body.innerHTML = `<h2>${esc(tr("profile.title"))}</h2>${head}${!ro && cs.signedIn && fv && fv.needsUsername ? `<div class="fr-card" id="pUserBox">${usernameBox()}</div>` : ""}
    <div class="p-filter">${sel("pInst", f.instruments, P.inst, tr("profile.all_instruments"), inn)}${sel("pDiff", f.difficulties, P.diff, tr("profile.all_difficulties"), dn)}
      ${ro ? "" : `<span class="hint">${esc(tr("profile.plays_here", { local: nf(d.sources.local) }) + (d.sources.cloud ? tr("profile.plays_cloud", { cloud: nf(d.sources.cloud) }) : "") + ". " + tr("profile.plays_note"))}</span>`}</div>
    ${ro ? "" : `<p class="hint" id="pSrc" title="${esc(tr("profile.src_tip"))}">${esc(tr("profile.src_line", { plays: nf(d.sources.local + d.sources.cloud) }) + (d.ch.accountId || d.sources.leaderboard ? tr("profile.src_lb", { count: nf(d.sources.leaderboard) }) + (d.sources.leaderboardRefreshing ? tr("profile.src_loading") : "") : "") + ". " + tr("profile.src_end") + (A ? " " + tr("profile.src_account", { name: A.username }) : ""))}</p>`}
    <div class="tiles">
      <div class="tile"><b>${nf(T.plays)}</b><span>${esc(tr("profile.tile.plays"))}</span></div><div class="tile"><b>${T.hours}</b><span>${esc(tr("profile.tile.hours"))}</span></div>
      <div class="tile"><b>${nf(T.songs)}</b><span>${esc(tr("profile.tile.songs"))}</span></div><div class="tile"><b>${pct(T.avgAccuracy)}</b><span>${esc(tr("profile.tile.accuracy"))}</span></div>
      <div class="tile"><b>${nf(T.fcs)}</b><span>${esc(tr("profile.tile.fcs"))}${esc(rk("totalFcs"))}</span></div><div class="tile"><b>${nf((T.starCounts || {})[5] || 0)}</b><span>${esc(tr("profile.tile.stars5"))}</span></div><div class="tile"><b>${nf((T.starCounts || {})[6] || 0)}</b><span>${esc(tr("profile.tile.stars6"))}</span></div><div class="tile"><b>${nf((T.starCounts || {})[7] || 0)}</b><span>${esc(tr("profile.tile.stars7"))}</span></div>
      <div class="tile"><b>${nf(T.totalScore)}</b><span>${esc(tr("profile.tile.score"))}${esc(rk("totalScore"))}</span></div>
      ${ro ? "" : `<div class="tile"><b>${nf(T.notesHit)}</b><span>${esc(tr("profile.tile.notes"))}</span></div>`}
      <div class="tile"><b>${T.streak}</b><span>${esc(tr("profile.tile.streak"))}</span></div><div class="tile"><b>${T.longestStreak}</b><span>${esc(tr("profile.tile.longest"))}</span></div></div>
    <section><h3>${esc(tr("profile.activity"))}</h3><div class="heat">${H.days.map((x) => `<i class="l${lvl(x.n)}" title="${esc(tr("profile.heat_tip", { date: x.d, count: x.n }))}"></i>`).join("")}</div></section>
    <div class="p-cols"><section><h3>${esc(tr("profile.by_weekday"))}</h3>${bars(d.weekday, I18N.weekdays())}</section>
      <section><h3>${esc(tr("profile.by_hour"))}</h3>${bars(d.hour, d.hour.map((_, h) => (h % 3 === 0 ? String(h) : "")))}</section></div>
    <div class="p-cols"><section><h3>${esc(tr("profile.most_played"))}</h3><div id="lMost"></div></section><section><h3>${esc(tr("profile.best"))}</h3><div id="lBest"></div></section>
      <section><h3>${esc(tr("profile.needs"))}</h3><div id="lNeeds"></div></section><section><h3>${esc(tr("profile.pbs"))}</h3><div id="lPb"></div></section></div>
    <section><h3>${esc(tr("profile.recent"))}</h3><div id="lRecent"></div></section>
    ${ro ? "" : `<section id="chAcct"><h3>${esc(tr("profile.ch_account"))}</h3><div id="chBody"><p class="hint">${esc(tr("common.loading"))}</p></div></section>`}`;
  const fill = (id, items, right, empty) => {
    const el = $(id); el.innerHTML = songList(items, right, empty);
    $$("li", el).forEach((li) => li.onclick = () => openSong(items[Number(li.dataset.i)]));
  };
  const when = (ts) => I18N.date(ts);
  const tags = (it) => `${esc(dn(it.difficulty))} ${esc(inn(it.instrument))}${it.fc ? " · " + esc(tr("profile.fc")) : ""}`;
  fill("#lMost", d.mostPlayed, (it) => `${it.plays}×<small>${pct(it.avgAccuracy)}</small>`, tr("profile.empty.most"));
  fill("#lBest", d.best, (it) => `${pct(it.bestAccuracy)}<small>${it.plays}×</small>`, tr("profile.empty.best"));
  fill("#lNeeds", d.needsPractice, (it) => `${esc(tr("profile.best_pct", { pct: pct(it.bestAccuracy) }))}<small>${it.plays}×</small>`, tr("profile.empty.needs"));
  fill("#lPb", d.personalBests, (it) => `${nf(it.score)}<small>${tags(it)}</small>`, tr("profile.empty.pbs"));
  fill("#lRecent", d.recent, (it) => `${nf(it.score)} · ${"★".repeat(Math.min(5, it.stars || 0))}${(it.stars || 0) > 5 ? "+" : ""} · ${pct(it.accuracy)}<small>${tags(it)} · ${when(it.playedAt)}</small>`, tr("profile.empty.recent"));
  $("#pInst").onchange = (e) => { P.inst = e.target.value; reload(); };
  $("#pDiff").onchange = (e) => { P.diff = e.target.value; reload(); };
  if (ro) { $("#pBack").onclick = () => show("friends"); return; }
  $("#pShare").onclick = () => openShare(d, cs);
  if ($("#pUserBox")) bindUsernameBox($("#pUserBox"), () => loadProfile());
  if (cs.signedIn) {
    $("#pOut").onclick = signOut; $("#pSync").onclick = syncNow;
    $("#pRename").onclick = async (e) => {
      e.preventDefault();
      const r = await ask({ title: tr("profile.name_title"), input: cs.name || "", ok: tr("common.save") }); if (!r || !r.input.trim()) return;
      try { await api("/api/cloud/name", { name: r.input }); loadProfile(); } catch (err) { toast(err.message, 4500); }
    };
  } else {
    $("#pIn").onclick = signIn;
    const msg = (t) => { $("#pMsg").textContent = t; };
    let lastSend = 0, tick = null;
    const cool = () => {
      clearInterval(tick);
      tick = setInterval(() => {
        const left = 60 - Math.floor((Date.now() - lastSend) / 1000), b = $("#pSend");
        if (!b) return clearInterval(tick);
        b.disabled = left > 0; b.textContent = left > 0 ? tr("profile.resend", { secs: left }) : tr("profile.resend_code");
        if (left <= 0) clearInterval(tick);
      }, 500);
    };
    $("#pSend").onclick = async () => {
      const email = $("#pEmail").value.trim();
      try {
        $("#pSend").disabled = true; await api("/api/cloud/email/send", { email });
        lastSend = Date.now(); cool(); $("#pCodeRow").hidden = false; $("#pCode").focus(); msg(tr("profile.code_sent", { email }));
      } catch (err) { $("#pSend").disabled = false; msg(err.message); }
    };
    $("#pVerify").onclick = async () => {
      try { await api("/api/cloud/email/verify", { email: $("#pEmail").value.trim(), code: $("#pCode").value }); toast(tr("toast.signed_in")); lib.version = -1; loadProfile(); }
      catch (err) { msg(err.message); }
    };
    $("#pCode").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#pVerify").click(); });
  }
  if (d.sources.leaderboardRefreshing) setTimeout(() => { if ($("#pSrc")) loadProfile(); }, 4000);
  loadChAccount();
}
async function loadChAccount() {
  const box = $("#chBody"); if (!box) return;
  const form = (v) => `<div class="d-row"><input type="text" id="chUser" placeholder="${esc(tr("profile.ch_user_ph"))}" value="${esc(v || "")}"><button id="chLink" class="primary">${esc(v ? tr("profile.change") : tr("profile.link"))}</button>${v ? `<button id="chUnlink">${esc(tr("profile.unlink"))}</button>` : ""}</div>
    <p class="lbnote">${esc(tr("profile.ch_note"))}</p>`;
  const bind = () => {
    $("#chLink").onclick = async () => { try { await api("/api/lb/link", { username: $("#chUser").value }); toast(tr("toast.linked")); loadProfile(); } catch (e) { toast(e.message, 4500); } };
    const u = $("#chUnlink"); if (u) u.onclick = async () => { await api("/api/lb/link", { username: "" }); loadProfile(); };
  };
  try {
    const p = await api("/api/lb/profile");
    if (!p.linked) { box.innerHTML = form(""); return bind(); }
    box.innerHTML = form(p.username) + `
      <p><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(tr("profile.ch_open", { name: p.username }))}</a></p>
      ${p.recent.length ? `<ul class="plist">${p.recent.map((r) => `<li class="static"><div class="t">${esc(r.name)}<small>${esc(r.artist)}</small></div><div class="v">${nf(r.score)}<small>${esc(dn(r.difficulty))} ${esc(inn(r.instrument))}${r.fc ? " · " + esc(tr("profile.fc")) : ""}</small></div></li>`).join("")}</ul>` : ""}`;
    bind();
  } catch (e) { box.innerHTML = `<p class="hint">${esc(tr("profile.ch_unreachable", { detail: e.message }))}</p>` + form($("#chUser")?.value || ""); bind(); }
}

/* ================================================================ global top scores (song detail) */
async function loadDetailLb(d, inst, diff) {
  const box = $("#dLb"); if (!box) return;
  box.innerHTML = `<p class="hint">${esc(tr("common.loading"))}</p>`;
  try {
    const p = new URLSearchParams({ id: d.id }); if (inst) p.set("instrument", inst); if (diff) p.set("difficulty", diff);
    const r = await api("/api/lb/song?" + p);
    if (!$("#dLb") || activeId !== d.id) return;
    const insts = ["guitar", "bass", "rhythm", "drums", "keys", "guitarghl", "bassghl"];
    const sel = `<div class="d-row"><select id="dLbInst">${insts.map((i) => `<option value="${i}"${i === r.instrument ? " selected" : ""}>${esc(inn(i))}</option>`).join("")}</select>
      <select id="dLbDiff">${["expert", "hard", "medium", "easy"].map((i) => `<option value="${i}"${i === r.difficulty ? " selected" : ""}>${esc(dn(i))}</option>`).join("")}</select></div>`;
    const rows_ = r.found ? (r.scores.length ? r.scores.map((x) => `<div class="r${r.you && x.accountId === r.you.accountId ? " me" : ""}"><span>${x.placement}</span><span>${esc(x.username)}</span><span>${nf(x.score)}</span></div>`).join("")
      : `<p class="hint">${esc(tr("lb.top_none"))}</p>`) : `<p class="hint">${esc(tr("lb.not_on_lb"))}</p>`;
    const you = r.ch ? (r.you ? `<div class="r me"><span>#${r.you.placement}</span><span>${esc(tr("lb.you", { name: r.ch }))}</span><span>${nf(r.you.score)}</span></div>` : `<p class="hint">${esc(tr("lb.no_score", { name: r.ch }))}</p>`) : `<p class="hint">${esc(tr("lb.link_hint"))}</p>`;
    box.innerHTML = sel + `<div class="dlb">${rows_}${r.found ? you : ""}${r.found ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(tr("lb.open_on"))}</a>` : ""}</div>`;
    const re = () => loadDetailLb(d, $("#dLbInst").value, $("#dLbDiff").value);
    $("#dLbInst").onchange = re; $("#dLbDiff").onchange = re;
  } catch (e) { if ($("#dLb")) $("#dLb").innerHTML = `<p class="hint">${esc(tr("lb.unreachable_now"))}</p>`; }
}

/* ================================================================ CH leaderboards view */
const lbv = { metric: "totalScore", tab: "trending", group: "fiveFret" };
async function loadLeaderboards() {
  const body = $("#lbBody");
  body.innerHTML = `<h2>${esc(tr("lb.title"))}</h2><p class="hint">${esc(tr("lb.loading"))}</p>`;
  let d;
  try { d = await api(`/api/lb/overview?metric=${lbv.metric}&tab=${lbv.tab}&group=${lbv.group}`); }
  catch (e) { body.innerHTML = `<h2>${esc(tr("lb.title"))}</h2><p class="hint">${esc(tr("lb.unreachable_rest", { detail: e.message }))}</p>`; return; }
  const T = d.totals || {};
  const o = (vals, cur) => vals.map(([v, l]) => `<option value="${v}"${v === cur ? " selected" : ""}>${esc(l)}</option>`).join("");
  const metricFmt = lbv.metric === "hoursPlayed" ? (v) => nf(Math.round(v)) : nf;
  body.innerHTML = `<h2>${esc(tr("lb.title"))}</h2>
    <p class="lbnote">${tr("lb.readonly", { url: esc(d.site) })}</p>
    <div class="tiles"><div class="tile"><b>${nf(T.users)}</b><span>${esc(tr("lb.tile.players"))}</span></div><div class="tile"><b>${nf(T.submissions)}</b><span>${esc(tr("lb.tile.submitted"))}</span></div>
      <div class="tile"><b>${nf(T.songs)}</b><span>${esc(tr("lb.tile.songs"))}</span></div><div class="tile"><b>${nf(T.fcs)}</b><span>${esc(tr("lb.tile.fcs"))}</span></div>
      <div class="tile"><b>${nf(T.hoursPlayed)}</b><span>${esc(tr("lb.tile.hours"))}</span></div><div class="tile"><b>${nf(T.notesHit)}</b><span>${esc(tr("lb.tile.notes"))}</span></div></div>
    <div class="p-cols"><section><h3>${esc(tr("lb.top_players"))}</h3><div class="d-row"><select id="lbMetric">${o(["totalScore", "submissions", "totalFcs", "totalPfcs", "hoursPlayed", "starsEarned", "notesHit"].map((k) => [k, tr("lb.metric." + k)]), lbv.metric)}</select></div>
        <ul class="plist">${d.players.map((p, i) => `<li data-u="${esc(p.url)}"><div class="t">${i + 1}. ${esc(p.username)}</div><div class="v">${metricFmt(p.value)}</div></li>`).join("")}</ul></section>
      <section><h3>${esc(tr("lb.songs"))}</h3><div class="d-row"><select id="lbTab">${o(["trending", "topAllTime", "topYear", "topMonth", "topWeek"].map((k) => [k, tr("lb.tab." + k)]), lbv.tab)}</select>
        <select id="lbGroup">${o(["fiveFret", "sixFret", "drums"].map((k) => [k, tr("lb.group." + k)]), lbv.group)}</select></div>
        <ul class="plist">${d.songs.map((s, i) => `<li data-u="${esc(s.url)}"><div class="t">${i + 1}. ${esc(s.name)}<small>${esc(s.artist)}</small></div><div class="v">${nf(s.plays)}<small>${esc(tr("lb.plays"))}</small></div></li>`).join("")}</ul></section></div>
    <section><h3>${esc(tr("lb.latest"))}</h3><ul class="plist">${d.latest.map((r) => `<li data-u="${esc(r.url || "")}"><div class="t">${esc(r.name)}<small>${esc(r.artist)} · ${esc(r.username)}</small></div><div class="v">${nf(r.score)}<small>${esc(dn(r.difficulty))} ${esc(inn(r.instrument))}${r.fc ? " · " + esc(tr("profile.fc")) : ""}</small></div></li>`).join("")}</ul></section>`;
  $$("#lbBody li[data-u]").forEach((li) => li.onclick = () => { if (li.dataset.u) window.open(li.dataset.u, "_blank", "noopener"); });
  $("#lbMetric").onchange = (e) => { lbv.metric = e.target.value; loadLeaderboards(); };
  $("#lbTab").onchange = (e) => { lbv.tab = e.target.value; loadLeaderboards(); };
  $("#lbGroup").onchange = (e) => { lbv.group = e.target.value; loadLeaderboards(); };
}

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

/* ================================================================ language */
let appLang = "auto";                       // the saved setting: "auto" or a language code
function fillLangSelects() {
  const opts = [["auto", tr("settings.language_auto")], ...Object.entries(I18N.LANGS)];
  $$("[data-lang-select]").forEach((sel) => {
    sel.innerHTML = opts.map(([v, l]) => `<option value="${v}"${v === appLang ? " selected" : ""}>${esc(l)}</option>`).join("");
  });
}
function bindLangSelects() {
  $$("[data-lang-select]").forEach((sel) => {
    if (!sel.options.length) fillLangSelects();
    sel.value = appLang;
    sel.onchange = async () => {
      appLang = sel.value;
      try { await api("/api/settings", { settings: { language: appLang } }); await I18N.init(false); } catch (e) { toast(e.message, 4000); }
    };
  });
}
function refreshView() {
  switch (currentView) {
    case "settings": loadSettings(); break;
    case "setup": loadSetup(); break;
    case "profile": loadProfile(profCur); break;
    case "friends": loadFriends(true); break;
    case "leaderboards": loadLeaderboards(); break;
    case "setlists": loadSetlists(); break;
    case "dupes": loadDupes(); break;
    case "charts": renderCharts(); break;
    case "spotify": if (spData) renderSpotify(spData); break;
    case "jobs": renderJobs(); break;
  }
}
I18N.onChange(() => {
  I18N.apply(document);
  fillLangSelects(); bindLangSelects();
  const g = $("#libGroup"); if (g.options[0]) g.options[0].textContent = tr("lib.all_folders");
  $("#runBtn").textContent = $("#runOpts").classList.contains("on") ? tr("lib.start", { count: st.sel.size }) : tr("lib.run");
  $("#plToggle").textContent = $("#plAudio").paused ? tr("player.play") : tr("player.pause");
  updateSelbar(); updateFavCount(); applyFilters(); renderCloudFavs(); paintShare();
  if (jobsSnap) renderDock();
  pollJobs(true);                            // the server re-translates job messages for the new language
  if (activeId && !previewing) refreshDetail();
  refreshView();
});

(async () => {
  await I18N.init();
  try { appLang = (await api("/api/settings")).settings.language || "auto"; } catch (e) { /* ignore */ }
  I18N.apply(document); fillLangSelects(); bindLangSelects(); I18N.observe();
  $("#runBtn").textContent = tr("lib.run"); $("#plToggle").textContent = tr("player.pause");
  loadLibrary().then(() => { pollLibrary(); pollJobs(); });
  loadCloud(); pollFriends(); setInterval(pollFriends, 30000);
  // First run (or the data folder vanished): open the setup screen.
  api("/api/setup").then((d) => {
    const need = !d.setupDone || !d.dataOk;   // ask once, even when everything was found
    $("#navSetup").classList.toggle("on", !d.setupDone || !d.dataOk);
    if (need) show("setup");
  }).catch(() => {});
})();
})();

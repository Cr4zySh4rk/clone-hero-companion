/* Mandatory Terms of Use / Privacy Policy acceptance screen (sibling of the update gate in update.js).
   The server enforces it too (HTTP 451 on state-changing calls until accepted); this is the full-window screen.
   The update gate has priority: while an update is required this screen stays hidden. */
(() => {
"use strict";
const tr = (k, v) => I18N.t(k, v);
const URLS = { terms: "https://cloneherocompanion.com/terms.html", privacy: "https://cloneherocompanion.com/privacy.html" };

const css = `
#termsGate{position:fixed;inset:0;z-index:2147482500;display:none;align-items:center;justify-content:center;padding:24px;background:#160d2b;color:#f6f1e7;font:15px/1.5 Barlow,"Segoe UI",system-ui,sans-serif;overflow:auto}
#termsGate.on{display:flex}
#termsGate .tm-box{width:min(640px,100%);max-height:100%;overflow:auto;display:flex;flex-direction:column;gap:14px;background:#1e1338;border:1px solid rgba(246,241,231,.12);border-radius:12px;padding:26px}
#termsGate h1{margin:0;font:900 30px/1.1 "Big Shoulders Display","Arial Narrow",Impact,sans-serif;text-transform:uppercase;letter-spacing:.02em;overflow-wrap:anywhere}
#termsGate p{margin:0;color:rgba(246,241,231,.75)}
#termsGate ul{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:8px;color:rgba(246,241,231,.85)}
#termsGate .tm-links{display:flex;flex-wrap:wrap;gap:6px 22px}
#termsGate a{color:#ffb52e}
#termsGate label{display:flex;align-items:flex-start;gap:10px;cursor:pointer;font-weight:600}
#termsGate input[type=checkbox]{flex:none;width:18px;height:18px;margin-top:3px;accent-color:#ffb52e}
#termsGate .tm-row{display:flex;flex-wrap:wrap;align-items:center;gap:12px}
#termsGate button{font:inherit;font-weight:700;cursor:pointer;border-radius:6px;padding:9px 20px;max-width:100%;overflow-wrap:anywhere}
#termsGate .tm-go{background:#ffb52e;color:#241500;border:1px solid #ffb52e}
#termsGate .tm-no{background:transparent;color:#f6f1e7;border:1px solid rgba(246,241,231,.35)}
#termsGate button:disabled{opacity:.5;cursor:default}
#termsGate button:focus-visible,#termsGate a:focus-visible,#termsGate input:focus-visible{outline:2px solid #f6f1e7;outline-offset:2px}
`;

let st = null, gate = null, timer = null, want = false;

function build() {
  const style = document.createElement("style"); style.textContent = css; document.head.appendChild(style);
  gate = document.createElement("div"); gate.id = "termsGate"; gate.setAttribute("role", "dialog"); gate.setAttribute("aria-modal", "true");
  gate.innerHTML = `<div class="tm-box"><h1></h1><p class="tm-intro"></p>
    <ul><li class="tm-p1"></li><li class="tm-p2"></li><li class="tm-p3"></li><li class="tm-p4"></li></ul>
    <div class="tm-links"><a href="${URLS.terms}" data-doc="terms"></a><a href="${URLS.privacy}" data-doc="privacy"></a></div>
    <label><input type="checkbox" class="tm-chk"><span class="tm-lbl"></span></label>
    <div class="tm-row"><button type="button" class="tm-go" disabled></button><button type="button" class="tm-no"></button></div></div>`;
  const chk = gate.querySelector(".tm-chk"), go = gate.querySelector(".tm-go");
  chk.onchange = () => { go.disabled = !chk.checked; };
  gate.querySelectorAll("a[data-doc]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    post("/api/open", { url: URLS[a.dataset.doc] }).catch(() => {});       // the system browser
  }));
  go.onclick = async () => {
    if (!chk.checked) return;
    go.disabled = true;
    try { st = await post("/api/terms/accept", {}); } catch (e) { go.disabled = false; return; }
    paint();
    if (!st.required) location.reload();                                     // load the app now that it may be used
  };
  gate.querySelector(".tm-no").onclick = () => { post("/api/quit", {}).catch(() => {}); try { window.close(); } catch (e) { /* ignore */ } };
  document.body.appendChild(gate);
}

async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}

function setBlocked(on) {
  [...document.body.children].forEach((n) => {
    if (n === gate) return;
    if (on) { if (!n.hasAttribute("inert")) { n.setAttribute("inert", ""); n.setAttribute("data-terms-block", ""); } }
    else if (n.hasAttribute("data-terms-block")) { n.removeAttribute("inert"); n.removeAttribute("data-terms-block"); }
  });
}

function paint() {
  if (!st || !gate) return;
  const updating = !!(window.CHCUpdate && window.CHCUpdate.state && window.CHCUpdate.state.required);   // the update gate wins
  want = !!st.required;
  const show = want && !updating;
  gate.classList.toggle("on", show);
  setBlocked(show);
  if (!want) return;
  const q = (s) => gate.querySelector(s);
  q("h1").textContent = tr("terms.title");
  q(".tm-intro").textContent = tr("terms.intro");
  ["p1", "p2", "p3", "p4"].forEach((k, i) => { q(".tm-" + k).textContent = tr("terms.point" + (i + 1)); });
  const [aT, aP] = gate.querySelectorAll("a[data-doc]");
  aT.textContent = tr("terms.read_terms"); aP.textContent = tr("terms.read_privacy");
  q(".tm-lbl").textContent = tr("terms.accept_label");
  q(".tm-go").textContent = tr("terms.accept"); q(".tm-no").textContent = tr("terms.decline");
  if (show && !gate.dataset.focused) { gate.dataset.focused = "1"; q(".tm-chk").focus(); }
}

async function refresh() {
  try { st = await (await fetch("/api/terms", { cache: "no-store" })).json(); } catch (e) { /* app is closing */ }
  if (st && st.required && !gate) build();
  paint();
  clearTimeout(timer);
  if (want) timer = setTimeout(refresh, 1500);          // follows the update gate and language changes
}

window.CHCTerms = { refresh, get state() { return st; } };
new MutationObserver(paint).observe(document.documentElement, { attributes: true, attributeFilter: ["lang"] });
(async () => { try { await I18N.loadCatalog("en"); } catch (e) { /* shown untranslated */ } refresh(); })();
})();

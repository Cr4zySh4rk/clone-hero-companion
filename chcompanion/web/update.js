/* Mandatory update gate and the "couldn't check" banner. Loaded by the main window and by Now Playing.
   The server enforces the gate too (HTTP 426 on state-changing calls); this is the full-window screen. */
(() => {
"use strict";
const tr = (k, v) => I18N.t(k, v);
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")}

/* safe markdown: same renderer as site/app.js (everything is HTML-escaped first; only http(s) links) */
function inline(s){
  // s is already HTML-escaped
  var codes=[];
  s=s.replace(/`([^`]+)`/g,function(_,c){codes.push(c);return "\u0000"+(codes.length-1)+"\u0000"}); // i18n-ok
  s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,function(_,t,u){return '<a href="'+u+'" rel="noopener noreferrer">'+t+'</a>'});
  s=s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,function(_,p,u){return p+'<a href="'+u+'" rel="noopener noreferrer">'+u+'</a>'});
  s=s.replace(/\*\*([^*]+)\*\*|__([^_]+)__/g,function(_,a,b){return "<strong>"+(a||b)+"</strong>"});
  s=s.replace(/(^|[^*\w])\*([^*\s][^*]*)\*(?!\w)/g,"$1<em>$2</em>");
  return s.replace(/\u0000(\d+)\u0000/g,function(_,i){return "<code>"+codes[+i]+"</code>"});
}
function renderMarkdown(md){
  var lines=esc(String(md).replace(/\r\n?/g,"\n")).split("\n"),out=[],i=0,para=[];
  function flush(){if(para.length){out.push("<p>"+inline(para.join(" "))+"</p>");para=[]}}
  while(i<lines.length){
    var l=lines[i],m;
    if(/^```/.test(l)){flush();var buf=[];i++;while(i<lines.length&&!/^```/.test(lines[i]))buf.push(lines[i++]);i++;out.push("<pre><code>"+buf.join("\n")+"</code></pre>");continue} // i18n-ok
    if((m=/^(#{1,6})\s+(.*?)\s*#*$/.exec(l))){flush();var lv=Math.min(6,m[1].length+2);out.push("<h"+lv+">"+inline(m[2])+"</h"+lv+">");i++;continue}
    if(/^\s*([-*+])\s+/.test(l)||/^\s*\d+[.)]\s+/.test(l)){
      flush();var ord=/^\s*\d+[.)]/.test(l),items=[];
      while(i<lines.length&&(ord?/^\s*\d+[.)]\s+/.test(lines[i]):/^\s*([-*+])\s+/.test(lines[i]))){
        var t=lines[i].replace(ord?/^\s*\d+[.)]\s+/:/^\s*[-*+]\s+/,"");i++;
        while(i<lines.length&&/^\s{2,}\S/.test(lines[i])&&!/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]))t+=" "+lines[i++].trim();
        items.push("<li>"+inline(t)+"</li>");
      }
      out.push((ord?"<ol>":"<ul>")+items.join("")+(ord?"</ol>":"</ul>"));continue;
    }
    if(/^\s*$/.test(l)){flush();i++;continue}
    if(/^(-{3,}|\*{3,})\s*$/.test(l)){flush();out.push("<hr>");i++;continue}
    para.push(l.trim());i++;
  }
  flush();return out.join("");
}


const css = `
#updGate{position:fixed;inset:0;z-index:2147483000;display:none;align-items:center;justify-content:center;padding:24px;background:#160d2b;color:#f6f1e7;font:15px/1.5 Barlow,"Segoe UI",system-ui,sans-serif;overflow:auto}
#updGate.on{display:flex}
#updGate .upd-box{width:min(640px,100%);max-height:100%;display:flex;flex-direction:column;gap:14px;background:#1e1338;border:1px solid rgba(246,241,231,.12);border-radius:12px;padding:26px}
#updGate h1{margin:0;font:900 30px/1.1 "Big Shoulders Display","Arial Narrow",Impact,sans-serif;text-transform:uppercase;letter-spacing:.02em}
#updGate .upd-ver{font-size:20px;font-weight:700;color:#ffb52e;overflow-wrap:anywhere}
#updGate p{margin:0;color:rgba(246,241,231,.75)}
#updGate .upd-notes{min-height:0;overflow:auto;max-height:38vh;background:#160d2b;border:1px solid rgba(246,241,231,.09);border-radius:8px;padding:4px 14px;font-size:14px}
#updGate .upd-notes h3,#updGate .upd-notes h4,#updGate .upd-notes h5,#updGate .upd-notes h6{margin:12px 0 4px;font-size:15px}
#updGate .upd-notes p{margin:6px 0;color:inherit}#updGate .upd-notes ul,#updGate .upd-notes ol{margin:6px 0;padding-left:22px}
#updGate .upd-notes code,#updGate .upd-notes pre{background:rgba(246,241,231,.08);border-radius:4px;padding:1px 4px}#updGate .upd-notes pre{overflow:auto;padding:8px}
#updGate .upd-notes a,#updGate a.upd-man{color:#ffb52e}
#updGate .upd-bar{height:8px;background:rgba(246,241,231,.12);border-radius:4px;overflow:hidden}#updGate .upd-bar i{display:block;height:100%;width:0;background:#ffb52e;transition:width .2s}
#updGate .upd-msg{min-height:1.5em}#updGate .upd-msg.upd-err{color:#ff8a80}
#updGate .upd-row{display:flex;flex-wrap:wrap;align-items:center;gap:14px}
#updGate button{font:inherit;font-weight:700;cursor:pointer;background:#ffb52e;color:#241500;border:1px solid #ffb52e;border-radius:6px;padding:9px 20px}
#updGate button:disabled{opacity:.5;cursor:default}
#updGate button:focus-visible,#updGate a:focus-visible{outline:2px solid #f6f1e7;outline-offset:2px}
#updBanner[hidden],#updGate [hidden]{display:none!important}
#updBanner{position:fixed;right:12px;bottom:12px;z-index:2147482000;display:flex;align-items:center;gap:8px;max-width:min(360px,calc(100vw - 24px));background:#352460;color:#f6f1e7;border:1px solid rgba(246,241,231,.2);border-radius:8px;padding:6px 8px 6px 12px;font:13px/1.3 Barlow,"Segoe UI",system-ui,sans-serif}
#updBanner button{font:inherit;cursor:pointer;background:transparent;color:inherit;border:0;padding:2px 6px}#updBanner span{min-width:0}#updBanner{flex-wrap:wrap}#updBanner small{order:9;flex-basis:100%;opacity:.75;font-size:11px;overflow-wrap:anywhere}#updBanner .upd-retry{flex:none;white-space:nowrap;border:1px solid rgba(246,241,231,.35);border-radius:6px}#updBanner .upd-retry:disabled{opacity:.6;cursor:default}
`;

let st = null, timer = null, gate = null, banner = null, dismissed = false;
const mb = (n) => I18N.nf(Math.round(n / 104857.6) / 10);

function build() {
  const style = document.createElement("style"); style.textContent = css; document.head.appendChild(style);
  gate = document.createElement("div"); gate.id = "updGate"; gate.setAttribute("role", "dialog"); gate.setAttribute("aria-modal", "true");
  gate.innerHTML = `<div class="upd-box"><h1></h1><div class="upd-ver"></div><p class="upd-sub"></p><div class="upd-notes"></div>
    <div class="upd-bar" hidden><i></i></div><div class="upd-msg" role="status"></div>
    <div class="upd-row"><button type="button" class="upd-go"></button><a class="upd-man" target="_blank" rel="noopener noreferrer"></a></div></div>`;
  gate.querySelector(".upd-go").onclick = install;
  banner = document.createElement("div"); banner.id = "updBanner"; banner.hidden = true; banner.setAttribute("role", "status");
  banner.innerHTML = `<span></span><small hidden></small><button type="button" class="upd-retry"></button><button type="button" class="upd-x">×</button>`;
  banner.querySelector(".upd-x").onclick = () => { dismissed = true; banner.hidden = true; };
  banner.querySelector(".upd-retry").onclick = retry;
  document.body.append(gate, banner);
}

function setBlocked(on) {
  [...document.body.children].forEach((n) => { if (n !== gate && n !== banner) { if (on) n.setAttribute("inert", ""); else if (!n.hasAttribute("data-terms-block")) n.removeAttribute("inert"); } });
}

async function retry() {
  if (st) { st.checking = true; paint(); }
  try { st = await (await fetch("/api/update/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })).json(); } catch (e) { /* still offline */ }
  if (st) st.checking = false;
  paint();
}

function paint() {
  if (!st || !gate) return;
  const q = (s) => gate.querySelector(s);
  banner.querySelector("span").textContent = tr("update.offline_banner");
  banner.querySelector(".upd-x").setAttribute("aria-label", tr("common.close"));
  banner.querySelector("span").title = st.error || "";
  const why = banner.querySelector("small"); why.textContent = st.error || ""; why.hidden = !st.error;
  const rb = banner.querySelector(".upd-retry");
  rb.textContent = st.checking ? tr("update.retrying") : tr("update.retry");
  rb.disabled = !!st.checking;
  banner.hidden = !(st.status === "offline" && !st.required) || dismissed;
  gate.classList.toggle("on", !!st.required);
  setBlocked(!!st.required);
  if (!st.required) return;
  const ins = st.install || {}, busy = ["downloading", "verifying", "installing", "done"].includes(ins.phase);
  q("h1").textContent = tr("update.title");
  q(".upd-ver").textContent = tr("update.versions", { current: st.current, latest: st.latest });
  q(".upd-sub").textContent = tr("update.subtitle");
  const notes = q(".upd-notes"); notes.hidden = !st.notes;
  if (notes.dataset.src !== st.notes) { notes.dataset.src = st.notes || ""; notes.innerHTML = renderMarkdown(st.notes || ""); }
  const bar = q(".upd-bar"); bar.hidden = ins.phase !== "downloading";
  bar.firstElementChild.style.width = ins.total ? Math.min(100, ins.done * 100 / ins.total) + "%" : "0";
  const msg = q(".upd-msg");
  msg.classList.toggle("upd-err", ins.phase === "error");
  msg.textContent = ins.phase === "error" ? (ins.error || "") : ins.phase === "downloading" && ins.total
    ? `${ins.message} ${tr("update.progress", { done: mb(ins.done), total: mb(ins.total) })}` : (ins.message || "");
  const go = q(".upd-go"); go.disabled = busy; go.textContent = tr("update.now");
  const man = q(".upd-man"); man.textContent = tr("update.manual"); man.href = st.url;
  if (!go.dataset.focused) { go.dataset.focused = "1"; go.focus(); }
}

async function install() {
  const go = gate && gate.querySelector(".upd-go");
  if (go) go.disabled = true;                       // one click is enough; it re-enables only if the start failed
  try { st = await (await fetch("/api/update/install", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })).json(); } catch (e) { if (go) go.disabled = false; return; }
  paint(); schedule();
}

async function refresh() {
  try { st = await (await fetch("/api/update", { cache: "no-store" })).json(); } catch (e) { /* app is closing or restarting */ }
  if (!gate) build();
  paint(); schedule();
}

function schedule() {
  clearTimeout(timer);
  const ins = st && st.install ? st.install.phase : "idle";
  timer = setTimeout(refresh, ["downloading", "verifying", "installing", "done"].includes(ins) ? 700 : st && st.required ? 5000 : 60000);
}

window.CHCUpdate = { refresh, get state() { return st; } };
new MutationObserver(paint).observe(document.documentElement, { attributes: true, attributeFilter: ["lang"] });   // the page switched language
(async () => { try { await I18N.loadCatalog("en"); } catch (e) { /* shown untranslated */ } refresh(); })();
})();

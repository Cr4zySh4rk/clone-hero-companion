(function(){
"use strict";
var REPO="Cr4zySh4rk/clone-hero-companion";
var API="https://api.github.com/repos/"+REPO+"/releases/latest";
var RELEASES="https://github.com/"+REPO+"/releases";
var LATEST=RELEASES+"/latest";
var CACHE="chc-latest-release",TTL=10*60*1000;
var NAMES={mac:"macOS",win:"Windows",linux:"Linux"};
var $=function(id){return document.getElementById(id)};
var ICON={mac:"",win:"",linux:""};

function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")}

/* ---------- OS detection ---------- */
function detectOS(){
  var uad=navigator.userAgentData,ua=navigator.userAgent||"";
  var p=((uad&&uad.platform)||"").toLowerCase();
  if(/android|ios|iphone|ipad/.test(p)||/Android|iPhone|iPad|iPod/.test(ua)) return "mobile";
  if(p){ if(p.indexOf("mac")>-1) return "mac"; if(p.indexOf("win")>-1) return "win"; if(p.indexOf("linux")>-1||p.indexOf("chrome os")>-1||p.indexOf("cros")>-1) return "linux"; }
  if(/Macintosh|Mac OS X/.test(ua)) return "mac";
  if(/Windows/.test(ua)) return "win";
  if(/Linux|X11|CrOS/.test(ua)) return "linux";
  return "unknown";
}
function detectMacArch(cb){
  try{
    if(navigator.userAgentData&&navigator.userAgentData.getHighEntropyValues){
      navigator.userAgentData.getHighEntropyValues(["architecture"]).then(function(v){
        cb(v.architecture==="arm"?"Apple Silicon":v.architecture==="x86"?"Intel":"");
      },function(){cb(guessMacGPU())});
      return;
    }
  }catch(e){}
  cb(guessMacGPU());
}
function guessMacGPU(){
  try{
    var c=document.createElement("canvas"),gl=c.getContext("webgl"),x=gl&&gl.getExtension("WEBGL_debug_renderer_info");
    var r=x?String(gl.getParameter(x.UNMASKED_RENDERER_WEBGL)):"";
    if(/Apple M|Apple GPU/i.test(r)) return "Apple Silicon";
    
  }catch(e){}
  return "";
}

/* ---------- release data ---------- */
function readCache(){
  try{var o=JSON.parse(sessionStorage.getItem(CACHE)||"null");if(o&&Date.now()-o.t<TTL)return o.d}catch(e){}
  return null;
}
function writeCache(d){try{sessionStorage.setItem(CACHE,JSON.stringify({t:Date.now(),d:d}))}catch(e){}}
function getRelease(){
  var c=readCache();
  if(c) return Promise.resolve(c);
  return fetch(API,{headers:{Accept:"application/vnd.github+json"}}).then(function(r){
    if(!r.ok) throw new Error("HTTP "+r.status);
    return r.json();
  }).then(function(d){
    if(!d||!d.tag_name) throw new Error("bad data");
    var slim={tag_name:d.tag_name,name:d.name,body:d.body||"",published_at:d.published_at,html_url:d.html_url,
      assets:(d.assets||[]).map(function(a){return{name:a.name,size:a.size,browser_download_url:a.browser_download_url}})};
    writeCache(slim);return slim;
  });
}
function osOfAsset(name){
  var n=name.toLowerCase();
  if(/\.dmg$/.test(n)) return "mac";
  if(/\.exe$/.test(n)) return "win";
  if(/\.(tar\.gz|tgz|appimage|deb|rpm|tar\.xz)$/.test(n)||/linux/.test(n)) return "linux";
  return null;
}
function pickAssets(rel){
  var out={};
  (rel.assets||[]).forEach(function(a){var o=osOfAsset(a.name);if(o&&!out[o])out[o]=a});
  return out;
}
function mb(n){return (n/1048576).toFixed(n>=104857600?0:1)+" MB"}

/* ---------- download UI ---------- */
function renderDownload(rel,os,arch){
  var btn=$("dl-btn"),label=$("dl-label"),meta=$("dl-meta"),others=$("dl-others");
  var assets=rel?pickAssets(rel):{};
  var ver=rel?rel.tag_name.replace(/^v?/,"v"):"";
  var target=NAMES[os]?os:null;
  var links=[];
  ["mac","win","linux"].forEach(function(k){
    if(k===target) return;
    links.push(assets[k]?'<a href="'+esc(assets[k].browser_download_url)+'">'+NAMES[k]+'</a>':'<a href="'+LATEST+'">'+NAMES[k]+'</a>');
  });
  var tail='<a href="'+RELEASES+'">All releases</a>';
  if(target&&assets[target]){
    btn.href=assets[target].browser_download_url;
    label.textContent="Download for "+NAMES[target];
    var m=ver+" · "+mb(assets[target].size);
    if(target==="mac"){
      meta.textContent=m+" · universal, Apple Silicon and Intel";
      detectMacArch(function(a){ if(a) meta.textContent=m+" · universal build, runs natively on your "+a+" Mac"; });
    } else meta.textContent=m+(target==="win"?" · Windows 10 and 11, 64-bit":" · x64 tar.gz");
    others.innerHTML="Also for: "+links.join("")+tail;
  } else {
    btn.href=LATEST;
    label.textContent=NAMES[target]?"Download for "+NAMES[target]:(os==="mobile"?"Get it on your computer":"Download");
    meta.textContent=rel?(ver+(os==="mobile"?" · a desktop app for macOS, Windows and Linux":"")):"Opens the latest release on GitHub";
    others.innerHTML=(rel?"Pick your system: ":"")+(rel?["mac","win","linux"].map(function(k){return assets[k]?'<a href="'+esc(assets[k].browser_download_url)+'">'+NAMES[k]+'</a>':'<a href="'+LATEST+'">'+NAMES[k]+'</a>'}).join(""):"")+tail;
  }
  others.querySelectorAll&&Array.prototype.forEach.call(others.querySelectorAll("a"),function(a,i,all){});
  // simple separators via CSS gap; nothing else needed
}

/* ---------- safe markdown ---------- */
function inline(s){
  // s is already HTML-escaped
  var codes=[];
  s=s.replace(/`([^`]+)`/g,function(_,c){codes.push(c);return "\u0000"+(codes.length-1)+"\u0000"});
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
    if(/^```/.test(l)){flush();var buf=[];i++;while(i<lines.length&&!/^```/.test(lines[i]))buf.push(lines[i++]);i++;out.push("<pre><code>"+buf.join("\n")+"</code></pre>");continue}
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
window.__renderMarkdown=renderMarkdown;

function renderNotes(rel){
  var v=rel.tag_name.replace(/^v?/,"v");
  $("rel-title").textContent="What’s new in "+v;
  var d=rel.published_at?new Date(rel.published_at):null;
  $("rel-date").innerHTML='Released '+(d&&!isNaN(d)?'<time datetime="'+esc(rel.published_at)+'">'+esc(d.toLocaleDateString("en-GB",{day:"numeric",month:"long",year:"numeric"}))+'</time>':"")+
    ' · <a href="'+esc(rel.html_url||LATEST)+'">View on GitHub</a>';
  $("rel-body").innerHTML=rel.body&&rel.body.trim()?renderMarkdown(rel.body):"<p>No release notes were published for this version.</p>";
}
function renderFail(){
  $("rel-body").innerHTML='<p>Could not load the release notes right now. See them on the <a href="'+LATEST+'">latest release page</a>.</p>';
  $("rel-date").textContent="";
}

/* ---------- install tabs ---------- */
function initTabs(os){
  var tabs=Array.prototype.slice.call(document.querySelectorAll('[role="tab"]'));
  function sel(t,focus){tabs.forEach(function(b){var on=b===t;b.setAttribute("aria-selected",on);b.tabIndex=on?0:-1;$(b.getAttribute("aria-controls")).hidden=!on});if(focus)t.focus()}
  tabs.forEach(function(t,i){
    t.addEventListener("click",function(){sel(t)});
    t.addEventListener("keydown",function(e){
      var n=e.key==="ArrowRight"?1:e.key==="ArrowLeft"?-1:0;
      if(n){e.preventDefault();sel(tabs[(i+n+tabs.length)%tabs.length],true)}
    });
  });
  if(NAMES[os]) sel($("t-"+os));
}

/* ---------- decorative: heatmap + note highway ---------- */
function heat(){
  var h=$("heat");if(!h)return;var s=7,html="";
  for(var i=0;i<26*7;i++){s=(s*9301+49297)%233280;var r=s/233280;var wk=Math.floor(i/7);var bias=wk/26*.35;var l=r+bias>1.0?4:r+bias>.78?3:r+bias>.55?2:r+bias>.35?1:0;html+='<i data-l="'+l+'"></i>'}
  h.style.gridTemplateColumns="repeat(26,1fr)";h.innerHTML=html;
}
function highway(){
  var cv=$("highway");if(!cv||!cv.getContext)return;
  var ctx=cv.getContext("2d"),cols=["#3cc85a","#e8413a","#ffb52e","#3a8ee8","#ff7a1a"];
  var reduce=window.matchMedia&&matchMedia("(prefers-reduced-motion: reduce)").matches;
  var W,H,dpr,gems=[],run=false,last=0;
  function size(){dpr=Math.min(2,window.devicePixelRatio||1);var r=cv.getBoundingClientRect();W=r.width;H=r.height;cv.width=W*dpr;cv.height=H*dpr;ctx.setTransform(dpr,0,0,dpr,0,0)}
  function proj(lane,t){ // t 0 far .. 1 near
    var vx=W*.5,vy=H*.02,sc=t*t*.95+.03,span=Math.max(W*.9,520);
    return{x:vx+(lane-2)*span*.19*sc*2.1,y:vy+(H*1.05-vy)*t*t,s:sc}
  }
  function draw(){
    ctx.clearRect(0,0,W,H);
    for(var l=-.5;l<=4.5;l++){ctx.beginPath();var a=proj(l,0.02),b=proj(l,1);ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.strokeStyle="rgba(160,150,200,.22)";ctx.lineWidth=1.2;ctx.stroke()}
    for(var k=1;k<8;k++){var t=Math.pow(k/8,1);var a2=proj(-.5,t),b2=proj(4.5,t);ctx.beginPath();ctx.moveTo(a2.x,a2.y);ctx.lineTo(b2.x,b2.y);ctx.strokeStyle="rgba(160,150,200,.10)";ctx.stroke()}
    gems.forEach(function(g){var p=proj(g.lane,g.t),r=6+p.s*34;ctx.fillStyle=cols[g.lane];ctx.shadowColor=cols[g.lane];ctx.shadowBlur=r*.9;
      ctx.beginPath();ctx.ellipse(p.x,p.y,r*1.25,r*.7,0,0,6.2832);ctx.fill();ctx.shadowBlur=0;ctx.fillStyle="rgba(255,255,255,.55)";ctx.beginPath();ctx.ellipse(p.x,p.y-r*.12,r*.55,r*.22,0,0,6.2832);ctx.fill()});
  }
  function seed(){gems=[];var n=18;for(var i=0;i<n;i++)gems.push({lane:Math.floor(Math.abs(Math.sin(i*12.9898)*43758.5453)%5),t:(i+.5)/n})}
  function tick(ts){if(!run)return;var dt=Math.min(.05,(ts-last)/1000||0);last=ts;
    gems.forEach(function(g){g.t+=dt*.16;if(g.t>1.05){g.t=0;g.lane=Math.floor(Math.random()*5)}});draw();requestAnimationFrame(tick)}
  size();seed();draw();
  window.addEventListener("resize",function(){size();draw()});
  if(reduce||!window.IntersectionObserver)return;
  new IntersectionObserver(function(e){var v=e[0].isIntersecting;if(v&&!run){run=true;last=performance.now();requestAnimationFrame(tick)}else if(!v)run=false}).observe(cv);
}

/* ---------- boot ---------- */
var os=detectOS();
initTabs(os);heat();highway();
renderDownload(null,os);
$("dl-meta").textContent="Looking up the latest version…";
getRelease().then(function(rel){renderDownload(rel,os);renderNotes(rel)},function(){
  renderDownload(null,os);renderFail();
});
})();

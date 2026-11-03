/* Tiny i18n runtime shared by the main UI and the Now Playing window.
   Catalogs: /static/i18n/<lang>.json, flat keys, {name} placeholders, plurals as key.one / key.few / key.many / key.other
   (category picked with Intl.PluralRules from the `count` variable). English is the source and the fallback.
   Pseudo-localisation for layout testing: add ?pseudo=1 to the address (or localStorage chc.pseudo = "1"). */
(() => {
"use strict";
const LANGS = { en: "English", ru: "Русский", "zh-CN": "简体中文", ko: "한국어", ja: "日本語", "pt-BR": "Português (Brasil)", es: "Español", fr: "Français", de: "Deutsch" };
const q = new URLSearchParams(location.search);
let pseudo = q.get("pseudo") === "1";
try { if (!pseudo && localStorage.getItem("chc.pseudo") === "1") pseudo = true; } catch (e) { /* storage blocked */ }
let lang = "en";
const cats = {};
const listeners = [];

function normalize(tag) {
  if (!tag) return "";
  const low = String(tag).replace(/_/g, "-").toLowerCase();
  if (low.startsWith("zh")) return /hant|-tw|-hk|-mo/.test(low) ? "" : "zh-CN";
  const base = low.split("-")[0];
  if (base === "pt") return "pt-BR";
  return Object.keys(LANGS).find((c) => c.toLowerCase() === base) || "";
}
function detect() {
  for (const tag of (navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language])) {
    const n = normalize(tag); if (n) return n;
  }
  return "en";
}
async function loadCatalog(code) {
  if (cats[code]) return cats[code];
  try {
    const r = await fetch(`/static/i18n/${code}.json`, { cache: "no-cache" });
    cats[code] = r.ok ? await r.json() : {};
  } catch (e) { cats[code] = {}; }
  return cats[code];
}
const PSEUDO = { a: "à", b: "ƀ", c: "ç", d: "ð", e: "é", f: "ƒ", g: "ĝ", h: "ĥ", i: "î", j: "ĵ", k: "ķ", l: "ŀ", m: "ɱ", n: "ñ", o: "ö", p: "þ", q: "ǫ", r: "ŕ", s: "š", t: "ţ", u: "û", v: "ṽ", w: "ŵ", x: "ẋ", y: "ý", z: "ž",
  A: "Å", B: "Ɓ", C: "Ç", D: "Ð", E: "É", F: "Ƒ", G: "Ĝ", H: "Ĥ", I: "Î", J: "Ĵ", K: "Ķ", L: "Ŀ", M: "Ṁ", N: "Ñ", O: "Ö", P: "Þ", Q: "Ǫ", R: "Ŕ", S: "Š", T: "Ţ", U: "Û", V: "Ṽ", W: "Ŵ", X: "Ẋ", Y: "Ý", Z: "Ž" };
function pseudoize(s) {
  // accent letters and pad ~40%, but leave {placeholders} and <tags> alone
  const out = s.replace(/(\{\w+\})|(<[^>]+>)|([A-Za-z])/g, (m, ph, tag, ch) => (ph || tag ? m : PSEUDO[ch]));
  return "[" + out + " " + "~".repeat(Math.max(2, Math.ceil(s.replace(/\{\w+\}|<[^>]+>/g, "").length * 0.4))).replace(/(~{5})(?=~)/g, "$1 ") + "]";
}
function plural(code, n) { try { return new Intl.PluralRules(code).select(Number(n)); } catch (e) { return Number(n) === 1 ? "one" : "other"; } }
function find(key, count) {
  for (const code of lang === "en" ? ["en"] : [lang, "en"]) {
    const c = cats[code] || {};
    if (count != null) {
      const cat = plural(code, count);
      for (const k of [`${key}.${cat}`, `${key}.other`]) if (k in c) return c[k];
    }
    if (key in c) return c[key];
  }
  return null;
}
function nf(n) { return Number(n || 0).toLocaleString(lang); }
function t(key, vars) {
  let s = find(key, vars && vars.count);
  if (s == null) return key;
  if (pseudo) s = pseudoize(s);
  return vars ? s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? (typeof vars[k] === "number" ? nf(vars[k]) : String(vars[k])) : m)) : s;
}
const has = (key) => key in (cats.en || {});

/* static markup: data-i18n (text), data-i18n-html (trusted markup such as <b>), data-i18n-title, -placeholder, -aria */
function apply(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  root.querySelectorAll("[data-i18n-html]").forEach((el) => { el.innerHTML = t(el.dataset.i18nHtml); });
  root.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
  root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  root.querySelectorAll("[data-i18n-aria]").forEach((el) => { el.setAttribute("aria-label", t(el.dataset.i18nAria)); });
  if (root === document) { document.documentElement.lang = lang; if (document.documentElement.dataset.titleKey) document.title = t(document.documentElement.dataset.titleKey); }
}

/* Anything that can be cut off by CSS ellipsis gets a title with its full text, kept in sync as the DOM changes. */
const AUTO_SEL = ".ellipsis, button, .pill, .badge, .tile span, .thead > div, .row .cell, .row .t b, .row .t span, .plist .t, .job .msg, option";
let titleQueued = false;
function autoTitles(root = document.body) {
  root.querySelectorAll(AUTO_SEL).forEach((el) => {
    if (el.title && el.dataset.autotitle !== "1") return;       // an explicit tooltip wins
    const txt = (el.textContent || "").trim();
    if (txt && el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).textOverflow === "ellipsis") { el.title = txt; el.dataset.autotitle = "1"; }
    else if (el.dataset.autotitle === "1") { el.removeAttribute("title"); delete el.dataset.autotitle; }
  });
}
function queueTitles() {
  if (titleQueued) return; titleQueued = true;
  requestAnimationFrame(() => { titleQueued = false; autoTitles(); });
}
function observe() {
  new MutationObserver(queueTitles).observe(document.body, { childList: true, subtree: true, characterData: true });
  window.addEventListener("resize", queueTitles);
  queueTitles();
}

async function setLang(code, silent) {
  code = normalize(code) || "en";
  await Promise.all([loadCatalog("en"), loadCatalog(code)]);
  const changed = code !== lang;
  lang = code;
  apply(document);
  if (changed && !silent) listeners.forEach((fn) => { try { fn(lang); } catch (e) { console.error(e); } });
  queueTitles();
  return lang;
}
const api = {
  LANGS, t, has, nf, normalize, detect, setLang, apply, observe, autoTitles, loadCatalog,
  get lang() { return lang; }, get pseudo() { return pseudo; },
  onChange: (fn) => listeners.push(fn),
  date: (d, opts) => (d ? new Date(d).toLocaleDateString(lang, opts || { month: "short", day: "numeric" }) : ""),
  time: (d, opts) => new Date(d).toLocaleTimeString(lang, opts),
  weekdays: () => [0, 1, 2, 3, 4, 5, 6].map((i) => new Date(2024, 0, 1 + i).toLocaleDateString(lang, { weekday: "short" })),
  ago(ts) {
    if (!ts) return t("ago.never");
    const m = Math.round((Date.now() / 1000 - ts) / 60);
    if (m < 1) return t("ago.now");
    const rtf = new Intl.RelativeTimeFormat(lang, { numeric: "auto" });
    return m < 60 ? rtf.format(-m, "minute") : m < 1440 ? rtf.format(-Math.round(m / 60), "hour") : rtf.format(-Math.round(m / 1440), "day");
  },
  /* Resolve the language: the saved setting ("auto" or a code) or the browser's; tell the server which locales to use for auto. */
  async init(silent = true) {
    const locs = (navigator.languages && navigator.languages.length ? [...navigator.languages] : [navigator.language]).filter(Boolean);
    let info = null;
    try { info = await (await fetch("/api/i18n", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ locales: locs }) })).json(); } catch (e) { /* offline */ }
    const code = normalize(info && info.lang) || detect();
    await setLang(code, silent);
    return lang;
  },
};
window.I18N = api;
})();

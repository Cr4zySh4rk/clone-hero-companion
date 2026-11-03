# Translator guide

Catalogs live in this folder, one flat JSON file per language: `en.json` (source of truth, generated from the
app's strings) and `ru.json`, `zh-CN.json`, `ko.json`, `ja.json`, `pt-BR.json` (Brazilian), `es.json`, `fr.json`,
`de.json`. An empty catalog (`{}`) means "not translated yet"; the app then shows English.

Translate **values only**. Every file must contain exactly the keys of `en.json` (plural keys: see below).
Check your work with:

    python3 scripts/i18n_check.py <lang>          # e.g. ru, zh-CN, pt-BR   (add --strict to reject an empty file)
    python3 -m pytest -q tests/test_i18n.py

Style: concise UI register (buttons are short imperative or noun phrases, no full stops on labels; hints and
messages are full sentences). Use the terms the Clone Hero community uses in that language; keep one translation per
term everywhere (see the table). Keep the typographic quotes `“ ”` or use the language's own quotation marks.
Keep `…`, `·`, `★`, `☆`, `✓`, `✗`, `×`, `→` and arrows as they are. Do not translate the letters in `diff.letter.*` and
`inst.letter.*` blindly: they are the one-letter badges on rows (E M H X / G B R D K V). Use one letter (or one
character) per badge that stays distinguishable, and keep the same letters for matching instruments across the app.

## Placeholders

* `{name}`, `{count}`, `{pct}` ... must appear **unchanged** (same spelling, braces and all) in the translation, in
  the order the target language needs. Never translate or reformat what is inside the braces, never add spaces
  inside them. `{count}` and numbers are formatted by the app (thousands separators, digits) before insertion.
* Some strings deliberately contain tokens that are the app's own naming-template words and must stay in English
  inside braces: `dlg.rename_text` ("Use {artist}, {name}, {charter}, {album}, {year}.") keeps all five tokens.
* Strings with HTML (`<b>`, `<code>`, `<kbd>`, `<a href=...>`; keys `setup.fda_step1/2`, `np.idle_text`,
  `np.help.note`, `jobs.scan_hint`, `settings.js_missing`, `lb.readonly`): keep the same tags in the same number;
  translate only the text between them. Keep `&amp;` as `&amp;`.
* `\n` inside a value is a line break; keep it (`jobs.yt_tip`).
* Error text from yt-dlp, HTTP, the OS or CH Leaderboards is inserted through a placeholder such as `{detail}`
  and is **not** translated; keep the placeholder where the sentence needs it.
* `job.error_in_step` ("{step}: {detail}") is the prefix the job list puts in front of an error: keep the shape
  `{step}` + colon + `{detail}` (a fullwidth colon `：` is fine for zh-CN/ja).

## Plurals

Keys ending in `.one` / `.few` / `.many` / `.other` form a family chosen by the `{count}` value (CLDR rules via
`Intl.PluralRules` in the UI and a table in `chcompanion/i18n.py`). Provide exactly the forms your language uses:

| Language | Forms to provide | Notes |
|----------|------------------|-------|
| en, de, es | `one`, `other` | `one` = exactly 1. (es/fr/pt also have a CLDR `many` for millions; optional, `other` is used) |
| fr, pt-BR | `one`, `other` | `one` = 0 and 1 |
| ru | `one`, `few`, `many`, `other` | `one`: 1, 21, 31 ... (not 11); `few`: 2-4, 22-24 ... (not 12-14); `many`: 0, 5-20, 25-30 ...; `other` is only for fractions: copy the `few` text |
| ja, ko, zh-CN | `other` only | no `.one` key; the checker rejects forms the language does not use |

A form may leave out `{count}` when the language says the number in words ("one file"), except `other`,
which must contain every placeholder of the English `other` form.

## Terms: keep as is (never translate)

Clone Hero, Companion (in "Clone Hero Companion"), Chorus Encore, RhythmVerse, CH Leaderboards, YouTube, Spotify,
Deno, Node, yt-dlp, ffmpeg, LRCLIB, MEGA, Google, OBS, Windows, macOS, Linux, Chrome, Edge, Firefox, Safari, Brave,
Rock Band, GHL, WebM, VP8, H.264, MP4, `.sng`, `.zip`, `cookies.txt`, `settings.ini`, `scorestats.json`,
resolutions (`720p`, `1080p`), keyboard keys in text (F, S, N, U, H, V, I, F11, Shift, ⌃⌘F), menu paths that name
the game's own English menus ("Settings → Scan Songs" is Clone Hero's menu: keep it English, you may add a gloss
in the target language in brackets if that helps).

## Terms: translate consistently

Use the familiar English loanword when the community uses it (typical for Japanese, Korean, Russian and
Chinese players); otherwise the native word. Pick one rendering per language and reuse it.

| English | Meaning here | Guidance |
|---------|--------------|----------|
| chart | a playable song file for Clone Hero (notes.chart / notes.mid / .sng) | loanword in ja (チャート), ko (차트), ru (чарт); zh: 谱面; de/fr/es/pt: "Chart" (de, fr), "chart" (es), "chart" (pt-BR) |
| charter | the person who made the chart | ja チャーター, ko 차터, ru чартер, zh 谱师/制谱者, de Charter, fr charteur, es charter, pt-BR charter |
| song / track | a song in the library | native word |
| setlist | named list of songs Clone Hero loads at start | ja セットリスト, ko 세트리스트, ru сетлист, zh 歌单/曲目表, de Setlist, fr setlist, es setlist, pt-BR setlist |
| favourite(s) | a chart the user starred | native word for "favorite" (pt-BR: favorito; es: favorito; zh: 收藏) |
| Library | the user's songs on this computer | native word ("Biblioteca", "Bibliothek", "Bibliothèque", "ライブラリ", "라이브러리", "Библиотека", "曲库") |
| lyrics | sing-along text | native word |
| music video / video | the background video | native word |
| HD | 640p or better | keep "HD" |
| Jobs | the background task queue | native word for "tasks/jobs" (Aufgaben, Tâches, Tareas, Tarefas, ジョブ, 작업, Задачи, 任务) |
| worker(s) | parallel processing threads | native "worker/thread/process" word, consistent with Jobs |
| Now playing | live song screen | native |
| Leaderboards | online rankings on CH Leaderboards | native word for "leaderboard" (ja リーダーボード, ko 리더보드, ru Таблицы лидеров, zh 排行榜) |
| full combo (FC) | finishing without missing a note | community term: ja フルコンボ, ko 풀 콤보, ru фулл-комбо, zh 全连 (FC), de Full Combo, fr full combo, es full combo, pt-BR full combo; keep "FC" as the abbreviation |
| 5 stars / gold stars | rating | keep numerals + native "stars" |
| Expert / Hard / Medium / Easy | difficulty names | the names Clone Hero shows in that language's UI; if it stays English there, keep English |
| Guitar / Bass / Rhythm / Drums / Keys / Vocals | parts | same rule |
| Sync, offset | timing adjustment of lyrics/video | native word (Versatz, décalage, desfase, ずれ/オフセット ...) |
| Trash | OS trash/recycle bin | the OS's own word per platform: Papierkorb, Corbeille, Papelera, Lixeira, ゴミ箱, 휴지통, Корзина, 回收站/废纸篓 (use the neutral one if unsure) |
| Disabled Songs / Chart Packs / Deleted Songs | folder names created on disk | the folder names are **not** translated by the app; in text, show them in quotes exactly as in English |
| Songs folder, data folder | locations | native |
| Sign in / Sign out | cloud account | native |
| cool-down | YouTube temporary pause | "waiting period / pause" native word |
| Full Disk Access | macOS privacy setting | use Apple's localized name for the setting in that language |
| Scan Songs | Clone Hero's own menu item | keep English |

## Layout

Text is shown in narrow places (sidebar 190 px, table headers, chips, buttons). Prefer short wording for
`nav.*`, `lib.filter.*`, `lib.col.*`, `lib.act.*`, buttons and chip labels. Longer text is cut with "…" and
a tooltip shows the full text, but a short label is always better. Test with `?pseudo=1` and
`python3 scripts/i18n_layout_check.py <lang>`.

## Where strings come from

Keys are grouped by prefix: `nav.` sidebar, `lib.` library, `detail.` song panel, `charts.`, `spotify.`, `setlists.`,
`dupes.`, `jobs.` / `job.` (queue + step messages from the engine), `settings.`, `setup.`, `profile.`, `lb.`,
`np.` (Now Playing window), `toast.` (short notifications), `dlg.` (dialog texts), `yt.` / `ingame.` / `install.` /
`dl.` / `cloud.` / `api.` / `auth.` (messages produced by the Python backend and shown in the UI).

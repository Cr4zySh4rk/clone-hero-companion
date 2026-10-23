# Plan: accounts, cloud favourites, play stats, CH Leaderboards (v2.1.0)

## 0. Already done (don't redo)
- Supabase project `clone-hero-companion`, ref `muhboxuefazapytpqzjc`
  - URL `https://muhboxuefazapytpqzjc.supabase.co`
  - publishable key `sb_publishable_MrDFLihLJ8El-VQ3WvfhEQ_BArBKEWx` (public, safe to ship; RLS protects data)
- Schema applied: `supabase/migrations/0001_init.sql`
  - tables `profiles`, `favorites`, `plays`; RLS = own rows only
  - `user_id` defaults to `auth.uid()`
  - a trigger creates the profile row on signup
- Auth config
  - redirect allow list `http://127.0.0.1:47823/**` and `http://localhost:47823/**` (the app's fixed port, cli.py `--port` default 47823)
  - email signups disabled
  - Google provider is being enabled separately; code must not depend on it to pass tests

## 1. Rules
- No new runtime dependencies. Use stdlib `urllib` and the existing `chcompanion/net.py` helpers. The PyInstaller builds must keep working.
- Everything works signed out and offline. Cloud is an add-on; failures are logged quietly and retried later, never shown as job errors.
- Match the existing UI style (`web/app.js`, `web/app.css`, sidebar views) and the existing patterns in `api.py` routes and `app.py` settings.
- Never store tokens in the repo. The session goes to `config_dir/cloud_session.json`, chmod 600.
- Add tests in the repo's existing test style. Run the full test suite.
- Bump `__version__` to `2.1.0` and add a README section.
- Commit locally with a clear message. Do NOT push or tag.

## 2. `chcompanion/cloud.py`: auth and REST
- Google sign-in uses the PKCE flow:
  1. `start_login()` makes a verifier and an S256 challenge, then opens the system browser (`webbrowser.open`) at `{URL}/auth/v1/authorize?provider=google&redirect_to=http://127.0.0.1:{port}/auth/callback&code_challenge=…&code_challenge_method=s256`.
  2. A new route `GET /auth/callback?code=…` in api.py exchanges the code: `POST {URL}/auth/v1/token?grant_type=pkce` with JSON `{auth_code, code_verifier}` and header `apikey`.
  3. It saves the session (access_token, refresh_token, expires_at, user id/email/name/avatar from `user.user_metadata`).
  4. It returns a small styled HTML page: "Signed in to Clone Hero Companion. You can close this tab."
- Refresh with `POST /auth/v1/token?grant_type=refresh_token` about 60 s before expiry. Sign out with `POST /auth/v1/logout`, then delete the session file.
- `rest(method, path, params, body, prefer)` calls `{URL}/rest/v1/...` with headers `apikey` and `Authorization: Bearer <access>`. On 401 it refreshes once.
- API routes:
  - `GET /api/cloud/status` returns `{signedIn, email, name, avatar, lastSync, pending}`
  - `POST /api/cloud/login`
  - `POST /api/cloud/logout`
  - `POST /api/cloud/sync`

## 3. Favourites sync
- Find how favourites are stored now (libmgr: `fav`, `fav_sync`, `_fav_lock`, fav setlist).
- `song_key` = `norm_key(artist - name) + "|" + norm_key(charter)`.
- Also send:
  - `checksum`: chart MD5 if the library has it, otherwise null
  - artist, name, charter, album
  - `source`: whatever lets us reinstall it, e.g. the Encore md5/hash or the download URL the chart came from if we recorded it; otherwise just metadata
- Two-way merge:
  - Keep local tombstones for unfavourites made while signed in, so they delete in the cloud.
  - Upsert with `Prefer: resolution=merge-duplicates` and `on_conflict=user_id,song_key`.
  - Sync on sign-in, after each favourite change (debounced about 3 s), and at app start.
- New UI in the Favourites view: a section "In your cloud, not on this machine (N)" with cards and an **Install all** button (plus per-item install).
  - Resolve each item through the Encore source (`sources/encore.py`): by md5/hash if Encore supports it (check the API), else search `artist name` and pick the matching charter.
  - Queue the existing download jobs, with the "then add video, lyrics and metadata" option, and mark them favourite once installed.
  - Items that can't be found get "not found on Chorus Encore".

## 4. Play history and the Profile view
### Capturing plays
- Clone Hero overwrites `<game folder>/scorestats.json` after every finished song. The game folder comes from `paths`.
  - Example fields: `checksum` (MD5), `song_name`, `artist_name`, `charter_name` (has `<color>` tags, strip them), `playback_speed`, `score_timestamp`, `players[]` with `instrument`, `difficulty`, `score`, `stars`, `notes_hit`, `total_notes`, `max_streak`, `profile_name`, etc.
  - Accuracy = notes_hit / total_notes.
  - FC = notes_hit == total_notes. Check for an explicit field too.
- New `chcompanion/playstats.py` polls the file's mtime (reuse the log-watch thread if convenient) and appends one record per player to `config_dir/plays.jsonl`.
  - `client_id` = `checksum + score_timestamp + player index`; skip duplicates.
  - Add `duration_s` from the song length if known, and `machine` = hostname.
- Optional backfill: Clone Hero's `scoredata.bin` (best score per song and play counts). Research the format and locations:
  - mac `~/Library/Application Support/com.srylain.CloneHero/`
  - Windows `%USERPROFILE%\Documents\Clone Hero` or AppData LocalLow `srylain Inc_\Clone Hero`
  - Linux `~/.clonehero`
  - Only implement it if you can pin down the format reliably; otherwise skip it and say so.
- Cloud: upload unsynced plays in batches (upsert on `user_id,client_id`). On sign-in, pull all cloud plays into a local merged cache so stats combine machines.

### Profile view (new sidebar item "Profile")
- Header: Google avatar and name with Sign in / Sign out, or a "Sign in with Google to sync favourites and stats across machines" call to action.
- Filter: instrument and difficulty, default All.
- Tiles: total plays, hours played, distinct songs, average accuracy, FCs, 5-star (or gold) count, current and longest daily streak.
- Activity heatmap for the last 26 weeks, plus plays per weekday and hour (small bars).
- Lists (clicking opens the song in Library):
  - Most played
  - Best songs (highest best-accuracy, ≥2 plays or ≥200 notes)
  - Needs practice (lowest average accuracy, ≥2 plays)
  - Recent plays with score, stars, accuracy and difficulty
  - Personal bests
- `GET /api/stats?instrument=&difficulty=` computes all of this server-side from the local cache.

## 5. CH Leaderboards (read-only)
- Public API base `https://api.clonehero.net`, read endpoints under `/v1`. The site's JS bundle is saved at `/tmp/claude-0/-home-claude/aff4c31c-d46e-5526-915a-8d020e38b601/scratchpad/lb/*.js`; grep it for exact params.
- Known endpoints:
  - `/v1/search/quick?q=` gives songs `[{songHash,name,artist,charter,icon,playCount,playerCount}]`
  - `/v1/scores?songHash&instrument&difficulty&controllerTypes(rep)&minSpeed&maxSpeed&topScores&modifiers(rep)&accountId&sort`
  - `/v1/scores/latest-scores`
  - `/v1/stats/site-totals`
  - `/v1/stats/player-leaderboard`
  - `/v1/stats/song-leaderboard?tab&controllerGroup`
  - `/v1/account/{id}/profile`
  - `/v1/search` (users?)
- Submitting is impossible. The game submits replays itself in leaderboards mode, so say that in the UI.
- `chcompanion/leaderboard.py`: client with a 10-minute cache and a polite rate.
  - Map a local song to `songHash` with `search/quick` on `artist name`, choosing the matching artist, name and charter (strip tags, compare by norm_key).
  - Cache the mapping in the store.
  - Leaderboards only support guitar right now; handle empty results gracefully.
- UI:
  - Song detail in Library and the Now Playing screen get "Global top scores": top 10 for the chosen instrument/difficulty (default Expert guitar, or the difficulty just played), plus "you: #rank" when a CH username is linked.
  - Profile gets "CH Leaderboards account": enter or link a username (store in settings and in `profiles.ch_username`), then show their public profile stats and recent scores (`scores/latest-user-scores` needs their token, so use profile and `scores?accountId=`).
  - New sidebar view "Leaderboards": site totals tiles, top players, song leaderboard (most played), latest scores feed. Rows link to `https://leaderboards.clonehero.net/` pages where the URL pattern is discoverable from the bundle's routes.

## 6. Finish
- All tests pass. Do a quick manual smoke test by running the app headless (`python -m chcompanion run --no-gui` or whatever cli offers) and curling the new endpoints: stats with a fake scorestats.json, leaderboard search for "War Pigs".
- Report back briefly:
  - what was built
  - files changed
  - anything skipped or uncertain
  - the commit hash

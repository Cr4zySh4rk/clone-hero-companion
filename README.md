# Clone Hero Companion

A desktop app for [Clone Hero](https://clonehero.net) that manages your song library and makes every song look its best in the game:

- **HD music videos behind the highway.** For any selection of songs, it finds the official music video on YouTube and downloads it. Only **640p or better** is accepted (720p preferred, 1080p optional). It lines the video up with the song by matching the video's soundtrack against the chart audio.
- **Lyrics in the game.** Charts with a vocals track already show lyrics. For the rest, synced lyrics from [LRCLIB](https://lrclib.net) are written into the chart and aligned to the chart's audio.
- **One-click metadata.** Album, year, genre, song length and high-resolution album art come from Chorus Encore and iTunes.
- **Fast batch processing.** Select songs (or the whole library), press **Run**, and a pool of worker threads processes them in parallel. YouTube requests are paced, and the app backs off automatically when YouTube pushes back.
- **A real library manager:**
  - search, filters (no video, video below 640p, no lyrics, favourites, recently added, disabled) and sort on every column
  - multi-select, favourites and ratings
  - disable (moves the song out of the game's way), Move to Trash, rename folders by template, export chart packs (.zip)
  - duplicate finder, Clone Hero **setlists** and a music player
  - a per-song sync editor with a live video and audio preview, plus a video picker that only lists HD candidates
- **Get charts.** Search **Chorus Encore** and **RhythmVerse** with instrument and difficulty filters and download with one click. Google Drive, Mediafire, Dropbox and direct links are supported, as are .zip, .7z and .sng. New charts can get their video, lyrics and metadata straight away.
- **Spotify import.** Paste a public playlist link and the app finds a chart for every song and marks the ones you already have.
- **Works while you play.** The app follows the game through its log. It prepares a song as soon as you highlight it in the song list, and every song you play is ready next time. A karaoke-style *Now playing* screen (`/now`) is there for a second monitor, a TV or OBS.

It brings together the ideas of [Bridge](https://github.com/christopherwk210/bridge) (library management + Chorus browsing), [Clone Hero Chart Manager](https://github.com/xlzipx/clone-hero-chart-manager) (Encore/RhythmVerse search and download, Spotify import, setlists, duplicates) and [BackstageHero](https://github.com/jmb988/BackstageHero) (background videos lined up by audio, self-updating yt-dlp, throttle handling), in one standalone app.

## Download

From the [latest release](../../releases/latest):

| System | File |
| --- | --- |
| macOS 11+ (Apple Silicon **and** Intel, universal) | `CloneHeroCompanion-macOS-universal.dmg` |
| Windows 10/11 | `CloneHeroCompanion-windows-x64.exe` |
| Linux x64 | `CloneHeroCompanion-linux-x64.tar.gz` |

**macOS:** open the .dmg and drag **Clone Hero Companion** to Applications. The app isn't notarized, so the first time, right-click it and choose *Open*, then *Open* again. If macOS says the app "is damaged", run `xattr -cr "/Applications/Clone Hero Companion.app"` once in Terminal.

**Windows:** run the .exe. SmartScreen may warn because it isn't code-signed: *More info → Run anyway*. The window uses Microsoft Edge WebView2, which is part of Windows 10/11.

**Linux:** `tar xzf CloneHeroCompanion-linux-x64.tar.gz && ./CloneHeroCompanion`. The app window opens in Chrome/Chromium/Edge app mode if one is installed, otherwise in your browser.

ffmpeg is built in. Nothing else to install.

## Using it

1. In Clone Hero, turn on *Settings → Custom Content → Song Videos* (and keep *Lyrics* on).
2. Open Clone Hero Companion. On first start the **Game & folders** screen shows what it found: the Clone Hero game, its data folder (the one with `settings.ini` and `Logs`) and every song folder. Confirm them, or use **Browse…** to point at any other location or drive; Clone Hero and your songs can be anywhere, on all three platforms. You can change them later from the same screen.
3. Select songs (click, ⌘/Ctrl-click, Shift-click, ⌘/Ctrl-A for everything shown), press **Run**, pick what to get (video, lyrics, metadata, replace existing videos) and **Start**. Progress shows at the bottom and in *Jobs*.
4. Keep the app open while you play, and new songs get prepared as you browse.

**Favourites:** click the ☆ on any song, select songs and choose *Add to favourites*, or press **F**. *★ Favourites* in the sidebar lists them, and a **Favourites** setlist is kept in sync so they're one click away inside Clone Hero too (you can turn that off in Settings).

**macOS and Full Disk Access:** without it, macOS asks separately before the app may look into Documents, Desktop, Downloads or external drives. *Game & folders → Grant Full Disk Access…* opens the right page of System Settings: switch **Clone Hero Companion** on, then choose *Quit & Reopen* (or *Restart the app*). The app is ad-hoc signed, so after installing a new version you may need to switch it off and on again.

After adding or downloading charts, run *Settings → Scan Songs* in Clone Hero so the game sees them. Setlist changes show after restarting the game.

### The 640p rule

A video's quality is measured the way YouTube labels it: `max(height, width × 9/16)`. So a widescreen 1280×526 video counts as 720p, while 854×480, 640×480 (4:3) and anything smaller are rejected. If none of a song's candidate videos exist in 640p or better, the song is marked *none found* rather than getting a low-quality video. Songs that already have a video below 640p are flagged in the library (*Video below 640p* filter), and Run replaces them.

### Sync

Each downloaded video is lined up automatically. Its soundtrack and the chart's audio stems are compared as onset curves in three windows of the song, and an offset is used only when at least two windows agree. Videos that must start *after* the song get black frames added at the start, so they don't rely on a negative `video_start_time`. To fine-tune a song, select it, press **Preview** in the detail panel, drag the *Video* or *Lyrics* slider until it lines up, then press **Save sync to game**.

### What gets changed in a song

| | Song folder | `.sng` package |
| --- | --- | --- |
| Video | `video.mp4` (H.264; `video.webm` VP8 on Linux) | added inside the package |
| Video alignment | `video_start_time` in `song.ini` | `video_start_time` in the package metadata |
| Lyrics (charts without lyrics) | vocals track in `notes.mid` / lyric events in `notes.chart` | same, inside the package |
| Metadata | `album`, `year`, `genre`, `song_length` in `song.ini`, `album.jpg` | package metadata, `album.jpg` |
| Backup | `*.chc-backup` next to each changed file | `<name>.sng.chc-backup` |

*Undo changes* (or `CloneHeroCompanion restore`) puts the originals back. Adding lyrics changes the chart file, so Clone Hero starts a new high-score list for that chart. Your old scores return if you undo. Turn off *Settings → In the game → Lyrics* to leave charts untouched; videos and metadata don't affect scores.

*Disable* moves songs to `<Clone Hero folder>/Disabled Songs`, where the game doesn't look. *Move to Trash* uses the system Trash / Recycle Bin.

## Command line

The same program has a command line (the app opens when it's started without arguments):

```
CloneHeroCompanion prepare [FILTER...] [--no-video] [--no-lyrics] [--force] [--dry-run]
CloneHeroCompanion restore [FILTER...]
CloneHeroCompanion doctor
CloneHeroCompanion run [--no-browser] [--host 0.0.0.0] [--port 47823]     # background service only
```

On macOS the binary is `"/Applications/Clone Hero Companion.app/Contents/MacOS/Clone Hero Companion"`.

## How it follows the game

Clone Hero writes `<data folder>/Logs/Session-*.log`. *Loading Song: Artist - Name* marks a song start. Seeks while playing are pauses (with exact song time), a 1.5 s rewind seek is a resume, *Replay input count* is the results screen. Seeks in the menus match a song's `preview_start_time`, which is how the app knows what's highlighted.

## Running from source

```
python -m pip install -r requirements.txt pywebview send2trash py7zr
python -m chcompanion           # app window
python -m unittest discover -s tests
```

The packaged app keeps yt-dlp up to date by itself (YouTube changes often). Builds come from GitHub Actions: pushing a tag like `v2.0.0` publishes the release.

# Clone Hero Companion

A small background app for [Clone Hero](https://clonehero.net) that works out which song you're playing and shows a sing-along screen for it:

- **The song's music video**, found on YouTube automatically and kept in sync with the game (muted, so the game audio stays in charge).
- **Karaoke-style lyrics** that fill in syllable by syllable. They come from the chart's own vocal track when it has one (Rock Band / Guitar Hero rips and most customs), otherwise from [LRCLIB](https://lrclib.net) line-synced lyrics.
- **Optional in-game videos**: one click (or `install-videos`) saves the video into the song folder, so Clone Hero plays it natively behind the highway.

It runs on **Windows, macOS and Linux**. It only reads the game's log file and your song folders. It never touches the game itself.

## Download

Grab the file for your system from the [latest release](../../releases/latest):

| System | File |
| --- | --- |
| Windows 10/11 | `CloneHeroCompanion-windows-x64.exe` |
| macOS, Apple Silicon (M1 and later) | `CloneHeroCompanion-macos-arm64.zip` |
| macOS, Intel | `CloneHeroCompanion-macos-x64.zip` |
| Linux x64 | `CloneHeroCompanion-linux-x64.tar.gz` |

**Windows:** double-click the `.exe`. SmartScreen may warn because the app isn't code-signed: click *More info → Run anyway*.

**macOS:** unzip, then double-click `CloneHeroCompanion`. The first time, macOS blocks unsigned apps. Right-click it, choose *Open*, then *Open* again. Or run `xattr -d com.apple.quarantine CloneHeroCompanion` once in Terminal.

**Linux:** `tar xzf CloneHeroCompanion-linux-x64.tar.gz && ./CloneHeroCompanion`

## Using it

1. Start the companion. It finds your Clone Hero data folder by itself and opens the companion page in your browser (`http://127.0.0.1:47823`).
2. Start Clone Hero and play a song. The page switches to that song's video and lyrics.
3. Drag the browser window to a second monitor or TV and press **F** for fullscreen. You can also add the page to OBS as a *Browser Source*; add `?obs` to the address for a transparent background.

Leave the companion running in the background for as long as you play. Close its window or press Ctrl+C to quit.

### Getting the sync right

The app keeps a song clock from the game's log. Every pause in Clone Hero tells the app the exact song position, so it re-syncs on each pause and resume. It also learns how long your machine takes to load a song, so sync improves after the first pause.

If something is still off for a particular song, move the mouse over the page to show the controls, or use the keyboard:

| Key | Action |
| --- | --- |
| ← / → | Lyrics earlier / later by 0.1 s (Shift: 0.5 s) |
| ↓ / ↑ | Video earlier / later by 0.1 s (Shift: 1 s) |
| N / P | Next / previous YouTube result for this song |
| U | Use a YouTube link you paste |
| I | Save the video into the song folder for in-game playback |
| V | Video on / off |
| F | Fullscreen |
| H | Hide the song card and controls |

Offsets and video choices are remembered per song.

### Videos inside Clone Hero

Clone Hero plays a `video.mp4` / `video.webm` placed in a song's folder behind the highway when *Settings → Custom Content → Song Videos* is on. The companion can put the right video there for you:

- Press **I** (or *Show in game*) on the companion page while a song is loaded. The video appears in-game the next time you play it.
- Or start the companion with `--auto-install` to do this for every song you play.
- Or fill your whole library in one go:

  ```
  CloneHeroCompanion install-videos                 # every song without a video
  CloneHeroCompanion install-videos metallica       # only songs matching "metallica"
  CloneHeroCompanion install-videos --dry-run       # show which video each song would get
  ```

If you shifted a video with ↓ / ↑, that offset is saved into the song's `song.ini` as `video_start_time` so the game uses it too.

Notes: `.sng` packages can't hold an extra file, so those songs keep their video on the companion screen only. On Linux, Clone Hero only plays VP8 WebM, so `ffmpeg` must be installed for in-game videos there.

## Command line options

```
CloneHeroCompanion [run] [--data-dir PATH] [--songs-dir PATH] [--port 47823] [--host 127.0.0.1]
                         [--no-browser] [--no-youtube] [--no-lrclib] [--auto-install] [--max-height 1080]
CloneHeroCompanion install-videos [FILTER...] [--force] [--limit N] [--dry-run]
CloneHeroCompanion doctor        # check that everything is found
CloneHeroCompanion lyrics "Artist - Song"   # print the synced lyrics it found
```

- `--host 0.0.0.0` lets a phone, tablet or smart TV on your network open the page at `http://<your-computer-ip>:47823`.
- The data folder defaults to `Documents\Clone Hero` (Windows), `~/Clone Hero` (macOS) or `~/.clonehero` (Linux). Use `--data-dir` if yours is somewhere else.
- Extra song folders listed in Clone Hero's settings are picked up automatically.

## How it works

Clone Hero writes a session log to `<data folder>/Logs/Session-*.log`. The companion follows it:

| Log line | Meaning |
| --- | --- |
| `Loading Song: Artist - Name` | a song was started (matched against `song.ini` / `.sng` metadata) |
| `Seeking to song time:X` while playing | paused at X seconds |
| `Seeking to song time:X−1.5` while paused | resumed (the game rewinds 1.5 s) |
| any other seek while paused | back in the menus |
| `Replay input count` / `Completing session` | song finished |

Lyrics are read from `PART VOCALS` in `notes.mid` (lyric events, phrase markers 105/106 and note lengths), or the `[Events]` lyrics in `notes.chart`. `.sng` packages are read directly. Videos are found with [yt-dlp](https://github.com/yt-dlp/yt-dlp), and the video-only stream is passed through the local server so the page can seek it precisely. If streaming fails, the page falls back to YouTube's embedded player.

Settings and learned values live in `%APPDATA%\CloneHeroCompanion` (Windows), `~/Library/Application Support/CloneHeroCompanion` (macOS) or `~/.config/CloneHeroCompanion` (Linux).

## Running from source

```
python -m pip install -r requirements.txt
python -m chcompanion            # same as the executable
python -m unittest discover -s tests
```

Installing [Deno](https://deno.com) makes yt-dlp's YouTube support more robust (it uses it to solve YouTube's JavaScript challenges). It works without it today, but YouTube changes often.

Executables are built by GitHub Actions with PyInstaller. Push a tag like `v1.2.3` to publish a release.

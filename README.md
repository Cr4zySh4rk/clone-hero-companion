# Clone Hero Companion

A small background app for [Clone Hero](https://clonehero.net) that adds **music videos** and **sing-along lyrics** to your songs, inside the game.

- **Music video behind the highway.** The official video is found on YouTube and saved into the song (Clone Hero's own *Song Videos* feature plays it). It is **lined up with the song automatically**: the video's soundtrack is cross-correlated with the chart audio, so intros and extra scenes don't throw it off.
- **Lyrics in the game.** Charts with a vocals track already show lyrics in Clone Hero. For charts without one, synced lyrics from [LRCLIB](https://lrclib.net) are written into the chart, also auto-aligned to the chart's audio (rips often have a few seconds of extra lead-in compared to the album).
- **Ready before you press play.** While you browse the song list, the companion notices which song is highlighted (from the preview the game plays) and prepares it in the background. Every song you play is prepared for next time, and you can prepare the whole library in one go.
- **Optional companion screen.** A karaoke-style page (`http://127.0.0.1:47823`) shows the same video and lyrics with a syllable-by-syllable wipe, for a second monitor, a TV for the family, or an OBS browser source.

Runs on **Windows, macOS and Linux**. It follows the game through its log file and never touches the game itself. Everything it changes in your songs is backed up and can be undone with `restore`.

## Download

Get the file for your system from the [latest release](../../releases/latest):

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

1. In Clone Hero, turn on *Settings → Custom Content → Song Videos* (and keep *Lyrics* on).
2. Start the companion and leave it running while you play. It finds your Clone Hero data folder by itself.
3. Play as usual. Songs you highlight or play get their video and lyrics. When a song is still being prepared as you start it, it'll have them from the next play.

To prepare your whole library at once (about 20 MB of disk per song at 720p):

```
CloneHeroCompanion prepare                 # every song
CloneHeroCompanion prepare metallica       # only songs matching "metallica"
CloneHeroCompanion prepare --dry-run       # just show which video each song would get
```

Or click *Prepare library* on the companion page, or start the app with `--prepare-library`.

To undo everything: `CloneHeroCompanion restore` (or `restore <filter>` for some songs). This removes the videos it added and puts back the original charts and `.sng` files.

### Troubleshooting

- **No video in game:** check *Settings → Custom Content → Song Videos* is on. If a song was prepared while Clone Hero was already showing it, open another song and come back, or run *Scan Songs* in the game settings.
- **Video or lyrics a bit early or late:** open the companion page while the song plays, nudge with ↑/↓ (video) or ←/→ (lyrics), then press **I**. The song is rewritten with your offsets as soon as it's not playing.
- **Wrong video:** press **N** on the companion page for the next search result, or **U** to paste a YouTube link, then **I**.
- `CloneHeroCompanion doctor` shows what the app found (data folder, songs, ffmpeg, game settings).

### What gets changed in a song

| | Song folder | `.sng` package |
| --- | --- | --- |
| Video | `video.mp4` added (`video.webm` on Linux); still-image uploads are skipped | video added inside the package |
| Video alignment | `video_start_time` in `song.ini` (a video that must start *after* the song gets black frames added instead) | `video_start_time` in the package metadata |
| Lyrics (only charts without lyrics) | vocals track added to `notes.mid` / lyrics events to `notes.chart` | same, inside the package |
| Backup | `notes.mid.chc-backup`, `song.ini.chc-backup` | `<name>.sng.chc-backup` |

Adding lyrics changes the chart file, so Clone Hero treats it as a new chart for **local high scores**. Your previous scores come back if you `restore`. Use `--no-game-lyrics` if you'd rather keep the charts untouched; videos don't affect scores.

### The companion screen

Open `http://127.0.0.1:47823` (it opens by itself on first launch). Move the mouse to show the controls, or use the keyboard:

| Key | Action |
| --- | --- |
| ← / → | Lyrics earlier / later by 0.1 s (Shift: 0.5 s) |
| ↓ / ↑ | Video earlier / later by 0.1 s (Shift: 1 s) |
| N / P | Next / previous YouTube result for this song |
| U | Use a YouTube link you paste |
| I | Update this song in the game with your changes (video choice, offsets) |
| V | Video on / off |
| F | Fullscreen |
| H | Hide the song card and controls |

Offsets and video choices are remembered per song, and *Update in game* (I) writes them into the song. Clicking the page takes focus away from the game, and with *Pause on focus lost* on, Clone Hero pauses. The page doesn't need clicks while you play. For OBS, add `?obs` to the address for a transparent background.

## Command line

```
CloneHeroCompanion [run] [--data-dir PATH] [--songs-dir PATH] [--port 47823] [--host 127.0.0.1]
                         [--no-in-game] [--no-prefetch] [--no-game-lyrics] [--prepare-library]
                         [--open-browser | --no-browser] [--no-youtube] [--no-lrclib]
                         [--game-max-height 720] [--max-height 1080]
CloneHeroCompanion prepare [FILTER...] [--no-video] [--no-lyrics] [--force] [--limit N] [--dry-run]
CloneHeroCompanion restore [FILTER...]
CloneHeroCompanion doctor
CloneHeroCompanion lyrics "Artist - Song"
```

- `--host 0.0.0.0` lets a phone, tablet or smart TV on your network open the page at `http://<your-computer-ip>:47823`.
- The data folder defaults to `Documents\Clone Hero` (Windows), `~/Clone Hero` (macOS) or `~/.clonehero` (Linux). Use `--data-dir` if yours is elsewhere. Extra song folders from Clone Hero's settings are picked up automatically.
- Starting the app again while it is running just opens the page.

## How it works

Clone Hero writes a session log to `<data folder>/Logs/Session-*.log`. The companion follows it:

| Log line | Meaning |
| --- | --- |
| `Seeking to song time:X` in the menus | a song preview started; matched against `preview_start_time` to find the highlighted song |
| `Loading Song: Artist - Name` | a song was started |
| `Seeking to song time:X` while playing | paused at X seconds (exact clock re-sync) |
| `Seeking to song time:X−1.5` while paused | resumed (the game rewinds 1.5 s) |
| `Replay input count` / `Completing session` | song finished |

The time between *Loading Song* and the song's first beat is learned from your pauses, so the companion screen gets more precise as you play.

**Video alignment:** the video's audio and the chart's audio stems are decoded to 8 kHz with ffmpeg, turned into onset curves and cross-correlated in three separate windows. An offset is only used when at least two windows agree and the match is strong; otherwise the video starts at 0. **Lyric alignment** works the same way against an upload of the album recording that LRCLIB's timestamps refer to. On test songs, the median lyric error went from 2.8–5.5 s to 0.2–0.5 s.

Lyrics are read from `PART VOCALS` in `notes.mid` (lyric events, phrase markers 105/106 and note lengths) or the `[Events]` lyrics in `notes.chart`. `.sng` packages are read and rewritten directly. Videos come from [yt-dlp](https://github.com/yt-dlp/yt-dlp). Auto-generated "Topic" uploads (a still image) and gameplay recordings are skipped.

Settings and learned values live in `%APPDATA%\CloneHeroCompanion` (Windows), `~/Library/Application Support/CloneHeroCompanion` (macOS) or `~/.config/CloneHeroCompanion` (Linux).

## Running from source

```
python -m pip install -r requirements.txt
python -m chcompanion
python -m unittest discover -s tests
```

Installing [Deno](https://deno.com) makes yt-dlp's YouTube support more robust. It works without it today, but YouTube changes often. Executables are built by GitHub Actions with PyInstaller; push a tag like `v1.2.3` to publish a release.

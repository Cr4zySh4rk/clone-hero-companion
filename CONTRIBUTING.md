# Contributing

Bug reports, ideas and pull requests are all welcome.

## Reporting bugs

Use the [bug report form](https://github.com/Cr4zySh4rk/clone-hero-companion/issues/new/choose) and include your system, the app version (Settings → About) and the end of `companion.log`. For a song-specific problem (wrong video, sync is off), the song's name and chart author help a lot.

## Development

```
python -m pip install -r requirements.txt pywebview send2trash py7zr pillow
python -m chcompanion                    # app window
python -m chcompanion run --no-browser   # background service only, UI at http://127.0.0.1:47823
python -m unittest discover -s tests
```

- Python 3.10+; the UI is plain HTML/CSS/JS in `chcompanion/web/` (no build step).
- `chcompanion/ingame.py` writes videos and lyrics into songs; `youtube.py` holds the search, quality rules and pacing; `libmgr.py` and `api.py` back the library UI.
- Keep the 640p minimum: videos below it must never be written into a song.
- Anything that modifies a chart must back up the original first (see `_backup` in `ingame.py`).

Releases are built by GitHub Actions for macOS (universal2), Windows and Linux when a `v*` tag is pushed.

By contributing you agree that your contribution is licensed under the GPL-3.0.

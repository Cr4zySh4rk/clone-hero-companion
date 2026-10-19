"""Find the time offset between a music video's soundtrack and the chart's audio.

Both signals are decoded to mono 8 kHz with ffmpeg, turned into onset-strength
curves (10 ms hops) and cross-correlated. The result is the number of seconds to
skip into the video so that it lines up with the song (Clone Hero's
`video_start_time`, positive = the video has a longer intro).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)
SR = 8000
HOP = 80            # 10 ms
AUDIO_STEMS = ("song", "guitar", "bass", "rhythm", "keys", "vocals", "vocals_1", "vocals_2",
               "drums", "drums_1", "drums_2", "drums_3", "drums_4")
AUDIO_EXT = (".ogg", ".opus", ".mp3", ".wav", ".flac", ".m4a")


def ffmpeg_path() -> str | None:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def chart_audio_files(names: list[str]) -> list[str]:
    out = []
    for n in names:
        stem, dot, ext = n.lower().rpartition(".")
        if dot and "." + ext in AUDIO_EXT and stem in AUDIO_STEMS:
            out.append(n)
    return out


def decode(paths: list[Path], seconds: float = 240.0, start: float = 0.0):
    import numpy as np
    ff = ffmpeg_path()
    if not ff:
        raise RuntimeError("ffmpeg not available")
    cmd = [ff, "-hide_banner", "-loglevel", "error"]
    for p in paths:
        cmd += ["-ss", str(start), "-t", str(seconds), "-i", str(p)]
    if len(paths) > 1:
        cmd += ["-filter_complex", f"amix=inputs={len(paths)}:normalize=0:duration=longest"]
    cmd += ["-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[-300:])
    return np.frombuffer(r.stdout, dtype=np.float32)


def onset_curve(x):
    import numpy as np
    n = len(x) // HOP
    if n < 10:
        return np.zeros(1)
    frames = x[: n * HOP].reshape(n, HOP)
    e = np.log1p(1000 * np.sqrt((frames ** 2).mean(axis=1)))
    d = np.diff(e, prepend=e[0])
    d[d < 0] = 0
    d -= d.mean()
    s = d.std()
    return d / s if s > 0 else d


def _xcorr(a, b, max_lag_s: float, min_lag_s: float):
    """Best lag k (in hops) maximising sum a[t]*b[t+k], k in [min, max]; returns (k, z)."""
    import numpy as np
    n = 1
    while n < len(a) + len(b):
        n *= 2
    corr = np.fft.irfft(np.conj(np.fft.rfft(a, n)) * np.fft.rfft(b, n), n)
    lo, hi = int(round(min_lag_s * SR / HOP)), int(round(max_lag_s * SR / HOP))
    ks = np.arange(lo, hi + 1)
    vals = corr[ks % n]
    i = int(np.argmax(vals))
    mask = np.ones_like(vals, dtype=bool)
    mask[max(0, i - 50): i + 50] = False
    rest = vals[mask]
    z = (vals[i] - rest.mean()) / (rest.std() + 1e-9) if rest.size else 0.0
    return int(ks[i]), float(z)


def find_offset(song_audio: list[Path], video_audio: Path, max_lag: float = 90.0) -> tuple[float, float]:
    """Returns (offset_seconds, confidence). offset > 0: skip that much video.
    The song is matched in two separate windows; confidence is 0 unless both agree."""
    vid = onset_curve(decode([video_audio], seconds=420.0))
    results = []
    for start, dur in ((5.0, 70.0), (75.0, 70.0)):
        seg = onset_curve(decode(song_audio, seconds=dur, start=start))
        if len(seg) < 1000 or not seg.any():
            continue
        # seg[t] (song time start+t) matches vid[t + k]  =>  offset = k*hop - start
        k, z = _xcorr(seg, vid, max_lag_s=start + max_lag, min_lag_s=max(0.0, start - max_lag))
        results.append((k * HOP / SR - start, z))
    if not results:
        return 0.0, 0.0
    if len(results) == 2 and abs(results[0][0] - results[1][0]) > 0.15:
        log.debug("alignment windows disagree: %s", results)
        return results[0][0] if results[0][1] > results[1][1] else results[1][0], 0.0
    off = sum(r[0] for r in results) / len(results)
    return float(off), float(min(r[1] for r in results))


def vocal_activity(paths: list[Path], seconds: float = 600.0):
    """Per-hop (10 ms) 0/1 curve of when the vocal stem is audible."""
    import numpy as np
    x = decode(paths, seconds=seconds)
    n = len(x) // HOP
    if n < 100:
        return np.zeros(1)
    rms = np.sqrt((x[: n * HOP].reshape(n, HOP) ** 2).mean(axis=1))
    # smooth over ~100 ms and threshold relative to the loud parts
    k = np.ones(10) / 10
    rms = np.convolve(rms, k, mode="same")
    thr = max(np.percentile(rms, 95) * 0.12, 1e-4)
    return (rms > thr).astype(np.float32)


def lyric_activity(lines: list[dict], n: int):
    import numpy as np
    a = np.zeros(n, dtype=np.float32)
    for line in lines:
        for s in line["syls"]:
            i, j = int(s["t"] * SR / HOP), int(max(s["e"], s["t"] + 0.15) * SR / HOP)
            if i < n:
                a[i:min(j, n)] = 1.0
    return a


def lyrics_offset(vocal_paths: list[Path], lines: list[dict], max_lag: float = 20.0) -> tuple[float, float]:
    """Seconds to add to the lyric timestamps so they line up with the vocal stem,
    and a confidence (0 when the two halves of the song disagree)."""
    import numpy as np
    voc = vocal_activity(vocal_paths)
    if not voc.any() or len(lines) < 4:
        return 0.0, 0.0
    v = voc - voc.mean()
    results = []
    half = len(lines) // 2
    for part in (lines[:half], lines[half:]):
        la = lyric_activity(part, len(voc))
        if not la.any():
            continue
        la -= la.mean()
        # la[t] (lyric time) matches v[t + k]  =>  shift lyrics by +k
        k, z = _xcorr(la, v, max_lag_s=max_lag, min_lag_s=-max_lag)
        results.append((k * HOP / SR, z))
    if len(results) < 2:
        return 0.0, 0.0
    if abs(results[0][0] - results[1][0]) > 0.35:
        log.debug("lyric alignment halves disagree: %s", results)
        return 0.0, 0.0
    return (results[0][0] + results[1][0]) / 2, min(r[1] for r in results)

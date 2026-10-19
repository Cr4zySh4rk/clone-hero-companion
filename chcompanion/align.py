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


def decode(paths: list[Path], seconds: float = 240.0):
    import numpy as np
    ff = ffmpeg_path()
    if not ff:
        raise RuntimeError("ffmpeg not available")
    cmd = [ff, "-hide_banner", "-loglevel", "error"]
    for p in paths:
        cmd += ["-t", str(seconds), "-i", str(p)]
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


def find_offset(song_audio: list[Path], video_audio: Path, max_lag: float = 90.0) -> tuple[float, float]:
    """Returns (offset_seconds, confidence). offset > 0: skip that much video."""
    import numpy as np
    a = onset_curve(decode(song_audio))
    b = onset_curve(decode([video_audio], seconds=240.0 + max_lag))
    n = 1
    while n < len(a) + len(b):
        n *= 2
    fa, fb = np.fft.rfft(a, n), np.fft.rfft(b, n)
    corr = np.fft.irfft(np.conj(fa) * fb, n)   # corr[k] = sum a[t] * b[t+k]
    max_k = int(max_lag * SR / HOP)
    lags = np.concatenate([np.arange(0, max_k + 1), np.arange(-max_k, 0)])
    vals = np.concatenate([corr[: max_k + 1], corr[n - max_k:]])
    i = int(np.argmax(vals))
    best = vals[i]
    overlap = min(len(a), len(b))
    peak = best / max(overlap, 1)
    # confidence: how far the peak stands above the rest of the lag range
    mask = np.ones_like(vals, dtype=bool)
    mask[max(0, i - 50): i + 50] = False
    rest = vals[mask]
    z = (best - rest.mean()) / (rest.std() + 1e-9) if rest.size else 0.0
    offset = lags[i] * HOP / SR
    log.debug("alignment: offset %.2fs peak %.3f z %.1f", offset, peak, z)
    return float(offset), float(z)

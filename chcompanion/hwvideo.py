"""How videos become VP8 WebM: the ffmpeg settings, and which hardware (if any) helps.

What can run on a GPU: DECODING the source (H.264/VP9/AV1 from YouTube) and SCALING it, through NVIDIA CUDA/NVDEC,
VideoToolbox (macOS), D3D11VA (Windows) or VAAPI (Linux Intel/AMD). The VP8 ENCODE itself is CPU (libvpx) almost
everywhere: NVENC, AMF, QSV and VideoToolbox have no VP8 encoder. The one exception is `vp8_vaapi` on some Intel
iGPUs under Linux, which is used only after it passed a self-test and each output is verified.

Everything is probed once per session through process.run, each path is self-tested on a 1-second clip, and
`plan()` returns the paths to try in order, always ending with the plain CPU path. A path that fails in a job is
switched off for the rest of the session (the job just continues with the next path).
"""
from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

from .process import run as run_process

log = logging.getLogger(__name__)

# --------------------------------------------------------------- VP8 settings (CPU)
# Measured on a 2-core box with a 720p30 H.264 test clip (see the 2.3.1 notes): `good`, cpu-used 5 with no
# alt-ref/lag and token partitions (-slices, which is what makes libvpx VP8 use several threads in ffmpeg)
# is the fastest setting whose quality stays at the old one.
VP8_RATE = ("-b:v", "2500k", "-crf", "10")
VP8_SPEED = ("-deadline", "good", "-cpu-used", "5", "-auto-alt-ref", "0", "-lag-in-frames", "0", "-slices", "4")
MAX_HEIGHT = 720          # larger sources are scaled down; the source frame rate is kept


def encode_slots() -> int:
    """How many encodes run at once: few big ones beat many small ones (each is multithreaded)."""
    return max(1, min(4, (os.cpu_count() or 2) // 4))


def encode_threads() -> int:
    """Threads per encode. ffmpeg's default for libvpx is half the cores, so be explicit."""
    return max(2, min(8, (os.cpu_count() or 2) // encode_slots()))


def target_size(w: int, h: int, max_height: int = MAX_HEIGHT) -> tuple[int, int] | None:
    """(w, h) to scale down to (even numbers), None when the source is not taller than max_height."""
    if not w or not h or h <= max_height:
        return None
    return int(round(w * max_height / h / 2)) * 2, max_height


def _pad_filter(pad: float) -> list[str]:
    return [f"tpad=start_duration={pad:.3f}:color=black"] if pad > 0.001 else []


def vp8_args(threads: int | None = None, pad: float = 0.0, max_height: int = MAX_HEIGHT) -> list[str]:
    """CPU path output arguments: no audio, at most `max_height` high, source fps, and `pad` seconds of black in
    front (the same single encode)."""
    vf = _pad_filter(pad) + [f"scale=-2:'min({max_height},ih)'"]
    return ["-map", "0:v:0", "-vf", ",".join(vf), "-c:v", "libvpx", "-pix_fmt", "yuv420p", *VP8_RATE, *VP8_SPEED,
            "-threads", str(threads or encode_threads())]


# --------------------------------------------------------------- paths
@dataclass(frozen=True)
class HwPath:
    key: str                    # cpu | cuda | videotoolbox | d3d11va | vaapi | vaapi_enc
    decode: str                 # label of the decoder ("" = CPU)
    encode: str                 # label of the encoder ("" = CPU libvpx)
    device: str = ""            # VAAPI render node
    gpu_scale: bool = False     # scale on the GPU when the source is taller than 720p and nothing is padded
    rank: int = 0               # lower = preferred


CPU = HwPath("cpu", "", "")

_DECODE_NAMES = {"cuda": "NVIDIA", "videotoolbox": "VideoToolbox", "d3d11va": "Direct3D 11", "vaapi": "VAAPI"}


def build(path: HwPath, w: int = 0, h: int = 0, pad: float = 0.0, threads: int | None = None) -> tuple[list[str], list[str]]:
    """(arguments before -i, arguments after it) for this path. Source size (w, h) decides on GPU scaling."""
    if path.key == "cpu":
        return [], vp8_args(threads, pad)
    size = target_size(w, h)
    gpu_frames = bool(size) and path.gpu_scale and not pad     # scale on the GPU, only without padding
    if path.key == "vaapi_enc":
        pre = ["-vaapi_device", path.device]
        if not pad:
            pre = ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi", *pre]
            vf = [f"scale_vaapi=w={size[0]}:h={size[1]}:format=nv12"] if size else ["scale_vaapi=format=nv12"]
        else:                    # padding is a software filter: decode on the GPU, pad+scale, upload, encode
            pre = ["-hwaccel", "vaapi", *pre]
            vf = _pad_filter(pad) + ([f"scale={size[0]}:{size[1]}"] if size else []) + ["format=nv12", "hwupload"]
        return pre, ["-map", "0:v:0", "-vf", ",".join(vf), "-c:v", "vp8_vaapi", *VP8_RATE[:2]]
    # GPU decode, libvpx encode
    out = ["-map", "0:v:0", "-c:v", "libvpx", "-pix_fmt", "yuv420p", *VP8_RATE, *VP8_SPEED,
           "-threads", str(threads or encode_threads())]
    if gpu_frames and path.key == "cuda":
        pre = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        vf = [f"scale_cuda={size[0]}:{size[1]}", "hwdownload", "format=nv12"]
    elif gpu_frames and path.key == "vaapi":
        pre = ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi", "-vaapi_device", path.device]
        vf = [f"scale_vaapi=w={size[0]}:h={size[1]}:format=nv12", "hwdownload", "format=nv12"]
    else:                        # decode on the GPU, frames come back to memory; scale/pad on the CPU
        pre = ["-hwaccel", path.key]
        if path.key == "vaapi":
            pre += ["-vaapi_device", path.device]
        vf = _pad_filter(pad) + [f"scale=-2:'min({MAX_HEIGHT},ih)'"]
    return pre, [*out[:2], "-vf", ",".join(vf), *out[2:]]


# --------------------------------------------------------------- probing
def parse_hwaccels(text: str) -> set[str]:
    out, on = set(), False
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("hardware acceleration methods"):
            on = True
        elif on and re.fullmatch(r"[a-z0-9_]+", line):
            out.add(line)
    return out


def parse_encoders(text: str) -> set[str]:
    return {m.group(1) for line in text.splitlines()
            if (m := re.match(r"\s*V[A-Z.]{5}\s+(\S+)\s", line))}


def render_nodes() -> list[tuple[str, str]]:
    """[(device path, vendor id)] of the DRM render nodes (Linux)."""
    out = []
    try:
        for p in sorted(Path("/dev/dri").glob("renderD*")):
            try:
                vendor = (Path("/sys/class/drm") / p.name / "device" / "vendor").read_text().strip()
            except OSError:
                vendor = ""
            out.append((str(p), vendor))
    except OSError:
        pass
    return out


def _vaapi_rank(vendor: str) -> int:
    return 5 if vendor == "0x8086" else 2      # an Intel iGPU is the integrated one; AMD (0x1002) counts as discrete


@dataclass
class Caps:
    paths: list[HwPath] = field(default_factory=list)      # working hardware paths, best first
    note: str = ""


_lock = threading.Lock()
_caps: Caps | None = None
_disabled: set[str] = set()


def _ff() -> str | None:
    from .align import ffmpeg_path
    return ffmpeg_path()


def _try(ff: str, args: list[str], timeout: int = 60) -> tuple[bool, str]:
    try:
        r = run_process([ff, "-hide_banner", "-nostdin", "-loglevel", "error", *args], capture_output=True,
                        text=True, timeout=timeout)
    except Exception as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or "")[-300:]


def count_frames(ff: str, p: Path) -> int:
    """Video frames in a file (decoded to nowhere); 0 when it can't be read."""
    try:
        r = run_process([ff, "-hide_banner", "-nostdin", "-i", str(p), "-map", "0:v:0", "-f", "null", "-"],
                        capture_output=True, text=True, timeout=600)
    except Exception:
        return 0
    n = re.findall(r"frame=\s*(\d+)", (r.stderr or "").replace("\r", "\n"))
    return int(n[-1]) if n and r.returncode == 0 else 0


def codec_of(ff: str, p: Path) -> str:
    try:
        err = run_process([ff, "-hide_banner", "-i", str(p)], capture_output=True, text=True, timeout=30).stderr
    except Exception:
        return ""
    m = re.search(r"Video:\s*([A-Za-z0-9_]+)", err or "")
    return m.group(1).lower() if m else ""


def verify_output(ff: str, out: Path, expect_frames: int) -> bool:
    """A hardware-made file must be VP8 and have (about) all the frames of the source."""
    if codec_of(ff, out) != "vp8":
        return False
    n = count_frames(ff, out)
    return n > 0 and (not expect_frames or n >= expect_frames * 0.98)


def probe(ff: str | None = None) -> Caps:
    """Find the working hardware paths: lists what ffmpeg offers, then self-tests each on a 1-second clip."""
    ff = ff or _ff()
    caps = Caps()
    if not ff:
        return caps
    try:
        r = run_process([ff, "-hide_banner", "-hwaccels"], capture_output=True, text=True, timeout=30)
        accels = parse_hwaccels((r.stdout or "") + (r.stderr or ""))
        r = run_process([ff, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30)
        encoders = parse_encoders((r.stdout or "") + (r.stderr or ""))
    except Exception as e:
        caps.note = str(e)
        return caps
    cands: list[HwPath] = []
    plat = sys.platform
    nodes = render_nodes() if plat.startswith("linux") else []
    if "cuda" in accels and (plat.startswith("linux") or plat == "win32"):
        cands.append(HwPath("cuda", _DECODE_NAMES["cuda"], "", gpu_scale=True, rank=1))
    if plat == "darwin" and "videotoolbox" in accels:
        cands.append(HwPath("videotoolbox", _DECODE_NAMES["videotoolbox"], "", rank=3))
    if plat == "win32" and "d3d11va" in accels:
        cands.append(HwPath("d3d11va", _DECODE_NAMES["d3d11va"], "", rank=3))
    if "vaapi" in accels:
        for dev, vendor in nodes:
            rank = _vaapi_rank(vendor)
            if "vp8_vaapi" in encoders:
                cands.append(HwPath("vaapi_enc", "VAAPI", "VAAPI", dev, gpu_scale=True, rank=rank - 10))
            cands.append(HwPath("vaapi", _DECODE_NAMES["vaapi"], "", dev, gpu_scale=True, rank=rank))
    if not cands:
        return caps
    with tempfile.TemporaryDirectory(prefix="chc-hw-") as td:
        clip = Path(td) / "t.mp4"
        ok, _err = _try(ff, ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=1", "-c:v", "libx264",
                             "-pix_fmt", "yuv420p", "-y", str(clip)])
        if not ok:
            return caps
        for c in sorted(cands, key=lambda c: c.rank):
            got = _selftest(ff, c, clip, Path(td))
            if got:
                caps.paths.append(got)
            else:
                log.info("Video path %s is not usable here", c.key)
    return caps


def _selftest(ff: str, c: HwPath, clip: Path, td: Path) -> HwPath | None:
    """The path, or None if it does not work. GPU scaling is tested separately (the test clip is 640x360, so a
    "1080p" source makes it scale to 1280x720): when only that fails, the path stays but scales on the CPU."""
    out = td / f"hw_{c.key}.webm"
    pre, post = build(c, 640, 360, 0.0, 2)
    ok, _err = _try(ff, [*pre, "-i", str(clip), *post, "-t", "1", "-y", str(out)])
    if not ok or (c.encode and not verify_output(ff, out, 29)):
        return None
    if c.gpu_scale:
        pre, post = build(c, 1920, 1080, 0.0, 2)
        ok, _err = _try(ff, [*pre, "-i", str(clip), *post, "-t", "1", "-y", str(out)])
        if not ok:
            c = replace(c, gpu_scale=False)
    return c


def capabilities() -> Caps:
    global _caps
    with _lock:
        if _caps is None:
            try:
                _caps = probe()
            except Exception as e:                    # never let probing break a conversion
                log.warning("Hardware video probe failed: %s", e)
                _caps = Caps(note=str(e))
            first = True
        else:
            first = False
        caps = _caps
    if first:
        log.info("Video conversion: %s", describe_text())
    return caps


def plan() -> list[HwPath]:
    """Paths to try, best first, always ending with the CPU. GPU encoders first (whole pipeline on the GPU), then
    GPU decode + libvpx (a discrete GPU before an integrated one), then the CPU."""
    paths = [p for p in capabilities().paths if p.key not in _disabled]
    paths.sort(key=lambda p: (p.encode == "", p.rank))
    return paths + [CPU]


def note_failed(path: HwPath, why: str) -> None:
    if path.key != "cpu":
        _disabled.add(path.key)
        log.warning("Video path %s failed (%s); using the next one", path.key, why[:200])


def best() -> HwPath:
    return plan()[0]


def describe_text(path: HwPath | None = None) -> str:
    """English, for the log (the app and doctor translate it: see describe())."""
    p = path or best()
    return f"{p.decode or 'CPU'} decode + {p.encode or 'CPU'} VP8 encode"


def describe(path: HwPath | None = None) -> str:
    """'Video conversion: NVIDIA decode + CPU VP8 encode' in the current language."""
    from .i18n import _
    p = path or best()
    if p.key == "cpu":
        return _("hw.line_cpu", threads=encode_threads())
    return _("hw.line", decode=p.decode or _("hw.cpu"), encode=p.encode or _("hw.cpu"))


def reset() -> None:
    """Forget the probe results (tests)."""
    global _caps
    with _lock:
        _caps = None
    _disabled.clear()

"""Download the pinned Deno release (scripts/deno.lock.json), verify SHA-256 and write the
binary for the app bundle: python scripts/fetch_deno.py {mac|windows|linux} OUT_FILE
macOS fuses the arm64 and x86_64 builds into one universal binary with lipo."""
import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

LOCK = json.loads((Path(__file__).with_name("deno.lock.json")).read_text("utf-8"))
TARGETS = {
    "mac": ["x86_64-apple-darwin", "aarch64-apple-darwin"],
    "windows": ["x86_64-pc-windows-msvc"],
    "linux": ["x86_64-unknown-linux-gnu"],
}


def fetch(target: str, tmp: Path) -> Path:
    name = f"deno-{target}.zip"
    url = f"https://github.com/denoland/deno/releases/download/v{LOCK['version']}/{name}"
    zpath = tmp / name
    with urllib.request.urlopen(url, timeout=120) as r, open(zpath, "wb") as f:
        f.write(r.read())
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    if digest != LOCK["assets"][name]["sha256"]:
        raise SystemExit(f"SHA-256 mismatch for {name}: {digest}")
    d = tmp / target
    with zipfile.ZipFile(zpath) as z:
        member = next(n for n in z.namelist() if n in ("deno", "deno.exe"))
        z.extract(member, d)
    return d / member


def main() -> None:
    platform, out = sys.argv[1], Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as t:
        bins = [fetch(target, Path(t)) for target in TARGETS[platform]]
        if platform == "mac":
            subprocess.run(["lipo", "-create", *map(str, bins), "-output", str(out)], check=True)
            print(subprocess.run(["lipo", "-archs", str(out)], capture_output=True, text=True).stdout.strip())
        else:
            out.write_bytes(bins[0].read_bytes())
    out.chmod(0o755)
    print(f"deno {LOCK['version']} -> {out}")


if __name__ == "__main__":
    main()

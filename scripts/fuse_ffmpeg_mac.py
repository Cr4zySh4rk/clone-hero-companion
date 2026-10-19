"""Build a universal (arm64 + x86_64) ffmpeg for the macOS app from the static
binaries shipped in the imageio-ffmpeg wheels."""
import glob
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

out = Path(sys.argv[1])
tmp = Path(tempfile.mkdtemp())
bins = []
for plat in ("macosx_11_0_arm64", "macosx_10_9_x86_64"):
    d = tmp / plat
    subprocess.run([sys.executable, "-m", "pip", "download", "imageio-ffmpeg", "--no-deps", "--only-binary=:all:",
                    "--platform", plat, "-d", str(d)], check=True)
    whl = glob.glob(str(d / "*.whl"))[0]
    with zipfile.ZipFile(whl) as z:
        name = next(n for n in z.namelist() if "/binaries/ffmpeg-" in n)
        z.extract(name, d)
        bins.append(d / name)
out.parent.mkdir(parents=True, exist_ok=True)
subprocess.run(["lipo", "-create", *map(str, bins), "-output", str(out)], check=True)
out.chmod(0o755)
print(subprocess.run(["lipo", "-archs", str(out)], capture_output=True, text=True).stdout.strip())

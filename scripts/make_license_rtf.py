"""Convert the repository's license to the plain RTF used by the MSI wizard."""
from pathlib import Path

text = Path("LICENSE").read_text("utf-8")
escaped = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
escaped = escaped.replace("\n", "\\par\n")
Path("build").mkdir(exist_ok=True)
Path("build/license.rtf").write_text("{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Courier New;}}\\f0\\fs18\n"
                                     + escaped + "}", encoding="ascii")

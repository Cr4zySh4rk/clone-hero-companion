"""Small secret store in the operating system's own secure storage (service "CloneHeroCompanion").

    get(name) -> str | None      set(name, value) -> bool      delete(name) -> None

macOS    the Keychain, through the `security` CLI. The secret is NOT put in argv: it is sent on stdin to
         `security -i` (interactive mode), base64-encoded so it needs no quoting. (`add-generic-password -w` without
         a value prompts on the terminal, which a GUI app does not have; `-w <secret>` would show it in `ps`.)
         Reading uses `find-generic-password -w`, whose output is the secret: stdout, not argv.
Windows  DPAPI (CryptProtectData / CryptUnprotectData, current-user scope) through ctypes; the encrypted blob is a
         file in the config dir that only this Windows user can decrypt.
Linux    `secret-tool` (libsecret: GNOME Keyring / KWallet) when installed and a keyring answers, otherwise a file in
         the config dir with mode 600 (readable by this user only, NOT encrypted). The fallback is logged, never the value.

The value is never logged, and never passed in a command line (macOS: stdin; Linux: stdin). Failures return None/False.
"""
from __future__ import annotations

import base64
import ctypes
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

SERVICE = "CloneHeroCompanion"
_TIMEOUT = 15


def _file(name: str) -> Path:
    from .paths import config_dir
    d = config_dir() / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    return d / (name.replace("/", "_").replace("\\", "_") + (".dpapi" if sys.platform == "win32" else ".secret"))


def _run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        log.info("%s unavailable: %s", args[0], type(e).__name__)
        return None


# ------------------------------------------------------------------ macOS Keychain
def _mac_set(name: str, value: str) -> bool:
    enc = base64.b64encode(value.encode("utf-8")).decode("ascii")          # [A-Za-z0-9+/=]: safe unquoted
    cmd = f"add-generic-password -U -s {SERVICE} -a {name} -w {enc}\n"
    r = _run(["security", "-i"], cmd)
    return bool(r and r.returncode == 0 and "error" not in (r.stderr or "").lower())


def _mac_get(name: str) -> str | None:
    r = _run(["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"])
    if not r or r.returncode != 0:
        return None
    try:
        return base64.b64decode(r.stdout.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _mac_delete(name: str) -> None:
    _run(["security", "delete-generic-password", "-s", SERVICE, "-a", name])


# ------------------------------------------------------------------ Windows DPAPI
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    windll = ctypes.windll  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    fn = windll.crypt32.CryptProtectData if protect else windll.crypt32.CryptUnprotectData
    # CurrentUser scope is the default (no CRYPTPROTECT_LOCAL_MACHINE flag).
    if not fn(ctypes.byref(src), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        windll.kernel32.LocalFree(out.pbData)


def _win_set(name: str, value: str) -> bool:
    try:
        p = _file(name)
        p.write_bytes(_dpapi(value.encode("utf-8"), True))
        return True
    except (OSError, AttributeError) as e:
        log.info("Could not store a secret with DPAPI: %s", type(e).__name__)
        return False


def _win_get(name: str) -> str | None:
    try:
        p = _file(name)
        return _dpapi(p.read_bytes(), False).decode("utf-8") if p.exists() else None
    except (OSError, AttributeError, UnicodeDecodeError) as e:
        log.info("Could not read a secret with DPAPI: %s", type(e).__name__)
        return None


# ------------------------------------------------------------------ Linux: secret-tool, else a private file
def _have_secret_tool() -> bool:
    from shutil import which
    return which("secret-tool") is not None


def _linux_set(name: str, value: str) -> bool:
    if _have_secret_tool():
        r = _run(["secret-tool", "store", f"--label={SERVICE} {name}", "service", SERVICE, "name", name], value)
        if r and r.returncode == 0:
            _unlink_quiet(_file(name))                                      # no stale plaintext copy
            return True
        log.info("secret-tool could not store the secret (no keyring running?); using a private file instead")
    else:
        log.info("secret-tool is not installed; keeping the secret in a private file (mode 600) in the config folder")
    try:
        p = _file(name)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
        os.chmod(p, 0o600)
        return True
    except OSError as e:
        log.info("Could not store a secret: %s", type(e).__name__)
        return False


def _linux_get(name: str) -> str | None:
    if _have_secret_tool():
        r = _run(["secret-tool", "lookup", "service", SERVICE, "name", name])
        if r and r.returncode == 0 and r.stdout:
            return r.stdout
    try:
        p = _file(name)
        return p.read_text(encoding="utf-8") if p.exists() else None
    except OSError:
        return None


def _linux_delete(name: str) -> None:
    if _have_secret_tool():
        _run(["secret-tool", "clear", "service", SERVICE, "name", name])
    _unlink_quiet(_file(name))


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


# ------------------------------------------------------------------ public API
def set(name: str, value: str) -> bool:  # noqa: A001 - the module is imported as `secrets`
    """Store `value` under `name`. Returns False when no backend could keep it."""
    if sys.platform == "darwin":
        return _mac_set(name, value)
    if sys.platform == "win32":
        return _win_set(name, value)
    return _linux_set(name, value)


def get(name: str) -> str | None:
    if sys.platform == "darwin":
        return _mac_get(name)
    if sys.platform == "win32":
        return _win_get(name)
    return _linux_get(name)


def delete(name: str) -> None:
    if sys.platform == "darwin":
        _mac_delete(name)
    elif sys.platform == "win32":
        _unlink_quiet(_file(name))
    else:
        _linux_delete(name)

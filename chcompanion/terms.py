"""Terms of Use / Privacy Policy acceptance (see TERMS.md and PRIVACY.md).

The app refuses state-changing calls (HTTP 451) and sends no usage statistics until the current TERMS_VERSION has been
accepted. Raise TERMS_VERSION whenever TERMS.md or PRIVACY.md changes in a material way; everyone is asked again.
CHC_ACCEPT_TERMS=1 accepts automatically (tests and CI only)."""
from __future__ import annotations

import os
import time

TERMS_VERSION = 1
URL_TERMS = "https://cloneherocompanion.com/terms.html"
URL_PRIVACY = "https://cloneherocompanion.com/privacy.html"
# POSTs that keep working before acceptance: accepting, declining (quit), opening the two documents, the update screen,
# and the UI reporting its language (so the screen is translated).
OPEN_POSTS = frozenset({"/api/terms/accept", "/api/quit", "/api/open", "/api/update/check", "/api/update/install", "/api/i18n"})


def env_accepts() -> bool:
    return os.environ.get("CHC_ACCEPT_TERMS") == "1"


def accepted(settings: dict) -> bool:
    if env_accepts():
        return True
    try:
        return int(settings.get("terms_version") or 0) >= TERMS_VERSION
    except (TypeError, ValueError):
        return False


def record() -> dict:
    return {"terms_version": TERMS_VERSION, "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def state(settings: dict) -> dict:
    return {"required": not accepted(settings), "version": TERMS_VERSION,
            "acceptedVersion": int(settings.get("terms_version") or 0), "acceptedAt": str(settings.get("accepted_at") or "")}

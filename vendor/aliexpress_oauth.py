"""OAuth stub — uses ALIEXPRESS_ACCESS_TOKEN env / actor input session."""
from __future__ import annotations

import os


def get_valid_access_token(user_id=None) -> str | None:
    token = (os.getenv("ALIEXPRESS_ACCESS_TOKEN") or "").strip()
    return token or None

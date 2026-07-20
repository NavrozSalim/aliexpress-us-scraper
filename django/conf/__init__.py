"""Minimal django.conf.settings stub for Apify (env-backed)."""
from __future__ import annotations

import os
from types import SimpleNamespace

settings = SimpleNamespace(
    ALIEXPRESS_APP_KEY=os.getenv("ALIEXPRESS_APP_KEY", ""),
    ALIEXPRESS_APP_SECRET=os.getenv("ALIEXPRESS_APP_SECRET", ""),
    ALIEXPRESS_API_URL=os.getenv("ALIEXPRESS_API_URL", ""),
    ALIEXPRESS_ACCESS_TOKEN=os.getenv("ALIEXPRESS_ACCESS_TOKEN", ""),
    ALIEXPRESS_IOP_GATEWAY=os.getenv("ALIEXPRESS_IOP_GATEWAY", ""),
)

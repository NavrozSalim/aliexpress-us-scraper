"""AliExpress Affiliate API market presets (country + currency + language)."""
from __future__ import annotations

import os
from typing import TypedDict


class AliExpressMarket(TypedDict):
    country: str
    target_currency: str
    target_language: str


ALIEXPRESS_MARKETS: dict[str, AliExpressMarket] = {
    # UK first — default via ALIEXPRESS_DEFAULT_MARKET=UK
    'UK': {'country': 'GB', 'target_currency': 'GBP', 'target_language': 'EN'},
    'USA': {'country': 'US', 'target_currency': 'USD', 'target_language': 'EN'},
    'AU': {'country': 'AU', 'target_currency': 'AUD', 'target_language': 'EN'},
}

# Store.region (and test_vendor_scrape --region) → AliExpress market key.
_STORE_REGION_TO_MARKET = {
    'UK': 'UK',
    'GB': 'UK',
    'USA': 'USA',
    'US': 'USA',
    'AU': 'AU',
    'AUSTRALIA': 'AU',
}


def default_aliexpress_market_key() -> str:
    key = (os.getenv('ALIEXPRESS_DEFAULT_MARKET') or 'UK').strip().upper()
    return key if key in ALIEXPRESS_MARKETS else 'UK'


def resolve_aliexpress_market(store_region: str | None) -> str:
    """Map store/test region to a configured AliExpress market (UK / USA / AU)."""
    r = (store_region or '').strip().upper()
    if r in _STORE_REGION_TO_MARKET:
        return _STORE_REGION_TO_MARKET[r]
    return default_aliexpress_market_key()


_ALIEXPRESS_VENDOR_MARKET: dict[str, str] = {
    'aliexpress': 'UK',
    'aliexpressuk': 'UK',
    'aliexpress uk': 'UK',
    'aliexpress_us': 'USA',
    'aliexpressus': 'USA',
    'aliexpress us': 'USA',
    'aliexpress_au': 'AU',
    'aliexpressau': 'AU',
    'aliexpress au': 'AU',
}


def scrape_region_for_aliexpress(vendor_code: str | None, store_region: str | None) -> str:
    """Prefer AliExpress vendor code (UK/US/AU) over store region for API market."""
    raw = (vendor_code or '').strip().lower()
    if raw in _ALIEXPRESS_VENDOR_MARKET:
        return _ALIEXPRESS_VENDOR_MARKET[raw]
    compact = raw.replace(' ', '')
    if compact in _ALIEXPRESS_VENDOR_MARKET:
        return _ALIEXPRESS_VENDOR_MARKET[compact]
    return resolve_aliexpress_market(store_region)


def get_aliexpress_market(store_region: str | None) -> AliExpressMarket:
    key = resolve_aliexpress_market(store_region)
    return ALIEXPRESS_MARKETS[key]

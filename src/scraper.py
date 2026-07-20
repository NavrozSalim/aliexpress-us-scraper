"""AliExpress USA market scraper (API preferred, HTML fallback)."""
from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Any

import httpx

from .helpers import fail, fetch_html, ok, parse_money, soup

MARKET_COUNTRY = "US"
MARKET_CURRENCY = "USD"
API_URL = "https://api-sg.aliexpress.com/sync"


def extract_product_id(url: str) -> str | None:
    text = (url or "").strip()
    if text.isdigit():
        return text
    for pat in (
        r"/item/(\d+)\.html",
        r"/i/(\d+)\.html",
        r"[?&]productIds?=(\d+)",
        r"/(\d{10,})\.html",
    ):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def _sign(params: dict[str, str], secret: str) -> str:
    items = sorted((k, v) for k, v in params.items() if k != "sign")
    raw = secret + "".join(f"{k}{v}" for k, v in items) + secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _affiliate_detail(product_id: str) -> dict | None:
    app_key = (os.getenv("ALIEXPRESS_APP_KEY") or "").strip()
    app_secret = (os.getenv("ALIEXPRESS_APP_SECRET") or "").strip()
    if not app_key or not app_secret:
        return None
    params = {
        "app_key": app_key,
        "method": "aliexpress.affiliate.productdetail.get",
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600)),
        "product_ids": product_id,
        "target_currency": MARKET_CURRENCY,
        "target_language": "EN",
        "ship_to_country": MARKET_COUNTRY,
    }
    params["sign"] = _sign(params, app_secret)
    with httpx.Client(timeout=30) as client:
        resp = client.get(API_URL, params=params)
        data = resp.json()
    root = data.get("aliexpress_affiliate_productdetail_get_response") or data
    result = root.get("resp_result") or root.get("result") or root
    products = (
        (result.get("result") or {}).get("products")
        or result.get("products")
        or []
    )
    if isinstance(products, dict):
        products = products.get("product") or products.get("products") or []
    if not products:
        return None
    p = products[0] if isinstance(products, list) else products
    price = parse_money(
        str(p.get("target_sale_price") or p.get("sale_price") or p.get("target_original_price") or "")
    )
    title = p.get("product_title") or p.get("title")
    if price is None and not title:
        return None
    return ok(price, 10 if price is not None else None, title, source="affiliate_api")


def scrape_product(
    *,
    url: str,
    region: str,
    vendor: str,
    proxy_urls: list[str],
    timeout_secs: int,
    max_retries: int,
    actor_input: dict[str, Any],
) -> dict:
    product_id = extract_product_id(url) or extract_product_id(str(actor_input.get("productId") or ""))
    if not product_id:
        return fail(
            "aliexpress_invalid_url",
            "Could not parse AliExpress product ID from URL",
            vendor=vendor,
            region=region,
            url=url,
        )

    api_result = _affiliate_detail(product_id)
    if api_result and api_result.get("success"):
        api_result.update(vendor=vendor, region=region, url=url, product_id=product_id)
        return api_result

    page_url = url if "aliexpress." in url else f"https://www.aliexpress.com/item/{product_id}.html"
    last_err = "api_unavailable_or_html_failed"
    for attempt in range(max(1, max_retries + 1)):
        try:
            html, status = fetch_html(page_url, proxy_urls=proxy_urls, timeout_secs=timeout_secs)
            if status >= 400:
                last_err = f"HTTP {status}"
                continue
            doc = soup(html)
            title_el = doc.select_one("h1") or doc.select_one("meta[property='og:title']")
            title = (
                title_el.get("content")
                if title_el and title_el.name == "meta"
                else (title_el.get_text(strip=True) if title_el else None)
            )
            price = None
            m = re.search(r'"formatedActivityPrice"\s*:\s*"([^"]+)"', html)
            if not m:
                m = re.search(r'"formatedPrice"\s*:\s*"([^"]+)"', html)
            if m:
                price = parse_money(m.group(1))
            if price is None:
                el = doc.select_one("[class*=Price]")
                if el:
                    price = parse_money(el.get_text())
            if price is None and not title:
                last_err = "parse_failed"
                continue
            return ok(
                price,
                10 if price is not None else None,
                title,
                vendor=vendor,
                region=region,
                url=page_url,
                product_id=product_id,
                source="html",
                attempt=attempt + 1,
            )
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)

    return fail(
        "aliexpress_scrape_failed",
        last_err + " — set ALIEXPRESS_APP_KEY / ALIEXPRESS_APP_SECRET for API mode",
        vendor=vendor,
        region=region,
        url=url,
        product_id=product_id,
    )

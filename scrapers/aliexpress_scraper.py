"""AliExpress price/title lookup via Drop Shipping API (OAuth) or Affiliate API fallback."""
from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

from django.conf import settings

from scrapers.aliexpress_client import AliExpressAPIError, fetch_product_detail, _credentials_configured
from scrapers.aliexpress_ds_parser import (
    best_freight_within_days,
    cheapest_sku_for_freight,
    price_from_ds_result,
    product_logistics_delivery_max,
    stock_from_ds_result,
    title_from_ds_result,
)
from scrapers.aliexpress_iop import AliExpressIOPError, fetch_ds_product, fetch_freight_calculate
from scrapers.aliexpress_markets import get_aliexpress_market, resolve_aliexpress_market
from vendor.aliexpress_oauth import get_valid_access_token

logger = logging.getLogger(__name__)

# DS API rarely exposes warehouse stock per SKU; treat priced onSelling listings as available.
DEFAULT_IN_STOCK_QTY = 999

ALIEXPRESS_HOST_MARKERS = ('aliexpress.com', 'aliexpress.us', 'aliexpress.co.uk', 'aliexpress.ru')

PRODUCT_ID_FROM_URL_RE = re.compile(
    r'(?:item/|/)(\d{8,20})(?:\.html|[/?#]|$)',
    re.IGNORECASE,
)


def is_aliexpress_vendor_code(vcode: str) -> bool:
    v = (vcode or '').strip().lower()
    if v.startswith('aliexpress'):
        return True
    return v.replace(' ', '') in (
        'aliexpress',
        'aliexpressuk',
        'aliexpressus',
        'aliexpressau',
        'aliexpress_us',
        'aliexpress_au',
    )


def is_aliexpress_url(url: str) -> bool:
    lower = (url or '').lower()
    return any(marker in lower for marker in ALIEXPRESS_HOST_MARKERS)


def extract_aliexpress_product_id(url_or_id: str) -> str | None:
    raw = (url_or_id or '').strip()
    if not raw:
        return None
    if raw.isdigit() and 8 <= len(raw) <= 20:
        return raw
    match = PRODUCT_ID_FROM_URL_RE.search(raw)
    if match:
        return match.group(1)
    return None


def build_aliexpress_item_url(product_id: str) -> str:
    pid = str(product_id or '').strip()
    return f'https://www.aliexpress.com/item/{pid}.html'


def _parse_price(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r'[^\d.,]', '', text.replace(',', ''))
    if not text:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if amount <= 0:
        return None
    return float(amount.quantize(Decimal('0.01')))


def _price_from_product_row(row: dict) -> float | None:
    for key in (
        'target_sale_price',
        'targetSalePrice',
        'target_app_sale_price',
        'targetAppSalePrice',
        'sale_price',
        'salePrice',
        'app_sale_price',
        'appSalePrice',
        'target_original_price',
        'targetOriginalPrice',
        'original_price',
        'originalPrice',
    ):
        price = _parse_price(row.get(key))
        if price is not None:
            return price
    return None


def _title_from_product_row(row: dict) -> str | None:
    for key in ('product_title', 'productTitle', 'title'):
        title = (row.get(key) or '').strip()
        if title:
            return title[:500]
    return None


def _oauth_user_id_from_session(session: dict | None) -> str | None:
    if not session:
        return None
    for key in ('aliexpress_user_id', 'user_id'):
        raw = session.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return None


def _max_delivery_days() -> int:
    try:
        return max(1, int(getattr(settings, 'ALIEXPRESS_MAX_DELIVERY_DAYS', 7) or 7))
    except (TypeError, ValueError):
        return 7


def _apply_shipping_and_delivery(
    product_id: str,
    region: str,
    access_token: str,
    ds_result: dict,
    item_price: float,
    stock: int | None,
) -> dict:
    """
    Fetch freight, add shipping to item price, zero stock when no option delivers within max days.
    """
    max_days = _max_delivery_days()
    sku = cheapest_sku_for_freight(ds_result)
    shipping_price = 0.0
    delivery_max: int | None = None
    qualified = False

    if sku:
        try:
            freight_result = fetch_freight_calculate(
                product_id,
                sku['price'],
                region,
                access_token,
            )
            if freight_result:
                best = best_freight_within_days(freight_result, max_delivery_days=max_days)
                if best:
                    qualified = True
                    shipping_price = best['shipping_price']
                    delivery_max = best['delivery_max']
        except AliExpressIOPError as exc:
            logger.warning(
                'AliExpress freight calculate failed for %s: %s',
                product_id,
                exc,
            )

    if not qualified:
        lead = product_logistics_delivery_max(ds_result)
        if lead is not None and lead <= max_days:
            qualified = True
            delivery_max = lead

    total_price = round(item_price + shipping_price, 2)
    if not qualified:
        return {
            'price': item_price,
            'stock': 0,
            'shipping_price': shipping_price if shipping_price else None,
            'delivery_days_max': delivery_max,
            'error_code': 'aliexpress_delivery_too_slow',
            'error_message': (
                f'No AliExpress shipping option within {max_days} days for product {product_id}'
            ),
        }

    out_stock = stock if stock is not None else DEFAULT_IN_STOCK_QTY
    return {
        'price': total_price,
        'stock': out_stock,
        'shipping_price': shipping_price,
        'delivery_days_max': delivery_max,
        'item_price': item_price,
    }


def _scrape_via_dropshipping(product_id: str, region: str, session: dict | None) -> dict | None:
    """Return scraper payload dict on success, None when DS path is unavailable."""
    user_id = _oauth_user_id_from_session(session)
    access_token = get_valid_access_token(user_id)
    if not access_token:
        return None

    market_key = resolve_aliexpress_market(region)
    market = get_aliexpress_market(region)
    try:
        result = fetch_ds_product(product_id, region, access_token)
    except AliExpressIOPError as exc:
        logger.warning('AliExpress DS API failed for %s: %s', product_id, exc)
        return {
            'price': None,
            'stock': None,
            'title': None,
            'error_code': 'aliexpress_ds_api_error',
            'error_message': str(exc)[:500],
        }

    if not result:
        return {
            'price': None,
            'stock': 0,
            'title': None,
            'error_code': 'aliexpress_product_not_found',
            'error_message': (
                f'No AliExpress DS product for ID {product_id} '
                f'(market={market_key}, country={market["country"]}, currency={market["target_currency"]})'
            ),
        }

    price = price_from_ds_result(result)
    title = title_from_ds_result(result)
    stock = stock_from_ds_result(result)
    if stock is None and price is not None:
        stock = DEFAULT_IN_STOCK_QTY
    if price is None:
        return {
            'price': None,
            'stock': stock if stock is not None else 0,
            'title': title,
            'error_code': 'aliexpress_no_price',
            'error_message': f'AliExpress DS returned product {product_id} without a usable price',
        }

    shipping_payload = _apply_shipping_and_delivery(
        product_id, region, access_token, result, price, stock
    )
    payload = {
        'price': shipping_payload['price'],
        'stock': shipping_payload['stock'],
        'title': title,
    }
    if shipping_payload.get('shipping_price') is not None:
        payload['shipping_price'] = shipping_payload['shipping_price']
    if shipping_payload.get('delivery_days_max') is not None:
        payload['delivery_days_max'] = shipping_payload['delivery_days_max']
    if shipping_payload.get('item_price') is not None:
        payload['item_price'] = shipping_payload['item_price']
    if shipping_payload.get('error_code'):
        payload['error_code'] = shipping_payload['error_code']
        payload['error_message'] = shipping_payload['error_message']
    return payload


def _scrape_via_affiliate(product_id: str, region: str) -> dict:
    market_key = resolve_aliexpress_market(region)
    market = get_aliexpress_market(region)
    try:
        row = fetch_product_detail(product_id, region)
    except AliExpressAPIError as exc:
        logger.warning('AliExpress affiliate API failed for %s: %s', product_id, exc)
        return {
            'price': None,
            'stock': None,
            'title': None,
            'error_code': 'aliexpress_api_error',
            'error_message': str(exc)[:500],
        }

    if not row:
        return {
            'price': None,
            'stock': 0,
            'title': None,
            'error_code': 'aliexpress_product_not_found',
            'error_message': (
                f'No AliExpress product detail for ID {product_id} '
                f'(market={market_key}, country={market["country"]}, currency={market["target_currency"]})'
            ),
        }

    price = _price_from_product_row(row)
    title = _title_from_product_row(row)
    if price is None:
        return {
            'price': None,
            'stock': 0,
            'title': title,
            'error_code': 'aliexpress_no_price',
            'error_message': f'AliExpress returned product {product_id} without a usable price',
        }

    return {
        'price': price,
        'stock': DEFAULT_IN_STOCK_QTY,
        'title': title,
    }


def scrape_aliexpress(vendor_url: str, region: str, session: dict | None = None) -> dict:
    """
    Fetch price/title for an AliExpress listing.

    Prefers Drop Shipping API (``aliexpress.ds.product.get``) when an OAuth
    access token is available for the store user (or ``ALIEXPRESS_ACCESS_TOKEN``
    env fallback). Falls back to the legacy Affiliate API when configured.
    """
    product_id = extract_aliexpress_product_id(vendor_url)
    if not product_id:
        return {
            'price': None,
            'stock': None,
            'title': None,
            'error_code': 'aliexpress_invalid_url',
            'error_message': 'Could not parse AliExpress product ID from URL or SKU',
        }
    if not _credentials_configured():
        return {
            'price': None,
            'stock': None,
            'title': None,
            'error_code': 'aliexpress_not_configured',
            'error_message': (
                'AliExpress API not configured — set ALIEXPRESS_APP_KEY and ALIEXPRESS_APP_SECRET '
                'on the worker'
            ),
        }

    ds_result = _scrape_via_dropshipping(product_id, region, session)
    if ds_result is not None:
        return ds_result

    affiliate_result = _scrape_via_affiliate(product_id, region)
    if affiliate_result.get('price') is not None:
        return affiliate_result

    return {
        'price': None,
        'stock': None,
        'title': affiliate_result.get('title'),
        'error_code': 'aliexpress_oauth_required',
        'error_message': (
            'AliExpress Drop Shipping requires OAuth — connect your AliExpress account via '
            'GET /api/v1/vendors/aliexpress/connect/ or set ALIEXPRESS_ACCESS_TOKEN on the worker'
        ),
    }


def close_aliexpress_session(session: dict | None) -> None:
    """No-op — API client holds no persistent session."""
    del session

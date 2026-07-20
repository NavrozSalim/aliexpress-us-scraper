"""AliExpress Open Platform client (Affiliate productdetail.get)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from django.conf import settings

from scrapers.aliexpress_markets import AliExpressMarket, get_aliexpress_market

logger = logging.getLogger(__name__)

# Overseas HTTPS gateway (EU/US VPS). Chinese gateway gw.api.taobao.com often times out abroad.
DEFAULT_API_URL = 'https://api.taobao.com/router/rest'
PRODUCT_DETAIL_METHOD = 'aliexpress.affiliate.productdetail.get'
DETAIL_FIELDS = 'product_id,product_title,sale_price,original_price,target_sale_price,target_original_price'


class AliExpressAPIError(Exception):
    def __init__(self, message: str, *, response_body: str | None = None):
        super().__init__(message)
        self.response_body = response_body


def get_api_url() -> str:
    url = (getattr(settings, 'ALIEXPRESS_API_URL', '') or '').strip()
    return url or DEFAULT_API_URL


def _credentials_configured() -> bool:
    return bool(
        (getattr(settings, 'ALIEXPRESS_APP_KEY', '') or '').strip()
        and (getattr(settings, 'ALIEXPRESS_APP_SECRET', '') or '').strip()
    )


def gmt8_timestamp() -> str:
    return datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')


def sign_params(params: dict, app_secret: str, sign_method: str = 'md5') -> str:
    """Taobao Open Platform request signature."""
    items = sorted(
        (k, v)
        for k, v in params.items()
        if k != 'sign' and v is not None and str(v) != ''
    )
    base = ''.join(f'{k}{v}' for k, v in items)
    method = (sign_method or 'md5').strip().lower()
    if method == 'hmac':
        digest = hmac.new(app_secret.encode('utf-8'), base.encode('utf-8'), hashlib.sha256)
        return digest.hexdigest().upper()
    raw = f'{app_secret}{base}{app_secret}'
    return hashlib.md5(raw.encode('utf-8')).hexdigest().upper()


def _base_params(method: str) -> dict:
    return {
        'method': method,
        'app_key': settings.ALIEXPRESS_APP_KEY,
        'sign_method': getattr(settings, 'ALIEXPRESS_SIGN_METHOD', 'md5'),
        'timestamp': gmt8_timestamp(),
        'format': 'json',
        'v': '2.0',
    }


def call_api_with_session(
    method: str,
    business_params: dict,
    session_token: str,
    *,
    sign_method: str = 'hmac',
    timeout: int = 30,
) -> dict:
    """TOP router call with OAuth session token (Drop Shipping APIs like ds.product.get)."""
    if not _credentials_configured():
        raise AliExpressAPIError(
            'AliExpress API credentials not configured (set ALIEXPRESS_APP_KEY and ALIEXPRESS_APP_SECRET)'
        )
    params = _base_params(method)
    params['sign_method'] = (sign_method or 'hmac').strip().lower()
    params['session'] = session_token
    params.update({k: v for k, v in business_params.items() if v is not None and str(v) != ''})
    params['sign'] = sign_params(params, settings.ALIEXPRESS_APP_SECRET, params['sign_method'])
    try:
        resp = requests.post(get_api_url(), data=params, timeout=timeout)
    except requests.RequestException as exc:
        raise AliExpressAPIError(str(exc)) from exc
    body = resp.text or ''
    if resp.status_code >= 400:
        raise AliExpressAPIError(
            f'AliExpress API HTTP {resp.status_code}',
            response_body=body[:500],
        )
    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise AliExpressAPIError('AliExpress API returned non-JSON response', response_body=body[:500]) from exc
    err = _extract_top_level_error(data)
    if err:
        raise AliExpressAPIError(err, response_body=body[:500])
    return data


def call_api(method: str, business_params: dict, *, timeout: int = 30) -> dict:
    if not _credentials_configured():
        raise AliExpressAPIError(
            'AliExpress API credentials not configured (set ALIEXPRESS_APP_KEY and ALIEXPRESS_APP_SECRET)'
        )
    params = _base_params(method)
    params.update({k: v for k, v in business_params.items() if v is not None and str(v) != ''})
    params['sign'] = sign_params(params, settings.ALIEXPRESS_APP_SECRET, params['sign_method'])
    try:
        resp = requests.post(get_api_url(), data=params, timeout=timeout)
    except requests.RequestException as exc:
        raise AliExpressAPIError(str(exc)) from exc
    body = resp.text or ''
    if resp.status_code >= 400:
        raise AliExpressAPIError(
            f'AliExpress API HTTP {resp.status_code}',
            response_body=body[:500],
        )
    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise AliExpressAPIError('AliExpress API returned non-JSON response', response_body=body[:500]) from exc
    err = _extract_top_level_error(data)
    if err:
        raise AliExpressAPIError(err, response_body=body[:500])
    return data


def _extract_top_level_error(data: dict) -> str | None:
    err_resp = data.get('error_response') if isinstance(data, dict) else None
    if not isinstance(err_resp, dict):
        return None
    code = err_resp.get('code') or err_resp.get('error_code') or ''
    msg = err_resp.get('msg') or err_resp.get('sub_msg') or err_resp.get('message') or 'API error'
    return f'{code}: {msg}'.strip(': ')


def _response_root(data: dict, method: str) -> dict | None:
    snake = method.replace('.', '_') + '_response'
    root = data.get(snake)
    if isinstance(root, dict):
        return root
    camel = ''.join(part.capitalize() if i else part for i, part in enumerate(snake.split('_')))
    root = data.get(camel)
    return root if isinstance(root, dict) else None


def _products_from_detail_response(data: dict) -> list[dict]:
    root = _response_root(data, PRODUCT_DETAIL_METHOD)
    if not root:
        return []
    resp_result = root.get('resp_result') or root.get('respResult') or {}
    if isinstance(resp_result, str):
        try:
            resp_result = json.loads(resp_result)
        except json.JSONDecodeError:
            return []
    if not isinstance(resp_result, dict):
        return []
    result = resp_result.get('result') or {}
    if not isinstance(result, dict):
        return []
    products = result.get('products') or {}
    if not isinstance(products, dict):
        return []
    product = products.get('product')
    if product is None:
        return []
    if isinstance(product, list):
        return [p for p in product if isinstance(p, dict)]
    if isinstance(product, dict):
        return [product]
    return []


def fetch_product_detail(product_id: str, store_region: str | None) -> dict | None:
    """Return one product dict from affiliate productdetail.get, or None if not found."""
    pid = str(product_id or '').strip()
    if not pid:
        return None
    market: AliExpressMarket = get_aliexpress_market(store_region)
    business = {
        'product_ids': pid,
        'fields': DETAIL_FIELDS,
        'target_currency': market['target_currency'],
        'target_language': market['target_language'],
        'country': market['country'],
    }
    tracking_id = (getattr(settings, 'ALIEXPRESS_TRACKING_ID', '') or '').strip()
    if tracking_id:
        business['tracking_id'] = tracking_id
    data = call_api(PRODUCT_DETAIL_METHOD, business)
    products = _products_from_detail_response(data)
    if not products:
        return None
    for row in products:
        row_id = str(row.get('product_id') or row.get('productId') or '').strip()
        if row_id == pid or not row_id:
            return row
    return products[0]

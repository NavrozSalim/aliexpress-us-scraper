"""AliExpress Open Platform (IOP) client for api-sg.aliexpress.com."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Callable

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_IOP_GATEWAY = 'https://api-sg.aliexpress.com'
IOP_SIGN_METHOD = 'sha256'

TOKEN_CREATE_PATH = '/auth/token/security/create'
TOKEN_REFRESH_PATH = '/auth/token/refresh'
DS_PRODUCT_GET_METHOD = 'aliexpress.ds.product.get'
FREIGHT_CALCULATE_METHOD = 'aliexpress.logistics.buyer.freight.calculate'


class AliExpressIOPError(Exception):
    def __init__(self, message: str, *, response_body: str | None = None):
        super().__init__(message)
        self.response_body = response_body


def get_iop_gateway() -> str:
    url = (getattr(settings, 'ALIEXPRESS_IOP_GATEWAY', '') or '').strip()
    return url or DEFAULT_IOP_GATEWAY


def iop_timestamp_ms() -> str:
    return str(int(time.time() * 1000))


def method_to_iop_path(api_method: str) -> str:
    """Dotted IOP path (``/aliexpress.ds.product.get``) — used in REST URL attempts."""
    name = (api_method or '').strip().lstrip('/')
    if not name:
        raise ValueError('api_method is required')
    return f'/{name}'


def method_to_slash_path(api_method: str) -> str:
    """Slash IOP path for signing (``/aliexpress/ds/product/get``)."""
    name = (api_method or '').strip().lstrip('/')
    if not name:
        raise ValueError('api_method is required')
    return '/' + name.replace('.', '/')


def _sorted_param_items(params: dict[str, Any]) -> list[tuple[str, str]]:
    return sorted(
        (k, str(v))
        for k, v in params.items()
        if k != 'sign' and v is not None and str(v) != ''
    )


def sign_iop_request(api_path: str, params: dict[str, Any], app_secret: str) -> str:
    """IOP HMAC-SHA256: api_path + sorted(key+value pairs), excluding sign."""
    base = api_path + ''.join(f'{k}{v}' for k, v in _sorted_param_items(params))
    digest = hmac.new(app_secret.encode('utf-8'), base.encode('utf-8'), hashlib.sha256)
    return digest.hexdigest().upper()


def sign_iop_sync_request(params: dict[str, Any], app_secret: str) -> str:
    """IOP /sync HMAC-SHA256: sorted(key+value) only — dotted ``method`` has no path prefix."""
    base = ''.join(f'{k}{v}' for k, v in _sorted_param_items(params))
    digest = hmac.new(app_secret.encode('utf-8'), base.encode('utf-8'), hashlib.sha256)
    return digest.hexdigest().upper()


def _app_credentials() -> tuple[str, str]:
    app_key = (getattr(settings, 'ALIEXPRESS_APP_KEY', '') or '').strip()
    app_secret = (getattr(settings, 'ALIEXPRESS_APP_SECRET', '') or '').strip()
    if not app_key or not app_secret:
        raise AliExpressIOPError(
            'AliExpress API credentials not configured (set ALIEXPRESS_APP_KEY and ALIEXPRESS_APP_SECRET)'
        )
    return app_key, app_secret


def _base_iop_params(*, timestamp_fn: Callable[[], str] = iop_timestamp_ms) -> dict[str, str]:
    app_key, _ = _app_credentials()
    return {
        'app_key': app_key,
        'sign_method': IOP_SIGN_METHOD,
        'timestamp': timestamp_fn(),
    }


def iop_system_request(api_path: str, business_params: dict[str, Any], *, timeout: int = 30) -> dict:
    """POST to {gateway}/rest{api_path} for token create/refresh (no access_token)."""
    _, app_secret = _app_credentials()
    path = api_path if api_path.startswith('/') else f'/{api_path}'
    params = _base_iop_params()
    params.update({k: str(v) for k, v in business_params.items() if v is not None and str(v) != ''})
    params['sign'] = sign_iop_request(path, params, app_secret)
    url = f'{get_iop_gateway().rstrip("/")}/rest{path}'
    return _post_iop(url, params, timeout=timeout)


def _build_sync_params(
    api_method: str,
    business_params: dict[str, Any],
    access_token: str,
    *,
    timestamp_fn: Callable[[], str],
) -> dict[str, str]:
    app_key, _ = _app_credentials()
    method = (api_method or '').strip()
    params: dict[str, str] = {
        'app_key': app_key,
        'method': method,
        'session': access_token,
        'sign_method': IOP_SIGN_METHOD,
        'timestamp': timestamp_fn(),
        'simplify': 'true',
    }
    params.update({k: str(v) for k, v in business_params.items() if v is not None and str(v) != ''})
    return params


def iop_sync_business_request(
    api_method: str,
    business_params: dict[str, Any],
    access_token: str,
    *,
    timeout: int = 30,
) -> dict:
    """
    POST to {gateway}/sync for dotted business APIs (``aliexpress.ds.product.get``).

    Signs sorted params only (no path prefix), uses millisecond timestamp, and sends
    parameters as the POST query string (AliExpress IOP /sync convention).
    """
    _, app_secret = _app_credentials()
    params = _build_sync_params(
        api_method,
        business_params,
        access_token,
        timestamp_fn=iop_timestamp_ms,
    )
    params['sign'] = sign_iop_sync_request(params, app_secret)
    url = f'{get_iop_gateway().rstrip("/")}/sync'
    return _post_iop(url, params, timeout=timeout, query_params=True)


def _iop_sync_business_request_form(
    api_method: str,
    business_params: dict[str, Any],
    access_token: str,
    *,
    timeout: int = 30,
) -> dict:
    """Same as ``iop_sync_business_request`` but POST body (form) instead of query string."""
    _, app_secret = _app_credentials()
    params = _build_sync_params(
        api_method,
        business_params,
        access_token,
        timestamp_fn=iop_timestamp_ms,
    )
    params['sign'] = sign_iop_sync_request(params, app_secret)
    url = f'{get_iop_gateway().rstrip("/")}/sync'
    return _post_iop(url, params, timeout=timeout, query_params=False)


def iop_rest_business_request(
    api_method: str,
    business_params: dict[str, Any],
    access_token: str,
    *,
    timeout: int = 30,
) -> dict:
    """POST to {gateway}/rest/aliexpress/ds/product/get with access_token (IOP REST)."""
    _, app_secret = _app_credentials()
    path = method_to_slash_path(api_method)
    params = _base_iop_params()
    params['access_token'] = access_token
    params['format'] = 'json'
    params['v'] = '2.0'
    params['simplify'] = 'true'
    params.update({k: str(v) for k, v in business_params.items() if v is not None and str(v) != ''})
    params['sign'] = sign_iop_request(path, params, app_secret)
    url = f'{get_iop_gateway().rstrip("/")}/rest{path}'
    return _post_iop(url, params, timeout=timeout)


def _post_iop(
    url: str,
    params: dict[str, str],
    *,
    timeout: int,
    query_params: bool = False,
) -> dict:
    try:
        if query_params:
            resp = requests.post(url, params=params, timeout=timeout)
        else:
            resp = requests.post(url, data=params, timeout=timeout)
    except requests.RequestException as exc:
        raise AliExpressIOPError(str(exc)) from exc
    body = resp.text or ''
    if resp.status_code >= 400:
        raise AliExpressIOPError(
            f'AliExpress IOP HTTP {resp.status_code}',
            response_body=body[:500],
        )
    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise AliExpressIOPError('AliExpress IOP returned non-JSON response', response_body=body[:500]) from exc
    err = _extract_iop_error(data)
    if err:
        raise AliExpressIOPError(err, response_body=body[:500])
    return data


def _extract_iop_error(data: dict) -> str | None:
    if not isinstance(data, dict):
        return 'Invalid IOP response'
    if data.get('error_response'):
        err = data['error_response']
        if isinstance(err, dict):
            code = err.get('code') or err.get('type') or ''
            msg = err.get('msg') or err.get('message') or 'API error'
            return f'{code}: {msg}'.strip(': ')
    code = data.get('code') or data.get('error_code') or ''
    if str(code) not in ('', '0', '200'):
        msg = data.get('message') or data.get('msg') or data.get('error_msg') or 'API error'
        return f'{code}: {msg}'.strip(': ')
    if data.get('type') and str(data.get('type')).upper() == 'ISV':
        return data.get('message') or data.get('code') or 'ISV error'
    return None


def create_token_from_code(code: str, *, uuid: str | None = None) -> dict:
    """Exchange OAuth authorization code for access/refresh tokens."""
    params: dict[str, Any] = {'code': code}
    if uuid:
        params['uuid'] = uuid
    return iop_system_request(TOKEN_CREATE_PATH, params)


def refresh_access_token(refresh_token: str) -> dict:
    """Refresh an expired access token."""
    return iop_system_request(TOKEN_REFRESH_PATH, {'refresh_token': refresh_token})


def _ds_business_params(product_id: str, store_region: str | None) -> dict[str, str]:
    from scrapers.aliexpress_markets import get_aliexpress_market

    market = get_aliexpress_market(store_region)
    return {
        'product_id': product_id,
        'ship_to_country': market['country'],
        'target_currency': market['target_currency'],
        'target_language': market['target_language'],
    }


def _try_extract_ds_result(data: dict | None, product_id: str) -> dict | None:
    result = _extract_ds_product_result(data) if data else None
    if result is not None:
        return result
    return None


def _iop_ds_request(api_method: str, business_params: dict[str, Any], access_token: str, *, log_id: str) -> dict:
    """Call a dotted DS business API via sync/rest fallbacks."""
    errors: list[str] = []
    attempts: list[tuple[str, Callable[[], dict]]] = [
        ('sync', lambda: iop_sync_business_request(api_method, business_params, access_token)),
        (
            'sync-form',
            lambda: _iop_sync_business_request_form(api_method, business_params, access_token),
        ),
        ('rest-slash', lambda: iop_rest_business_request(api_method, business_params, access_token)),
    ]
    for label, call in attempts:
        try:
            return call()
        except AliExpressIOPError as exc:
            errors.append(f'{label}: {exc}')
            logger.warning('AliExpress DS %s %s failed for %s: %s', api_method, label, log_id, exc)
    raise AliExpressIOPError('; '.join(errors))


def fetch_ds_product(product_id: str, store_region: str | None, access_token: str) -> dict | None:
    """Call aliexpress.ds.product.get and return the ``result`` object, or None."""
    pid = str(product_id or '').strip()
    if not pid:
        return None
    business = _ds_business_params(pid, store_region)
    try:
        data = _iop_ds_request(DS_PRODUCT_GET_METHOD, business, access_token, log_id=pid)
    except AliExpressIOPError:
        raise
    return _try_extract_ds_result(data, pid) or None


def fetch_freight_calculate(
    product_id: str,
    sku_price: float,
    store_region: str | None,
    access_token: str,
) -> dict | None:
    """Call aliexpress.logistics.buyer.freight.calculate; return ``result`` dict or None."""
    from scrapers.aliexpress_markets import get_aliexpress_market

    pid = str(product_id or '').strip()
    if not pid or sku_price is None:
        return None
    market = get_aliexpress_market(store_region)
    # Official DTO fields only — do not send sku_id (causes UnsupportedParamMapping).
    freight_dto = {
        'country_code': market['country'],
        'product_id': pid,
        'product_num': '1',
        'send_goods_country_code': 'CN',
        'price': f'{float(sku_price):.2f}',
        'price_currency': market['target_currency'],
    }
    business = {
        'param_aeop_freight_calculate_for_buyer_d_t_o': json.dumps(freight_dto, separators=(',', ':')),
    }
    try:
        data = _iop_ds_request(FREIGHT_CALCULATE_METHOD, business, access_token, log_id=pid)
    except AliExpressIOPError:
        raise
    return _extract_freight_calculate_result(data)


def _extract_freight_calculate_result(data: dict) -> dict | None:
    if not isinstance(data, dict):
        return None
    for key in (
        'aliexpress_logistics_buyer_freight_calculate_response',
        'aliexpressLogisticsBuyerFreightCalculateResponse',
    ):
        root = data.get(key)
        if isinstance(root, dict):
            result = root.get('result')
            if isinstance(result, dict):
                return result
    result = data.get('result')
    return result if isinstance(result, dict) else None


def _extract_ds_product_result(data: dict) -> dict | None:
    if not isinstance(data, dict):
        return None
    for key in (
        'aliexpress_ds_product_get_response',
        'aliexpressDsProductGetResponse',
    ):
        root = data.get(key)
        if isinstance(root, dict):
            result = root.get('result')
            if isinstance(result, dict):
                return result
    result = data.get('result')
    return result if isinstance(result, dict) else None

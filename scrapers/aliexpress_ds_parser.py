"""Parse aliexpress.ds.product.get responses into scraper fields."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


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


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


_SKU_NESTED_KEYS = (
    'ae_item_sku_info_dto',
    'ae_item_sku_info_d_t_o',  # simplify=true responses
    'aeItemSkuInfoDto',
    'ae_item_sku_info',
    'aeItemSkuInfo',
)

_SKU_PRICE_KEYS = (
    'offer_sale_price',
    'offerSalePrice',
    'offer_bulk_sale_price',
    'offerBulkSalePrice',
    'sku_price',
    'skuPrice',
    'target_sale_price',
    'targetSalePrice',
    'target_original_price',
    'targetOriginalPrice',
    'sale_price',
    'salePrice',
)

_BASE_PRICE_KEYS = (
    'target_sale_price',
    'targetSalePrice',
    'product_min_price',
    'productMinPrice',
    'product_max_price',
    'productMaxPrice',
)


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in _SKU_NESTED_KEYS:
            inner = value.get(key)
            if isinstance(inner, list):
                return inner
            if isinstance(inner, dict):
                return [inner]
    return []


def _sku_rows(result: dict) -> list[dict]:
    skus = result.get('ae_item_sku_info_dtos') or result.get('aeItemSkuInfoDtos')
    if skus is None:
        return []
    return _as_list(skus)


def _base_info(result: dict) -> dict:
    return _as_dict(result.get('ae_item_base_info_dto') or result.get('aeItemBaseInfoDto'))


def title_from_ds_result(result: dict) -> str | None:
    base = _base_info(result)
    title = (base.get('subject') or result.get('subject') or '').strip()
    return title[:500] if title else None


def _first_price(row: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        price = _parse_price(row.get(key))
        if price is not None:
            return price
    return None


def price_from_ds_result(result: dict) -> float | None:
    prices: list[float] = []
    for row in _sku_rows(result):
        price = _first_price(row, _SKU_PRICE_KEYS)
        if price is not None:
            prices.append(price)
    if prices:
        return min(prices)
    base = _base_info(result)
    return _first_price(base, _BASE_PRICE_KEYS)


def stock_from_ds_result(result: dict) -> int | None:
    total = 0
    found = False
    for row in _sku_rows(result):
        for key in (
            'sku_available_stock',
            'skuAvailableStock',
            'ipm_sku_stock',
            'ipmSkuStock',
            'available_stock',
            'availableStock',
        ):
            raw = row.get(key)
            if raw is None or raw == '':
                continue
            try:
                qty = int(float(str(raw).strip()))
            except (TypeError, ValueError):
                continue
            total += max(0, qty)
            found = True
            break
        if not found and row.get('sku_stock') is True:
            total += 1
            found = True
    if found:
        return total
    base = _base_info(result)
    status = (base.get('product_status_type') or base.get('productStatusType') or '').strip().lower()
    if status == 'onselling':
        return None
    if status:
        return 0
    return None


def parse_delivery_days(text) -> tuple[int | None, int | None]:
    """Parse AliExpress delivery strings such as ``5-8day`` or ``3~10`` into (min, max) days."""
    nums = [int(n) for n in re.findall(r'\d+', str(text or ''))]
    if not nums:
        return None, None
    return min(nums), max(nums)


def _sku_id_from_row(row: dict) -> str | None:
    for key in ('id', 'sku_id', 'skuId'):
        raw = row.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return None


def cheapest_sku_for_freight(result: dict) -> dict | None:
    """Return cheapest SKU row with id + price for freight.calculate."""
    best: dict | None = None
    best_price: float | None = None
    for row in _sku_rows(result):
        price = _first_price(row, _SKU_PRICE_KEYS)
        sku_id = _sku_id_from_row(row)
        if price is None or not sku_id:
            continue
        if best_price is None or price < best_price:
            best_price = price
            best = {'sku_id': sku_id, 'price': price}
    return best


def _freight_list_from_result(freight_result: dict) -> list[dict]:
    if not isinstance(freight_result, dict):
        return []
    if str(freight_result.get('success', '')).lower() == 'false':
        return []
    nested_row_keys = (
        'aeop_freight_calculate_result_for_buyer_dto',
        'aeop_freight_calculate_result_for_buyer_d_t_o',
    )
    for key in (
        'aeop_freight_calculate_result_for_buyer_d_t_o_list',
        'aeop_freight_calculate_result_for_buyer_dtolist',
        'aeop_freight_calculate_result_for_buyer_dto_list',
    ):
        container = freight_result.get(key)
        if container is None:
            continue
        if isinstance(container, list):
            return [r for r in container if isinstance(r, dict)]
        if isinstance(container, dict):
            for nested in nested_row_keys:
                inner = container.get(nested)
                if isinstance(inner, list):
                    return [r for r in inner if isinstance(r, dict)]
                if isinstance(inner, dict):
                    return [inner]
    return []


def freight_amount_from_option(option: dict) -> float | None:
    freight = _as_dict(option.get('freight'))
    amount = _parse_price(freight.get('amount'))
    if amount is not None:
        return amount
    cent = freight.get('cent')
    if cent is not None and str(cent).strip().isdigit():
        return round(int(str(cent).strip()) / 100.0, 2)
    return _parse_price(option.get('amount'))


def best_freight_within_days(freight_result: dict, *, max_delivery_days: int) -> dict | None:
    """
    Pick the cheapest freight option whose **max** estimated delivery days is <= max_delivery_days.

    Returns dict with shipping_price, delivery_min, delivery_max, service_name; or None.
    """
    best: dict | None = None
    best_shipping: float | None = None
    cap = max(1, int(max_delivery_days))
    for option in _freight_list_from_result(freight_result):
        delivery_min, delivery_max = parse_delivery_days(
            option.get('estimated_delivery_time') or option.get('estimatedDeliveryTime')
        )
        if delivery_max is None:
            continue
        if delivery_max > cap:
            continue
        shipping = freight_amount_from_option(option)
        if shipping is None:
            continue
        if best_shipping is None or shipping < best_shipping:
            best_shipping = shipping
            best = {
                'shipping_price': shipping,
                'delivery_min': delivery_min,
                'delivery_max': delivery_max,
                'service_name': (
                    option.get('service_name') or option.get('serviceName') or ''
                ).strip(),
            }
    return best


def product_logistics_delivery_max(result: dict) -> int | None:
    """Fallback lead time from ds.product.get ``logistics_info_dto.delivery_time``."""
    logistics = _as_dict(result.get('logistics_info_dto') or result.get('logisticsInfoDto'))
    raw = logistics.get('delivery_time') if logistics else None
    if raw is None:
        raw = logistics.get('deliveryTime')
    if raw is None:
        return None
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None

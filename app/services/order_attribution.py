"""
Order attribution for agent-routed carts.

When the ChekOut AI chat agent builds or routes a cart, it stamps cart
attributes (Shopify "note attributes") prefixed with `_chekout_ai`. Those
attributes flow through to the resulting order's `note_attributes`, which we
read back here to confirm which orders originated from an agent-assisted
session and to summarize what converted.

Privacy posture: we request orders with an explicit `fields` allowlist that
EXCLUDES all protected customer data (no `customer`, `email`, `billing_address`,
`shipping_address`, `client_details`, etc.). We only read the attributes we
stamped ourselves, line items, discount codes, cart token, and order source —
none of which is customer PII. This keeps the feature within the Protected
Customer Data "Level 1 / no-PII" boundary.

Requires the `read_orders` scope. Callers must gate on the merchant's actual
granted scopes (see utils.helpers.has_scope) before invoking this module — the
scope is optional and not granted to every merchant.
"""

import logging
from typing import Dict, List, Optional

from app.models import ShopifyStore
from app.services.shopify_oauth import ShopifyOAuth

logger = logging.getLogger(__name__)

# Cart-attribute key prefix the agent stamps. Underscore-prefixed note
# attributes are hidden from the customer in Shopify's UI. We match leniently
# so `chekout_ai_session` and `_chekout_ai_session` both attribute correctly.
ATTRIBUTION_PREFIX = "chekout_ai"

# Order scope required to read order data.
REQUIRED_SCOPE = "read_orders"

# Explicit, PII-free field allowlist. Anything not listed here is never fetched.
_ORDER_FIELDS = ",".join([
    "id",
    "name",            # human-readable order number, e.g. "#1001"
    "created_at",
    "note_attributes",  # where our agent attribution lives
    "line_items",       # product/variant/qty/price — no customer PII
    "discount_codes",   # agent-issued coupons
    "total_price",
    "currency",
    "financial_status",
    "source_name",      # "web", "pos", or the app that created the order
    "cart_token",       # links order to the cart the agent built
])


def _is_attribution_attr(name: Optional[str]) -> bool:
    """True if a note-attribute name is one the agent stamped."""
    if not name:
        return False
    return name.lstrip("_").lower().startswith(ATTRIBUTION_PREFIX)


def extract_attribution(order: Dict) -> Optional[Dict]:
    """
    Return a PII-free attribution summary for an order if it was routed by the
    agent, else None. `order` is a raw Shopify REST order dict.
    """
    note_attributes = order.get("note_attributes") or []
    agent_attrs = {
        attr.get("name"): attr.get("value")
        for attr in note_attributes
        if _is_attribution_attr(attr.get("name"))
    }
    if not agent_attrs:
        return None

    line_items = [
        {
            "product_id": li.get("product_id"),
            "variant_id": li.get("variant_id"),
            "title": li.get("title"),
            "sku": li.get("sku"),
            "quantity": li.get("quantity"),
            "price": li.get("price"),
        }
        for li in (order.get("line_items") or [])
    ]

    return {
        "order_id": order.get("id"),
        "order_name": order.get("name"),
        "created_at": order.get("created_at"),
        "financial_status": order.get("financial_status"),
        "source_name": order.get("source_name"),
        "cart_token": order.get("cart_token"),
        "total_price": order.get("total_price"),
        "currency": order.get("currency"),
        "attribution": agent_attrs,
        "discount_codes": order.get("discount_codes") or [],
        "line_items": line_items,
    }


async def fetch_attributed_orders(
    merchant: ShopifyStore,
    limit: int = 250,
    since_id: Optional[int] = None,
) -> Dict:
    """
    Fetch recent orders (last 60 days under read_orders) and return only those
    the agent attributed. Field-limited and PII-free.

    Returns a dict with the attributed-order summaries and basic counts. Does
    NOT persist anything — this is a live read. Callers must verify the merchant
    has the read_orders scope before calling.
    """
    oauth = ShopifyOAuth()

    endpoint = f"/orders.json?status=any&limit={min(max(limit, 1), 250)}&fields={_ORDER_FIELDS}"
    if since_id:
        endpoint += f"&since_id={since_id}"

    data = await oauth.make_shopify_request(
        shop_domain=merchant.shop_domain,
        access_token=merchant.access_token,
        endpoint=endpoint,
        method="GET",
    )

    orders = data.get("orders", []) if isinstance(data, dict) else []
    attributed: List[Dict] = []
    for order in orders:
        summary = extract_attribution(order)
        if summary:
            attributed.append(summary)

    logger.info(
        f"[Order Attribution] {merchant.merchant_id}: "
        f"{len(attributed)}/{len(orders)} orders attributed to agent"
    )

    return {
        "merchant_id": merchant.merchant_id,
        "shop_domain": merchant.shop_domain,
        "orders_scanned": len(orders),
        "attributed_count": len(attributed),
        "attributed_orders": attributed,
    }

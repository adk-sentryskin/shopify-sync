"""
Persistence layer for agent-attributed Shopify orders.

Writes only attributed orders (those carrying `_chekout_ai_*` cart attributes)
into `shopify_sync.orders`. This is the OLTP source of truth for the
sales-attribution dashboard and for the (separate) periodic export that
mirrors a slim projection of this table into BigQuery for analytics.

Three responsibilities:
  - upsert_order  : parse a raw Shopify order and write/update its DB row
  - backfill_attributed_orders : fetch the last 60 days on first grant
  - prune_orders  : rolling 60-day delete to enforce the PCD attestation

Field selection mirrors `order_attribution._ORDER_FIELDS` — together they form
the PII-free allowlist enforced from the API request through to the DB.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from sqlalchemy.orm import Session

from app.models import Order, ShopifyStore
from app.services.order_attribution import (
    ATTRIBUTION_PREFIX,
    _ORDER_FIELDS,
    _is_attribution_attr,
)
from app.services.shopify_oauth import ShopifyOAuth

logger = logging.getLogger(__name__)

# Retention window we attested to in the Protected Customer Data request.
RETENTION_DAYS = 60

# Explicit attribute keys the agent stamps via /cart/update.js. We tolerate a
# leading underscore (the agent uses `_chekout_ai_*` to hide them from the
# customer cart UI).
_ATTR_SESSION = f"{ATTRIBUTION_PREFIX}_session"
_ATTR_AGENT = f"{ATTRIBUTION_PREFIX}_agent"
_ATTR_SOURCE = f"{ATTRIBUTION_PREFIX}_source"


def _parse_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """Tolerantly parse Shopify's RFC3339 timestamps."""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _extract_attrs(note_attributes: list) -> Dict[str, Optional[str]]:
    """
    Pull `chekout_ai_session/agent/source` out of a Shopify note_attributes list.
    Returns a dict with three keys (values may be None). Returns empty dict if
    the order carries no attribution attrs at all — caller treats that as
    "skip this order."
    """
    if not note_attributes:
        return {}
    found = {}
    for attr in note_attributes:
        name = attr.get("name") if isinstance(attr, dict) else None
        if not _is_attribution_attr(name):
            continue
        key = name.lstrip("_").lower()
        if key == _ATTR_SESSION:
            found["chekout_ai_session"] = attr.get("value")
        elif key == _ATTR_AGENT:
            found["chekout_ai_agent"] = attr.get("value")
        elif key == _ATTR_SOURCE:
            found["chekout_ai_source"] = attr.get("value")
        else:
            # Some chekout_ai_* attr we don't have a dedicated column for —
            # presence alone still flags this as an attributed order; the raw
            # value is preserved in note_attributes_raw.
            found.setdefault("_other", True)
    return found


def parse_order_for_db(
    raw_order: Dict,
    store: ShopifyStore,
    event_type: str,
) -> Optional[Dict]:
    """
    Convert a raw Shopify order dict into the kwargs for an `Order` row.
    Returns None if the order is NOT agent-attributed (no `_chekout_ai_*`
    attrs) — we only persist orders we can attribute.
    """
    note_attributes = raw_order.get("note_attributes") or []
    attrs = _extract_attrs(note_attributes)
    if not attrs:
        return None

    created_at = _parse_datetime(raw_order.get("created_at"))
    if not created_at:
        # Shopify always emits created_at on real orders. A missing one means
        # the payload is malformed (or a test fixture); skip rather than guess.
        logger.warning(
            f"[Order Persistence] Skipping order {raw_order.get('id')}: "
            f"missing or unparseable created_at"
        )
        return None

    return {
        "shopify_order_id": raw_order.get("id"),
        "order_name": raw_order.get("name"),
        "store_id": store.id,
        "merchant_id": store.merchant_id,
        "shop_domain": store.shop_domain,
        "shopify_created_at": created_at,
        "shopify_updated_at": _parse_datetime(raw_order.get("updated_at")),
        "chekout_ai_session": attrs.get("chekout_ai_session"),
        "chekout_ai_agent": attrs.get("chekout_ai_agent"),
        "chekout_ai_source": attrs.get("chekout_ai_source"),
        "line_items": raw_order.get("line_items") or [],
        "discount_codes": raw_order.get("discount_codes") or [],
        "total_price": raw_order.get("total_price"),
        "currency": raw_order.get("currency"),
        "financial_status": raw_order.get("financial_status"),
        "source_name": raw_order.get("source_name"),
        "cart_token": raw_order.get("cart_token"),
        "note_attributes_raw": note_attributes,
        "last_event_type": event_type,
    }


def upsert_order(
    db: Session,
    store: ShopifyStore,
    raw_order: Dict,
    event_type: str,
) -> Optional[Order]:
    """
    Upsert a Shopify order if it's agent-attributed. Returns the Order row
    (inserted or updated), or None if not attributed.
    """
    row = parse_order_for_db(raw_order, store, event_type)
    if row is None:
        return None

    existing = db.query(Order).filter(
        Order.shopify_order_id == row["shopify_order_id"]
    ).first()

    if existing:
        # Update mutable fields only — never reassign the primary identifiers
        # (id, shopify_order_id, store_id) or shopify_created_at on subsequent
        # events; those should be stable across the lifetime of the order.
        mutable_fields = (
            "order_name", "shopify_updated_at",
            "chekout_ai_session", "chekout_ai_agent", "chekout_ai_source",
            "line_items", "discount_codes", "total_price", "currency",
            "financial_status", "source_name", "cart_token",
            "note_attributes_raw", "last_event_type",
        )
        for field in mutable_fields:
            setattr(existing, field, row[field])
        order = existing
    else:
        order = Order(**row)
        db.add(order)

    db.commit()
    db.refresh(order)
    return order


async def backfill_attributed_orders(
    db: Session,
    store: ShopifyStore,
) -> Dict:
    """
    Fetch the merchant's recent orders (Shopify's read_orders gives last
    60 days by default) and persist the attributed subset. Called once when
    a merchant first grants `read_orders`.

    Walks a single page (up to 250 orders). In practice every merchant has
    far fewer agent-attributed orders than 250 in their first 60-day window;
    if that ever stops being true we'll add since_id pagination.
    """
    oauth = ShopifyOAuth()
    endpoint = f"/orders.json?status=any&limit=250&fields={_ORDER_FIELDS}"

    scanned = 0
    persisted = 0

    try:
        data = await oauth.make_shopify_request(
            shop_domain=store.shop_domain,
            access_token=store.access_token,
            endpoint=endpoint,
            method="GET",
        )
        orders = data.get("orders", []) if isinstance(data, dict) else []
        scanned = len(orders)
        for raw_order in orders:
            if upsert_order(db, store, raw_order, event_type="backfill") is not None:
                persisted += 1
    except Exception as e:
        logger.error(
            f"[Order Backfill] Failed for {store.merchant_id}: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "merchant_id": store.merchant_id,
            "scanned": scanned,
            "persisted": persisted,
            "error": str(e),
        }

    logger.info(
        f"[Order Backfill] {store.merchant_id}: scanned {scanned}, "
        f"persisted {persisted} attributed"
    )
    return {
        "status": "completed",
        "merchant_id": store.merchant_id,
        "scanned": scanned,
        "persisted": persisted,
    }


def prune_orders(db: Session, retention_days: int = RETENTION_DAYS) -> int:
    """
    Hard-delete orders whose `shopify_created_at` is older than retention_days.
    Enforces the 60-day retention attested to with Shopify. Returns count.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = db.query(Order).filter(Order.shopify_created_at < cutoff).delete(
        synchronize_session=False
    )
    db.commit()
    logger.info(
        f"[Order Prune] Deleted {deleted} orders older than "
        f"{retention_days} days (< {cutoff.isoformat()})"
    )
    return deleted


def delete_orders_for_merchant(db: Session, store_id: int) -> int:
    """
    Hard-delete all orders for a given store. Called from the `shop/redact`
    webhook 48h after a merchant uninstalls the app.
    """
    deleted = db.query(Order).filter(Order.store_id == store_id).delete(
        synchronize_session=False
    )
    db.commit()
    logger.info(f"[Order Redact] Deleted {deleted} orders for store_id={store_id}")
    return deleted

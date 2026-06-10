"""
Order attribution endpoints.

Surfaces which orders were routed through the ChekOut AI agent (via cart
attributes). All read paths are Postgres-backed (sub-100ms aggregations);
Shopify is read only for the one-time backfill triggered at scope-grant time.

Gated on the merchant's actual granted `read_orders` scope — merchants without
the scope get a clean 403 rather than a Shopify API error.
"""

from decimal import Decimal
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.models import Order, ShopifyStore
from app.middleware.auth import get_merchant_from_header
from app.services.order_attribution import REQUIRED_SCOPE
from app.services.scope_reconciliation import (
    provision_order_access_background,
    reconcile_scopes_from_shopify,
)
from app.utils.helpers import has_scope

router = APIRouter(prefix="/api/orders", tags=["Orders"])


def _ensure_scope(merchant: ShopifyStore) -> None:
    """Raise 403 if the merchant hasn't granted read_orders."""
    if not has_scope(merchant.scope, REQUIRED_SCOPE):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Merchant has not granted the '{REQUIRED_SCOPE}' scope. "
                "Sales attribution is unavailable until the merchant authorizes "
                "order access."
            ),
        )


@router.get("/scope-status")
async def order_scope_status(
    background_tasks: BackgroundTasks,
    merchant: ShopifyStore = Depends(get_merchant_from_header),
    db: Session = Depends(get_db),
):
    """
    Report whether this merchant has granted the sales-attribution scope.

    Drives the dashboard CTA: if not granted, show the "Enable sales
    attribution" prompt; if granted, render the attribution widgets.

    Self-healing reconcile: when our stored scope doesn't yet include
    read_orders, re-read the real granted scopes from Shopify. The managed-
    install optional-scopes grant never hits /oauth/complete, so this is how
    the dashboard learns the merchant just enabled it (the frontend polls this
    endpoint after redirecting them through the grant URL). On first sight of
    read_orders we provision order access (webhooks + 60-day backfill) in the
    background.

    Headers:
        - X-ShopifyStore-Id: ShopifyStore identifier (required)
    """
    granted = has_scope(merchant.scope, REQUIRED_SCOPE)

    if not granted:
        _changed, newly_granted = await reconcile_scopes_from_shopify(db, merchant)
        if newly_granted:
            background_tasks.add_task(provision_order_access_background, merchant.id)
        granted = has_scope(merchant.scope, REQUIRED_SCOPE)

    return {
        "merchant_id": merchant.merchant_id,
        "required_scope": REQUIRED_SCOPE,
        "granted": granted,
        "sales_attribution_available": granted,
    }


@router.get("/attributed")
async def get_attributed_orders(
    limit: int = Query(100, ge=1, le=500, description="Max orders to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    merchant: ShopifyStore = Depends(get_merchant_from_header),
    db: Session = Depends(get_db),
):
    """
    Return the merchant's agent-attributed orders, newest first.

    Backed by the local `shopify_sync.orders` table — fast, no Shopify call
    in the request path. The table is populated by the webhook handlers
    (orders/create + updated + cancelled) and the one-time backfill at grant.

    Headers:
        - X-ShopifyStore-Id: ShopifyStore identifier (required)
    """
    _ensure_scope(merchant)

    rows = (
        db.query(Order)
        .filter(Order.merchant_id == merchant.merchant_id)
        .order_by(Order.shopify_created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )

    return {
        "merchant_id": merchant.merchant_id,
        "shop_domain": merchant.shop_domain,
        "count": len(rows),
        "orders": [
            {
                "order_id": r.shopify_order_id,
                "order_name": r.order_name,
                "created_at": r.shopify_created_at.isoformat() if r.shopify_created_at else None,
                "financial_status": r.financial_status,
                "source_name": r.source_name,
                "cart_token": r.cart_token,
                "total_price": str(r.total_price) if r.total_price is not None else None,
                "currency": r.currency,
                "chekout_ai_session": r.chekout_ai_session,
                "line_items": r.line_items or [],
                "discount_codes": r.discount_codes or [],
            }
            for r in rows
        ],
    }


@router.get("/attribution-summary")
async def attribution_summary(
    merchant: ShopifyStore = Depends(get_merchant_from_header),
    db: Session = Depends(get_db),
):
    """
    Aggregate stats for the sales-attribution dashboard widgets.

    Returns counts and totals derived from `shopify_sync.orders` (Postgres
    aggregation, sub-100ms). The Sankey "products" count, the revenue card,
    and the conversion KPI all source from this.

    Headers:
        - X-ShopifyStore-Id: ShopifyStore identifier (required)
    """
    _ensure_scope(merchant)

    base = db.query(Order).filter(Order.merchant_id == merchant.merchant_id)

    orders_attributed = base.count()

    # Revenue + units summed over PAID orders only — unpaid/cancelled don't
    # represent realized revenue and would skew the dashboard.
    paid = base.filter(Order.financial_status == "paid")
    revenue_row = paid.with_entities(
        func.coalesce(func.sum(Order.total_price), Decimal(0)).label("revenue"),
    ).one()

    # Currency is per-merchant in practice; pick the modal currency across
    # paid orders. (Shopify allows multi-currency stores; for v1 we surface
    # the dominant one and the dashboard can break out by currency later.)
    currency_row = (
        paid.with_entities(Order.currency, func.count(Order.id).label("n"))
        .group_by(Order.currency)
        .order_by(func.count(Order.id).desc())
        .first()
    )
    currency = currency_row[0] if currency_row else None

    # Counts by financial_status — useful for the revenue widget's
    # "paid / pending / refunded" breakdown.
    status_rows = (
        base.with_entities(Order.financial_status, func.count(Order.id))
        .group_by(Order.financial_status)
        .all()
    )
    by_status = {s or "unknown": int(n) for s, n in status_rows}

    # Units sold and distinct product count — both derived from line_items
    # JSONB. Postgres jsonb_array_elements + a CTE would be marginally faster,
    # but at our scale a small Python aggregation off the indexed paid rows
    # is fine and keeps the query simple.
    units_sold = 0
    unique_products = set()
    for (line_items,) in paid.with_entities(Order.line_items).all():
        for li in (line_items or []):
            try:
                units_sold += int(li.get("quantity", 0) or 0)
            except (TypeError, ValueError):
                pass
            pid = li.get("product_id")
            if pid is not None:
                unique_products.add(pid)

    return {
        "merchant_id": merchant.merchant_id,
        "shop_domain": merchant.shop_domain,
        "window_days": 60,
        "summary": {
            "orders_attributed": orders_attributed,
            "total_revenue": str(revenue_row.revenue),
            "currency": currency,
            "units_sold": units_sold,
            "unique_products": len(unique_products),
            "by_financial_status": by_status,
        },
    }

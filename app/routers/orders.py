"""
Order attribution endpoints.

Surfaces which orders were routed through the ChekOut AI agent (via cart
attributes). Gated on the merchant's actual granted `read_orders` scope — this
scope is optional and protected-customer-data reviewed, so merchants without it
get a clear 403 rather than a Shopify API error.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from app.models import ShopifyStore
from app.middleware.auth import get_merchant_from_header
from app.services.order_attribution import (
    REQUIRED_SCOPE,
    fetch_attributed_orders,
)
from app.utils.helpers import has_scope

router = APIRouter(prefix="/api/orders", tags=["Orders"])


@router.get("/scope-status")
async def order_scope_status(
    merchant: ShopifyStore = Depends(get_merchant_from_header),
):
    """
    Report whether this merchant has granted the order-attribution scope.

    Lets the dashboard/frontend decide whether to show order-attribution UI or
    prompt the merchant to grant `read_orders`, without triggering a failing
    Shopify call.

    Headers:
        - X-ShopifyStore-Id: ShopifyStore identifier (required)
    """
    granted = has_scope(merchant.scope, REQUIRED_SCOPE)
    return {
        "merchant_id": merchant.merchant_id,
        "required_scope": REQUIRED_SCOPE,
        "granted": granted,
        "order_attribution_available": granted,
    }


@router.get("/attributed")
async def get_attributed_orders(
    limit: int = Query(250, ge=1, le=250, description="Max orders to scan"),
    since_id: Optional[int] = Query(None, description="Scan orders after this ID"),
    merchant: ShopifyStore = Depends(get_merchant_from_header),
):
    """
    Return orders that were routed through the ChekOut AI agent.

    Gated on the `read_orders` scope. Reads a PII-free, field-limited view of
    orders and filters to those carrying agent cart attributes.

    Headers:
        - X-ShopifyStore-Id: ShopifyStore identifier (required)
    """
    if not has_scope(merchant.scope, REQUIRED_SCOPE):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Merchant has not granted the '{REQUIRED_SCOPE}' scope. "
                "Order attribution is unavailable until the merchant re-authorizes "
                "with order access granted."
            ),
        )

    try:
        return await fetch_attributed_orders(merchant, limit=limit, since_id=since_id)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch orders from Shopify: {str(e)}",
        )

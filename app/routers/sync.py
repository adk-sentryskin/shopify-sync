from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Dict
from app.database import get_db
from app.models import Merchant, Product
from app.middleware.auth import get_merchant_from_header
from app.services.product_reconciliation import reconcile_products, force_full_resync

router = APIRouter(prefix="/api/sync", tags=["Sync & Reconciliation"])


@router.post("/reconcile")
async def reconcile_products_endpoint(
    mark_deleted: bool = Query(
        False,
        description="Mark products as deleted if they don't exist in Shopify"
    ),
    merchant: Merchant = Depends(get_merchant_from_header),
    db: Session = Depends(get_db)
):
    """
    Reconcile products between Shopify and local database

    Compares the local database with Shopify to detect and fix:
    - Products missing from the database
    - Products that are out of sync (different updated_at timestamps)
    - Products deleted in Shopify but still active in database

    This is a safety net for webhook failures or extended downtime.

    Headers:
        - X-Merchant-Id: Merchant identifier (required)

    Query Parameters:
        - mark_deleted: If True, marks products as deleted if they don't exist in Shopify (default: False)

    Returns:
        Detailed reconciliation report including:
        - Products in Shopify vs Database counts
        - Missing products (added to database)
        - Deleted products (marked as deleted if mark_deleted=True)
        - Out of sync products (re-synced)
        - Total synced and marked deleted counts
    """
    if not merchant.access_token:
        raise HTTPException(
            status_code=403,
            detail="Merchant has not completed OAuth. Please authenticate first."
        )

    try:
        results = await reconcile_products(
            db=db,
            merchant=merchant,
            shop_domain=merchant.shop_domain,
            access_token=merchant.access_token,
            mark_deleted=mark_deleted
        )

        return {
            "merchant_id": merchant.merchant_id,
            "shop_domain": merchant.shop_domain,
            **results
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Reconciliation failed: {str(e)}"
        )


@router.post("/force-resync")
async def force_full_resync_endpoint(
    merchant: Merchant = Depends(get_merchant_from_header),
    db: Session = Depends(get_db)
):
    """
    Force a full re-sync of all products from Shopify

    Fetches ALL products from Shopify and updates the database.
    More aggressive than reconciliation - updates all products
    regardless of whether they appear out of sync.

    Use this when:
    - You suspect widespread data inconsistencies
    - After recovering from extended downtime
    - When webhooks have been failing for a long time

    Headers:
        - X-Merchant-Id: Merchant identifier (required)

    Returns:
        Sync statistics including:
        - Total products synced
        - Created vs updated counts
        - Duration and status
    """
    if not merchant.access_token:
        raise HTTPException(
            status_code=403,
            detail="Merchant has not completed OAuth. Please authenticate first."
        )

    try:
        results = await force_full_resync(
            db=db,
            merchant=merchant,
            shop_domain=merchant.shop_domain,
            access_token=merchant.access_token
        )

        return {
            "merchant_id": merchant.merchant_id,
            "shop_domain": merchant.shop_domain,
            **results
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Full re-sync failed: {str(e)}"
        )


@router.get("/status")
async def get_sync_status(
    merchant: Merchant = Depends(get_merchant_from_header),
    db: Session = Depends(get_db)
):
    """
    Get current sync status for a merchant

    Returns statistics about products in the database including:
    - Total products (active only)
    - Total deleted products
    - Oldest and newest synced products
    - Products by status (active, draft, archived)

    Headers:
        - X-Merchant-Id: Merchant identifier (required)

    Returns:
        Sync status statistics
    """
    from sqlalchemy import func

    # Count active products
    active_count = db.query(func.count(Product.id)).filter(
        Product.merchant_id == merchant.id,
        Product.is_deleted == 0
    ).scalar()

    # Count deleted products
    deleted_count = db.query(func.count(Product.id)).filter(
        Product.merchant_id == merchant.id,
        Product.is_deleted == 1
    ).scalar()

    # Get oldest and newest sync times
    oldest_sync = db.query(func.min(Product.synced_at)).filter(
        Product.merchant_id == merchant.id,
        Product.is_deleted == 0
    ).scalar()

    newest_sync = db.query(func.max(Product.synced_at)).filter(
        Product.merchant_id == merchant.id,
        Product.is_deleted == 0
    ).scalar()

    # Count by status
    status_counts = {}
    statuses = db.query(
        Product.status,
        func.count(Product.id)
    ).filter(
        Product.merchant_id == merchant.id,
        Product.is_deleted == 0
    ).group_by(Product.status).all()

    for status, count in statuses:
        status_counts[status or 'unknown'] = count

    return {
        "merchant_id": merchant.merchant_id,
        "shop_domain": merchant.shop_domain,
        "products": {
            "total_active": active_count,
            "total_deleted": deleted_count,
            "by_status": status_counts
        },
        "sync_info": {
            "oldest_sync": oldest_sync,
            "newest_sync": newest_sync
        }
    }


@router.get("/")
async def sync_info():
    """
    Information about sync and reconciliation endpoints

    Returns documentation about available sync endpoints and their usage
    """
    return {
        "description": "Sync & Reconciliation API - Detect and fix data inconsistencies",
        "endpoints": [
            {
                "method": "POST",
                "path": "/api/sync/reconcile",
                "description": "Reconcile products between Shopify and database",
                "parameters": {
                    "mark_deleted": "If True, marks products as deleted if they don't exist in Shopify (default: False)"
                },
                "use_cases": [
                    "Detect products missing from database",
                    "Find products out of sync",
                    "Identify products deleted in Shopify",
                    "Recovery after webhook failures"
                ]
            },
            {
                "method": "POST",
                "path": "/api/sync/force-resync",
                "description": "Force a complete re-sync of all products",
                "use_cases": [
                    "After extended downtime",
                    "When webhooks have been failing",
                    "Suspect widespread inconsistencies"
                ]
            },
            {
                "method": "GET",
                "path": "/api/sync/status",
                "description": "Get current sync status and statistics",
                "returns": [
                    "Product counts (active, deleted)",
                    "Products by status",
                    "Oldest and newest sync times"
                ]
            }
        ],
        "reconciliation_process": {
            "step_1": "Fetch all products from Shopify",
            "step_2": "Compare with local database",
            "step_3": "Sync missing products",
            "step_4": "Re-sync out-of-date products",
            "step_5": "Optionally mark deleted products",
            "step_6": "Return detailed drift report"
        },
        "authentication": {
            "required": True,
            "header": "X-Merchant-Id"
        }
    }

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Dict, Optional
from app.database import get_db
from app.models import ShopifyStore, Product
from app.middleware.auth import get_merchant_from_header
from app.services.product_reconciliation import reconcile_products, force_full_resync
from app.services.scheduler import (
    get_scheduler_status,
    trigger_manual_reconciliation,
    reschedule_job
)

router = APIRouter(prefix="/api/sync", tags=["Sync & Reconciliation"])


@router.post("/reconcile")
async def reconcile_products_endpoint(
    mark_deleted: bool = Query(
        False,
        description="Mark products as deleted if they don't exist in Shopify"
    ),
    merchant: ShopifyStore = Depends(get_merchant_from_header),
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
        - X-ShopifyStore-Id: ShopifyStore identifier (required)

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
            detail="ShopifyStore has not completed OAuth. Please authenticate first."
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
    merchant: ShopifyStore = Depends(get_merchant_from_header),
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
        - X-ShopifyStore-Id: ShopifyStore identifier (required)

    Returns:
        Sync statistics including:
        - Total products synced
        - Created vs updated counts
        - Duration and status
    """
    if not merchant.access_token:
        raise HTTPException(
            status_code=403,
            detail="ShopifyStore has not completed OAuth. Please authenticate first."
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
    merchant: ShopifyStore = Depends(get_merchant_from_header),
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
        - X-ShopifyStore-Id: ShopifyStore identifier (required)

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
            "header": "X-ShopifyStore-Id"
        }
    }


@router.get("/scheduler/status")
async def get_scheduler_status_endpoint():
    """
    Get scheduler status and job information

    Returns information about the scheduled reconciliation job including:
    - Whether scheduler is running
    - Next scheduled run time
    - Job configuration

    No authentication required - this is a system status endpoint
    """
    try:
        status = get_scheduler_status()
        return {
            "scheduler": status
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get scheduler status: {str(e)}"
        )


@router.post("/scheduler/trigger")
async def trigger_manual_reconciliation_endpoint(
    merchant_id: Optional[str] = Query(None, description="Optional merchant ID. If not provided, runs for all merchants")
):
    """
    Manually trigger reconciliation (bypasses schedule)

    Triggers an immediate reconciliation run without waiting for the scheduled time.

    Query Parameters:
        - merchant_id: Optional merchant ID. If provided, only reconciles that merchant.
                      If not provided, reconciles all merchants.

    Returns:
        Execution results
    """
    try:
        # Get merchant database ID if merchant_id provided
        merchant_db_id = None
        if merchant_id:
            from app.database import SessionLocal
            db = SessionLocal()
            try:
                merchant = db.query(ShopifyStore).filter(
                    ShopifyStore.merchant_id == merchant_id,
                    ShopifyStore.is_active == 1
                ).first()

                if not merchant:
                    raise HTTPException(
                        status_code=404,
                        detail=f"ShopifyStore {merchant_id} not found or inactive"
                    )

                merchant_db_id = merchant.id
            finally:
                db.close()

        result = await trigger_manual_reconciliation(merchant_db_id)

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to trigger reconciliation: {str(e)}"
        )


@router.post("/scheduler/reschedule")
async def reschedule_job_endpoint(
    hour: int = Query(..., description="Hour of day (0-23) to run reconciliation", ge=0, le=23),
    minute: int = Query(0, description="Minute of hour (0-59) to run reconciliation", ge=0, le=59)
):
    """
    Reschedule the daily reconciliation job

    Changes the time when the daily reconciliation job runs.

    Query Parameters:
        - hour: Hour of the day (0-23)
        - minute: Minute of the hour (0-59, default: 0)

    Returns:
        Confirmation with new schedule time

    Example:
        POST /api/sync/scheduler/reschedule?hour=3&minute=30
        Reschedules to run daily at 3:30 AM UTC
    """
    try:
        reschedule_job(hour=hour, minute=minute)

        return {
            "status": "success",
            "message": f"Reconciliation rescheduled to {hour:02d}:{minute:02d} UTC",
            "new_schedule": {
                "hour": hour,
                "minute": minute,
                "timezone": "UTC"
            }
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reschedule job: {str(e)}"
        )

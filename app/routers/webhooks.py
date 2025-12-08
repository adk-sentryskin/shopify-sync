from fastapi import APIRouter, Request, HTTPException, Depends, Header, Query
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timezone
import json
from app.database import get_db
from app.models import Merchant, Product
from app.services.product_sync import upsert_product
from app.utils.webhook_verification import verify_webhook, extract_shop_domain, extract_webhook_topic
from app.services.webhook_manager import register_webhooks, list_webhooks, delete_webhook, sync_webhooks

router = APIRouter(prefix="/api/webhooks", tags=["Webhooks"])


async def verify_shopify_webhook(
    request: Request,
    x_shopify_hmac_sha256: Optional[str] = Header(None),
    x_shopify_shop_domain: Optional[str] = Header(None),
    x_shopify_topic: Optional[str] = Header(None)
):
    """
    Dependency to verify Shopify webhook authenticity
    """
    # Read raw body for HMAC verification
    body = await request.body()

    # Verify HMAC
    if not verify_webhook(body, x_shopify_hmac_sha256):
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature"
        )

    if not x_shopify_shop_domain:
        raise HTTPException(
            status_code=400,
            detail="Missing shop domain in webhook"
        )

    return {
        "body": body,
        "shop_domain": x_shopify_shop_domain,
        "topic": x_shopify_topic
    }


@router.post("/products/create")
async def product_create_webhook(
    request: Request,
    db: Session = Depends(get_db),
    webhook_data: dict = Depends(verify_shopify_webhook)
):
    """
    Handle Shopify product/create webhook

    Triggered when a new product is created in Shopify
    """
    try:
        shop_domain = webhook_data["shop_domain"]
        product_data = json.loads(webhook_data["body"])

        # Find merchant by shop domain
        merchant = db.query(Merchant).filter(
            Merchant.shop_domain == shop_domain,
            Merchant.is_active == 1
        ).first()

        if not merchant:
            raise HTTPException(
                status_code=404,
                detail=f"Merchant not found for shop: {shop_domain}"
            )

        # Sync the new product
        upsert_product(db, merchant, product_data)

        return {
            "status": "success",
            "message": "Product created and synced",
            "product_id": product_data.get('id'),
            "shop_domain": shop_domain
        }

    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON in webhook body: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process webhook: {str(e)}"
        )


@router.post("/products/update")
async def product_update_webhook(
    request: Request,
    db: Session = Depends(get_db),
    webhook_data: dict = Depends(verify_shopify_webhook)
):
    """
    Handle Shopify product/update webhook

    Triggered when a product is updated in Shopify
    """
    try:
        shop_domain = webhook_data["shop_domain"]
        product_data = json.loads(webhook_data["body"])

        # Find merchant by shop domain
        merchant = db.query(Merchant).filter(
            Merchant.shop_domain == shop_domain,
            Merchant.is_active == 1
        ).first()

        if not merchant:
            raise HTTPException(
                status_code=404,
                detail=f"Merchant not found for shop: {shop_domain}"
            )

        # Sync the updated product
        upsert_product(db, merchant, product_data)

        return {
            "status": "success",
            "message": "Product updated and synced",
            "product_id": product_data.get('id'),
            "shop_domain": shop_domain
        }

    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON in webhook body: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process webhook: {str(e)}"
        )


@router.post("/products/delete")
async def product_delete_webhook(
    request: Request,
    db: Session = Depends(get_db),
    webhook_data: dict = Depends(verify_shopify_webhook)
):
    """
    Handle Shopify product/delete webhook

    Triggered when a product is deleted in Shopify
    """
    try:
        shop_domain = webhook_data["shop_domain"]
        product_data = json.loads(webhook_data["body"])
        shopify_product_id = product_data.get('id')

        # Find merchant by shop domain
        merchant = db.query(Merchant).filter(
            Merchant.shop_domain == shop_domain,
            Merchant.is_active == 1
        ).first()

        if not merchant:
            raise HTTPException(
                status_code=404,
                detail=f"Merchant not found for shop: {shop_domain}"
            )

        # Find and soft delete the product
        product = db.query(Product).filter(
            Product.shopify_product_id == shopify_product_id,
            Product.merchant_id == merchant.id
        ).first()

        if product:
            # Soft delete: Mark as deleted instead of removing from database
            product.is_deleted = 1
            product.status = 'deleted'
            product.deleted_at = datetime.now(timezone.utc)
            db.commit()
            message = "Product soft deleted (marked as deleted in database)"
        else:
            message = "Product not found in database (already deleted or never synced)"

        return {
            "status": "success",
            "message": message,
            "product_id": shopify_product_id,
            "shop_domain": shop_domain
        }

    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON in webhook body: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process webhook: {str(e)}"
        )


@router.get("/")
async def webhook_info():
    """
    Information about available webhooks
    """
    return {
        "webhooks": [
            {
                "topic": "products/create",
                "endpoint": "/api/webhooks/products/create",
                "description": "Triggered when a product is created"
            },
            {
                "topic": "products/update",
                "endpoint": "/api/webhooks/products/update",
                "description": "Triggered when a product is updated"
            },
            {
                "topic": "products/delete",
                "endpoint": "/api/webhooks/products/delete",
                "description": "Triggered when a product is deleted"
            }
        ],
        "setup": {
            "automatic": "Webhooks are automatically registered during OAuth flow",
            "manual_registration": "Use POST /api/webhooks/register?merchant_id=<id> to manually register",
            "verification": "All webhooks are automatically verified using HMAC signatures"
        }
    }


@router.post("/register")
async def register_webhooks_endpoint(
    merchant_id: str = Query(..., description="Merchant ID to register webhooks for"),
    db: Session = Depends(get_db)
):
    """
    Manually register/update webhooks for a merchant

    Webhooks are automatically registered during OAuth.
    This endpoint allows manual re-registration if needed (e.g., after webhook deletion or URL changes).

    Query Parameters:
        - merchant_id: Unique merchant identifier

    Returns:
        List of webhook registration results
    """
    merchant = db.query(Merchant).filter(
        Merchant.merchant_id == merchant_id,
        Merchant.is_active == 1
    ).first()

    if not merchant:
        raise HTTPException(
            status_code=404,
            detail="Merchant not found or not active"
        )

    if not merchant.access_token:
        raise HTTPException(
            status_code=400,
            detail="Merchant has no access token. Complete OAuth first."
        )

    try:
        results = await register_webhooks(merchant.shop_domain, merchant.access_token, db, merchant.id)

        return {
            "status": "success",
            "merchant_id": merchant_id,
            "shop_domain": merchant.shop_domain,
            "webhooks": results
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to register webhooks: {str(e)}"
        )


@router.get("/list")
async def list_webhooks_endpoint(
    merchant_id: str = Query(..., description="Merchant ID to list webhooks for"),
    db: Session = Depends(get_db)
):
    """
    List all registered webhooks for a merchant from Shopify

    Query Parameters:
        - merchant_id: Unique merchant identifier

    Returns:
        List of all webhooks currently registered in Shopify for this merchant
    """
    merchant = db.query(Merchant).filter(
        Merchant.merchant_id == merchant_id,
        Merchant.is_active == 1
    ).first()

    if not merchant:
        raise HTTPException(
            status_code=404,
            detail="Merchant not found or not active"
        )

    if not merchant.access_token:
        raise HTTPException(
            status_code=400,
            detail="Merchant has no access token. Complete OAuth first."
        )

    try:
        webhooks = await list_webhooks(merchant.shop_domain, merchant.access_token)

        return {
            "status": "success",
            "merchant_id": merchant_id,
            "shop_domain": merchant.shop_domain,
            "webhook_count": len(webhooks),
            "webhooks": webhooks
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list webhooks: {str(e)}"
        )


@router.delete("/delete/{webhook_id}")
async def delete_webhook_endpoint(
    webhook_id: int,
    merchant_id: str = Query(..., description="Merchant ID that owns the webhook"),
    db: Session = Depends(get_db)
):
    """
    Delete a specific webhook subscription

    Path Parameters:
        - webhook_id: Shopify webhook ID to delete

    Query Parameters:
        - merchant_id: Unique merchant identifier

    Returns:
        Deletion confirmation
    """
    merchant = db.query(Merchant).filter(
        Merchant.merchant_id == merchant_id,
        Merchant.is_active == 1
    ).first()

    if not merchant:
        raise HTTPException(
            status_code=404,
            detail="Merchant not found or not active"
        )

    if not merchant.access_token:
        raise HTTPException(
            status_code=400,
            detail="Merchant has no access token. Complete OAuth first."
        )

    try:
        await delete_webhook(merchant.shop_domain, merchant.access_token, webhook_id, db)

        return {
            "status": "success",
            "message": "Webhook deleted successfully (marked as inactive in database)",
            "webhook_id": webhook_id,
            "merchant_id": merchant_id
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete webhook: {str(e)}"
        )


@router.post("/sync")
async def sync_webhooks_endpoint(
    merchant_id: str = Query(..., description="Merchant ID to sync webhooks for"),
    db: Session = Depends(get_db)
):
    """
    Sync webhooks between database and Shopify

    Detects and fixes drift:
    - Marks webhooks as inactive if deleted from Shopify
    - Discovers webhooks created outside this app

    Query Parameters:
        - merchant_id: Unique merchant identifier

    Returns:
        Sync results with counts
    """
    merchant = db.query(Merchant).filter(
        Merchant.merchant_id == merchant_id,
        Merchant.is_active == 1
    ).first()

    if not merchant:
        raise HTTPException(
            status_code=404,
            detail="Merchant not found or not active"
        )

    if not merchant.access_token:
        raise HTTPException(
            status_code=400,
            detail="Merchant has no access token. Complete OAuth first."
        )

    try:
        sync_results = await sync_webhooks(merchant.shop_domain, merchant.access_token, db, merchant.id)

        return {
            **sync_results,
            "merchant_id": merchant_id,
            "shop_domain": merchant.shop_domain
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to sync webhooks: {str(e)}"
        )

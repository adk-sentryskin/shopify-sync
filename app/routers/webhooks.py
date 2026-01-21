from fastapi import APIRouter, Request, HTTPException, Depends, Header, Query
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timezone
import json
from app.database import get_db
from app.models import ShopifyStore, Product
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
        merchant = db.query(ShopifyStore).filter(
            ShopifyStore.shop_domain == shop_domain,
            ShopifyStore.is_active == 1
        ).first()

        if not merchant:
            raise HTTPException(
                status_code=404,
                detail=f"ShopifyStore not found for shop: {shop_domain}"
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
        merchant = db.query(ShopifyStore).filter(
            ShopifyStore.shop_domain == shop_domain,
            ShopifyStore.is_active == 1
        ).first()

        if not merchant:
            raise HTTPException(
                status_code=404,
                detail=f"ShopifyStore not found for shop: {shop_domain}"
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
        merchant = db.query(ShopifyStore).filter(
            ShopifyStore.shop_domain == shop_domain,
            ShopifyStore.is_active == 1
        ).first()

        if not merchant:
            raise HTTPException(
                status_code=404,
                detail=f"ShopifyStore not found for shop: {shop_domain}"
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


@router.post("/customers/data_request")
async def customers_data_request_webhook(
    request: Request,
    db: Session = Depends(get_db),
    webhook_data: dict = Depends(verify_shopify_webhook)
):
    """
    Handle Shopify customers/data_request webhook (GDPR compliance)

    Triggered when a customer requests their data.
    This endpoint acknowledges the request. The actual data export
    should be handled according to your data retention policies.
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        shop_domain = webhook_data["shop_domain"]
        request_data = json.loads(webhook_data["body"])

        # Log the data request for compliance tracking
        logger.info(f"[GDPR] Customer data request received from shop: {shop_domain}")
        logger.info(f"[GDPR] Request details: shop_id={request_data.get('shop_id')}, "
                   f"shop_domain={request_data.get('shop_domain')}, "
                   f"customer_id={request_data.get('customer', {}).get('id')}, "
                   f"email={request_data.get('customer', {}).get('email')}")

        # Find merchant by shop domain
        merchant = db.query(ShopifyStore).filter(
            ShopifyStore.shop_domain == shop_domain
        ).first()

        if merchant:
            logger.info(f"[GDPR] Data request for merchant_id: {merchant.merchant_id}")

        # Acknowledge receipt - Shopify expects a 200 response
        # Actual data gathering would happen async based on your policies
        return {
            "status": "success",
            "message": "Customer data request acknowledged",
            "shop_domain": shop_domain
        }

    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON in webhook body: {str(e)}"
        )


@router.post("/customers/redact")
async def customers_redact_webhook(
    request: Request,
    db: Session = Depends(get_db),
    webhook_data: dict = Depends(verify_shopify_webhook)
):
    """
    Handle Shopify customers/redact webhook (GDPR compliance)

    Triggered when a store owner requests deletion of customer data,
    or when a customer requests deletion of their data.
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        shop_domain = webhook_data["shop_domain"]
        request_data = json.loads(webhook_data["body"])

        customer_id = request_data.get('customer', {}).get('id')
        customer_email = request_data.get('customer', {}).get('email')

        logger.info(f"[GDPR] Customer redact request received from shop: {shop_domain}")
        logger.info(f"[GDPR] Customer to redact: id={customer_id}, email={customer_email}")

        # Find merchant by shop domain
        merchant = db.query(ShopifyStore).filter(
            ShopifyStore.shop_domain == shop_domain
        ).first()

        if merchant:
            logger.info(f"[GDPR] Processing redact for merchant_id: {merchant.merchant_id}")
            # This app primarily stores product data, not customer data
            # If you store customer data, delete it here

        # Acknowledge receipt - Shopify expects a 200 response
        return {
            "status": "success",
            "message": "Customer redact request acknowledged",
            "shop_domain": shop_domain,
            "customer_id": customer_id
        }

    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON in webhook body: {str(e)}"
        )


@router.post("/shop/redact")
async def shop_redact_webhook(
    request: Request,
    db: Session = Depends(get_db),
    webhook_data: dict = Depends(verify_shopify_webhook)
):
    """
    Handle Shopify shop/redact webhook (GDPR compliance)

    Triggered 48 hours after a store owner uninstalls the app.
    This is the signal to delete all data associated with this shop.
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        shop_domain = webhook_data["shop_domain"]
        request_data = json.loads(webhook_data["body"])

        shop_id = request_data.get('shop_id')

        logger.info(f"[GDPR] Shop redact request received for shop: {shop_domain}, shop_id: {shop_id}")

        # Find merchant by shop domain
        merchant = db.query(ShopifyStore).filter(
            ShopifyStore.shop_domain == shop_domain
        ).first()

        if merchant:
            logger.info(f"[GDPR] Marking merchant as inactive and clearing data: {merchant.merchant_id}")

            # Soft delete products associated with this merchant
            products_deleted = db.query(Product).filter(
                Product.merchant_id == merchant.id
            ).update({
                "is_deleted": 1,
                "status": "redacted",
                "deleted_at": datetime.now(timezone.utc)
            })

            # Mark webhooks as inactive
            from app.models import Webhook
            db.query(Webhook).filter(
                Webhook.store_id == merchant.id
            ).update({"is_active": 0})

            # Mark merchant as inactive and clear sensitive data
            merchant.is_active = 0
            merchant.access_token = None  # Clear the access token

            db.commit()

            logger.info(f"[GDPR] Shop redact complete: {products_deleted} products marked as redacted")

        # Acknowledge receipt - Shopify expects a 200 response
        return {
            "status": "success",
            "message": "Shop redact request processed",
            "shop_domain": shop_domain
        }

    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON in webhook body: {str(e)}"
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
            },
            {
                "topic": "customers/data_request",
                "endpoint": "/api/webhooks/customers/data_request",
                "description": "GDPR: Triggered when a customer requests their data"
            },
            {
                "topic": "customers/redact",
                "endpoint": "/api/webhooks/customers/redact",
                "description": "GDPR: Triggered when customer data should be deleted"
            },
            {
                "topic": "shop/redact",
                "endpoint": "/api/webhooks/shop/redact",
                "description": "GDPR: Triggered 48h after app uninstall to delete shop data"
            }
        ],
        "setup": {
            "automatic": "Product webhooks are automatically registered during OAuth flow",
            "compliance": "GDPR webhooks must be configured in Shopify Partner Dashboard under App Setup",
            "manual_registration": "Use POST /api/webhooks/register?merchant_id=<id> to manually register product webhooks",
            "verification": "All webhooks are automatically verified using HMAC signatures"
        }
    }


@router.post("/register")
async def register_webhooks_endpoint(
    merchant_id: str = Query(..., description="ShopifyStore ID to register webhooks for"),
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
    merchant = db.query(ShopifyStore).filter(
        ShopifyStore.merchant_id == merchant_id,
        ShopifyStore.is_active == 1
    ).first()

    if not merchant:
        raise HTTPException(
            status_code=404,
            detail="ShopifyStore not found or not active"
        )

    if not merchant.access_token:
        raise HTTPException(
            status_code=400,
            detail="ShopifyStore has no access token. Complete OAuth first."
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
    merchant_id: str = Query(..., description="ShopifyStore ID to list webhooks for"),
    db: Session = Depends(get_db)
):
    """
    List all registered webhooks for a merchant from Shopify

    Query Parameters:
        - merchant_id: Unique merchant identifier

    Returns:
        List of all webhooks currently registered in Shopify for this merchant
    """
    merchant = db.query(ShopifyStore).filter(
        ShopifyStore.merchant_id == merchant_id,
        ShopifyStore.is_active == 1
    ).first()

    if not merchant:
        raise HTTPException(
            status_code=404,
            detail="ShopifyStore not found or not active"
        )

    if not merchant.access_token:
        raise HTTPException(
            status_code=400,
            detail="ShopifyStore has no access token. Complete OAuth first."
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
    merchant_id: str = Query(..., description="ShopifyStore ID that owns the webhook"),
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
    merchant = db.query(ShopifyStore).filter(
        ShopifyStore.merchant_id == merchant_id,
        ShopifyStore.is_active == 1
    ).first()

    if not merchant:
        raise HTTPException(
            status_code=404,
            detail="ShopifyStore not found or not active"
        )

    if not merchant.access_token:
        raise HTTPException(
            status_code=400,
            detail="ShopifyStore has no access token. Complete OAuth first."
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
    merchant_id: str = Query(..., description="ShopifyStore ID to sync webhooks for"),
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
    merchant = db.query(ShopifyStore).filter(
        ShopifyStore.merchant_id == merchant_id,
        ShopifyStore.is_active == 1
    ).first()

    if not merchant:
        raise HTTPException(
            status_code=404,
            detail="ShopifyStore not found or not active"
        )

    if not merchant.access_token:
        raise HTTPException(
            status_code=400,
            detail="ShopifyStore has no access token. Complete OAuth first."
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

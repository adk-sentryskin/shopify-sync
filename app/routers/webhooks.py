from fastapi import APIRouter, Request, HTTPException, Depends, Header
from sqlalchemy.orm import Session
from typing import Optional
import json
from app.database import get_db
from app.models import Merchant, Product
from app.services.product_sync import upsert_product
from app.utils.webhook_verification import verify_webhook, extract_shop_domain, extract_webhook_topic

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

        # Find and delete the product
        product = db.query(Product).filter(
            Product.shopify_product_id == shopify_product_id,
            Product.merchant_id == merchant.id
        ).first()

        if product:
            db.delete(product)
            db.commit()
            message = "Product deleted from database"
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
        "setup_instructions": {
            "1": "Go to Shopify Admin > Settings > Notifications > Webhooks",
            "2": "Add webhook subscriptions for the topics above",
            "3": "Use the full endpoint URLs (e.g., https://your-domain.com/api/webhooks/products/create)",
            "4": "Webhooks are automatically verified using HMAC signatures"
        }
    }

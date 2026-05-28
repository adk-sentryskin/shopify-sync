"""
Custom App connection endpoint.

Allows merchants to connect their Shopify store by providing a Custom App
Admin API access token (created in Shopify Admin > Settings > Apps > Develop apps).
No OAuth dance required — the merchant pastes their token and we pull everything.
"""

import re
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from app.config import settings
from app.database import get_db
from app.models import ShopifyStore
from app.services.shopify_oauth import ShopifyOAuth
from app.services.product_sync import fetch_all_products_from_shopify
from app.services.webhook_manager import register_webhooks
from app.utils.helpers import sanitize_shop_domain
import logging
import re

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/custom-app", tags=["Custom App"])

_MYSHOPIFY_RE = re.compile(r"^[a-zA-Z0-9\-]+\.myshopify\.com$")


class CustomAppConnect(BaseModel):
    """Request schema for connecting a Shopify Custom App."""
    merchant_id: str = Field(..., description="Unique merchant identifier (e.g. 'pacific-soul')")
    shop_domain: str = Field(..., description="Shopify store domain (e.g. 'mystore.myshopify.com')")
    admin_access_token: str = Field(..., description="Admin API access token from Shopify Custom App")

    @field_validator('merchant_id', mode='before')
    @classmethod
    def normalize_merchant_id(cls, v: str) -> str:
        return re.sub(r'\s+', '-', v.strip().lower())


class CustomAppResponse(BaseModel):
    message: str
    merchant_id: str
    shop_domain: str
    shop_name: Optional[str] = None
    primary_domain: Optional[str] = None
    status: str
    product_sync: dict


@router.post("/connect", response_model=CustomAppResponse)
async def connect_custom_app(
    data: CustomAppConnect,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Connect a Shopify store using a Custom App Admin API access token.

    The merchant creates a Custom App in Shopify Admin:
      Settings > Apps > Develop apps > Create an app > Configure Admin API scopes

    Required scopes (read-only, no review): read_products, read_inventory,
    read_product_listings, read_price_rules, read_discounts, read_gift_cards,
    read_legal_policies, read_shipping, read_content.
    Optional: read_orders (for agent order-attribution; protected customer data).

    Whatever the merchant actually grants is read back from Shopify and stored
    on the merchant record — we don't assume a fixed scope set.

    This endpoint:
      1. Validates the token against Shopify's API
      2. Stores the connection in shopify_sync.shopify_stores
      3. Kicks off background product sync (fetches all products with real IDs)
    """
    shop_domain = sanitize_shop_domain(data.shop_domain)

    if not _MYSHOPIFY_RE.match(shop_domain):
        raise HTTPException(
            status_code=400,
            detail="Invalid shop domain — must be *.myshopify.com"
        )

    access_token = data.admin_access_token.strip()
    if not access_token or len(access_token) < 10:
        raise HTTPException(
            status_code=400,
            detail="Invalid access token"
        )

    # Validate token by calling Shopify's GraphQL API
    shopify_oauth = ShopifyOAuth()
    try:
        shop_info = await shopify_oauth.get_shop_info_graphql(shop_domain, access_token)
    except Exception as e:
        logger.error(f"[Custom App] Token validation failed for {shop_domain}: {e}")
        raise HTTPException(
            status_code=401,
            detail="Invalid access token — could not connect to Shopify. Please check your token and shop domain."
        )

    shop_name = shop_info.get("name", shop_domain)
    primary_domain = (shop_info.get("primaryDomain") or {}).get("host", shop_domain)

    logger.info(f"[Custom App] Token validated for {shop_domain} — shop: {shop_name}")

    # Read the scopes the merchant actually granted to this token, so the
    # scope gate (has_scope) reflects reality. Fall back to the configured
    # required set if the lookup fails (shouldn't block the connection).
    try:
        granted_scopes = await shopify_oauth.get_access_scopes(shop_domain, access_token)
    except Exception as e:
        logger.warning(f"[Custom App] Could not read access scopes for {shop_domain}: {e}")
        granted_scopes = settings.SHOPIFY_SCOPES

    # Upsert store record
    merchant = db.query(ShopifyStore).filter(
        (ShopifyStore.merchant_id == data.merchant_id) |
        (ShopifyStore.shop_domain == shop_domain)
    ).first()

    if merchant:
        merchant.merchant_id = data.merchant_id
        merchant.shop_domain = shop_domain
        merchant.access_token = access_token
        merchant.scope = granted_scopes
        merchant.is_active = 1
    else:
        merchant = ShopifyStore(
            merchant_id=data.merchant_id,
            shop_domain=shop_domain,
        )
        merchant.access_token = access_token
        merchant.scope = granted_scopes
        merchant.is_active = 1
        db.add(merchant)

    db.commit()
    db.refresh(merchant)

    # Register webhooks (non-blocking)
    try:
        await register_webhooks(shop_domain, access_token, db, merchant.id)
    except Exception as e:
        logger.warning(f"[Custom App] Webhook registration failed: {e}")

    # Kick off background product sync
    background_tasks.add_task(
        _background_product_sync,
        merchant.id,
        shop_domain,
        access_token,
    )

    return CustomAppResponse(
        message="Shopify store connected successfully. Product sync started in background.",
        merchant_id=data.merchant_id,
        shop_domain=shop_domain,
        shop_name=shop_name,
        primary_domain=primary_domain,
        status="connected",
        product_sync={"status": "started_in_background"},
    )


@router.get("/sync-status")
async def get_sync_status(
    merchant_id: str,
    db: Session = Depends(get_db),
):
    """Check product sync status for a merchant."""
    from app.models import Product

    merchant = db.query(ShopifyStore).filter(
        ShopifyStore.merchant_id == merchant_id,
    ).first()

    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    product_count = db.query(Product).filter(
        Product.merchant_id == merchant_id,
        Product.is_deleted == 0,
    ).count()

    has_embeddings = db.query(Product).filter(
        Product.merchant_id == merchant_id,
        Product.is_deleted == 0,
        Product.embedding.isnot(None),
    ).count()

    return {
        "merchant_id": merchant_id,
        "shop_domain": merchant.shop_domain,
        "is_active": merchant.is_active,
        "total_products": product_count,
        "products_with_embeddings": has_embeddings,
        "sync_complete": product_count > 0 and product_count == has_embeddings,
    }


async def _background_product_sync(
    store_id: int,
    shop_domain: str,
    access_token: str,
):
    """Background task: fetch all products from Shopify and sync to DB."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        merchant = db.query(ShopifyStore).filter(ShopifyStore.id == store_id).first()
        if not merchant:
            logger.error(f"[Custom App Sync] Store {store_id} not found")
            return

        logger.info(f"[Custom App Sync] Starting product sync for {merchant.merchant_id}")

        result = await fetch_all_products_from_shopify(
            db=db,
            merchant=merchant,
            shop_domain=shop_domain,
            access_token=access_token,
        )

        logger.info(f"[Custom App Sync] Completed for {merchant.merchant_id}: "
                     f"{result.get('synced_count', 0)} synced, "
                     f"{result.get('failed_count', 0)} failed, "
                     f"{result.get('duration_seconds', 0)}s")
    except Exception as e:
        logger.error(f"[Custom App Sync] Error: {e}", exc_info=True)
    finally:
        db.close()

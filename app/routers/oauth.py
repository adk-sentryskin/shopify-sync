from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from typing import Dict
from datetime import datetime, timezone
from app.database import get_db
from app.models import Merchant
from app.schemas import OAuthGenerateURL, MerchantResponse, OAuthComplete
from app.services.shopify_oauth import ShopifyOAuth
from app.services.webhook_manager import register_webhooks
from app.services.product_sync import fetch_all_products_from_shopify
from app.middleware.auth import get_merchant_from_header
from app.utils.helpers import sanitize_shop_domain
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/oauth", tags=["OAuth"])
shopify_oauth = ShopifyOAuth()


async def initial_product_sync_background(
    merchant_id: int,
    shop_domain: str,
    access_token: str
):
    """Background task to perform initial bulk product sync after OAuth"""
    try:
        # Get a new database session for this background task
        from app.database import SessionLocal
        db = SessionLocal()

        try:
            merchant = db.query(Merchant).filter(Merchant.id == merchant_id).first()

            if not merchant:
                logger.warning(f"[Initial Sync] Merchant {merchant_id} not found")
                return

            logger.info(f"[Initial Sync] Starting bulk product sync for merchant {merchant.merchant_id}")

            sync_result = await fetch_all_products_from_shopify(
                db=db,
                merchant=merchant,
                shop_domain=shop_domain,
                access_token=access_token
            )

            logger.info(f"[Initial Sync] Completed for {merchant.merchant_id}: {sync_result}")

        finally:
            db.close()

    except Exception as e:
        logger.error(f"[Initial Sync] Error during background sync: {str(e)}")


@router.post("/generate-url", response_model=Dict[str, str])
async def generate_oauth_url(oauth_data: OAuthGenerateURL):
    """
    Generate Shopify OAuth authorization URL

    Frontend provides shop domain, merchant ID, and their callback URL.
    Backend generates the complete authorization URL with proper parameters.
    """
    from app.config import settings
    from urllib.parse import urlencode

    shop_domain = sanitize_shop_domain(oauth_data.shop_domain)

    # Build OAuth authorization URL
    params = {
        "client_id": settings.SHOPIFY_API_KEY,
        "scope": settings.SHOPIFY_SCOPES,
        "redirect_uri": oauth_data.redirect_uri,
        "state": oauth_data.merchant_id
    }

    auth_url = f"https://{shop_domain}/admin/oauth/authorize?{urlencode(params)}"

    logger.info(f"[OAuth] Generated authorization URL for merchant: {oauth_data.merchant_id}, shop: {shop_domain}")

    return {
        "authorization_url": auth_url,
        "merchant_id": oauth_data.merchant_id,
        "shop_domain": shop_domain
    }


@router.post("/complete")
async def complete_oauth(
    oauth_data: OAuthComplete,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Complete OAuth flow from frontend

    Frontend receives callback from Shopify with query parameters and sends them here.
    This endpoint validates HMAC and exchanges the code for an access token.
    """
    shop_domain = sanitize_shop_domain(oauth_data.shop)

    # Build params dict for HMAC verification
    params = {
        "code": oauth_data.code,
        "shop": oauth_data.shop,
        "state": oauth_data.merchant_id,
        "hmac": oauth_data.hmac
    }

    if oauth_data.timestamp:
        params["timestamp"] = oauth_data.timestamp

        # Validate timestamp
        try:
            callback_timestamp = int(oauth_data.timestamp)
            current_timestamp = int(datetime.now(timezone.utc).timestamp())
            time_difference = abs(current_timestamp - callback_timestamp)

            if time_difference > 300:
                raise HTTPException(
                    status_code=400,
                    detail="OAuth callback timestamp expired (must be within 5 minutes)"
                )
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid timestamp format"
            )

    if oauth_data.host:
        params["host"] = oauth_data.host

    logger.info(f"[OAuth Complete] Received request for shop: {shop_domain}, merchant: {oauth_data.merchant_id}")

    # Verify HMAC
    if not shopify_oauth.verify_hmac(params):
        logger.error(f"[OAuth Complete] HMAC verification failed for shop: {shop_domain}")
        raise HTTPException(
            status_code=400,
            detail="Invalid HMAC signature"
        )

    logger.info(f"[OAuth Complete] HMAC verification successful for shop: {shop_domain}")

    # Find or create merchant
    merchant = db.query(Merchant).filter(
        Merchant.merchant_id == oauth_data.merchant_id
    ).first()

    if not merchant:
        merchant = Merchant(
            merchant_id=oauth_data.merchant_id,
            shop_domain=shop_domain
        )
        db.add(merchant)
        db.flush()
    else:
        merchant.shop_domain = shop_domain

    try:
        # Exchange code for access token
        token_data = await shopify_oauth.exchange_code_for_token(shop_domain, oauth_data.code)

        merchant.access_token = token_data.get("access_token")
        merchant.scope = token_data.get("scope")
        merchant.is_active = 1

        db.commit()

        # Get shop info
        shop_info = await shopify_oauth.get_shop_info(shop_domain, merchant.access_token)

        # Register webhooks
        webhook_results = await register_webhooks(shop_domain, merchant.access_token, db, merchant.id)

        # Start initial product sync in background
        background_tasks.add_task(
            initial_product_sync_background,
            merchant_id=merchant.id,
            shop_domain=shop_domain,
            access_token=merchant.access_token
        )

        logger.info(f"[OAuth Complete] Successfully completed OAuth for merchant: {oauth_data.merchant_id}")

        return {
            "message": "OAuth successful",
            "merchant_id": merchant.merchant_id,
            "shop_domain": shop_domain,
            "shop_name": shop_info.get("shop", {}).get("name"),
            "status": "authenticated",
            "webhooks_registered": webhook_results,
            "initial_product_sync": {
                "status": "started",
                "message": "Initial product sync is running in the background. This may take several minutes for large stores."
            }
        }

    except Exception as e:
        db.rollback()
        logger.error(f"[OAuth Complete] Error: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"OAuth failed: {str(e)}"
        )


@router.get("/status", response_model=MerchantResponse)
async def check_oauth_status(
    merchant: Merchant = Depends(get_merchant_from_header)
):
    """Check OAuth status for a merchant"""
    return merchant

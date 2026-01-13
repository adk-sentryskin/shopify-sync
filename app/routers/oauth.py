from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from typing import Dict
from datetime import datetime, timezone
from app.database import get_db
from app.models import ShopifyStore
from app.schemas import OAuthGenerateURL, ShopifyStoreResponse, OAuthComplete
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
            merchant = db.query(ShopifyStore).filter(ShopifyStore.id == merchant_id).first()

            if not merchant:
                logger.warning(f"[Initial Sync] ShopifyStore {merchant_id} not found")
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


@router.options("/generate-url")
async def generate_url_preflight():
    """Handle CORS preflight for generate URL endpoint"""
    return {}


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


@router.options("/complete")
async def complete_oauth_preflight():
    """Handle CORS preflight for complete OAuth endpoint"""
    return {}


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

    # Validate timestamp (required for replay attack prevention)
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

    # Build params dict for HMAC verification
    params = {
        "code": oauth_data.code,
        "shop": oauth_data.shop,
        "state": oauth_data.merchant_id,
        "hmac": oauth_data.hmac,
        "timestamp": oauth_data.timestamp
    }

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

    # Find merchant by merchant_id OR shop_domain (to handle both unique constraints)
    merchant = db.query(ShopifyStore).filter(
        (ShopifyStore.merchant_id == oauth_data.merchant_id) |
        (ShopifyStore.shop_domain == shop_domain)
    ).first()

    # Check for duplicate OAuth completion (prevent replay attacks)
    if merchant and merchant.is_active == 1 and merchant.access_token:
        if merchant.updated_at:
            time_since_last_oauth = (datetime.now(timezone.utc) - merchant.updated_at.replace(tzinfo=timezone.utc)).total_seconds()
            if time_since_last_oauth < 60:  # Less than 60 seconds ago
                logger.warning(f"[OAuth Complete] Duplicate OAuth attempt detected for merchant {oauth_data.merchant_id} (last completed {time_since_last_oauth:.1f}s ago)")
                raise HTTPException(
                    status_code=409,
                    detail=f"OAuth was recently completed for this merchant. Please wait before retrying."
                )

    if not merchant:
        # Create new merchant record
        merchant = ShopifyStore(
            merchant_id=oauth_data.merchant_id,
            shop_domain=shop_domain
        )
        db.add(merchant)
        db.flush()
    else:
        # Update existing record (handles both merchant_id and shop_domain changes)
        merchant.merchant_id = oauth_data.merchant_id
        merchant.shop_domain = shop_domain

    try:
        # Exchange code for access token
        try:
            token_data = await shopify_oauth.exchange_code_for_token(shop_domain, oauth_data.code)
        except Exception as token_error:
            db.rollback()
            error_msg = str(token_error)
            logger.error(f"[OAuth Complete] Token exchange failed: {error_msg}")

            # Detect duplicate/invalid code errors from Shopify
            if "400" in error_msg or "invalid" in error_msg.lower() or "already" in error_msg.lower():
                raise HTTPException(
                    status_code=400,
                    detail="Invalid or already used authorization code. Please restart the OAuth flow."
                )
            raise HTTPException(
                status_code=502,
                detail=f"Failed to exchange authorization code with Shopify: {error_msg}"
            )

        # Update merchant with new credentials
        merchant.access_token = token_data.get("access_token")
        merchant.scope = token_data.get("scope")
        merchant.is_active = 1

        # Commit the merchant update before proceeding with other operations
        db.commit()
        db.refresh(merchant)

        logger.info(f"[OAuth Complete] Access token obtained for merchant: {oauth_data.merchant_id}")

        # Get shop info
        try:
            shop_info = await shopify_oauth.get_shop_info(shop_domain, merchant.access_token)
        except Exception as shop_error:
            logger.error(f"[OAuth Complete] Failed to fetch shop info: {str(shop_error)}")
            shop_info = {"shop": {"name": shop_domain}}  # Fallback

        # Register webhooks (non-blocking - log errors but don't fail OAuth)
        try:
            webhook_results = await register_webhooks(shop_domain, merchant.access_token, db, merchant.id)
        except Exception as webhook_error:
            logger.error(f"[OAuth Complete] Webhook registration failed: {str(webhook_error)}")
            webhook_results = {"error": "Webhook registration failed, will retry later"}

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

    except HTTPException:
        # Re-raise HTTP exceptions (already properly formatted)
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"[OAuth Complete] Unexpected error: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"OAuth failed: {str(e)}"
        )


@router.options("/status")
async def oauth_status_preflight():
    """Handle CORS preflight for OAuth status endpoint"""
    return {}


@router.get("/status", response_model=ShopifyStoreResponse)
async def check_oauth_status(
    merchant: ShopifyStore = Depends(get_merchant_from_header)
):
    """Check OAuth status for a merchant"""
    return merchant

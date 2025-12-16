from fastapi import APIRouter, Depends, HTTPException, Query, Request, BackgroundTasks
from sqlalchemy.orm import Session
from typing import Dict
from datetime import datetime, timezone
from app.database import get_db
from app.models import Merchant
from app.schemas import OAuthInitiate, MerchantResponse
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


@router.post("/initiate", response_model=Dict[str, str])
async def initiate_oauth(
    oauth_data: OAuthInitiate,
    db: Session = Depends(get_db)
):
    """Initiate OAuth flow for a merchant"""
    shop_domain = sanitize_shop_domain(oauth_data.shop_domain)

    # Check if merchant exists, create if not
    merchant = db.query(Merchant).filter(
        Merchant.merchant_id == oauth_data.merchant_id
    ).first()

    if not merchant:
        merchant = Merchant(
            merchant_id=oauth_data.merchant_id,
            shop_domain=shop_domain
        )
        db.add(merchant)
    else:
        merchant.shop_domain = shop_domain

    db.commit()

    auth_url = shopify_oauth.get_authorization_url(
        shop_domain=shop_domain,
        state=oauth_data.merchant_id
    )

    return {
        "authorization_url": auth_url,
        "merchant_id": oauth_data.merchant_id
    }


@router.get("/callback")
async def oauth_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    code: str = Query(..., description="Authorization code from Shopify"),
    shop: str = Query(..., description="Shop domain"),
    state: str = Query(None, description="State parameter (merchant_id)"),
    db: Session = Depends(get_db)
):
    """OAuth callback endpoint - Shopify redirects here after authorization"""
    params = dict(request.query_params)

    logger.info(f"[OAuth Callback] Received params: {list(params.keys())}")
    logger.debug(f"[OAuth Callback] Full params (excluding sensitive): shop={params.get('shop')}, state={params.get('state')}, has_hmac={('hmac' in params)}, has_code={('code' in params)}")

    if "timestamp" in params:
        try:
            callback_timestamp = int(params["timestamp"])
            current_timestamp = int(datetime.now(timezone.utc).timestamp())
            time_difference = abs(current_timestamp - callback_timestamp)

            if time_difference > 300:
                raise HTTPException(
                    status_code=400,
                    detail="OAuth callback timestamp expired"
                )
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid timestamp format"
            )

    if not shopify_oauth.verify_hmac(params):
        logger.error(f"[OAuth Callback] HMAC verification failed for shop: {params.get('shop')}")
        raise HTTPException(
            status_code=400,
            detail="Invalid HMAC signature"
        )

    logger.info(f"[OAuth Callback] HMAC verification successful for shop: {params.get('shop')}")

    if not state:
        raise HTTPException(
            status_code=400,
            detail="Missing state parameter"
        )

    merchant = db.query(Merchant).filter(
        Merchant.merchant_id == state,
        Merchant.shop_domain == shop
    ).first()

    if not merchant:
        raise HTTPException(
            status_code=404,
            detail="Merchant not found"
        )

    try:
        token_data = await shopify_oauth.exchange_code_for_token(shop, code)

        merchant.access_token = token_data.get("access_token")
        merchant.scope = token_data.get("scope")
        merchant.is_active = 1

        db.commit()

        shop_info = await shopify_oauth.get_shop_info(shop, merchant.access_token)
        webhook_results = await register_webhooks(shop, merchant.access_token, db, merchant.id)

        background_tasks.add_task(
            initial_product_sync_background,
            merchant_id=merchant.id,
            shop_domain=shop,
            access_token=merchant.access_token
        )

        return {
            "message": "OAuth successful",
            "merchant_id": merchant.merchant_id,
            "shop_domain": shop,
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

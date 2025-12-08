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

router = APIRouter(prefix="/api/oauth", tags=["OAuth"])
shopify_oauth = ShopifyOAuth()


async def initial_product_sync_background(
    merchant_id: int,
    shop_domain: str,
    access_token: str
):
    """
    Background task to perform initial bulk product sync after OAuth.

    This runs asynchronously to avoid blocking the OAuth callback response.
    Stores with thousands of products could take several minutes to sync.

    Args:
        merchant_id: Merchant database ID
        shop_domain: Shopify shop domain
        access_token: OAuth access token
    """
    try:
        # Get a new database session for this background task
        from app.database import SessionLocal
        db = SessionLocal()

        try:
            # Get merchant from database
            merchant = db.query(Merchant).filter(Merchant.id == merchant_id).first()

            if not merchant:
                print(f"[Initial Sync] Merchant {merchant_id} not found")
                return

            print(f"[Initial Sync] Starting bulk product sync for merchant {merchant.merchant_id} ({shop_domain})")

            # Fetch and sync all products
            sync_result = await fetch_all_products_from_shopify(
                db=db,
                merchant=merchant,
                shop_domain=shop_domain,
                access_token=access_token
            )

            print(f"[Initial Sync] Completed for {merchant.merchant_id}: {sync_result}")

        finally:
            db.close()

    except Exception as e:
        print(f"[Initial Sync] Error during background sync: {str(e)}")


@router.post("/initiate", response_model=Dict[str, str])
async def initiate_oauth(
    oauth_data: OAuthInitiate,
    db: Session = Depends(get_db)
):
    """
    Initiate OAuth flow for a merchant

    Request Body:
        - shop_domain: Shopify shop domain (e.g., mystore.myshopify.com)
        - merchant_id: Unique identifier for the merchant

    Returns:
        authorization_url: URL to redirect the merchant for OAuth authorization
    """
    # Sanitize shop_domain - remove protocol if present
    shop_domain = oauth_data.shop_domain.replace("https://", "").replace("http://", "").strip("/")

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
        # Update shop domain if changed
        merchant.shop_domain = shop_domain

    db.commit()

    # Generate authorization URL
    auth_url = shopify_oauth.get_authorization_url(
        shop_domain=shop_domain,
        state=oauth_data.merchant_id  # Use merchant_id as state for verification
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
    """
    OAuth callback endpoint - Shopify redirects here after authorization

    Query Parameters:
        - code: Authorization code
        - shop: Shop domain
        - state: Merchant ID (passed as state)
        - hmac: HMAC signature for verification
        - host: Host parameter from Shopify
        - timestamp: Timestamp parameter from Shopify
    """
    # Get ALL query parameters for HMAC verification
    # Shopify includes all params (code, shop, state, timestamp, host) in HMAC calculation
    params = dict(request.query_params)

    # Validate timestamp to prevent replay attacks (Shopify recommends < 5 minutes)
    if "timestamp" in params:
        try:
            callback_timestamp = int(params["timestamp"])
            current_timestamp = int(datetime.now(timezone.utc).timestamp())
            time_difference = abs(current_timestamp - callback_timestamp)

            # Reject if older than 5 minutes (300 seconds)
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
        raise HTTPException(
            status_code=400,
            detail="Invalid HMAC signature"
        )

    # Find merchant by state (merchant_id)
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
        # Exchange code for access token
        token_data = await shopify_oauth.exchange_code_for_token(shop, code)

        # Update merchant with access token
        merchant.access_token = token_data.get("access_token")
        merchant.scope = token_data.get("scope")
        merchant.is_active = 1

        db.commit()

        # Get shop info to verify
        shop_info = await shopify_oauth.get_shop_info(shop, merchant.access_token)

        # Automatically register webhooks after OAuth (with database tracking)
        webhook_results = await register_webhooks(shop, merchant.access_token, db, merchant.id)

        # Trigger initial bulk product sync in background
        # This prevents blocking the OAuth response for stores with many products
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
    merchant_id: str = Query(..., description="Merchant ID to check"),
    db: Session = Depends(get_db)
):
    """
    Check OAuth status for a merchant

    Query Parameters:
        - merchant_id: Merchant identifier
    """
    merchant = db.query(Merchant).filter(
        Merchant.merchant_id == merchant_id
    ).first()

    if not merchant:
        raise HTTPException(
            status_code=404,
            detail="Merchant not found"
        )

    return merchant

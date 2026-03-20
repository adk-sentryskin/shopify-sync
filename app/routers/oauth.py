from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from fastapi.responses import RedirectResponse
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
import re

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/oauth", tags=["OAuth"])
shopify_oauth = ShopifyOAuth()

# Matches *.myshopify.com (with optional path prefix stripped)
_MYSHOPIFY_RE = re.compile(r"^[a-zA-Z0-9\-]+\.myshopify\.com$")


def _is_myshopify_domain(domain: str) -> bool:
    return bool(_MYSHOPIFY_RE.match(domain))


@router.get("/install")
async def shopify_install(request: Request):
    """
    Shopify App Store install entry point.

    Shopify calls this URL (the configured App URL) when a merchant installs
    the app from the Shopify App Store. It receives `shop`, `hmac`, `timestamp`,
    and `host` as query parameters — the shop domain is provided automatically
    by Shopify, no merchant input required.

    Flow:
      1. Validate shop domain format (must be *.myshopify.com)
      2. Verify Shopify HMAC signature
      3. Redirect merchant to the store-specific OAuth authorization URL

    The `state` param is set to the sanitized shop domain so the callback can
    resolve the store even if there is no pre-existing merchant record.
    """
    from app.config import settings
    from urllib.parse import urlencode

    params = dict(request.query_params)

    shop = params.get("shop", "").strip()
    hmac_value = params.get("hmac", "")
    timestamp = params.get("timestamp", "")

    if not shop:
        raise HTTPException(status_code=400, detail="Missing 'shop' parameter")

    shop = sanitize_shop_domain(shop)

    if not _is_myshopify_domain(shop):
        raise HTTPException(status_code=400, detail="Invalid shop domain — must be *.myshopify.com")

    # Validate timestamp (prevent replay attacks)
    try:
        ts = int(timestamp)
        now = int(datetime.now(timezone.utc).timestamp())
        if abs(now - ts) > 300:
            raise HTTPException(status_code=400, detail="Install request timestamp expired")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid timestamp")

    # Verify Shopify HMAC over the query params — fail closed: no HMAC = reject
    if not hmac_value or not shopify_oauth.verify_hmac(params):
        logger.error(f"[Install] HMAC verification failed for shop: {shop}")
        raise HTTPException(status_code=400, detail="Invalid HMAC signature")

    # Build the per-store OAuth URL.
    # state = shop domain — used to resolve the store in the callback when
    # no pre-existing merchant_id is available.
    auth_params = {
        "client_id": settings.SHOPIFY_API_KEY,
        "scope": settings.SHOPIFY_SCOPES,
        "redirect_uri": settings.OAUTH_REDIRECT_URL,
        "state": shop,
    }
    auth_url = f"https://{shop}/admin/oauth/authorize?{urlencode(auth_params)}"

    logger.info(f"[Install] Redirecting shop '{shop}' to OAuth authorization")
    return RedirectResponse(url=auth_url, status_code=302)


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

    shop_domain = sanitize_shop_domain(oauth_data.shop_domain) if oauth_data.shop_domain else None

    # Always use the server-side configured redirect URI — never trust the frontend value.
    # Shopify rejects requests whose redirect_uri doesn't exactly match the allowlist.
    base_params = {
        "client_id": settings.SHOPIFY_API_KEY,
        "scope": settings.SHOPIFY_SCOPES,
        "redirect_uri": settings.OAUTH_REDIRECT_URL,
        "state": oauth_data.merchant_id,
    }

    if shop_domain:
        # Direct store install — Shopify's per-store endpoint does NOT accept response_type
        auth_url = f"https://{shop_domain}/admin/oauth/authorize?{urlencode(base_params)}"
    else:
        # No store domain — accounts.shopify.com uses standard OAuth 2.0, requires response_type=code
        params = {**base_params, "response_type": "code"}
        auth_url = f"https://accounts.shopify.com/oauth/authorize?{urlencode(params)}"

    logger.info(f"[OAuth] Generated authorization URL for merchant: {oauth_data.merchant_id}, shop: {shop_domain or 'unknown (merchant will select)'}")

    return {
        "authorization_url": auth_url,
        "merchant_id": oauth_data.merchant_id,
        "shop_domain": shop_domain or ""
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

    # Resolve merchant_id from the request.
    #
    # Three scenarios:
    #   1. Dashboard flow: state = merchant_id (set by generate-url)
    #   2. App Store / Connect button (SaaS user logged in):
    #      state = shop_domain (set by /install), merchant_id sent by frontend from session
    #   3. Pure App Store install (no SaaS account yet):
    #      state = shop_domain, no merchant_id → use shop domain as temporary ID
    #
    # Priority: if state is a shop domain, prefer explicit merchant_id from frontend.
    raw_state = oauth_data.state
    app_store_install = bool(raw_state and _is_myshopify_domain(raw_state))

    if app_store_install and oauth_data.merchant_id:
        # "Connect Shopify" from SaaS dashboard — frontend passes merchant_id from session
        merchant_id = oauth_data.merchant_id
    elif raw_state:
        # Dashboard flow (state=merchant_id) or pure App Store (state=shop_domain)
        merchant_id = raw_state
    elif oauth_data.merchant_id:
        merchant_id = oauth_data.merchant_id
    else:
        raise HTTPException(
            status_code=400,
            detail="Missing merchant identifier: provide 'state' (Shopify callback param) or 'merchant_id'"
        )

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

    # Build params dict for HMAC verification.
    # Must include exactly the params Shopify signed — use the raw 'state' value
    # (the original value Shopify received), NOT the resolved merchant_id.
    hmac_state = raw_state or merchant_id
    params = {
        "code": oauth_data.code,
        "shop": oauth_data.shop,
        "state": hmac_state,
        "hmac": oauth_data.hmac,
        "timestamp": oauth_data.timestamp
    }

    if oauth_data.host:
        params["host"] = oauth_data.host

    logger.info(f"[OAuth Complete] Received request for shop: {shop_domain}, merchant: {merchant_id}")

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
        (ShopifyStore.merchant_id == merchant_id) |
        (ShopifyStore.shop_domain == shop_domain)
    ).first()

    # Check for duplicate OAuth completion (prevent replay attacks)
    if merchant and merchant.is_active == 1 and merchant.access_token:
        if merchant.updated_at:
            time_since_last_oauth = (datetime.now(timezone.utc) - merchant.updated_at.replace(tzinfo=timezone.utc)).total_seconds()
            if time_since_last_oauth < 60:  # Less than 60 seconds ago
                logger.warning(f"[OAuth Complete] Duplicate OAuth attempt detected for merchant {merchant_id} (last completed {time_since_last_oauth:.1f}s ago)")
                raise HTTPException(
                    status_code=409,
                    detail=f"OAuth was recently completed for this merchant. Please wait before retrying."
                )

    if not merchant:
        # Create new merchant record — shop_domain comes from Shopify's callback
        merchant = ShopifyStore(
            merchant_id=merchant_id,
            shop_domain=shop_domain
        )
        db.add(merchant)
        db.flush()
    else:
        # Update existing record (handles both merchant_id and shop_domain changes)
        merchant.merchant_id = merchant_id
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

        logger.info(f"[OAuth Complete] Access token obtained for merchant: {merchant_id}")

        # Get shop info via Admin GraphQL — returns canonical domain, name, primary domain.
        # Preferred over REST; also resolves the real myshopify domain for App Store installs.
        try:
            gql_shop = await shopify_oauth.get_shop_info_graphql(shop_domain, merchant.access_token)
            shop_name = gql_shop.get("name", shop_domain)
            canonical_domain = gql_shop.get("myshopifyDomain", shop_domain)
            primary_domain = (gql_shop.get("primaryDomain") or {}).get("host", shop_domain)
            logger.info(f"[OAuth Complete] Shop info — name: {shop_name}, myshopify: {canonical_domain}, primary: {primary_domain}")

            # For pure App Store installs (no SaaS account), state=shop_domain was
            # used as a temporary merchant_id. Replace it with the canonical domain.
            # But if the frontend sent a real merchant_id, keep it — the merchant
            # already has a SaaS account and we don't want to overwrite their ID.
            if app_store_install and not oauth_data.merchant_id:
                # Use shop handle (e.g. "cool-store") instead of full domain
                shop_handle = canonical_domain.replace(".myshopify.com", "")
                merchant.merchant_id = shop_handle
                db.commit()
                db.refresh(merchant)
        except Exception as shop_error:
            logger.error(f"[OAuth Complete] GraphQL shop info failed: {str(shop_error)}")
            shop_name = shop_domain
            canonical_domain = shop_domain
            primary_domain = shop_domain

        # Write merchant_id as shop metafield for the Liquid theme extension
        try:
            await shopify_oauth.write_merchant_id_metafield(shop_domain, merchant.access_token, merchant.merchant_id)
        except Exception as meta_error:
            logger.warning(f"[OAuth Complete] Metafield write failed (non-blocking): {str(meta_error)}")

        # Register webhooks (non-blocking - log errors but don't fail OAuth)
        try:
            webhook_results = await register_webhooks(shop_domain, merchant.access_token, db, merchant.id)
        except Exception as webhook_error:
            logger.error(f"[OAuth Complete] Webhook registration failed: {str(webhook_error)}")
            webhook_results = {"error": "Webhook registration failed, will retry later"}

        # Run initial product sync inline (batch embeddings make this fast ~2-5s)
        sync_result = {}
        try:
            sync_result = await fetch_all_products_from_shopify(
                db=db,
                merchant=merchant,
                shop_domain=shop_domain,
                access_token=merchant.access_token
            )
            logger.info(f"[Initial Sync] Completed for {merchant.merchant_id}: {sync_result}")
        except Exception as sync_error:
            logger.error(f"[Initial Sync] Failed for {merchant.merchant_id}: {sync_error}")
            sync_result = {"status": "failed", "error": str(sync_error)}

        logger.info(f"[OAuth Complete] Successfully completed OAuth for merchant: {merchant_id}")

        return {
            "message": "OAuth successful",
            "merchant_id": merchant.merchant_id,
            "shop_domain": shop_domain,
            "shop_name": shop_name,
            "canonical_domain": canonical_domain,
            "primary_domain": primary_domain,
            "app_store_install": app_store_install,
            "status": "authenticated",
            "webhooks_registered": webhook_results,
            "initial_product_sync": sync_result
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

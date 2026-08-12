from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import text as sa_text
from typing import Dict, Optional
from datetime import datetime, timezone
from app.database import get_db
from app.models import ShopifyStore
from app.schemas import OAuthGenerateURL, ShopifyStoreResponse, OAuthComplete
from app.services.shopify_oauth import ShopifyOAuth
from app.services.webhook_manager import register_webhooks
from app.services.product_sync import fetch_all_products_from_shopify
from app.middleware.auth import get_merchant_from_header
from app.utils.helpers import sanitize_shop_domain
from app.config import settings
import json as _json
import logging
import re
import hmac as hmac_lib
import hashlib

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/oauth", tags=["OAuth"])
shopify_oauth = ShopifyOAuth()

# Matches *.myshopify.com (with optional path prefix stripped)
_MYSHOPIFY_RE = re.compile(r"^[a-zA-Z0-9\-]+\.myshopify\.com$")


def _persist_merchant_domains(merchant_id: str, primary_url: Optional[str], all_domains: list) -> bool:
    """
    Write shop_url + allowed_domains onto public.merchants.
    Returns True if a row was updated. Safe to call when the row does not
    exist yet (returns False) — AI persona create may run a few seconds later.
    """
    if not merchant_id:
        return False
    from app.database import SessionLocal
    domains_json = _json.dumps(all_domains) if all_domains else None
    db = SessionLocal()
    try:
        result = db.execute(
            sa_text(
                "UPDATE public.merchants SET shop_url = :shop_url, "
                "allowed_domains = CAST(:allowed_domains AS jsonb), updated_at = now() "
                "WHERE merchant_id = :mid"
            ),
            {
                "shop_url": primary_url,
                "allowed_domains": domains_json,
                "mid": merchant_id,
            },
        )
        db.commit()
        updated = (result.rowcount or 0) > 0
        if updated:
            logger.info(
                f"[OAuth Complete] Updated public.merchants shop_url={primary_url} "
                f"allowed_domains={all_domains} for {merchant_id}"
            )
        return updated
    except Exception as e:
        db.rollback()
        logger.warning(f"[OAuth Complete] Failed to update public.merchants domains: {e}")
        return False
    finally:
        db.close()


def _persist_merchant_domains_with_retry(
    merchant_id: str,
    primary_url: Optional[str],
    all_domains: list,
    attempts: int = 6,
    delay_seconds: float = 10.0,
):
    """
    Retry domain persistence — merchant row is often created a few seconds
    after OAuth when the user saves AI Persona.
    """
    import time
    for i in range(attempts):
        if _persist_merchant_domains(merchant_id, primary_url, all_domains):
            return
        if i < attempts - 1:
            time.sleep(delay_seconds)
    logger.warning(
        f"[OAuth Complete] public.merchants row not found for {merchant_id} "
        f"after {attempts} attempts — domains not persisted from OAuth"
    )


def _create_signed_state(merchant_id: str, secret: str) -> str:
    """
    Create a tamper-proof state token by appending an HMAC signature.
    Format: {merchant_id}:{hex_sig_16chars}
    Stateless — no storage needed, works across Cloud Run instances.
    """
    sig = hmac_lib.new(secret.encode(), merchant_id.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{merchant_id}:{sig}"


def _extract_signed_merchant_id(state: str, secret: str) -> Optional[str]:
    """
    Verify the signed state and return the embedded merchant_id.
    Returns None if the signature is invalid or state is malformed.
    """
    if not state or ":" not in state:
        return None
    merchant_id, _, sig = state.rpartition(":")
    expected = hmac_lib.new(secret.encode(), merchant_id.encode(), hashlib.sha256).hexdigest()[:16]
    if hmac_lib.compare_digest(sig, expected):
        return merchant_id
    return None


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


async def _oauth_order_backfill_background(store_id: int):
    """
    Backfill the last 60 days of agent-attributed orders for a merchant who
    just granted read_orders. Runs as a FastAPI background task so the OAuth
    callback returns immediately.
    """
    from app.database import SessionLocal
    from app.services.order_persistence import backfill_attributed_orders

    db = SessionLocal()
    try:
        merchant = db.query(ShopifyStore).filter(ShopifyStore.id == store_id).first()
        if not merchant:
            logger.error(f"[Order Backfill] Store {store_id} not found")
            return
        logger.info(f"[Order Backfill] Starting for {merchant.merchant_id}")
        result = await backfill_attributed_orders(db, merchant)
        logger.info(f"[Order Backfill] Result: {result}")
    except Exception as e:
        logger.error(f"[Order Backfill] Error: {e}", exc_info=True)
    finally:
        db.close()


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
    # state is HMAC-signed so it can't be forged or guessed (CSRF protection).
    signed_state = _create_signed_state(oauth_data.merchant_id, settings.SHOPIFY_API_SECRET)
    base_params = {
        "client_id": settings.SHOPIFY_API_KEY,
        "scope": settings.SHOPIFY_SCOPES,
        "redirect_uri": settings.OAUTH_REDIRECT_URL,
        "state": signed_state,
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


@router.options("/upgrade-scopes")
async def upgrade_scopes_preflight():
    """Handle CORS preflight for the scope-upgrade endpoint"""
    return {}


@router.post("/upgrade-scopes", response_model=Dict[str, str])
async def upgrade_scopes(
    merchant_id: str = Query(..., description="merchant_id of an already-connected store"),
    db: Session = Depends(get_db),
):
    """
    Build the URL that prompts an already-connected merchant to grant the
    optional, PCD-gated scope(s) (currently read_orders) on top of the scopes
    they approved at install.

    IMPORTANT: this app uses Shopify-managed installation
    (use_legacy_install_flow = false). In that mode the legacy
    /admin/oauth/authorize?scope=... endpoint IGNORES any extra scope appended
    to the query — Shopify only grants what's declared in the app config. So
    optional scopes MUST be requested via the managed-install optional-scopes
    URL instead:

        https://admin.shopify.com/store/{store_handle}/oauth/install
            ?client_id={client_id}&optional_scopes={comma_separated_scopes}

    `optional_scopes` must be a subset of the `optional_scopes` declared in
    shopify.app.toml. Shopify shows the merchant a grant modal for just these
    scopes; on approval the grant is recorded against the existing app
    installation (the stored offline token automatically gains access — no
    re-exchange). The grant does NOT hit /complete, so the dashboard must
    reconcile afterwards (poll /api/orders/scope-status, which re-reads the
    real granted scopes from Shopify and fires the backfill on first sight of
    read_orders).

    Drives the dashboard "Enable sales attribution" CTA: the frontend calls
    this, then redirects the merchant to the returned URL.
    """
    from app.config import settings
    from urllib.parse import urlencode

    merchant = db.query(ShopifyStore).filter(
        ShopifyStore.merchant_id == merchant_id,
        ShopifyStore.is_active == 1,
    ).first()

    if not merchant or not merchant.shop_domain:
        raise HTTPException(
            status_code=404,
            detail="No active connected store found for this merchant_id",
        )

    if not settings.SHOPIFY_OPTIONAL_SCOPES:
        raise HTTPException(
            status_code=400,
            detail="No optional scopes are configured to request",
        )

    # Managed-install optional-scopes request URL. The store handle is the
    # myshopify subdomain (admin.shopify.com routes by handle, not full domain).
    store_handle = sanitize_shop_domain(merchant.shop_domain).replace(".myshopify.com", "")
    optional_scopes = settings.SHOPIFY_OPTIONAL_SCOPES.replace(" ", "")
    install_url = (
        f"https://admin.shopify.com/store/{store_handle}/oauth/install?"
        + urlencode({
            "client_id": settings.SHOPIFY_API_KEY,
            "optional_scopes": optional_scopes,
        })
    )

    logger.info(
        f"[Scope Upgrade] Built optional-scopes ({optional_scopes}) request URL "
        f"for merchant: {merchant_id}, store: {store_handle}"
    )

    return {
        "authorization_url": install_url,
        "merchant_id": merchant_id,
        "shop_domain": merchant.shop_domain,
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
    #   1. Dashboard flow: state = signed "{merchant_id}:{hmac}" token (set by generate-url)
    #      → verify signature and extract merchant_id
    #   2. App Store install (SaaS user logged in):
    #      state = shop_domain (set by /install), merchant_id sent by frontend from session
    #   3. Pure App Store install (no SaaS account yet):
    #      state = shop_domain, no merchant_id → use shop domain as temporary ID
    from app.config import settings

    raw_state = oauth_data.state
    app_store_install = bool(raw_state and _is_myshopify_domain(raw_state))

    # Try to extract merchant_id from a signed dashboard-flow state first
    signed_merchant_id = _extract_signed_merchant_id(raw_state or "", settings.SHOPIFY_API_SECRET)

    if signed_merchant_id:
        # Dashboard flow — signature verified, merchant_id is trusted
        merchant_id = signed_merchant_id
        app_store_install = False
    elif app_store_install and oauth_data.merchant_id:
        # App Store install with SaaS user already logged in
        merchant_id = oauth_data.merchant_id
    elif raw_state:
        # Pure App Store install — state is shop_domain, use as temporary ID
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

    if not merchant:
        # Create new merchant record — shop_domain comes from Shopify's callback
        merchant = ShopifyStore(
            merchant_id=merchant_id,
            shop_domain=shop_domain
        )
        db.add(merchant)
        db.flush()
    else:
        # Update shop_domain if changed. Only update merchant_id if explicitly
        # provided by the frontend — don't let App Store re-auth flows overwrite
        # a merchant_id that was set by the SaaS dashboard.
        merchant.shop_domain = shop_domain
        if not app_store_install and merchant_id:
            merchant.merchant_id = merchant_id

    try:
        # Try to exchange code for a fresh access token.
        # If it fails (code already used on re-auth), fall back to existing token.
        token_exchanged = False
        try:
            token_data = await shopify_oauth.exchange_code_for_token(shop_domain, oauth_data.code)
            merchant.access_token = token_data.get("access_token")
            merchant.scope = token_data.get("scope")
            merchant.is_active = 1
            token_exchanged = True
        except Exception as token_error:
            error_msg = str(token_error)
            logger.warning(f"[OAuth Complete] Token exchange failed: {error_msg}")

            # If we have an existing valid token, this is just a re-auth with a
            # reused code — return existing data instead of erroring.
            if merchant and merchant.is_active == 1 and merchant.access_token:
                logger.info(f"[OAuth Complete] Using existing token for merchant: {merchant.merchant_id}")
            else:
                # No existing token to fall back on — genuinely broken
                db.rollback()
                if "400" in error_msg or "invalid" in error_msg.lower() or "already" in error_msg.lower():
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid or already used authorization code. Please restart the OAuth flow."
                    )
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to exchange authorization code with Shopify: {error_msg}"
                )

        # Commit the merchant update before proceeding with other operations
        db.commit()
        db.refresh(merchant)

        if token_exchanged:
            logger.info(f"[OAuth Complete] Access token obtained for merchant: {merchant.merchant_id}")

        # Get shop info via Admin GraphQL — returns canonical domain, name, primary + all domains.
        # Preferred over REST; also resolves the real myshopify domain for App Store installs.
        all_domains: list = []
        try:
            gql_shop = await shopify_oauth.get_shop_info_graphql(shop_domain, merchant.access_token)
            shop_name = gql_shop.get("name", shop_domain)
            canonical_domain = gql_shop.get("myshopifyDomain", shop_domain)
            primary_domain = (gql_shop.get("primaryDomain") or {}).get("host", shop_domain)
            all_domains = [d["host"] for d in (gql_shop.get("domains") or []) if d.get("host")]
            # Always include myshopify + primary even if domains list is sparse
            for host in (canonical_domain, primary_domain):
                if host and host not in all_domains:
                    all_domains.append(host)
            logger.info(
                f"[OAuth Complete] Shop info — name: {shop_name}, myshopify: {canonical_domain}, "
                f"primary: {primary_domain}, domains: {all_domains}"
            )

            # For pure App Store installs (no SaaS account), state=shop_domain was
            # used as a temporary merchant_id. Replace it with the canonical domain.
            # Only do this if the merchant_id still looks like a domain (temporary).
            # Never overwrite a merchant_id that was explicitly set by the SaaS dashboard.
            if app_store_install and not oauth_data.merchant_id:
                current_mid = merchant.merchant_id or ""
                if _is_myshopify_domain(current_mid) or current_mid == shop_domain:
                    shop_handle = canonical_domain.replace(".myshopify.com", "")
                    merchant.merchant_id = shop_handle
                    db.commit()
                    db.refresh(merchant)

            # Persist custom domain + allowlist onto public.merchants so the
            # chatbot widget can mount on the storefront (not just *.myshopify.com).
            # Merchant row may not exist yet (created on AI Persona save) — retry in background.
            if merchant.merchant_id:
                primary_url = (
                    f"https://{primary_domain}"
                    if primary_domain and not str(primary_domain).startswith("http")
                    else primary_domain
                )
                if not _persist_merchant_domains(merchant.merchant_id, primary_url, all_domains):
                    background_tasks.add_task(
                        _persist_merchant_domains_with_retry,
                        merchant.merchant_id,
                        primary_url,
                        list(all_domains),
                    )
        except Exception as shop_error:
            logger.error(f"[OAuth Complete] GraphQL shop info failed: {str(shop_error)}")
            shop_name = shop_domain
            canonical_domain = shop_domain
            primary_domain = shop_domain
            all_domains = [shop_domain] if shop_domain else []

        # Write merchant_id as shop metafield for the Liquid theme extension
        try:
            await shopify_oauth.write_merchant_id_metafield(shop_domain, merchant.access_token, merchant.merchant_id, settings.CHATBOT_SRC)
        except Exception as meta_error:
            logger.warning(f"[OAuth Complete] Metafield write failed (non-blocking): {str(meta_error)}")

        # Register webhooks (non-blocking - log errors but don't fail OAuth)
        try:
            webhook_results = await register_webhooks(shop_domain, merchant.access_token, db, merchant.id)
        except Exception as webhook_error:
            logger.error(f"[OAuth Complete] Webhook registration failed: {str(webhook_error)}")
            webhook_results = {"error": "Webhook registration failed, will retry later"}

        # Run initial product sync in background — don't block the OAuth response.
        # For stores with many products this can take 10s+, causing frontend timeouts.
        if token_exchanged:
            background_tasks.add_task(
                initial_product_sync_background,
                merchant.id,
                shop_domain,
                merchant.access_token
            )
            sync_result = {"status": "started_in_background"}
        else:
            sync_result = {"status": "skipped", "reason": "re-auth with existing token"}

        # If the merchant just granted read_orders (new grant, not re-auth),
        # backfill the last 60 days of attributed orders. Gated on the real
        # scope set returned by Shopify, so merchants who didn't opt in skip.
        from app.utils.helpers import has_scope
        if token_exchanged and has_scope(merchant.scope, "read_orders"):
            background_tasks.add_task(_oauth_order_backfill_background, merchant.id)

        logger.info(f"[OAuth Complete] Successfully completed OAuth for merchant: {merchant.merchant_id}")

        return {
            "message": "OAuth successful",
            "merchant_id": merchant.merchant_id,
            "shop_domain": shop_domain,
            "shop_name": shop_name,
            "canonical_domain": canonical_domain,
            "primary_domain": primary_domain,
            "all_domains": all_domains,
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


@router.get("/lookup")
async def lookup_merchant_by_shop(
    shop: str,
    db: Session = Depends(get_db)
):
    """
    Public endpoint — look up a merchant by shop domain.
    Used by the admin connect page on load to detect existing connections.
    Returns merchant_id if found and active, 404 otherwise.
    """
    shop_domain = sanitize_shop_domain(shop)
    merchant = db.query(ShopifyStore).filter(
        ShopifyStore.shop_domain == shop_domain,
        ShopifyStore.is_active == 1
    ).first()

    if not merchant:
        raise HTTPException(status_code=404, detail="No active merchant found for this shop")

    return {
        "merchant_id": merchant.merchant_id,
        "shop_domain": merchant.shop_domain,
        "is_active": True
    }


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


@router.options("/disconnect")
async def disconnect_preflight():
    """Handle CORS preflight for disconnect endpoint"""
    return {}


@router.delete("/disconnect")
async def disconnect_merchant(
    merchant: ShopifyStore = Depends(get_merchant_from_header),
    db: Session = Depends(get_db)
):
    """
    Deactivate a merchant's Shopify connection.

    Called when an agent is deleted from the dashboard so the store
    no longer appears as connected.
    """
    merchant.is_active = 0
    merchant.access_token = None
    db.commit()
    logger.info(f"[Disconnect] Merchant {merchant.merchant_id} deactivated")
    return {"message": "Disconnected", "merchant_id": merchant.merchant_id}

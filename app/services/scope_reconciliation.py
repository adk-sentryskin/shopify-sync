"""
Scope reconciliation for the managed-install optional-scopes flow.

Why this exists: this app uses Shopify-managed installation
(use_legacy_install_flow = false). Optional scopes (read_orders) are granted
by the merchant via the managed-install optional-scopes URL
(admin.shopify.com/store/{handle}/oauth/install?optional_scopes=read_orders),
NOT through the legacy /admin/oauth/authorize redirect. That grant is recorded
by Shopify against the existing app installation and does NOT call our
/api/oauth/complete callback — so merchant.scope in our DB goes stale and the
order pipeline never wakes up.

This module closes that gap two ways:
  1. Event-driven (preferred): the app/scopes_update webhook calls
     apply_scope_update() with the new scope set.
  2. Pull (self-healing): /api/orders/scope-status calls
     reconcile_scopes_from_shopify() when it sees a merchant without
     read_orders, re-reading the real granted scopes from Shopify.

Either way, the first time read_orders appears we provision order access:
register the orders/* webhooks (they require the scope at subscription time)
and backfill the last 60 days of attributed orders.
"""

import logging
from typing import Iterable, Tuple

from sqlalchemy.orm import Session

from app.models import ShopifyStore
from app.services.order_attribution import REQUIRED_SCOPE
from app.services.shopify_oauth import ShopifyOAuth
from app.utils.helpers import has_scope, parse_scopes

logger = logging.getLogger(__name__)
_oauth = ShopifyOAuth()


async def reconcile_scopes_from_shopify(db: Session, store: ShopifyStore) -> Tuple[bool, bool]:
    """
    Re-read the scopes actually granted to this store's token from Shopify and
    update store.scope if it drifted from what we have stored.

    Returns (changed, read_orders_newly_granted). On any Shopify error we leave
    the stored scope untouched and report (False, False) — the caller falls
    back to whatever is already in the DB.
    """
    if not store.access_token or not store.shop_domain:
        return (False, False)

    try:
        granted_str = await _oauth.get_access_scopes(store.shop_domain, store.access_token)
    except Exception as e:  # network / auth / 404 — non-fatal, just don't reconcile
        logger.warning(f"[Scope Reconcile] Could not read scopes for {store.merchant_id}: {e}")
        return (False, False)

    return _apply(db, store, parse_scopes(granted_str), raw=granted_str)


def apply_scope_update(db: Session, store: ShopifyStore, current_scopes: Iterable[str]) -> Tuple[bool, bool]:
    """
    Apply a scope set delivered by the app/scopes_update webhook (no Shopify
    round-trip needed — the payload already carries the current scopes).

    Returns (changed, read_orders_newly_granted).
    """
    scope_set = {s.strip() for s in current_scopes if s and s.strip()}
    return _apply(db, store, scope_set, raw=",".join(sorted(scope_set)))


def _apply(db: Session, store: ShopifyStore, new_set: set, raw: str) -> Tuple[bool, bool]:
    had_read_orders = has_scope(store.scope, REQUIRED_SCOPE)
    changed = new_set != parse_scopes(store.scope)

    if changed:
        store.scope = raw
        db.commit()
        logger.info(f"[Scope Reconcile] {store.merchant_id} scope updated -> {raw}")

    newly_granted = (REQUIRED_SCOPE in new_set) and not had_read_orders
    return (changed, newly_granted)


async def provision_order_access_background(store_id: int) -> None:
    """
    Run once when a merchant first grants read_orders. Registers the orders/*
    webhooks (gated on scope at subscription time, so they couldn't be created
    until now) and backfills the last 60 days of attributed orders.

    Designed to run as a FastAPI background task with its own DB session.
    """
    from app.database import SessionLocal
    from app.services.order_persistence import backfill_attributed_orders
    from app.services.webhook_manager import register_webhooks

    db = SessionLocal()
    try:
        store = db.query(ShopifyStore).filter(ShopifyStore.id == store_id).first()
        if not store or not store.access_token:
            logger.error(f"[Provision Orders] Store {store_id} missing or no token")
            return

        if not has_scope(store.scope, REQUIRED_SCOPE):
            logger.warning(f"[Provision Orders] {store.merchant_id} no longer has read_orders, skipping")
            return

        logger.info(f"[Provision Orders] Starting for {store.merchant_id}")

        # Register orders/* webhooks now that the scope exists.
        try:
            await register_webhooks(store.shop_domain, store.access_token, db, store.id)
        except Exception as e:
            logger.error(f"[Provision Orders] Webhook registration failed for {store.merchant_id}: {e}")

        # Backfill the last 60 days of attributed orders.
        try:
            result = await backfill_attributed_orders(db, store)
            logger.info(f"[Provision Orders] Backfill result for {store.merchant_id}: {result}")
        except Exception as e:
            logger.error(f"[Provision Orders] Backfill failed for {store.merchant_id}: {e}", exc_info=True)
    finally:
        db.close()

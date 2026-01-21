import httpx
from typing import List, Dict, Optional
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.config import settings
from app.models import Webhook, ShopifyStore
from app.utils.helpers import sanitize_shop_domain


WEBHOOK_CONFIG = [
    {
        "topic": "products/create",
        "address": "{app_url}/api/webhooks/products/create",
        "format": "json"
    },
    {
        "topic": "products/update",
        "address": "{app_url}/api/webhooks/products/update",
        "format": "json"
    },
    {
        "topic": "products/delete",
        "address": "{app_url}/api/webhooks/products/delete",
        "format": "json"
    },
    # Mandatory compliance webhooks for GDPR/CCPA
    {
        "topic": "customers/data_request",
        "address": "{app_url}/api/webhooks/customers/data_request",
        "format": "json"
    },
    {
        "topic": "customers/redact",
        "address": "{app_url}/api/webhooks/customers/redact",
        "format": "json"
    },
    {
        "topic": "shop/redact",
        "address": "{app_url}/api/webhooks/shop/redact",
        "format": "json"
    }
]


async def register_webhooks(shop_domain: str, access_token: str, db: Session, merchant_id: int) -> List[Dict]:
    """
    Register webhooks for a shop after OAuth installation

    Check local database first to avoid unnecessary API calls,
    then sync with Shopify and save webhook IDs for tracking.

    Args:
        shop_domain: Shopify shop domain (e.g., mystore.myshopify.com)
        access_token: OAuth access token for the shop
        db: Database session for tracking webhooks
        merchant_id: ShopifyStore ID (integer) for associating webhooks

    Returns:
        List of results for each webhook (created/updated/failed)
    """
    results = []

    # Get the shopify store to fetch merchant_id string
    from app.models import ShopifyStore
    store = db.query(ShopifyStore).filter(ShopifyStore.id == merchant_id).first()
    if not store:
        raise ValueError(f"ShopifyStore with id {merchant_id} not found")

    store_id = merchant_id  # FK to shopify_stores
    tenant_id = store.merchant_id  # VARCHAR merchant identifier

    # Get the app URL from settings
    app_url = getattr(settings, 'APP_URL', settings.OAUTH_REDIRECT_URL.rsplit('/api/', 1)[0])

    for webhook_config in WEBHOOK_CONFIG:
        try:
            # Format the webhook address with actual app URL
            webhook = {
                **webhook_config,
                "address": webhook_config["address"].format(app_url=app_url)
            }

            # Check database first
            db_webhook = db.query(Webhook).filter(
                Webhook.store_id == store_id,
                Webhook.topic == webhook["topic"],
                Webhook.is_active == 1
            ).first()

            if db_webhook:
                # Verify webhook still exists in Shopify
                shopify_webhook = await get_existing_webhook_by_id(
                    shop_domain, access_token, db_webhook.shopify_webhook_id
                )

                if shopify_webhook:
                    # Webhook exists - check if URL changed
                    if shopify_webhook.get("address") != webhook["address"]:
                        # Update in Shopify
                        await update_webhook(shop_domain, access_token, db_webhook.shopify_webhook_id, webhook)
                        # Update in database
                        db_webhook.address = webhook["address"]
                        db_webhook.last_verified_at = datetime.now(timezone.utc)
                        db.commit()

                        results.append({
                            "topic": webhook["topic"],
                            "action": "updated",
                            "webhook_id": db_webhook.shopify_webhook_id,
                            "status": "success"
                        })
                    else:
                        # Already exists and up to date
                        db_webhook.last_verified_at = datetime.now(timezone.utc)
                        db.commit()

                        results.append({
                            "topic": webhook["topic"],
                            "action": "already_exists",
                            "webhook_id": db_webhook.shopify_webhook_id,
                            "status": "success"
                        })
                else:
                    # Webhook deleted from Shopify - recreate it
                    created = await create_webhook(shop_domain, access_token, webhook)
                    shopify_webhook_id = created.get("webhook", {}).get("id")

                    # Update database record
                    db_webhook.shopify_webhook_id = shopify_webhook_id
                    db_webhook.address = webhook["address"]
                    db_webhook.is_active = 1
                    db_webhook.last_verified_at = datetime.now(timezone.utc)
                    db.commit()

                    results.append({
                        "topic": webhook["topic"],
                        "action": "recreated",
                        "webhook_id": shopify_webhook_id,
                        "status": "success"
                    })
            else:
                # Not in database - check Shopify API
                existing = await get_existing_webhook(shop_domain, access_token, webhook["topic"])

                if existing:
                    # Exists in Shopify but not in DB - save to DB
                    new_webhook = Webhook(
                        store_id=store_id,
                        merchant_id=tenant_id,
                        shopify_webhook_id=existing["id"],
                        topic=webhook["topic"],
                        address=existing["address"],
                        format=webhook.get("format", "json"),
                        is_active=1,
                        last_verified_at=datetime.now(timezone.utc)
                    )
                    db.add(new_webhook)
                    db.commit()

                    # Update if URL changed
                    if existing.get("address") != webhook["address"]:
                        await update_webhook(shop_domain, access_token, existing["id"], webhook)
                        new_webhook.address = webhook["address"]
                        db.commit()
                        action = "updated"
                    else:
                        action = "already_exists"

                    results.append({
                        "topic": webhook["topic"],
                        "action": action,
                        "webhook_id": existing["id"],
                        "status": "success"
                    })
                else:
                    # Create new webhook in Shopify
                    created = await create_webhook(shop_domain, access_token, webhook)
                    shopify_webhook_id = created.get("webhook", {}).get("id")

                    # Save to database
                    new_webhook = Webhook(
                        store_id=store_id,
                        merchant_id=tenant_id,
                        shopify_webhook_id=shopify_webhook_id,
                        topic=webhook["topic"],
                        address=webhook["address"],
                        format=webhook.get("format", "json"),
                        is_active=1,
                        last_verified_at=datetime.now(timezone.utc)
                    )
                    db.add(new_webhook)
                    db.commit()

                    results.append({
                        "topic": webhook["topic"],
                        "action": "created",
                        "webhook_id": shopify_webhook_id,
                        "status": "success"
                    })

        except Exception as e:
            results.append({
                "topic": webhook_config["topic"],
                "action": "failed",
                "status": "error",
                "error": str(e)
            })

    return results


async def create_webhook(shop_domain: str, access_token: str, webhook: Dict) -> Dict:
    """
    Create a webhook subscription via Shopify Admin API

    Args:
        shop_domain: Shopify shop domain
        access_token: OAuth access token
        webhook: Webhook configuration dict with topic, address, format

    Returns:
        Shopify API response with created webhook details
    """
    shop_domain = sanitize_shop_domain(shop_domain)
    url = f"https://{shop_domain}/admin/api/{settings.SHOPIFY_API_VERSION}/webhooks.json"

    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json"
    }

    payload = {"webhook": webhook}

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()


async def update_webhook(shop_domain: str, access_token: str, webhook_id: int, webhook: Dict) -> Dict:
    """
    Update an existing webhook subscription

    Args:
        shop_domain: Shopify shop domain
        access_token: OAuth access token
        webhook_id: ID of the webhook to update
        webhook: Updated webhook configuration

    Returns:
        Shopify API response with updated webhook details
    """
    shop_domain = sanitize_shop_domain(shop_domain)
    url = f"https://{shop_domain}/admin/api/{settings.SHOPIFY_API_VERSION}/webhooks/{webhook_id}.json"

    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json"
    }

    payload = {"webhook": webhook}

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.put(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()


async def get_existing_webhook(shop_domain: str, access_token: str, topic: str) -> Optional[Dict]:
    """
    Check if a webhook already exists for a specific topic

    Args:
        shop_domain: Shopify shop domain
        access_token: OAuth access token
        topic: Webhook topic to search for (e.g., "products/create")

    Returns:
        Webhook dict if found, None otherwise
    """
    shop_domain = sanitize_shop_domain(shop_domain)
    url = f"https://{shop_domain}/admin/api/{settings.SHOPIFY_API_VERSION}/webhooks.json"

    headers = {
        "X-Shopify-Access-Token": access_token
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        webhooks = response.json().get("webhooks", [])

        # Find webhook matching this topic
        for webhook in webhooks:
            if webhook.get("topic") == topic:
                return webhook

        return None


async def get_existing_webhook_by_id(shop_domain: str, access_token: str, webhook_id: int) -> Optional[Dict]:
    """
    Get a specific webhook by ID from Shopify

    Args:
        shop_domain: Shopify shop domain
        access_token: OAuth access token
        webhook_id: Shopify webhook ID

    Returns:
        Webhook dict if found, None if deleted
    """
    shop_domain = sanitize_shop_domain(shop_domain)
    url = f"https://{shop_domain}/admin/api/{settings.SHOPIFY_API_VERSION}/webhooks/{webhook_id}.json"

    headers = {
        "X-Shopify-Access-Token": access_token
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json().get("webhook")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise


async def list_webhooks(shop_domain: str, access_token: str) -> List[Dict]:
    """
    List all registered webhooks for a shop

    Args:
        shop_domain: Shopify shop domain
        access_token: OAuth access token

    Returns:
        List of all webhooks registered for this shop
    """
    shop_domain = sanitize_shop_domain(shop_domain)
    url = f"https://{shop_domain}/admin/api/{settings.SHOPIFY_API_VERSION}/webhooks.json"

    headers = {
        "X-Shopify-Access-Token": access_token
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json().get("webhooks", [])


async def delete_webhook(shop_domain: str, access_token: str, webhook_id: int, db: Optional[Session] = None) -> bool:
    """
    Delete a webhook subscription

    Args:
        shop_domain: Shopify shop domain
        access_token: OAuth access token
        webhook_id: ID of the webhook to delete
        db: Optional database session to update tracking

    Returns:
        True if deleted successfully
    """
    shop_domain = sanitize_shop_domain(shop_domain)
    url = f"https://{shop_domain}/admin/api/{settings.SHOPIFY_API_VERSION}/webhooks/{webhook_id}.json"

    headers = {
        "X-Shopify-Access-Token": access_token
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.delete(url, headers=headers)
        response.raise_for_status()

    # Mark as inactive in database if db session provided
    if db:
        db_webhook = db.query(Webhook).filter(
            Webhook.shopify_webhook_id == webhook_id
        ).first()
        if db_webhook:
            db_webhook.is_active = 0
            db.commit()

    return True


async def sync_webhooks(shop_domain: str, access_token: str, db: Session, merchant_id: int) -> Dict:
    """
    Sync webhook state between database and Shopify

    Detects drift: webhooks deleted in Shopify, or created outside this app.

    Args:
        shop_domain: Shopify shop domain
        access_token: OAuth access token
        db: Database session
        merchant_id: ShopifyStore ID (integer)

    Returns:
        Sync results with counts of created, deleted, and synced webhooks
    """
    # Get the shopify store to fetch merchant_id string
    from app.models import ShopifyStore
    store = db.query(ShopifyStore).filter(ShopifyStore.id == merchant_id).first()
    if not store:
        raise ValueError(f"ShopifyStore with id {merchant_id} not found")

    store_id = merchant_id  # FK to shopify_stores
    tenant_id = store.merchant_id  # VARCHAR merchant identifier

    # Get all webhooks from Shopify
    shopify_webhooks = await list_webhooks(shop_domain, access_token)
    shopify_webhook_ids = {w["id"] for w in shopify_webhooks}

    # Get all active webhooks from database
    db_webhooks = db.query(Webhook).filter(
        Webhook.store_id == store_id,
        Webhook.is_active == 1
    ).all()

    deleted_count = 0
    discovered_count = 0

    # Mark webhooks as inactive if deleted from Shopify
    for db_webhook in db_webhooks:
        if db_webhook.shopify_webhook_id not in shopify_webhook_ids:
            db_webhook.is_active = 0
            deleted_count += 1

    # Add webhooks that exist in Shopify but not in database
    db_webhook_ids = {w.shopify_webhook_id for w in db_webhooks}
    for shopify_webhook in shopify_webhooks:
        if shopify_webhook["id"] not in db_webhook_ids:
            new_webhook = Webhook(
                store_id=store_id,
                merchant_id=tenant_id,
                shopify_webhook_id=shopify_webhook["id"],
                topic=shopify_webhook["topic"],
                address=shopify_webhook["address"],
                format=shopify_webhook.get("format", "json"),
                is_active=1,
                last_verified_at=datetime.now(timezone.utc)
            )
            db.add(new_webhook)
            discovered_count += 1

    db.commit()

    return {
        "status": "success",
        "shopify_count": len(shopify_webhooks),
        "database_count": len([w for w in db_webhooks if w.is_active]),
        "deleted_count": deleted_count,
        "discovered_count": discovered_count
    }

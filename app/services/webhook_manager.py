import httpx
from typing import List, Dict, Optional
from app.config import settings


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
    }
]


async def register_webhooks(shop_domain: str, access_token: str) -> List[Dict]:
    """
    Register webhooks for a shop after OAuth installation

    Args:
        shop_domain: Shopify shop domain (e.g., mystore.myshopify.com)
        access_token: OAuth access token for the shop

    Returns:
        List of results for each webhook (created/updated/failed)
    """
    results = []

    # Get the app URL from settings
    app_url = getattr(settings, 'APP_URL', settings.OAUTH_REDIRECT_URL.rsplit('/api/', 1)[0])

    for webhook_config in WEBHOOK_CONFIG:
        try:
            # Format the webhook address with actual app URL
            webhook = {
                **webhook_config,
                "address": webhook_config["address"].format(app_url=app_url)
            }

            # Check if webhook already exists for this topic
            existing = await get_existing_webhook(shop_domain, access_token, webhook["topic"])

            if existing:
                # Update existing webhook (in case URL changed)
                if existing.get("address") != webhook["address"]:
                    await update_webhook(shop_domain, access_token, existing["id"], webhook)
                    results.append({
                        "topic": webhook["topic"],
                        "action": "updated",
                        "webhook_id": existing["id"],
                        "status": "success"
                    })
                else:
                    results.append({
                        "topic": webhook["topic"],
                        "action": "already_exists",
                        "webhook_id": existing["id"],
                        "status": "success"
                    })
            else:
                # Create new webhook
                created = await create_webhook(shop_domain, access_token, webhook)
                results.append({
                    "topic": webhook["topic"],
                    "action": "created",
                    "webhook_id": created.get("webhook", {}).get("id"),
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
    shop_domain = shop_domain.replace("https://", "").replace("http://", "").strip("/")
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
    shop_domain = shop_domain.replace("https://", "").replace("http://", "").strip("/")
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
    shop_domain = shop_domain.replace("https://", "").replace("http://", "").strip("/")
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


async def list_webhooks(shop_domain: str, access_token: str) -> List[Dict]:
    """
    List all registered webhooks for a shop

    Args:
        shop_domain: Shopify shop domain
        access_token: OAuth access token

    Returns:
        List of all webhooks registered for this shop
    """
    shop_domain = shop_domain.replace("https://", "").replace("http://", "").strip("/")
    url = f"https://{shop_domain}/admin/api/{settings.SHOPIFY_API_VERSION}/webhooks.json"

    headers = {
        "X-Shopify-Access-Token": access_token
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json().get("webhooks", [])


async def delete_webhook(shop_domain: str, access_token: str, webhook_id: int) -> bool:
    """
    Delete a webhook subscription

    Args:
        shop_domain: Shopify shop domain
        access_token: OAuth access token
        webhook_id: ID of the webhook to delete

    Returns:
        True if deleted successfully
    """
    shop_domain = shop_domain.replace("https://", "").replace("http://", "").strip("/")
    url = f"https://{shop_domain}/admin/api/{settings.SHOPIFY_API_VERSION}/webhooks/{webhook_id}.json"

    headers = {
        "X-Shopify-Access-Token": access_token
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.delete(url, headers=headers)
        response.raise_for_status()
        return True

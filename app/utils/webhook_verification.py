import hmac
import hashlib
import base64
from typing import Optional
from app.config import settings


def verify_webhook(data: bytes, hmac_header: Optional[str]) -> bool:
    """
    Verify Shopify webhook HMAC signature

    Args:
        data: Raw request body as bytes
        hmac_header: X-Shopify-Hmac-SHA256 header value

    Returns:
        True if HMAC is valid, False otherwise
    """
    if not hmac_header:
        return False

    # Calculate HMAC
    calculated_hmac = base64.b64encode(
        hmac.new(
            settings.SHOPIFY_API_SECRET.encode('utf-8'),
            data,
            hashlib.sha256
        ).digest()
    ).decode('utf-8')

    # Compare with provided HMAC
    return hmac.compare_digest(calculated_hmac, hmac_header)


def extract_shop_domain(headers: dict) -> Optional[str]:
    """
    Extract shop domain from webhook headers

    Args:
        headers: Request headers dict

    Returns:
        Shop domain or None
    """
    # Shopify sends the shop domain in X-Shopify-Shop-Domain header
    return headers.get('x-shopify-shop-domain')


def extract_webhook_topic(headers: dict) -> Optional[str]:
    """
    Extract webhook topic from headers

    Args:
        headers: Request headers dict

    Returns:
        Webhook topic (e.g., 'products/create') or None
    """
    return headers.get('x-shopify-topic')

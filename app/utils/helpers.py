"""Common utility functions"""


def sanitize_shop_domain(shop_domain: str) -> str:
    """
    Remove protocol and trailing slashes from shop domain

    Args:
        shop_domain: Shop domain with or without protocol

    Returns:
        Sanitized shop domain (e.g., 'mystore.myshopify.com')
    """
    return shop_domain.replace("https://", "").replace("http://", "").strip("/")

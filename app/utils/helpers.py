"""Common utility functions"""

from typing import Optional, Set


def parse_scopes(scope_string: Optional[str]) -> Set[str]:
    """
    Parse a comma-separated Shopify scope string into a set of scope handles.

    Tolerates None, whitespace, and empty entries. Used to inspect what a
    merchant actually granted (stored on ShopifyStore.scope) before calling
    APIs that require a specific scope.
    """
    if not scope_string:
        return set()
    return {s.strip() for s in scope_string.split(",") if s.strip()}


def has_scope(scope_string: Optional[str], required: str) -> bool:
    """
    Return True if `required` is present in the merchant's granted scopes.

    This gates access to scopes that aren't in the default required set
    (e.g. read_orders), so order-reading code stays dormant — and never
    crashes with a 403 — for merchants who haven't granted it.
    """
    return required in parse_scopes(scope_string)


def sanitize_shop_domain(shop_domain: str) -> str:
    """
    Remove protocol and trailing slashes from shop domain

    Args:
        shop_domain: Shop domain with or without protocol

    Returns:
        Sanitized shop domain (e.g., 'mystore.myshopify.com')
    """
    return shop_domain.replace("https://", "").replace("http://", "").strip("/")

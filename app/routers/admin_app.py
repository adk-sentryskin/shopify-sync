"""
Admin App router — serves the embedded admin page for Shopify App Review.
Shopify opens this when a merchant clicks the app in their Admin.
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pathlib import Path
import logging

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin App"])

_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "admin_connect.html"


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def admin_app_home():
    """
    Shopify Admin App entry point.
    Serves the merchant connect page. Shopify passes ?shop=mystore.myshopify.com.
    This route is public (no API key required).
    """
    try:
        html = _TEMPLATE_PATH.read_text(encoding="utf-8")
        return HTMLResponse(content=html)
    except Exception as e:
        logger.error(f"Failed to load admin connect template: {e}")
        return HTMLResponse(content="<h1>Service temporarily unavailable</h1>", status_code=500)

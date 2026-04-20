"""
Admin App router — serves the embedded admin page for Shopify App Review.
Shopify opens this when a merchant clicks the app in their Admin.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pathlib import Path
import logging

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin App"])

_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "admin_connect.html"

_PROD_BUILDER = "https://app.chekout.ai"
_STAGING_BUILDER = "https://ai-builder.chekout.ai"

_PROD_HOSTS = {"shopify-sync-579388332064.us-central1.run.app", "shopify-sync-l6tdgloqva-uc.a.run.app"}


def _builder_base(request: Request) -> str:
    # 1. Referer tells us which builder the user came from
    referer = request.headers.get("referer", "")
    if _PROD_BUILDER in referer:
        return _PROD_BUILDER
    if _STAGING_BUILDER in referer:
        return _STAGING_BUILDER

    # 2. Fall back to which sync service received the request
    host = request.headers.get("host", "").split(":")[0]
    if host in _PROD_HOSTS:
        return _PROD_BUILDER

    # 3. Coming directly from Shopify with no builder referer → default prod
    return _PROD_BUILDER


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def admin_app_home(request: Request):
    """
    Shopify Admin App entry point.
    Serves the merchant connect page. Shopify passes ?shop=mystore.myshopify.com.
    This route is public (no API key required).
    """
    try:
        html = _TEMPLATE_PATH.read_text(encoding="utf-8")
        html = html.replace("__BUILDER_BASE_URL__", _builder_base(request))
        return HTMLResponse(content=html)
    except Exception as e:
        logger.error(f"Failed to load admin connect template: {e}")
        return HTMLResponse(content="<h1>Service temporarily unavailable</h1>", status_code=500)

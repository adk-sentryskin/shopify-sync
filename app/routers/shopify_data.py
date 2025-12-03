from fastapi import APIRouter, Depends, HTTPException, Query, Path
from sqlalchemy.orm import Session
from typing import Dict, Optional
from app.database import get_db
from app.models import Merchant
from app.middleware.auth import get_merchant_from_header
from app.services.shopify_oauth import ShopifyOAuth

router = APIRouter(prefix="/api/products", tags=["Products"])
shopify_oauth = ShopifyOAuth()


@router.get("/")
async def get_products(
    limit: int = Query(50, description="Number of products to retrieve", ge=1, le=250),
    since_id: Optional[int] = Query(None, description="Retrieve products after this ID"),
    fields: Optional[str] = Query(None, description="Comma-separated list of fields to return"),
    merchant: Merchant = Depends(get_merchant_from_header)
):
    """
    Get all products from Shopify store

    Headers:
        - X-Merchant-Id: Merchant identifier (required)

    Query Parameters:
        - limit: Number of products (default: 50, max: 250)
        - since_id: Get products after this ID (for pagination)
        - fields: Comma-separated fields (e.g., "id,title,variants,images")
    """
    try:
        endpoint = f"/products.json?limit={limit}"
        if since_id:
            endpoint += f"&since_id={since_id}"
        if fields:
            endpoint += f"&fields={fields}"

        data = await shopify_oauth.make_shopify_request(
            shop_domain=merchant.shop_domain,
            access_token=merchant.access_token,
            endpoint=endpoint,
            method="GET"
        )

        return {
            "merchant_id": merchant.merchant_id,
            "shop_domain": merchant.shop_domain,
            "data": data
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch products: {str(e)}"
        )


@router.get("/count")
async def get_products_count(
    merchant: Merchant = Depends(get_merchant_from_header)
):
    """
    Get total count of products in the store

    Headers:
        - X-Merchant-Id: Merchant identifier (required)
    """
    try:
        data = await shopify_oauth.make_shopify_request(
            shop_domain=merchant.shop_domain,
            access_token=merchant.access_token,
            endpoint="/products/count.json",
            method="GET"
        )

        return {
            "merchant_id": merchant.merchant_id,
            "shop_domain": merchant.shop_domain,
            "count": data.get("count", 0)
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch product count: {str(e)}"
        )


@router.get("/{product_id}")
async def get_product(
    product_id: int = Path(..., description="Shopify product ID"),
    fields: Optional[str] = Query(None, description="Comma-separated list of fields to return"),
    merchant: Merchant = Depends(get_merchant_from_header)
):
    """
    Get a single product by ID

    Headers:
        - X-Merchant-Id: Merchant identifier (required)

    Path Parameters:
        - product_id: Shopify product ID

    Query Parameters:
        - fields: Comma-separated fields (e.g., "id,title,variants,images")
    """
    try:
        endpoint = f"/products/{product_id}.json"
        if fields:
            endpoint += f"?fields={fields}"

        data = await shopify_oauth.make_shopify_request(
            shop_domain=merchant.shop_domain,
            access_token=merchant.access_token,
            endpoint=endpoint,
            method="GET"
        )

        return {
            "merchant_id": merchant.merchant_id,
            "shop_domain": merchant.shop_domain,
            "data": data
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch product: {str(e)}"
        )

from fastapi import APIRouter, Depends, HTTPException, Query, Path
from sqlalchemy.orm import Session
from typing import List, Dict
from app.database import get_db
from app.models import Merchant, Product
from app.middleware.auth import get_merchant_from_header
from app.services.product_sync import (
    extract_variants_from_product,
    get_total_inventory,
    search_products_by_sku,
    find_low_inventory_products
)

router = APIRouter(prefix="/api/variants", tags=["Variants"])


@router.get("/{product_id}")
async def get_product_variants(
    product_id: int = Path(..., description="Shopify product ID"),
    merchant: Merchant = Depends(get_merchant_from_header),
    db: Session = Depends(get_db)
):
    """
    Get all variants for a specific product

    Extracts and returns all variants from the product's raw_data field
    with normalized, easy-to-consume format.

    Headers:
        - X-Merchant-Id: Merchant identifier (required)

    Path Parameters:
        - product_id: Shopify product ID

    Returns:
        Product details with complete variants array including:
        - SKU, barcode, title
        - Price and compare_at_price
        - Inventory quantity
        - Weight and options
    """
    product = db.query(Product).filter(
        Product.shopify_product_id == product_id,
        Product.merchant_id == merchant.id,
        Product.is_deleted == 0  # Only show active products
    ).first()

    if not product:
        raise HTTPException(
            status_code=404,
            detail=f"Product {product_id} not found for this merchant"
        )

    variants = extract_variants_from_product(product)

    return {
        "product_id": product.shopify_product_id,
        "title": product.title,
        "vendor": product.vendor,
        "product_type": product.product_type,
        "handle": product.handle,
        "status": product.status,
        "total_variants": len(variants),
        "total_inventory": get_total_inventory(product),
        "variants": variants
    }


@router.get("/search/by-sku")
async def search_by_sku(
    sku: str = Query(..., description="Variant SKU to search for", min_length=1),
    merchant: Merchant = Depends(get_merchant_from_header),
    db: Session = Depends(get_db)
):
    """
    Search products by variant SKU

    Searches through all products to find those containing a variant
    with the specified SKU. Returns matching products with their variants.

    Headers:
        - X-Merchant-Id: Merchant identifier (required)

    Query Parameters:
        - sku: Variant SKU to search for (required)

    Returns:
        List of products containing variants with this SKU
    """
    if not sku or not sku.strip():
        raise HTTPException(
            status_code=400,
            detail="SKU parameter cannot be empty"
        )

    products = search_products_by_sku(db, merchant, sku.strip())

    results = []
    for product in products:
        variants = extract_variants_from_product(product)
        # Filter to only variants matching this SKU
        matching_variants = [v for v in variants if v.get('sku') == sku.strip()]

        results.append({
            "product_id": product.shopify_product_id,
            "title": product.title,
            "vendor": product.vendor,
            "handle": product.handle,
            "status": product.status,
            "total_variants": len(variants),
            "matching_variants": matching_variants
        })

    return {
        "sku": sku.strip(),
        "total_products_found": len(results),
        "products": results
    }


@router.get("/inventory/low")
async def get_low_inventory_products(
    threshold: int = Query(10, description="Inventory threshold (products below this level)", ge=0, le=1000),
    merchant: Merchant = Depends(get_merchant_from_header),
    db: Session = Depends(get_db)
):
    """
    Get products with low inventory

    Returns all active products where the total inventory (sum of all
    variant inventory quantities) is below the specified threshold.
    Useful for inventory management and low stock alerts.

    Headers:
        - X-Merchant-Id: Merchant identifier (required)

    Query Parameters:
        - threshold: Inventory threshold (default: 10, min: 0, max: 1000)

    Returns:
        List of products with low inventory including:
        - Product details (ID, title, vendor)
        - Total inventory count
        - All variants with inventory details
    """
    low_inventory = find_low_inventory_products(db, merchant, threshold)

    # Sort by total_inventory ascending (lowest first)
    low_inventory.sort(key=lambda x: x['total_inventory'])

    return {
        "threshold": threshold,
        "total_products": len(low_inventory),
        "merchant_id": merchant.merchant_id,
        "products": low_inventory
    }


@router.get("/")
async def variants_info():
    """
    Information about variants API endpoints

    Returns documentation and usage examples for the variants API
    """
    return {
        "description": "Variants API - Access product variants and inventory information",
        "endpoints": [
            {
                "method": "GET",
                "path": "/api/variants/{product_id}",
                "description": "Get all variants for a specific product",
                "example": "/api/variants/5678901234"
            },
            {
                "method": "GET",
                "path": "/api/variants/search/by-sku",
                "description": "Search products by variant SKU",
                "example": "/api/variants/search/by-sku?sku=TSHIRT-BLUE-M"
            },
            {
                "method": "GET",
                "path": "/api/variants/inventory/low",
                "description": "Get products with low inventory",
                "example": "/api/variants/inventory/low?threshold=10"
            }
        ],
        "authentication": {
            "required": True,
            "header": "X-Merchant-Id",
            "description": "All endpoints require merchant authentication via X-Merchant-Id header"
        },
        "variant_fields": {
            "variant_id": "Shopify variant ID",
            "product_id": "Parent product ID",
            "sku": "Stock Keeping Unit",
            "barcode": "Product barcode",
            "title": "Variant title (e.g., 'Blue / Medium')",
            "price": "Variant price",
            "compare_at_price": "Original price (for discounts)",
            "inventory_quantity": "Current stock level",
            "inventory_policy": "Policy when out of stock",
            "weight": "Variant weight",
            "weight_unit": "Unit of weight",
            "option1": "First variant option (e.g., color)",
            "option2": "Second variant option (e.g., size)",
            "option3": "Third variant option",
            "image_id": "Associated image ID"
        }
    }

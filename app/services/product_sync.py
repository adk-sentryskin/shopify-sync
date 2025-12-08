from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import func
from typing import Dict, List, Optional
from datetime import datetime
import httpx
import time
from app.models import Product, Merchant
from app.config import settings


def parse_shopify_product(product_data: dict) -> dict:
    """
    Extract and normalize Shopify product data for database storage

    Args:
        product_data: Raw product data from Shopify API

    Returns:
        Dictionary with normalized product fields
    """
    # Parse timestamps
    def parse_datetime(dt_str: Optional[str]) -> Optional[datetime]:
        if not dt_str:
            return None
        try:
            return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return None

    return {
        'shopify_product_id': product_data.get('id'),
        'title': product_data.get('title'),
        'vendor': product_data.get('vendor'),
        'product_type': product_data.get('product_type'),
        'handle': product_data.get('handle'),
        'status': product_data.get('status'),
        'shopify_created_at': parse_datetime(product_data.get('created_at')),
        'shopify_updated_at': parse_datetime(product_data.get('updated_at')),
        'published_at': parse_datetime(product_data.get('published_at')),
        'raw_data': product_data  # Store complete JSON
    }


def upsert_product(db: Session, merchant: Merchant, product_data: dict) -> Product:
    """
    Insert or update a single product in the database

    Args:
        db: Database session
        merchant: Merchant object
        product_data: Raw Shopify product data

    Returns:
        Product object (either created or updated)
    """
    parsed_data = parse_shopify_product(product_data)
    parsed_data['merchant_id'] = merchant.id

    # Use PostgreSQL's INSERT ... ON CONFLICT DO UPDATE (upsert)
    stmt = insert(Product).values(**parsed_data)

    # On conflict (duplicate shopify_product_id), update the record
    stmt = stmt.on_conflict_do_update(
        index_elements=['shopify_product_id'],
        set_={
            'title': parsed_data['title'],
            'vendor': parsed_data['vendor'],
            'product_type': parsed_data['product_type'],
            'handle': parsed_data['handle'],
            'status': parsed_data['status'],
            'shopify_created_at': parsed_data['shopify_created_at'],
            'shopify_updated_at': parsed_data['shopify_updated_at'],
            'published_at': parsed_data['published_at'],
            'raw_data': parsed_data['raw_data'],
            'synced_at': func.now(),
            'updated_at': func.now()
        }
    )

    # Execute the upsert
    db.execute(stmt)
    db.commit()

    # Fetch and return the product
    product = db.query(Product).filter(
        Product.shopify_product_id == parsed_data['shopify_product_id']
    ).first()

    return product


def sync_products(db: Session, merchant: Merchant, products_data: List[dict]) -> Dict:
    """
    Bulk sync multiple products to the database

    Args:
        db: Database session
        merchant: Merchant object
        products_data: List of raw Shopify product data

    Returns:
        Dictionary with sync statistics (synced_count, created_count, updated_count, failed_count)
    """
    stats = {
        'synced_count': 0,
        'created_count': 0,
        'updated_count': 0,
        'failed_count': 0
    }

    for product_data in products_data:
        try:
            # Check if product already exists to determine if it's a create or update
            existing_product = db.query(Product).filter(
                Product.shopify_product_id == product_data.get('id')
            ).first()

            is_update = existing_product is not None

            # Upsert the product
            upsert_product(db, merchant, product_data)

            stats['synced_count'] += 1
            if is_update:
                stats['updated_count'] += 1
            else:
                stats['created_count'] += 1

        except Exception as e:
            stats['failed_count'] += 1
            print(f"Error syncing product {product_data.get('id')}: {str(e)}")
            # Continue with next product instead of failing entire batch
            continue

    return stats


def sync_single_product(db: Session, merchant: Merchant, product_data: dict) -> Dict:
    """
    Sync a single product and return sync status

    Args:
        db: Database session
        merchant: Merchant object
        product_data: Raw Shopify product data (can be wrapped in 'product' key)

    Returns:
        Dictionary with sync statistics
    """
    # Handle both {"product": {...}} and direct product data
    if 'product' in product_data:
        product_data = product_data['product']

    # Check if product already exists
    existing_product = db.query(Product).filter(
        Product.shopify_product_id == product_data.get('id')
    ).first()

    is_update = existing_product is not None

    try:
        upsert_product(db, merchant, product_data)

        return {
            'synced_count': 1,
            'created_count': 0 if is_update else 1,
            'updated_count': 1 if is_update else 0,
            'failed_count': 0
        }
    except Exception as e:
        print(f"Error syncing product {product_data.get('id')}: {str(e)}")
        return {
            'synced_count': 0,
            'created_count': 0,
            'updated_count': 0,
            'failed_count': 1
        }


async def fetch_all_products_from_shopify(
    db: Session,
    merchant: Merchant,
    shop_domain: str,
    access_token: str
) -> Dict:
    """
    Fetch ALL products from Shopify with automatic pagination and sync to database.

    This function is designed for initial bulk sync after OAuth.
    Uses Shopify's cursor-based pagination to handle stores with thousands of products.

    Args:
        db: Database session
        merchant: Merchant object
        shop_domain: Shopify shop domain (e.g., mystore.myshopify.com)
        access_token: OAuth access token

    Returns:
        Dictionary with comprehensive sync statistics:
        {
            'status': 'completed' | 'partial' | 'failed',
            'total_products': int,
            'synced_count': int,
            'created_count': int,
            'updated_count': int,
            'failed_count': int,
            'pages_fetched': int,
            'duration_seconds': float,
            'error': str (only if status is 'failed')
        }
    """
    start_time = time.time()

    # Sanitize shop domain
    shop_domain = shop_domain.replace("https://", "").replace("http://", "").strip("/")

    # Aggregate statistics
    total_stats = {
        'status': 'completed',
        'total_products': 0,
        'synced_count': 0,
        'created_count': 0,
        'updated_count': 0,
        'failed_count': 0,
        'pages_fetched': 0,
        'duration_seconds': 0.0
    }

    try:
        # Shopify allows max 250 products per request
        limit = 250
        since_id = 0  # Start from the beginning

        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                # Build URL for this page
                url = f"https://{shop_domain}/admin/api/{settings.SHOPIFY_API_VERSION}/products.json"
                params = {
                    'limit': limit,
                    'since_id': since_id
                }

                headers = {
                    'X-Shopify-Access-Token': access_token,
                    'Content-Type': 'application/json'
                }

                # Fetch products from Shopify
                try:
                    response = await client.get(url, headers=headers, params=params)
                    response.raise_for_status()
                    data = response.json()
                except httpx.HTTPError as e:
                    print(f"HTTP error fetching products: {str(e)}")
                    total_stats['status'] = 'partial' if total_stats['synced_count'] > 0 else 'failed'
                    total_stats['error'] = f"HTTP error: {str(e)}"
                    break

                products = data.get('products', [])
                total_stats['pages_fetched'] += 1

                # If no products returned, we've reached the end
                if not products:
                    break

                # Sync this batch of products
                batch_stats = sync_products(db, merchant, products)

                # Aggregate statistics
                total_stats['synced_count'] += batch_stats['synced_count']
                total_stats['created_count'] += batch_stats['created_count']
                total_stats['updated_count'] += batch_stats['updated_count']
                total_stats['failed_count'] += batch_stats['failed_count']
                total_stats['total_products'] += len(products)

                print(f"Synced page {total_stats['pages_fetched']}: {batch_stats['synced_count']}/{len(products)} products")

                # If we got fewer products than the limit, we've reached the end
                if len(products) < limit:
                    break

                # Update since_id to the last product's ID for pagination
                since_id = products[-1]['id']

                # Optional: Add a small delay to be respectful to Shopify's API
                # Shopify's rate limit is 2 requests/second for standard plans
                await httpx.AsyncClient().aclose()
                time.sleep(0.5)  # 500ms delay between requests

        # Calculate duration
        total_stats['duration_seconds'] = round(time.time() - start_time, 2)

        # Set final status
        if total_stats['failed_count'] > 0 and total_stats['synced_count'] == 0:
            total_stats['status'] = 'failed'
        elif total_stats['failed_count'] > 0:
            total_stats['status'] = 'partial'
        else:
            total_stats['status'] = 'completed'

        return total_stats

    except Exception as e:
        total_stats['status'] = 'failed'
        total_stats['error'] = str(e)
        total_stats['duration_seconds'] = round(time.time() - start_time, 2)
        print(f"Error in bulk product fetch: {str(e)}")
        return total_stats


def extract_variants_from_product(product: Product) -> List[Dict]:
    """
    Extract all variants from a product's raw_data

    Parses the JSONB raw_data field and returns a clean list of variants
    with normalized fields for easy consumption by API endpoints.

    Args:
        product: Product object with raw_data field

    Returns:
        List of variant dictionaries with normalized fields:
        - variant_id: Shopify variant ID
        - product_id: Parent product ID
        - sku: Stock Keeping Unit
        - barcode: Product barcode
        - title: Variant title (e.g., "Blue / Medium")
        - price: Variant price
        - compare_at_price: Original price (for discounts)
        - inventory_quantity: Current stock level
        - inventory_policy: "deny" or "continue" when out of stock
        - weight: Variant weight
        - weight_unit: Unit of weight (kg, lb, etc)
        - option1, option2, option3: Variant options (size, color, etc)
        - image_id: Associated image ID
    """
    if not product.raw_data:
        return []

    variants = product.raw_data.get('variants', [])

    # Normalize variant data
    return [
        {
            'variant_id': v.get('id'),
            'product_id': product.shopify_product_id,
            'sku': v.get('sku'),
            'barcode': v.get('barcode'),
            'title': v.get('title'),
            'price': v.get('price'),
            'compare_at_price': v.get('compare_at_price'),
            'inventory_quantity': v.get('inventory_quantity', 0),
            'inventory_policy': v.get('inventory_policy'),
            'weight': v.get('weight'),
            'weight_unit': v.get('weight_unit'),
            'option1': v.get('option1'),
            'option2': v.get('option2'),
            'option3': v.get('option3'),
            'image_id': v.get('image_id')
        }
        for v in variants
    ]


def get_total_inventory(product: Product) -> int:
    """
    Calculate total inventory across all variants

    Sums up inventory_quantity for all variants in a product.
    Useful for determining if a product is in stock or low on inventory.

    Args:
        product: Product object

    Returns:
        Total inventory quantity across all variants
    """
    variants = extract_variants_from_product(product)
    return sum(v.get('inventory_quantity', 0) for v in variants)


def search_products_by_sku(db: Session, merchant: Merchant, sku: str) -> List[Product]:
    """
    Find products that have a variant with the specified SKU

    Searches through the JSONB raw_data field to find matching SKUs.
    Note: This is a simple implementation. For better performance with
    large datasets, consider adding GIN indexes on the variants field.

    Args:
        db: Database session
        merchant: Merchant object
        sku: Variant SKU to search for

    Returns:
        List of products containing variants with this SKU
    """
    # Get all products for this merchant
    products = db.query(Product).filter(
        Product.merchant_id == merchant.id
    ).all()

    # Filter products that have a variant with this SKU
    matching_products = []
    for product in products:
        variants = extract_variants_from_product(product)
        if any(v.get('sku') == sku for v in variants):
            matching_products.append(product)

    return matching_products


def find_low_inventory_products(
    db: Session,
    merchant: Merchant,
    threshold: int = 10
) -> List[Dict]:
    """
    Find products with total inventory below threshold

    Useful for inventory management and low stock alerts.

    Args:
        db: Database session
        merchant: Merchant object
        threshold: Inventory threshold (default: 10)

    Returns:
        List of products with low inventory, including:
        - product_id: Shopify product ID
        - title: Product title
        - total_inventory: Sum of all variant inventory
        - variants: List of all variants with inventory details
    """
    products = db.query(Product).filter(
        Product.merchant_id == merchant.id,
        Product.status == 'active'
    ).all()

    low_inventory = []

    for product in products:
        total_inv = get_total_inventory(product)
        if total_inv < threshold:
            low_inventory.append({
                'product_id': product.shopify_product_id,
                'title': product.title,
                'vendor': product.vendor,
                'handle': product.handle,
                'total_inventory': total_inv,
                'variants': extract_variants_from_product(product)
            })

    return low_inventory

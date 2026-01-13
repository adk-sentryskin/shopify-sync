from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import func
from typing import Dict, List, Optional
from datetime import datetime
import httpx
import time
import logging
from app.models import Product, ShopifyStore
from app.config import settings
from app.utils.helpers import sanitize_shop_domain

logger = logging.getLogger(__name__)


def parse_shopify_product(product_data: dict) -> dict:
    """Extract and normalize Shopify product data for database storage"""
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
        'raw_data': product_data
    }


def upsert_product(db: Session, merchant: ShopifyStore, product_data: dict) -> Product:
    """Insert or update a single product in the database"""
    parsed_data = parse_shopify_product(product_data)

    # Set FK to shopify_stores table
    parsed_data['store_id'] = merchant.id

    # Set denormalized merchant_id for fast multi-tenant queries
    parsed_data['merchant_id'] = merchant.merchant_id

    stmt = insert(Product).values(**parsed_data)
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

    db.execute(stmt)
    db.commit()

    product = db.query(Product).filter(
        Product.shopify_product_id == parsed_data['shopify_product_id']
    ).first()

    return product


def sync_products(db: Session, merchant: ShopifyStore, products_data: List[dict]) -> Dict:
    """Bulk sync multiple products to the database"""
    stats = {
        'synced_count': 0,
        'created_count': 0,
        'updated_count': 0,
        'failed_count': 0
    }

    for product_data in products_data:
        try:
            existing_product = db.query(Product).filter(
                Product.shopify_product_id == product_data.get('id')
            ).first()

            is_update = existing_product is not None
            upsert_product(db, merchant, product_data)

            stats['synced_count'] += 1
            if is_update:
                stats['updated_count'] += 1
            else:
                stats['created_count'] += 1

        except Exception as e:
            stats['failed_count'] += 1
            logger.error(f"Error syncing product {product_data.get('id')}: {str(e)}")
            continue

    return stats


def sync_single_product(db: Session, merchant: ShopifyStore, product_data: dict) -> Dict:
    """Sync a single product and return sync status"""
    if 'product' in product_data:
        product_data = product_data['product']

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
        logger.error(f"Error syncing product {product_data.get('id')}: {str(e)}")
        return {
            'synced_count': 0,
            'created_count': 0,
            'updated_count': 0,
            'failed_count': 1
        }


async def fetch_all_products_from_shopify(
    db: Session,
    merchant: ShopifyStore,
    shop_domain: str,
    access_token: str
) -> Dict:
    """Fetch ALL products from Shopify with automatic pagination and sync to database"""
    start_time = time.time()
    shop_domain = sanitize_shop_domain(shop_domain)

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
        limit = 250
        since_id = 0

        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                url = f"https://{shop_domain}/admin/api/{settings.SHOPIFY_API_VERSION}/products.json"
                params = {
                    'limit': limit,
                    'since_id': since_id
                }

                headers = {
                    'X-Shopify-Access-Token': access_token,
                    'Content-Type': 'application/json'
                }

                try:
                    response = await client.get(url, headers=headers, params=params)
                    response.raise_for_status()
                    data = response.json()
                except httpx.HTTPError as e:
                    logger.error(f"HTTP error fetching products: {str(e)}")
                    total_stats['status'] = 'partial' if total_stats['synced_count'] > 0 else 'failed'
                    total_stats['error'] = f"HTTP error: {str(e)}"
                    break

                products = data.get('products', [])
                total_stats['pages_fetched'] += 1

                if not products:
                    break

                batch_stats = sync_products(db, merchant, products)

                total_stats['synced_count'] += batch_stats['synced_count']
                total_stats['created_count'] += batch_stats['created_count']
                total_stats['updated_count'] += batch_stats['updated_count']
                total_stats['failed_count'] += batch_stats['failed_count']
                total_stats['total_products'] += len(products)

                logger.info(f"Synced page {total_stats['pages_fetched']}: {batch_stats['synced_count']}/{len(products)} products")

                if len(products) < limit:
                    break

                since_id = products[-1]['id']
                await httpx.AsyncClient().aclose()
                time.sleep(0.5)

        total_stats['duration_seconds'] = round(time.time() - start_time, 2)

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
        logger.error(f"Error in bulk product fetch: {str(e)}")
        return total_stats


def extract_variants_from_product(product: Product) -> List[Dict]:
    """Extract all variants from a product's raw_data"""
    if not product.raw_data:
        return []

    variants = product.raw_data.get('variants', [])

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
    """Calculate total inventory across all variants"""
    variants = extract_variants_from_product(product)
    return sum(v.get('inventory_quantity', 0) for v in variants)


def search_products_by_sku(db: Session, merchant: ShopifyStore, sku: str) -> List[Product]:
    """Find products that have a variant with the specified SKU"""
    products = db.query(Product).filter(
        Product.merchant_id == merchant.merchant_id,
        Product.is_deleted == 0
    ).all()

    matching_products = []
    for product in products:
        variants = extract_variants_from_product(product)
        if any(v.get('sku') == sku for v in variants):
            matching_products.append(product)

    return matching_products


def find_low_inventory_products(
    db: Session,
    merchant: ShopifyStore,
    threshold: int = 10
) -> List[Dict]:
    """Find products with total inventory below threshold"""
    products = db.query(Product).filter(
        Product.merchant_id == merchant.merchant_id,
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

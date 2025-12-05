from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import func
from typing import Dict, List, Optional
from datetime import datetime
from app.models import Product, Merchant


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

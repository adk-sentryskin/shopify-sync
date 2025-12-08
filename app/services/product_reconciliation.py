from sqlalchemy.orm import Session
from typing import Dict, List
from datetime import datetime, timezone
import httpx
import time
from app.models import Product, Merchant
from app.config import settings
from app.services.product_sync import parse_shopify_product, upsert_product


async def reconcile_products(
    db: Session,
    merchant: Merchant,
    shop_domain: str,
    access_token: str,
    mark_deleted: bool = False
) -> Dict:
    """
    Reconcile products between Shopify and local database

    Compares products in the database with products in Shopify to detect:
    - Products that exist in Shopify but not in database (missing)
    - Products in database that are deleted in Shopify
    - Products that are out of sync (different updated_at timestamps)

    This is a safety net for webhook failures or extended downtime.

    Args:
        db: Database session
        merchant: Merchant object
        shop_domain: Shopify shop domain
        access_token: OAuth access token
        mark_deleted: If True, marks products as deleted if they don't exist in Shopify

    Returns:
        Dictionary with reconciliation results:
        {
            'status': 'completed' | 'partial' | 'failed',
            'products_in_shopify': int,
            'products_in_database': int (active only),
            'missing_in_db': int,
            'missing_in_db_product_ids': list,
            'deleted_in_shopify': int,
            'deleted_in_shopify_product_ids': list,
            'out_of_sync': int,
            'out_of_sync_product_ids': list,
            'synced_count': int,
            'marked_deleted_count': int,
            'duration_seconds': float
        }
    """
    start_time = time.time()

    # Sanitize shop domain
    shop_domain = shop_domain.replace("https://", "").replace("http://", "").strip("/")

    # Initialize results
    results = {
        'status': 'completed',
        'products_in_shopify': 0,
        'products_in_database': 0,
        'missing_in_db': 0,
        'missing_in_db_product_ids': [],
        'deleted_in_shopify': 0,
        'deleted_in_shopify_product_ids': [],
        'out_of_sync': 0,
        'out_of_sync_product_ids': [],
        'synced_count': 0,
        'marked_deleted_count': 0,
        'duration_seconds': 0.0
    }

    try:
        # Step 1: Fetch all products from Shopify
        shopify_products = await fetch_all_products_from_shopify_for_reconciliation(
            shop_domain, access_token
        )

        if shopify_products is None:
            results['status'] = 'failed'
            results['error'] = 'Failed to fetch products from Shopify'
            results['duration_seconds'] = round(time.time() - start_time, 2)
            return results

        results['products_in_shopify'] = len(shopify_products)

        # Create a map of Shopify products by ID
        shopify_product_map = {p['id']: p for p in shopify_products}
        shopify_product_ids = set(shopify_product_map.keys())

        # Step 2: Get all products from database (active only)
        db_products = db.query(Product).filter(
            Product.merchant_id == merchant.id,
            Product.is_deleted == 0
        ).all()

        results['products_in_database'] = len(db_products)

        # Create a map of database products by Shopify ID
        db_product_map = {p.shopify_product_id: p for p in db_products}
        db_product_ids = set(db_product_map.keys())

        # Step 3: Find products missing in database
        missing_in_db = shopify_product_ids - db_product_ids
        results['missing_in_db'] = len(missing_in_db)
        results['missing_in_db_product_ids'] = list(missing_in_db)

        # Sync missing products
        for product_id in missing_in_db:
            try:
                upsert_product(db, merchant, shopify_product_map[product_id])
                results['synced_count'] += 1
            except Exception as e:
                print(f"Error syncing missing product {product_id}: {str(e)}")

        # Step 4: Find products deleted in Shopify
        deleted_in_shopify = db_product_ids - shopify_product_ids
        results['deleted_in_shopify'] = len(deleted_in_shopify)
        results['deleted_in_shopify_product_ids'] = list(deleted_in_shopify)

        # Mark as deleted if requested
        if mark_deleted and deleted_in_shopify:
            for product_id in deleted_in_shopify:
                try:
                    product = db_product_map[product_id]
                    product.is_deleted = 1
                    product.status = 'deleted'
                    product.deleted_at = datetime.now(timezone.utc)
                    results['marked_deleted_count'] += 1
                except Exception as e:
                    print(f"Error marking product {product_id} as deleted: {str(e)}")

            db.commit()

        # Step 5: Check for out-of-sync products (different updated_at)
        out_of_sync = []
        for product_id in shopify_product_ids.intersection(db_product_ids):
            shopify_product = shopify_product_map[product_id]
            db_product = db_product_map[product_id]

            # Parse Shopify updated_at
            shopify_updated_str = shopify_product.get('updated_at')
            if shopify_updated_str:
                try:
                    shopify_updated = datetime.fromisoformat(
                        shopify_updated_str.replace('Z', '+00:00')
                    )

                    # Compare timestamps (allow 1 second tolerance for rounding)
                    if db_product.shopify_updated_at:
                        time_diff = abs(
                            (shopify_updated - db_product.shopify_updated_at).total_seconds()
                        )

                        if time_diff > 1:  # More than 1 second difference
                            out_of_sync.append(product_id)

                            # Re-sync the product
                            try:
                                upsert_product(db, merchant, shopify_product)
                                results['synced_count'] += 1
                            except Exception as e:
                                print(f"Error re-syncing product {product_id}: {str(e)}")

                except (ValueError, AttributeError) as e:
                    print(f"Error parsing timestamp for product {product_id}: {str(e)}")

        results['out_of_sync'] = len(out_of_sync)
        results['out_of_sync_product_ids'] = out_of_sync

        # Calculate duration
        results['duration_seconds'] = round(time.time() - start_time, 2)

        # Set final status
        if results['synced_count'] == 0 and results['marked_deleted_count'] == 0:
            if results['missing_in_db'] == 0 and results['out_of_sync'] == 0:
                results['status'] = 'completed'
                results['message'] = 'All products are in sync'
            else:
                results['status'] = 'failed'
                results['message'] = 'Reconciliation found issues but failed to fix them'
        else:
            results['status'] = 'completed'
            results['message'] = f'Reconciliation completed: {results["synced_count"]} synced, {results["marked_deleted_count"]} marked deleted'

        return results

    except Exception as e:
        results['status'] = 'failed'
        results['error'] = str(e)
        results['duration_seconds'] = round(time.time() - start_time, 2)
        print(f"Error in product reconciliation: {str(e)}")
        return results


async def fetch_all_products_from_shopify_for_reconciliation(
    shop_domain: str,
    access_token: str
) -> List[Dict]:
    """
    Fetch ALL products from Shopify for reconciliation

    Similar to fetch_all_products_from_shopify but only fetches basic fields
    for faster comparison. Returns list of product dictionaries.

    Args:
        shop_domain: Shopify shop domain
        access_token: OAuth access token

    Returns:
        List of product dictionaries, or None if failed
    """
    all_products = []
    limit = 250
    since_id = 0

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                url = f"https://{shop_domain}/admin/api/{settings.SHOPIFY_API_VERSION}/products.json"
                params = {
                    'limit': limit,
                    'since_id': since_id,
                    'fields': 'id,title,updated_at'  # Only fetch fields we need for comparison
                }

                headers = {
                    'X-Shopify-Access-Token': access_token,
                    'Content-Type': 'application/json'
                }

                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()
                data = response.json()

                products = data.get('products', [])

                if not products:
                    break

                all_products.extend(products)

                if len(products) < limit:
                    break

                since_id = products[-1]['id']
                time.sleep(0.5)  # Rate limiting

        return all_products

    except Exception as e:
        print(f"Error fetching products from Shopify: {str(e)}")
        return None


async def force_full_resync(
    db: Session,
    merchant: Merchant,
    shop_domain: str,
    access_token: str
) -> Dict:
    """
    Force a full re-sync of all products from Shopify

    Fetches all products from Shopify and upserts them into the database.
    This is more aggressive than reconciliation - it updates all products
    regardless of whether they appear out of sync.

    Args:
        db: Database session
        merchant: Merchant object
        shop_domain: Shopify shop domain
        access_token: OAuth access token

    Returns:
        Dictionary with sync statistics
    """
    from app.services.product_sync import fetch_all_products_from_shopify

    return await fetch_all_products_from_shopify(
        db=db,
        merchant=merchant,
        shop_domain=shop_domain,
        access_token=access_token
    )

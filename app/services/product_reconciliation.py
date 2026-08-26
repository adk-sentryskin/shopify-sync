from sqlalchemy.orm import Session
from typing import Dict, List
from datetime import datetime, timezone
import httpx
import asyncio
import time
import logging
from app.models import Product, ShopifyStore
from app.config import settings
from app.services.product_sync import sync_products
from app.utils.helpers import sanitize_shop_domain

logger = logging.getLogger(__name__)

# Soft-deleting is the one reconciliation step that destroys data, and it acts
# on absence — everything Shopify *didn't* return. A fetch that came back short
# (a scoped token, an empty 200, pagination that stopped early) therefore looks
# exactly like "the merchant deleted their catalog". These bounds make that
# failure mode a refusal instead of a wipe; pass force_delete=True to override
# for a genuinely large, intentional deletion.
MAX_DELETE_RATIO = 0.10   # never auto-delete more than 10% of a catalog at once
MAX_DELETE_FLOOR = 10     # ...but always allow a handful, for small catalogs


async def reconcile_products(
    db: Session,
    merchant: ShopifyStore,
    shop_domain: str,
    access_token: str,
    mark_deleted: bool = False,
    force_delete: bool = False
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
        merchant: ShopifyStore object
        shop_domain: Shopify shop domain
        access_token: OAuth access token
        mark_deleted: If True, marks products as deleted if they don't exist in Shopify
        force_delete: Bypass the bulk-deletion safety bound. Only set this when
            a large deletion is known to be real.

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

    shop_domain = sanitize_shop_domain(shop_domain)

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
            Product.merchant_id == merchant.merchant_id,
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

        # Sync missing products in batched pages rather than one commit each
        if missing_in_db:
            # Off the event loop — sync_products blocks on Postgres and, with
            # embeddings on, a synchronous Vertex call for the whole batch.
            missing_stats = await asyncio.to_thread(
                sync_products, db, merchant,
                [shopify_product_map[pid] for pid in missing_in_db]
            )
            results['synced_count'] += missing_stats['synced_count']
            if missing_stats['failed_count']:
                logger.error(
                    f"Reconciliation failed to sync {missing_stats['failed_count']} "
                    f"missing products for merchant {merchant.merchant_id}"
                )

        # Step 4: Find products deleted in Shopify
        deleted_in_shopify = db_product_ids - shopify_product_ids
        results['deleted_in_shopify'] = len(deleted_in_shopify)
        results['deleted_in_shopify_product_ids'] = list(deleted_in_shopify)

        # Mark as deleted if requested — bounded, see MAX_DELETE_RATIO above
        if mark_deleted and deleted_in_shopify:
            delete_limit = max(
                MAX_DELETE_FLOOR,
                int(results['products_in_database'] * MAX_DELETE_RATIO)
            )
            refusal = None
            if results['products_in_shopify'] == 0 and results['products_in_database'] > 0:
                refusal = (
                    "Shopify returned 0 products while the database holds "
                    f"{results['products_in_database']} — treating this as a failed "
                    "fetch, not an emptied catalog"
                )
            elif len(deleted_in_shopify) > delete_limit:
                refusal = (
                    f"{len(deleted_in_shopify)} products would be marked deleted, "
                    f"over the safety bound of {delete_limit} "
                    f"({int(MAX_DELETE_RATIO * 100)}% of {results['products_in_database']})"
                )

            if refusal and not force_delete:
                results['mark_deleted_skipped'] = True
                results['mark_deleted_skipped_reason'] = refusal
                logger.error(
                    f"Refusing bulk product deletion for merchant "
                    f"{merchant.merchant_id}: {refusal}. Re-run with force_delete=true "
                    f"if this deletion is real."
                )
                deleted_in_shopify = set()
            elif refusal:
                logger.warning(
                    f"force_delete=True — proceeding with bulk deletion for merchant "
                    f"{merchant.merchant_id} despite: {refusal}"
                )

        if mark_deleted and deleted_in_shopify:
            for product_id in deleted_in_shopify:
                try:
                    product = db_product_map[product_id]
                    product.is_deleted = 1
                    product.status = 'deleted'
                    product.deleted_at = datetime.now(timezone.utc)
                    results['marked_deleted_count'] += 1
                except Exception as e:
                    logger.error(f"Error marking product {product_id} as deleted: {str(e)}")

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

                except (ValueError, AttributeError) as e:
                    logger.error(f"Error parsing timestamp for product {product_id}: {str(e)}")

        # Re-sync every drifted product in one batched pass
        if out_of_sync:
            resync_stats = await asyncio.to_thread(
                sync_products, db, merchant,
                [shopify_product_map[pid] for pid in out_of_sync]
            )
            results['synced_count'] += resync_stats['synced_count']
            if resync_stats['failed_count']:
                logger.error(
                    f"Reconciliation failed to re-sync {resync_stats['failed_count']} "
                    f"drifted products for merchant {merchant.merchant_id}"
                )

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
        logger.error(f"Error in product reconciliation: {str(e)}")
        return results


async def fetch_all_products_from_shopify_for_reconciliation(
    shop_domain: str,
    access_token: str
) -> List[Dict]:
    """
    Fetch ALL products from Shopify for reconciliation

    Returns full product payloads, not just the comparison fields. Anything
    reconciliation finds missing or drifted gets written straight back through
    sync_products(), so a trimmed `fields=` payload would null out vendor,
    product_type, handle, status, published_at and raw_data on every row it
    touched. Same number of API calls either way.

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
                    'since_id': since_id
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
                await asyncio.sleep(0.5)  # Rate limiting

        return all_products

    except Exception as e:
        logger.error(f"Error fetching products from Shopify: {str(e)}")
        return None


async def force_full_resync(
    db: Session,
    merchant: ShopifyStore,
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
        merchant: ShopifyStore object
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

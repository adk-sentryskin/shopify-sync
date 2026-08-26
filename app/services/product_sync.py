from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from typing import Dict, List, Optional
from datetime import datetime
import httpx
import time
import asyncio
import logging
from app.models import Product, ShopifyStore
from app.config import settings
from app.utils.helpers import sanitize_shop_domain

logger = logging.getLogger(__name__)

# Lazy import for embedding service (only if embeddings enabled)
_embedding_service = None

# DB column -> the Shopify payload key it is derived from.
#
# A column is written ONLY when its source key is actually present in the
# payload. This is the failsafe against a narrowed or truncated response
# blanking out good data: GET /api/shopify/products?fields=id,title, a
# `fields=`-filtered webhook, or a half-read body all yield payloads that are
# missing most of these keys, and a naive parse turns every absent key into
# NULL. Absent key means "not mentioned" (leave the stored value alone), while
# a present-but-null key means "actually cleared" (e.g. published_at on a
# product that got unpublished) and is written through.
_COLUMN_SOURCE_KEYS = {
    'title': 'title',
    'vendor': 'vendor',
    'product_type': 'product_type',
    'handle': 'handle',
    'status': 'status',
    'shopify_created_at': 'created_at',
    'shopify_updated_at': 'updated_at',
    'published_at': 'published_at',
}

_DATETIME_COLUMNS = frozenset({'shopify_created_at', 'shopify_updated_at', 'published_at'})

# Keys a complete Shopify product carries. Only a payload with all of them is
# trusted to (a) overwrite raw_data — the blob the agent reads variants and
# pricing out of — and (b) create a brand-new product row.
_FULL_PAYLOAD_KEYS = frozenset(_COLUMN_SOURCE_KEYS.values()) | {'id', 'variants'}

# Rows per INSERT statement. A Shopify page is 250 products and the whole page
# rides one transaction regardless, so this only bounds statement size.
UPSERT_CHUNK_SIZE = 250


def get_embedding_service():
    """Lazy load embedding service. Retries on every call so a transient
    failure (IAM propagation delay, cold start) doesn't permanently disable
    embeddings for the lifetime of this instance."""
    global _embedding_service
    if _embedding_service is None and settings.ENABLE_EMBEDDINGS:
        try:
            from app.services.embedding_service import get_embedding_service as _get_svc
            _embedding_service = _get_svc()
            logger.info("✅ Embedding service initialized")
        except Exception as e:
            logger.warning(f"⚠️ Embedding service not available: {e}")
    return _embedding_service


def _parse_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None


def is_full_product_payload(product_data: dict) -> bool:
    """True when the payload carries every field a complete Shopify product has.

    Anything less is a partial: safe to update the fields it does carry, never
    safe to overwrite raw_data with or to create a new row from.
    """
    return _FULL_PAYLOAD_KEYS.issubset(product_data.keys())


def missing_product_keys(product_data: dict) -> List[str]:
    """Which of the expected product keys a payload is missing (for logging)."""
    return sorted(_FULL_PAYLOAD_KEYS - set(product_data.keys()))


def _parse_product_columns(product_data: dict) -> dict:
    """Map a Shopify payload to DB columns, skipping columns the payload
    doesn't mention. raw_data is written only for a complete payload."""
    row = {'shopify_product_id': product_data.get('id')}

    for column, key in _COLUMN_SOURCE_KEYS.items():
        if key not in product_data:
            continue
        value = product_data[key]
        row[column] = _parse_datetime(value) if column in _DATETIME_COLUMNS else value

    if is_full_product_payload(product_data):
        row['raw_data'] = product_data

    return row


def upsert_product(db: Session, merchant: ShopifyStore, product_data: dict, precomputed_embedding: Optional[List] = None) -> Product:
    """Insert or update a single product in the database.

    Single-product path, used by the products/create and products/update
    webhooks. Bulk callers should use sync_products(), which writes a whole
    page in one statement instead of one round trip per product.

    Raises ValueError if the payload has no product id, or if it is incomplete
    and names a product we don't already hold — see is_full_product_payload().
    """
    if product_data.get('id') is None:
        raise ValueError("Cannot sync a product payload with no id")

    # Only write the columns this payload actually carries, so a truncated body
    # can refresh what it mentions without blanking the rest of the row.
    parsed_data = _parse_product_columns(product_data)

    # Set FK to shopify_stores table
    parsed_data['store_id'] = merchant.id

    # Set denormalized merchant_id for fast multi-tenant queries
    parsed_data['merchant_id'] = merchant.merchant_id

    # Use precomputed embedding if provided, otherwise generate individually
    embedding = precomputed_embedding
    if embedding is None and settings.ENABLE_EMBEDDINGS:
        try:
            emb_service = get_embedding_service()
            if emb_service:
                product_text = emb_service.prepare_product_text(product_data)
                embedding = emb_service.generate_embedding(product_text)
                if embedding:
                    logger.debug(f"Generated embedding for product {product_data.get('id')}")
        except Exception as e:
            logger.warning(f"Failed to generate embedding for product {product_data.get('id')}: {e}")

    if embedding:
        parsed_data['embedding'] = embedding

    try:
        product = db.query(Product).filter(
            Product.shopify_product_id == parsed_data['shopify_product_id']
        ).first()

        if product is None:
            if _is_partial_row(parsed_data):
                # Creating from a partial payload would leave a row with no
                # raw_data — no variants, no pricing for the agent to read.
                raise ValueError(
                    f"Refusing to create product {parsed_data['shopify_product_id']} "
                    f"from an incomplete payload (missing {missing_product_keys(product_data)})"
                )
            product = Product(**parsed_data)
            db.add(product)
        else:
            if _is_partial_row(parsed_data):
                logger.warning(
                    f"Product {parsed_data['shopify_product_id']} arrived incomplete "
                    f"(missing {missing_product_keys(product_data)}); updating only "
                    f"the fields it carries"
                )
            # Update all fields — includes store_id/merchant_id so ownership
            # transfers correctly when a shop reconnects under a different merchant
            for key, val in parsed_data.items():
                setattr(product, key, val)

        db.commit()
    except Exception:
        db.rollback()
        raise

    return product


def _generate_page_embeddings(products_data: List[dict]) -> Dict:
    """Batch-generate embeddings for a page of products, keyed by product id.

    Returns an empty map when embeddings are disabled or the batch call fails.
    A missing entry means "no embedding this round" — the upsert leaves any
    stored vector alone rather than clobbering it with NULL.
    """
    if not settings.ENABLE_EMBEDDINGS:
        return {}

    try:
        emb_service = get_embedding_service()
        if not emb_service:
            return {}

        product_ids = [pd.get('id') for pd in products_data]
        texts = [emb_service.prepare_product_text(pd) for pd in products_data]
        batch_embeddings = emb_service.generate_embeddings_batch(texts)

        embeddings_map = {
            pid: emb
            for pid, emb in zip(product_ids, batch_embeddings)
            if pid is not None and emb is not None
        }
        logger.info(f"Batch-generated {len(embeddings_map)}/{len(products_data)} embeddings")
        return embeddings_map
    except Exception as e:
        # Products that already have a vector keep it; brand-new products land
        # without one and are picked up by scripts/backfill_embeddings.py.
        logger.warning(
            f"Batch embedding generation failed for page of {len(products_data)} products: {e}"
        )
        return {}


def _prepare_product_rows(
    merchant: ShopifyStore,
    products_data: List[dict],
    embeddings_map: Dict
) -> tuple:
    """Parse a page of Shopify products into rows ready for a bulk upsert.

    Deduplicated by shopify_product_id (last occurrence wins): a single
    INSERT ... ON CONFLICT cannot touch the same row twice, so a page that
    repeats an id would abort the whole statement.

    Returns (rows, skipped_count) where skipped counts products with no id.
    """
    rows_by_id = {}
    skipped = 0

    for product_data in products_data:
        product_id = product_data.get('id')
        if product_id is None:
            skipped += 1
            logger.error("Skipping product with no id in sync payload")
            continue

        row = _parse_product_columns(product_data)
        row['store_id'] = merchant.id
        row['merchant_id'] = merchant.merchant_id
        # None is meaningful here — see _execute_upsert's COALESCE.
        row['embedding'] = embeddings_map.get(product_id)
        rows_by_id[product_id] = row

    return list(rows_by_id.values()), skipped


def _is_partial_row(row: dict) -> bool:
    """A row built from an incomplete payload. It may update the columns it
    carries but must never create a product — see sync_products()."""
    return 'raw_data' not in row


def _execute_upsert(db: Session, rows: List[dict]) -> None:
    """Write prepared rows with INSERT ... ON CONFLICT DO UPDATE. Does not commit.

    The DO UPDATE SET lists only the columns the row actually carries, so a
    column the payload never mentioned keeps whatever is already stored. Rows
    are grouped by column signature because one INSERT statement needs a
    uniform column list — a page from a single Shopify fetch is one group.
    """
    table = Product.__table__

    groups = {}
    for row in rows:
        groups.setdefault(frozenset(row.keys()), []).append(row)

    for columns, group in groups.items():
        for start in range(0, len(group), UPSERT_CHUNK_SIZE):
            chunk = group[start:start + UPSERT_CHUNK_SIZE]
            stmt = pg_insert(table).values(chunk)

            set_ = {
                col: getattr(stmt.excluded, col)
                for col in columns
                if col not in ('shopify_product_id', 'embedding')
            }
            # Never overwrite a stored vector with NULL: if this page's embedding
            # batch failed, each product keeps the embedding it already had.
            set_['embedding'] = func.coalesce(stmt.excluded.embedding, table.c.embedding)
            # Column-level onupdate defaults don't fire for ON CONFLICT DO UPDATE,
            # so stamp the sync timestamps explicitly.
            set_['synced_at'] = func.now()
            set_['updated_at'] = func.now()

            db.execute(
                stmt.on_conflict_do_update(
                    index_elements=['shopify_product_id'],
                    set_=set_
                )
            )


def _sync_products_individually(
    db: Session,
    rows: List[dict],
    existing_ids: set,
    stats: Dict
) -> Dict:
    """Fallback for a page whose bulk upsert failed: same statement, one row
    and one commit at a time, so a single bad product can't cost the page."""
    for row in rows:
        try:
            _execute_upsert(db, [row])
            db.commit()
            stats['synced_count'] += 1
            if row['shopify_product_id'] in existing_ids:
                stats['updated_count'] += 1
            else:
                stats['created_count'] += 1
        except Exception as e:
            db.rollback()
            stats['failed_count'] += 1
            logger.error(f"Error syncing product {row['shopify_product_id']}: {str(e)}")

    return stats


def sync_products(db: Session, merchant: ShopifyStore, products_data: List[dict]) -> Dict:
    """Sync a page of products to Postgres in a single round trip.

    One embedding batch, one SELECT to classify created-vs-updated, and one
    INSERT ... ON CONFLICT for the whole page, committed once. The previous
    implementation looked each product up twice (once here, once inside
    upsert_product) and committed and refreshed per product — roughly five
    round trips per product, or 1,250 for a full 250-product page.
    """
    stats = {
        'synced_count': 0,
        'created_count': 0,
        'updated_count': 0,
        'failed_count': 0,
        'skipped_partial_count': 0
    }

    if not products_data:
        return stats

    embeddings_map = _generate_page_embeddings(products_data)
    rows, skipped = _prepare_product_rows(merchant, products_data, embeddings_map)
    stats['failed_count'] += skipped

    if not rows:
        return stats

    page_ids = [row['shopify_product_id'] for row in rows]
    existing_ids = {
        pid for (pid,) in db.query(Product.shopify_product_id).filter(
            Product.shopify_product_id.in_(page_ids)
        ).all()
    }

    # A partial payload may refresh the columns it carries on a product we
    # already hold, but must never create one — that would leave a row with no
    # raw_data, i.e. a product the agent can't read variants or pricing from.
    writable_rows = []
    for row in rows:
        if _is_partial_row(row) and row['shopify_product_id'] not in existing_ids:
            stats['skipped_partial_count'] += 1
            continue
        writable_rows.append(row)

    if stats['skipped_partial_count']:
        logger.warning(
            f"Skipped {stats['skipped_partial_count']} unknown products from an "
            f"incomplete payload for merchant {merchant.merchant_id} — a partial "
            f"payload can update an existing product but cannot create one"
        )

    partial_updates = sum(1 for row in writable_rows if _is_partial_row(row))
    if partial_updates:
        sample = next((pd for pd in products_data if not is_full_product_payload(pd)), {})
        logger.warning(
            f"{partial_updates}/{len(writable_rows)} products for merchant "
            f"{merchant.merchant_id} came from an incomplete payload (missing "
            f"{missing_product_keys(sample)}); updating only the fields it "
            f"carries and leaving raw_data intact"
        )

    if not writable_rows:
        return stats

    try:
        _execute_upsert(db, writable_rows)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(
            f"Bulk upsert failed for page of {len(writable_rows)} products, "
            f"falling back to per-product writes: {str(e)}"
        )
        return _sync_products_individually(db, writable_rows, existing_ids, stats)

    written_ids = [row['shopify_product_id'] for row in writable_rows]
    stats['synced_count'] = len(writable_rows)
    stats['created_count'] = sum(1 for pid in written_ids if pid not in existing_ids)
    stats['updated_count'] = len(writable_rows) - stats['created_count']

    return stats


def sync_single_product(db: Session, merchant: ShopifyStore, product_data: dict) -> Dict:
    """Sync a single product and return sync status"""
    if 'product' in product_data:
        product_data = product_data['product']

    # id-only query — no need to drag raw_data and the 768-dim vector back
    # just to answer "does this row exist?"
    is_update = db.query(Product.id).filter(
        Product.shopify_product_id == product_data.get('id')
    ).first() is not None

    try:
        upsert_product(db, merchant, product_data)
        return {
            'synced_count': 1,
            'created_count': 0 if is_update else 1,
            'updated_count': 1 if is_update else 0,
            'failed_count': 0,
            'skipped_partial_count': 0
        }
    except Exception as e:
        logger.error(f"Error syncing product {product_data.get('id')}: {str(e)}")
        return {
            'synced_count': 0,
            'created_count': 0,
            'updated_count': 0,
            'failed_count': 1,
            'skipped_partial_count': 0
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
        'skipped_partial_count': 0,
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

                # sync_products blocks (Postgres round trip + a synchronous
                # Vertex embedding call for the whole page), so keep it off the
                # event loop — this runs inside a FastAPI request/background task.
                batch_stats = await asyncio.to_thread(sync_products, db, merchant, products)

                total_stats['synced_count'] += batch_stats['synced_count']
                total_stats['created_count'] += batch_stats['created_count']
                total_stats['updated_count'] += batch_stats['updated_count']
                total_stats['failed_count'] += batch_stats['failed_count']
                total_stats['skipped_partial_count'] += batch_stats.get('skipped_partial_count', 0)
                total_stats['total_products'] += len(products)

                logger.info(f"Synced page {total_stats['pages_fetched']}: {batch_stats['synced_count']}/{len(products)} products")

                # Stop rather than grind through the rest of the catalog when a
                # whole page writes nothing — a systemically bad payload shape
                # or a database that has stopped accepting writes.
                if batch_stats['synced_count'] == 0:
                    logger.error(
                        f"Page {total_stats['pages_fetched']} wrote 0 of {len(products)} "
                        f"products for merchant {merchant.merchant_id}; aborting the run "
                        f"instead of continuing"
                    )
                    total_stats['error'] = (
                        f"Aborted after page {total_stats['pages_fetched']} wrote no products"
                    )
                    break

                if len(products) < limit:
                    break

                since_id = products[-1]['id']
                await asyncio.sleep(0.5)

        total_stats['duration_seconds'] = round(time.time() - start_time, 2)

        unwritten = total_stats['failed_count'] + total_stats['skipped_partial_count']
        if unwritten > 0 and total_stats['synced_count'] == 0:
            total_stats['status'] = 'failed'
        elif unwritten > 0 or total_stats.get('error'):
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

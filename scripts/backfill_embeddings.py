"""
Batch Embedding Backfill Script

Generates embeddings for all existing products in the database.
Run this after migration 004 to populate embeddings for existing products.

Usage:
    python scripts/backfill_embeddings.py [--merchant-id MERCHANT_ID] [--batch-size 100] [--dry-run]

Examples:
    # Backfill all merchants
    python scripts/backfill_embeddings.py

    # Backfill specific merchant
    python scripts/backfill_embeddings.py --merchant-id pu-oauth-testing

    # Dry run (show what would be done)
    python scripts/backfill_embeddings.py --dry-run

    # Custom batch size
    python scripts/backfill_embeddings.py --batch-size 50
"""

import sys
import os
import argparse
import logging
from typing import List, Dict
from sqlalchemy import text

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import get_db
from app.models import Product
from app.services.embedding_service import get_embedding_service
from app.config import settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def get_products_without_embeddings(db, merchant_id: str = None, limit: int = None) -> List[Product]:
    """
    Fetch products that don't have embeddings yet.

    Args:
        db: Database session
        merchant_id: Optional merchant filter
        limit: Optional limit for testing

    Returns:
        List of Product objects
    """
    query = db.query(Product).filter(
        Product.embedding.is_(None),
        Product.status == 'active',
        Product.is_deleted == 0
    )

    if merchant_id:
        query = query.filter(Product.merchant_id == merchant_id)

    if limit:
        query = query.limit(limit)

    return query.all()


def backfill_embeddings_batch(
    db,
    products: List[Product],
    batch_size: int = 100,
    dry_run: bool = False
) -> Dict[str, int]:
    """
    Generate embeddings for a batch of products.

    Args:
        db: Database session
        products: List of products to process
        batch_size: Number of products to process in one API call
        dry_run: If True, don't actually update database

    Returns:
        Statistics dict
    """
    stats = {
        'total': len(products),
        'processed': 0,
        'success': 0,
        'failed': 0,
        'skipped': 0
    }

    if not products:
        logger.info("No products to process")
        return stats

    logger.info(f"Processing {len(products)} products...")

    # Get embedding service
    try:
        embedding_service = get_embedding_service()
    except Exception as e:
        logger.error(f"Failed to initialize embedding service: {e}")
        stats['failed'] = len(products)
        return stats

    # Process in batches for efficiency
    for i in range(0, len(products), batch_size):
        batch = products[i:i + batch_size]
        batch_num = (i // batch_size) + 1
        total_batches = (len(products) + batch_size - 1) // batch_size

        logger.info(f"Processing batch {batch_num}/{total_batches} ({len(batch)} products)...")

        # Prepare texts for embedding generation
        texts = []
        valid_products = []

        for product in batch:
            try:
                if product.raw_data:
                    text = embedding_service.prepare_product_text(product.raw_data)
                    if text:
                        texts.append(text)
                        valid_products.append(product)
                    else:
                        logger.warning(f"Empty text for product {product.shopify_product_id}")
                        stats['skipped'] += 1
                else:
                    logger.warning(f"No raw_data for product {product.shopify_product_id}")
                    stats['skipped'] += 1
            except Exception as e:
                logger.error(f"Error preparing product {product.shopify_product_id}: {e}")
                stats['failed'] += 1

        if not texts:
            logger.warning(f"Batch {batch_num}: No valid texts to process")
            continue

        # Generate embeddings in batch
        try:
            embeddings = embedding_service.generate_embeddings_batch(texts, batch_size=250)

            # Update products with embeddings
            for product, embedding in zip(valid_products, embeddings):
                stats['processed'] += 1

                if embedding is None:
                    logger.warning(f"Failed to generate embedding for product {product.shopify_product_id}")
                    stats['failed'] += 1
                    continue

                if dry_run:
                    logger.info(f"[DRY RUN] Would update product {product.shopify_product_id} with embedding")
                    stats['success'] += 1
                else:
                    try:
                        # Update product with embedding
                        product.embedding = embedding
                        db.commit()
                        stats['success'] += 1
                        logger.debug(f"✅ Updated product {product.shopify_product_id}")
                    except Exception as e:
                        logger.error(f"Failed to save embedding for product {product.shopify_product_id}: {e}")
                        db.rollback()
                        stats['failed'] += 1

            logger.info(f"✅ Batch {batch_num}/{total_batches} complete: {stats['success']}/{stats['total']} successful")

        except Exception as e:
            logger.error(f"Error processing batch {batch_num}: {e}")
            stats['failed'] += len(valid_products)
            continue

    return stats


def get_merchant_stats(db):
    """Get statistics about embeddings per merchant"""
    query = text("""
        SELECT
            merchant_id,
            COUNT(*) as total_products,
            COUNT(embedding) as with_embeddings,
            COUNT(*) - COUNT(embedding) as without_embeddings,
            ROUND(100.0 * COUNT(embedding) / COUNT(*), 1) as coverage_pct
        FROM shopify_sync.products
        WHERE status = 'active' AND is_deleted = 0
        GROUP BY merchant_id
        ORDER BY total_products DESC
    """)

    result = db.execute(query)
    return result.fetchall()


def main():
    parser = argparse.ArgumentParser(
        description='Backfill embeddings for existing products',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        '--merchant-id',
        type=str,
        help='Process only this merchant (default: all merchants)'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=100,
        help='Number of products per batch (default: 100)'
    )
    parser.add_argument(
        '--limit',
        type=int,
        help='Limit total products to process (for testing)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without making changes'
    )
    parser.add_argument(
        '--stats-only',
        action='store_true',
        help='Show statistics only, do not process'
    )

    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Embedding Backfill Script")
    logger.info("=" * 60)

    if not settings.ENABLE_EMBEDDINGS:
        logger.error("ENABLE_EMBEDDINGS is False in settings. Set it to True to continue.")
        sys.exit(1)

    if not settings.GCP_PROJECT_ID:
        logger.error("GCP_PROJECT_ID not set. Please configure Google Cloud credentials.")
        sys.exit(1)

    # Get database session
    db = next(get_db())

    try:
        # Show current statistics
        logger.info("\n📊 Current Embedding Coverage:")
        logger.info("-" * 60)
        stats = get_merchant_stats(db)
        for row in stats:
            logger.info(
                f"{row.merchant_id:20} | "
                f"Total: {row.total_products:3} | "
                f"With Embeddings: {row.with_embeddings:3} | "
                f"Missing: {row.without_embeddings:3} | "
                f"Coverage: {row.coverage_pct:5.1f}%"
            )
        logger.info("-" * 60)

        if args.stats_only:
            logger.info("\nStats-only mode. Exiting.")
            return

        # Get products without embeddings
        products = get_products_without_embeddings(
            db,
            merchant_id=args.merchant_id,
            limit=args.limit
        )

        if not products:
            logger.info("\n✅ All products already have embeddings!")
            return

        logger.info(f"\n📦 Found {len(products)} products without embeddings")

        if args.merchant_id:
            logger.info(f"🏪 Filtering by merchant: {args.merchant_id}")

        if args.dry_run:
            logger.info("🧪 DRY RUN MODE - No changes will be made")

        # Confirm before proceeding
        if not args.dry_run:
            response = input(f"\nProceed with backfilling {len(products)} products? [y/N]: ")
            if response.lower() != 'y':
                logger.info("Cancelled by user")
                return

        # Process embeddings
        logger.info(f"\n🚀 Starting backfill with batch size {args.batch_size}...")
        logger.info("-" * 60)

        result_stats = backfill_embeddings_batch(
            db,
            products,
            batch_size=args.batch_size,
            dry_run=args.dry_run
        )

        # Show final results
        logger.info("\n" + "=" * 60)
        logger.info("📈 Backfill Results:")
        logger.info("=" * 60)
        logger.info(f"Total products:     {result_stats['total']}")
        logger.info(f"Processed:          {result_stats['processed']}")
        logger.info(f"Successfully updated: {result_stats['success']}")
        logger.info(f"Failed:             {result_stats['failed']}")
        logger.info(f"Skipped:            {result_stats['skipped']}")
        logger.info("=" * 60)

        if not args.dry_run and result_stats['success'] > 0:
            logger.info("\n📊 Updated Embedding Coverage:")
            logger.info("-" * 60)
            updated_stats = get_merchant_stats(db)
            for row in updated_stats:
                logger.info(
                    f"{row.merchant_id:20} | "
                    f"Total: {row.total_products:3} | "
                    f"With Embeddings: {row.with_embeddings:3} | "
                    f"Missing: {row.without_embeddings:3} | "
                    f"Coverage: {row.coverage_pct:5.1f}%"
                )
            logger.info("-" * 60)

        logger.info("\n✅ Backfill complete!")

    except KeyboardInterrupt:
        logger.info("\n\n⚠️  Interrupted by user. Rolling back current transaction...")
        db.rollback()
        sys.exit(1)
    except Exception as e:
        logger.error(f"\n❌ Error: {e}")
        db.rollback()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()

# Database Migrations

This directory contains SQL migration scripts for database schema changes.

## Migration Order (For Existing Databases)

Run migrations in chronological order:

1. `001_add_webhooks_table.sql` (2025-12-08) - Adds webhooks tracking table
2. `001_add_soft_delete_fields.sql` (2025-12-08) - Adds soft delete to products
3. `002_rename_merchants_to_shopify_stores.sql` (2026-01-13) - Renames merchants → shopify_stores
4. `003_rename_to_store_id_and_denormalize_merchant_id.sql` (2026-01-13) - **BREAKING**: Multi-tenant optimization
5. `004_add_vector_embeddings.sql` (2026-01-13) - Adds pgvector for semantic product search

## Fresh Installation

For **new databases**, skip migrations and use ORM:

```bash
python init_db.py
```

This creates all tables with the latest schema.

## How to Run Migrations

```bash
# Connect to PostgreSQL
psql -h your-host -U your-user -d your-database

# Run migrations in order
\i migrations/001_add_webhooks_table.sql
\i migrations/001_add_soft_delete_fields.sql
\i migrations/002_rename_merchants_to_shopify_stores.sql
\i migrations/003_rename_to_store_id_and_denormalize_merchant_id.sql
\i migrations/004_add_vector_embeddings.sql
```

Or using environment variables:

```bash
# Load from .env
export DB_HOST="your-host"
export DB_USER="your-user"
export DB_NAME="your-database"

# Run migration
psql -h $DB_HOST -U $DB_USER -d $DB_NAME -f migrations/003_rename_to_store_id_and_denormalize_merchant_id.sql
```

## Migration Files

### 001_add_webhooks_table.sql (2025-12-08)
Adds `shopify_sync.webhooks` table for tracking webhook subscriptions with Shopify.

### 001_add_soft_delete_fields.sql (2025-12-08)
Adds `is_deleted` and `deleted_at` columns to products table for soft delete functionality.

### 002_rename_merchants_to_shopify_stores.sql (2026-01-13)
**Breaking Change:** Renames `shopify_sync.merchants` → `shopify_sync.shopify_stores` for clarity.
- Updates all indexes and foreign keys
- No backward compatibility

### 003_rename_to_store_id_and_denormalize_merchant_id.sql (2026-01-13)
**Breaking Change:** Implements industry-standard multi-tenant pattern.

**Changes:**
- `products.merchant_id` (INT FK) → `products.store_id` (INT FK)
- Adds `products.merchant_id` (VARCHAR) - denormalized tenant ID
- `webhooks.merchant_id` (INT FK) → `webhooks.store_id` (INT FK)
- Adds `webhooks.merchant_id` (VARCHAR) - denormalized tenant ID
- Creates triggers to auto-populate denormalized fields

**Benefits:**
- ✅ Eliminates JOINs for tenant queries (massive performance gain)
- ✅ Follows industry patterns (Shopify, Stripe, AWS)
- ✅ Clear semantics: `store_id` = FK, `merchant_id` = tenant ID

**Query Before:**
```sql
SELECT * FROM shopify_sync.products p
JOIN shopify_sync.shopify_stores s ON p.merchant_id = s.id
WHERE s.merchant_id = 'by-kind';
```

**Query After:**
```sql
SELECT * FROM shopify_sync.products
WHERE merchant_id = 'by-kind';  -- No JOIN!
```

## Rollback

**Migration 001 (Webhooks):**
```sql
DROP TABLE IF EXISTS shopify_sync.webhooks CASCADE;
```

**Migration 001 (Soft Delete):**
```sql
DROP INDEX IF EXISTS shopify_sync.idx_products_is_deleted;
ALTER TABLE shopify_sync.products DROP COLUMN IF EXISTS deleted_at;
ALTER TABLE shopify_sync.products DROP COLUMN IF EXISTS is_deleted;
```

**Migration 002 (Rename Table):**
```sql
ALTER TABLE shopify_sync.shopify_stores RENAME TO merchants;
-- Update all code references back to "merchants"
```

**Migration 003 (Store ID):**
Migration 003 is a breaking change with no automatic rollback. Requires manual intervention and code updates.

### 004_add_vector_embeddings.sql (2026-01-13)
Adds vector embedding support for semantic product search using pgvector and Vertex AI.

**Changes:**
- Installs pgvector extension
- Adds `products.embedding` column (vector(768))
- Creates HNSW index for fast cosine similarity search
- Adds helper function for semantic search

**Benefits:**
- ✅ Reduces LLM token usage by 90-95% (300 products → 10-20 relevant)
- ✅ Semantic search (understands meaning, not just keywords)
- ✅ Scales to millions of products
- ✅ Sub-millisecond query performance with HNSW index

**Post-Migration Steps:**
1. Install dependencies: `pip install pgvector google-cloud-aiplatform`
2. Set environment variables: `GCP_PROJECT_ID`, `GOOGLE_APPLICATION_CREDENTIALS`
3. Run backfill script: `python scripts/backfill_embeddings.py`
4. Replace `database_products_loader` with `semantic_product_search` in Langflow

**Example Query:**
```sql
-- Find products similar to "red summer dress"
SELECT * FROM shopify_sync.search_products_semantic(
    'merchant-id',
    (SELECT embedding FROM shopify_sync.products WHERE title ILIKE '%red dress%' LIMIT 1),
    20,  -- max results
    0.5  -- similarity threshold
);
```

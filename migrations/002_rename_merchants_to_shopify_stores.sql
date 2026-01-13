-- Migration: Rename merchants table to shopify_stores
-- Date: 2026-01-13
-- Description: Breaking change - rename shopify_sync.merchants to shopify_sync.shopify_stores for clarity
--              This table stores Shopify OAuth credentials and store connections, not general merchant data
--              The authoritative merchant entity data remains in public.merchants table

-- Step 1: Rename the table
ALTER TABLE shopify_sync.merchants RENAME TO shopify_stores;

-- Step 2: Update table comment for clarity
COMMENT ON TABLE shopify_sync.shopify_stores IS 'Stores Shopify OAuth credentials and API connection data for multi-tenant Shopify integration';

-- Note: PostgreSQL automatically handles:
-- - Primary key constraint renaming
-- - Index renaming (though they keep old names unless explicitly renamed)
-- - Foreign key references from other tables (webhooks table FK will still work)
-- - Sequence renaming for SERIAL columns

-- Optional: Rename indexes for consistency (PostgreSQL doesn't auto-rename these)
ALTER INDEX IF EXISTS shopify_sync.merchants_pkey RENAME TO shopify_stores_pkey;
ALTER INDEX IF EXISTS shopify_sync.merchants_merchant_id_key RENAME TO shopify_stores_merchant_id_key;
ALTER INDEX IF EXISTS shopify_sync.merchants_shop_domain_key RENAME TO shopify_stores_shop_domain_key;

-- Optional: Rename sequence
ALTER SEQUENCE IF EXISTS shopify_sync.merchants_id_seq RENAME TO shopify_stores_id_seq;

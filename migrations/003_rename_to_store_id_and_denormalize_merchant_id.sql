-- Migration: Rename FK columns to store_id and denormalize merchant_id for multi-tenancy
-- Date: 2026-01-13
-- Description: Breaking change - Implement industry-standard multi-tenant pattern
--              - Rename products.merchant_id → products.store_id (INTEGER FK)
--              - Add products.merchant_id (VARCHAR) - denormalized tenant identifier
--              - Rename webhooks.merchant_id → webhooks.store_id (INTEGER FK)
--              - Add webhooks.merchant_id (VARCHAR) - denormalized tenant identifier
--              - Create triggers to auto-populate denormalized merchant_id fields

-- ============================================================================
-- STEP 1: Update Products Table
-- ============================================================================

-- Add new denormalized merchant_id column (VARCHAR)
ALTER TABLE shopify_sync.products
ADD COLUMN merchant_id_new VARCHAR(255);

-- Backfill merchant_id from shopify_stores
UPDATE shopify_sync.products p
SET merchant_id_new = s.merchant_id
FROM shopify_sync.shopify_stores s
WHERE p.merchant_id = s.id;

-- Rename old merchant_id to store_id
ALTER TABLE shopify_sync.products
RENAME COLUMN merchant_id TO store_id;

-- Rename new merchant_id_new to merchant_id
ALTER TABLE shopify_sync.products
RENAME COLUMN merchant_id_new TO merchant_id;

-- Make merchant_id NOT NULL
ALTER TABLE shopify_sync.products
ALTER COLUMN merchant_id SET NOT NULL;

-- Add index on merchant_id for fast tenant queries
CREATE INDEX idx_products_merchant_id ON shopify_sync.products(merchant_id);

-- Add composite index for common query pattern
CREATE INDEX idx_products_merchant_id_status_active ON shopify_sync.products(merchant_id, status)
WHERE is_deleted = 0;

-- Update column comments
COMMENT ON COLUMN shopify_sync.products.store_id IS 'Foreign key to shopify_stores.id (internal store reference)';
COMMENT ON COLUMN shopify_sync.products.merchant_id IS 'Denormalized merchant identifier for fast multi-tenant queries (matches public.merchants.merchant_id)';

-- ============================================================================
-- STEP 2: Update Webhooks Table
-- ============================================================================

-- Add new denormalized merchant_id column (VARCHAR)
ALTER TABLE shopify_sync.webhooks
ADD COLUMN merchant_id_new VARCHAR(255);

-- Backfill merchant_id from shopify_stores
UPDATE shopify_sync.webhooks w
SET merchant_id_new = s.merchant_id
FROM shopify_sync.shopify_stores s
WHERE w.merchant_id = s.id;

-- Rename old merchant_id to store_id
ALTER TABLE shopify_sync.webhooks
RENAME COLUMN merchant_id TO store_id;

-- Rename new merchant_id_new to merchant_id
ALTER TABLE shopify_sync.webhooks
RENAME COLUMN merchant_id_new TO merchant_id;

-- Make merchant_id NOT NULL
ALTER TABLE shopify_sync.webhooks
ALTER COLUMN merchant_id SET NOT NULL;

-- Add index on merchant_id
CREATE INDEX idx_webhooks_merchant_id ON shopify_sync.webhooks(merchant_id);

-- Update column comments
COMMENT ON COLUMN shopify_sync.webhooks.store_id IS 'Foreign key to shopify_stores.id (internal store reference)';
COMMENT ON COLUMN shopify_sync.webhooks.merchant_id IS 'Denormalized merchant identifier for fast multi-tenant queries (matches public.merchants.merchant_id)';

-- ============================================================================
-- STEP 3: Create Triggers to Auto-Populate merchant_id
-- ============================================================================

-- Trigger function for products table
CREATE OR REPLACE FUNCTION shopify_sync.sync_product_merchant_id()
RETURNS TRIGGER AS $$
BEGIN
    -- Auto-populate merchant_id from shopify_stores when store_id is set
    IF NEW.store_id IS NOT NULL THEN
        SELECT merchant_id INTO NEW.merchant_id
        FROM shopify_sync.shopify_stores
        WHERE id = NEW.store_id;

        IF NEW.merchant_id IS NULL THEN
            RAISE EXCEPTION 'Cannot find merchant_id for store_id %', NEW.store_id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger on products table
CREATE TRIGGER before_product_insert_or_update
    BEFORE INSERT OR UPDATE OF store_id ON shopify_sync.products
    FOR EACH ROW
    EXECUTE FUNCTION shopify_sync.sync_product_merchant_id();

-- Trigger function for webhooks table
CREATE OR REPLACE FUNCTION shopify_sync.sync_webhook_merchant_id()
RETURNS TRIGGER AS $$
BEGIN
    -- Auto-populate merchant_id from shopify_stores when store_id is set
    IF NEW.store_id IS NOT NULL THEN
        SELECT merchant_id INTO NEW.merchant_id
        FROM shopify_sync.shopify_stores
        WHERE id = NEW.store_id;

        IF NEW.merchant_id IS NULL THEN
            RAISE EXCEPTION 'Cannot find merchant_id for store_id %', NEW.store_id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger on webhooks table
CREATE TRIGGER before_webhook_insert_or_update
    BEFORE INSERT OR UPDATE OF store_id ON shopify_sync.webhooks
    FOR EACH ROW
    EXECUTE FUNCTION shopify_sync.sync_webhook_merchant_id();

-- ============================================================================
-- VERIFICATION QUERIES (Run these after migration to verify)
-- ============================================================================

-- Verify products have merchant_id populated
-- SELECT COUNT(*) as total,
--        COUNT(merchant_id) as with_merchant_id,
--        COUNT(store_id) as with_store_id
-- FROM shopify_sync.products;

-- Verify webhooks have merchant_id populated
-- SELECT COUNT(*) as total,
--        COUNT(merchant_id) as with_merchant_id,
--        COUNT(store_id) as with_store_id
-- FROM shopify_sync.webhooks;

-- Test query performance (should be fast without JOIN)
-- EXPLAIN ANALYZE
-- SELECT COUNT(*) FROM shopify_sync.products
-- WHERE merchant_id = 'by-kind' AND status = 'active' AND is_deleted = 0;

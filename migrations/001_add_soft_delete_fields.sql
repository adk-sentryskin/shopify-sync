-- Migration: Add soft delete fields to products table
-- Date: 2025-12-08
-- Description: Adds is_deleted and deleted_at columns for soft delete functionality

-- Add is_deleted column (0=active, 1=deleted)
ALTER TABLE shopify_sync.products
ADD COLUMN IF NOT EXISTS is_deleted INTEGER DEFAULT 0;

-- Add deleted_at column (timestamp when product was deleted)
ALTER TABLE shopify_sync.products
ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE;

-- Set is_deleted to 0 for all existing products (they are all active)
UPDATE shopify_sync.products
SET is_deleted = 0
WHERE is_deleted IS NULL;

-- Add index on is_deleted for faster queries
CREATE INDEX IF NOT EXISTS idx_products_is_deleted
ON shopify_sync.products(is_deleted);

-- Add comment to table
COMMENT ON COLUMN shopify_sync.products.is_deleted IS 'Soft delete flag: 0=active, 1=deleted';
COMMENT ON COLUMN shopify_sync.products.deleted_at IS 'Timestamp when product was soft deleted in Shopify';

-- Migration: Add webhooks table for tracking webhook registrations
-- Date: 2025-12-08
-- Description: Industry standard webhook tracking to avoid duplicate registrations

CREATE TABLE IF NOT EXISTS shopify_sync.webhooks (
    id SERIAL PRIMARY KEY,
    merchant_id INTEGER NOT NULL REFERENCES shopify_sync.shopify_stores(id) ON DELETE CASCADE,
    shopify_webhook_id BIGINT NOT NULL UNIQUE,
    topic VARCHAR(100) NOT NULL,
    address VARCHAR(500) NOT NULL,
    format VARCHAR(20) DEFAULT 'json',
    is_active INTEGER DEFAULT 1,
    last_verified_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE
);

-- Create indexes for better query performance
CREATE INDEX IF NOT EXISTS idx_webhooks_merchant_id ON shopify_sync.webhooks(merchant_id);
CREATE INDEX IF NOT EXISTS idx_webhooks_topic ON shopify_sync.webhooks(topic);
CREATE INDEX IF NOT EXISTS idx_webhooks_shopify_webhook_id ON shopify_sync.webhooks(shopify_webhook_id);

-- Add comment to table
COMMENT ON TABLE shopify_sync.webhooks IS 'Tracks webhook subscriptions registered with Shopify for audit trail and idempotency';

-- Add comments to columns
COMMENT ON COLUMN shopify_sync.webhooks.shopify_webhook_id IS 'Webhook ID from Shopify API';
COMMENT ON COLUMN shopify_sync.webhooks.topic IS 'Webhook topic (e.g., products/create)';
COMMENT ON COLUMN shopify_sync.webhooks.address IS 'Full webhook URL endpoint';
COMMENT ON COLUMN shopify_sync.webhooks.is_active IS '1=active, 0=deleted/inactive';
COMMENT ON COLUMN shopify_sync.webhooks.last_verified_at IS 'Last time we verified webhook exists in Shopify';

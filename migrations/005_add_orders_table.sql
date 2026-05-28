-- Migration: Add orders table for agent attribution
-- Date: 2026-05-28
-- Description: Adds shopify_sync.orders for agent-attributed Shopify orders.
--              PII-free by design (no customer name/email/phone/address) to
--              match the Protected Customer Data Level 1 attestation. Rows
--              older than 60 days are pruned by a scheduled job to match
--              the retention period we attested to.

-- ============================================================================
-- STEP 1: Create the orders table
-- ============================================================================

CREATE TABLE IF NOT EXISTS shopify_sync.orders (
    -- Primary key
    id                  SERIAL          PRIMARY KEY,

    -- Shopify identifier (globally unique across Shopify)
    shopify_order_id    BIGINT          NOT NULL UNIQUE,
    order_name          VARCHAR(50),        -- e.g. "#1001"

    -- Multi-tenant identifiers (matches the products-table pattern from 003_*)
    store_id            INTEGER         NOT NULL REFERENCES shopify_sync.shopify_stores(id) ON DELETE CASCADE,
    merchant_id         VARCHAR(255)    NOT NULL,
    shop_domain         VARCHAR(255)    NOT NULL,

    -- Shopify timestamps
    shopify_created_at  TIMESTAMPTZ     NOT NULL,
    shopify_updated_at  TIMESTAMPTZ,

    -- Agent-attribution keys, extracted from order.note_attributes by the
    -- ingest pipeline. chekout_ai_session is the JOIN key with the conversation
    -- (Databricks) dataset that lets the dashboard's Sankey thread cart events
    -- through to purchases.
    chekout_ai_session  VARCHAR(255),
    chekout_ai_agent    VARCHAR(255),
    chekout_ai_source   VARCHAR(100),

    -- Order body — PII-free; field allowlist enforced server-side at fetch time.
    -- Deliberately no customer / email / phone / address columns.
    line_items          JSONB,
    discount_codes      JSONB,
    total_price         NUMERIC(12, 2),
    currency            VARCHAR(10),
    financial_status    VARCHAR(50),
    source_name         VARCHAR(100),
    cart_token          VARCHAR(255),

    -- Full attribution attributes for forensics / future attribute additions
    note_attributes_raw JSONB,

    -- Provenance: which event last touched this row
    -- ('backfill' | 'create' | 'update' | 'cancel')
    last_event_type     VARCHAR(20)     NOT NULL,

    -- Local timestamps
    ingested_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ
);

COMMENT ON TABLE shopify_sync.orders IS
    'Agent-attributed Shopify orders. PII-free per Protected Customer Data Level 1 attestation. '
    '60-day rolling retention enforced by scheduled prune job in app/services/scheduler.py.';

-- ============================================================================
-- STEP 2: Indexes
-- ============================================================================

-- Fast tenant-scoped queries (the dominant access pattern: "all orders for merchant X")
CREATE INDEX IF NOT EXISTS idx_orders_merchant_id
    ON shopify_sync.orders(merchant_id);

-- Retention prune scans by shopify_created_at < now() - 60d
CREATE INDEX IF NOT EXISTS idx_orders_shopify_created_at
    ON shopify_sync.orders(shopify_created_at);

-- Joining to chatbot sessions / looking up "which order matches this session"
CREATE INDEX IF NOT EXISTS idx_orders_chekout_ai_session
    ON shopify_sync.orders(chekout_ai_session)
    WHERE chekout_ai_session IS NOT NULL;

-- Cart-token lookups (debugging, cross-reference with abandoned carts later)
CREATE INDEX IF NOT EXISTS idx_orders_cart_token
    ON shopify_sync.orders(cart_token)
    WHERE cart_token IS NOT NULL;

-- Composite for the dashboard's most common query
-- (merchant scoped + sorted by recency, filtered to paid for revenue widgets)
CREATE INDEX IF NOT EXISTS idx_orders_merchant_created
    ON shopify_sync.orders(merchant_id, shopify_created_at DESC);

-- ============================================================================
-- ROLLBACK
-- ============================================================================

-- DROP TABLE IF EXISTS shopify_sync.orders CASCADE;

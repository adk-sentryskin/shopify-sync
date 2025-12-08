# Database Migrations

This directory contains SQL migration scripts for database schema changes.

## How to Run Migrations

Connect to your PostgreSQL database and execute the SQL files in order:

```bash
# Connect to PostgreSQL
psql -h your-host -U your-user -d your-database

# Run migrations in order
\i migrations/001_add_soft_delete_fields.sql
```

Or using environment variables:

```bash
# Load from .env or set manually
export DB_HOST="your-host"
export DB_USER="your-user"
export DB_NAME="your-database"

# Run migration
psql -h $DB_HOST -U $DB_USER -d $DB_NAME -f migrations/001_add_soft_delete_fields.sql
```

## Migration Files

- **001_add_soft_delete_fields.sql**: Adds `is_deleted` and `deleted_at` columns to products table for soft delete functionality

## Rollback (if needed)

To rollback the soft delete migration:

```sql
DROP INDEX IF EXISTS shopify_sync.idx_products_is_deleted;
ALTER TABLE shopify_sync.products DROP COLUMN IF EXISTS deleted_at;
ALTER TABLE shopify_sync.products DROP COLUMN IF EXISTS is_deleted;
```

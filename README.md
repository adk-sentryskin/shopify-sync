# Shopify Sync Microservice

A Python microservice for handling Shopify OAuth authentication and data synchronization on a per-merchant basis.

## Features

- **Multi-Merchant OAuth**: Each merchant has their own OAuth flow and access tokens
- **Merchant-based Authentication**: Uses `X-Merchant-Id` header for request authentication
- **Shopify API Integration**: Fetch products, orders, customers, and more
- **PostgreSQL Database**: Stores merchant credentials and OAuth tokens securely
- **FastAPI Framework**: Modern, fast, and async API framework
- **Docker Support**: Easy deployment with Docker Compose

## Architecture

- **FastAPI** - Web framework
- **SQLAlchemy** - ORM for database operations
- **PostgreSQL** - Database for storing merchant data
- **httpx** - Async HTTP client for Shopify API calls

## Project Structure

```
shopify-sync/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI application
│   ├── config.py            # Configuration settings
│   ├── database.py          # Database connection
│   ├── models.py            # SQLAlchemy models
│   ├── schemas.py           # Pydantic schemas
│   ├── middleware/
│   │   ├── __init__.py
│   │   └── auth.py          # Merchant authentication
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── oauth.py         # OAuth endpoints
│   │   └── shopify_data.py  # Shopify data endpoints
│   └── services/
│       ├── __init__.py
│       └── shopify_oauth.py # Shopify OAuth service
├── .env                     # Environment variables (create from .env.example)
├── .env.example             # Example environment variables
├── requirements.txt         # Python dependencies
├── Dockerfile               # Docker configuration
├── docker-compose.yml       # Docker Compose setup
├── init_db.py              # Database initialization script
└── run.py                  # Development server runner
```

## Setup Instructions

### Option 1: Using Docker (Recommended)

1. **Clone the repository**
   ```bash
   cd shopify-sync
   ```

2. **Create environment file**
   ```bash
   cp .env.example .env
   ```

3. **Edit .env file** with your Shopify credentials:
   ```env
   DB_DSN=postgresql://shopify:shopify123@localhost:5432/shopify_sync?options=-c%20search_path=shopify_sync
   SHOPIFY_API_KEY=your_shopify_api_key
   SHOPIFY_API_SECRET=your_shopify_api_secret
   OAUTH_REDIRECT_URL=http://localhost:8000/api/oauth/callback
   ```

4. **Start the services**
   ```bash
   docker-compose up -d
   ```

5. **Check logs**
   ```bash
   docker-compose logs -f app
   ```

6. **Access the API**
   - API: http://localhost:8000
   - Documentation: http://localhost:8000/docs
   - Database: localhost:5432

### Option 2: Local Development

1. **Prerequisites**
   - Python 3.11+
   - PostgreSQL 15+
   - pip

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Setup PostgreSQL database**
   ```sql
   CREATE DATABASE shopify_sync;
   CREATE USER shopify WITH PASSWORD 'shopify123';
   GRANT ALL PRIVILEGES ON DATABASE shopify_sync TO shopify;
   ```

5. **Create .env file**
   ```bash
   cp .env.example .env
   ```

   Edit with your configuration:
   ```env
   DB_DSN=postgresql://shopify:shopify123@localhost:5432/shopify_sync?options=-c%20search_path=shopify_sync
   SHOPIFY_API_KEY=your_shopify_api_key
   SHOPIFY_API_SECRET=your_shopify_api_secret
   OAUTH_REDIRECT_URL=http://localhost:8000/api/oauth/callback
   ```

6. **Initialize database**
   ```bash
   python setup_database.py
   ```

7. **Run the application**
   ```bash
   python run.py
   ```

   Or with uvicorn directly:
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

## API Usage

### 1. Initiate OAuth Flow

Start the OAuth process for a merchant:

```bash
curl -X POST "http://localhost:8000/api/oauth/initiate" \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merchant_001",
    "shop_domain": "mystore.myshopify.com"
  }'
```

Response:
```json
{
  "authorization_url": "https://mystore.myshopify.com/admin/oauth/authorize?...",
  "merchant_id": "merchant_001"
}
```

**Action**: Redirect the merchant to the `authorization_url` to complete OAuth.

### 2. OAuth Callback

After the merchant authorizes, Shopify redirects to `/api/oauth/callback` automatically.
The service will exchange the code for an access token and store it.

### 3. Check OAuth Status

```bash
curl "http://localhost:8000/api/oauth/status?merchant_id=merchant_001"
```

### 4. Fetch Shopify Data

All data endpoints require the `X-Merchant-Id` header:

#### Get Products
```bash
curl "http://localhost:8000/api/shopify/products?limit=10" \
  -H "X-Merchant-Id: merchant_001"
```

#### Get Orders
```bash
curl "http://localhost:8000/api/shopify/orders?limit=10&status=any" \
  -H "X-Merchant-Id: merchant_001"
```

#### Get Customers
```bash
curl "http://localhost:8000/api/shopify/customers?limit=10" \
  -H "X-Merchant-Id: merchant_001"
```

#### Get Shop Info
```bash
curl "http://localhost:8000/api/shopify/shop" \
  -H "X-Merchant-Id: merchant_001"
```

#### Custom Endpoint
```bash
curl "http://localhost:8000/api/shopify/custom?endpoint=/products/count.json&method=GET" \
  -H "X-Merchant-Id: merchant_001"
```

## Database Schema

### merchants table

| Column       | Type      | Description                          |
|--------------|-----------|--------------------------------------|
| id           | Integer   | Primary key                          |
| merchant_id  | String    | Unique merchant identifier           |
| shop_domain  | String    | Shopify shop domain                  |
| access_token | Text      | OAuth access token (encrypted)       |
| scope        | String    | OAuth scopes granted                 |
| is_active    | Integer   | Active status (1=active, 0=inactive) |
| created_at   | DateTime  | Creation timestamp                   |
| updated_at   | DateTime  | Last update timestamp                |

## Authentication Flow

1. **Initiate OAuth**: Client calls `/api/oauth/initiate` with merchant_id and shop_domain
2. **User Authorization**: Client redirects merchant to Shopify authorization URL
3. **Callback**: Shopify redirects to `/api/oauth/callback` with authorization code
4. **Token Exchange**: Service exchanges code for access token and stores it
5. **API Calls**: Client makes requests with `X-Merchant-Id` header
6. **Token Retrieval**: Middleware fetches access token for the merchant
7. **Shopify API**: Service makes authenticated requests to Shopify

## Environment Variables

| Variable              | Description                                      | Required | Default     |
|-----------------------|--------------------------------------------------|----------|-------------|
| DB_DSN                | PostgreSQL DSN with schema search path           | Yes      | -           |
| SHOPIFY_API_KEY       | Shopify App API Key                              | Yes      | -           |
| SHOPIFY_API_SECRET    | Shopify App API Secret                           | Yes      | -           |
| SHOPIFY_API_VERSION   | Shopify API version (e.g., 2024-01)              | No       | 2024-01     |
| SHOPIFY_SCOPES        | OAuth scopes (comma-separated)                   | No       | read_products,read_orders,read_customers |
| APP_HOST              | Application host (optional)                      | No       | 0.0.0.0     |
| APP_PORT              | Application port (optional)                      | No       | 8000        |
| APP_SECRET_KEY        | Secret key for encryption (optional)             | No       | None        |
| OAUTH_REDIRECT_URL    | OAuth callback URL                               | Yes      | -           |

## Development

### Running Tests
```bash
pytest
```

### Code Formatting
```bash
black app/
```

### Linting
```bash
flake8 app/
```

## Deployment

### Docker Deployment

1. Build and push image:
   ```bash
   docker build -t shopify-sync:latest .
   docker tag shopify-sync:latest your-registry/shopify-sync:latest
   docker push your-registry/shopify-sync:latest
   ```

2. Deploy with docker-compose or Kubernetes

### Environment-specific Configuration

- **Development**: Use `.env` file
- **Production**: Use environment variables or secrets management (AWS Secrets Manager, Vault, etc.)

## Security Considerations

- Store `SHOPIFY_API_SECRET` securely (use environment variables or secrets management)
- Use HTTPS in production
- Implement rate limiting
- Add request validation and sanitization
- Consider encrypting access tokens at rest
- Implement token rotation
- Add logging and monitoring
- If using `APP_SECRET_KEY`, ensure it's strong and kept secret

## API Documentation

Once running, visit:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## Troubleshooting

### Database Connection Issues
```bash
# Check if PostgreSQL is running
docker-compose ps

# Check logs
docker-compose logs db
```

### OAuth Issues
- Verify your Shopify App credentials
- Ensure redirect URL matches Shopify App settings
- Check that shop domain is correct (include .myshopify.com)

### Application Logs
```bash
docker-compose logs -f app
```

## License

MIT

## Support

For issues and questions, please create an issue in the repository.
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.openapi.utils import get_openapi
from app.database import engine, Base
from app.routers import oauth, shopify_data, webhooks, variants, sync
from app.config import settings
from app.services.scheduler import start_scheduler, stop_scheduler
from sqlalchemy import text
import logging
import secrets

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create schema if it doesn't exist
with engine.connect() as conn:
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS shopify_sync"))
    conn.commit()

# Create database tables
Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan event handler - manages startup and shutdown events
    """
    # Startup
    if settings.ENABLE_SCHEDULER:
        logger.info("Starting scheduler for daily product reconciliation")
        start_scheduler()
    else:
        logger.info("Scheduler disabled (ENABLE_SCHEDULER=False)")

    yield

    # Shutdown
    logger.info("Shutting down scheduler")
    stop_scheduler()


# Initialize FastAPI app with security schemes for Swagger UI
app = FastAPI(
    title="Shopify Products API",
    description="OAuth-based microservice for syncing Shopify products per merchant",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
    swagger_ui_parameters={
        "persistAuthorization": True  # Remember authorization between page refreshes
    }
)

# Define security schemes for Swagger UI
# This adds "Authorize" button in Swagger UI to input headers
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    # Add security schemes for API Key and Merchant ID headers
    openapi_schema["components"]["securitySchemes"] = {
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "API Key for authentication (required for all endpoints except public ones)"
        },
        "MerchantIdAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Merchant-Id",
            "description": "Merchant ID header (required for product, variant, and sync endpoints)"
        }
    }

    # Apply security to specific endpoints only (not globally)
    # Endpoints that should be PUBLIC (no lock icon):
    # - Webhook receivers: /api/webhooks/products/* (use HMAC verification)
    # - Health/info endpoints: /, /health
    # - Documentation endpoints: /api/webhooks/, /api/variants/, /api/sync/

    public_paths = {
        "/",
        "/health",
        "/api/webhooks/products/create",
        "/api/webhooks/products/update",
        "/api/webhooks/products/delete",
        "/api/webhooks/compliance",  # Single endpoint for all compliance webhooks
        "/api/webhooks/customers/data_request",
        "/api/webhooks/customers/redact",
        "/api/webhooks/shop/redact",
        "/api/webhooks/",
        "/api/variants/",
        "/api/sync/"
    }

    # Apply security requirements to each endpoint individually
    for path_item in openapi_schema.get("paths", {}).values():
        for operation in path_item.values():
            if isinstance(operation, dict) and "operationId" in operation:
                # Get the actual path to determine if it's public
                operation_path = None
                for path, item in openapi_schema["paths"].items():
                    if operation in item.values():
                        operation_path = path
                        break

                # Don't add security to public paths
                if operation_path not in public_paths:
                    # OAuth endpoints - API Key only (no merchant exists yet during OAuth flow)
                    if "oauth" in str(operation_path):
                        operation["security"] = [{"ApiKeyAuth": []}]
                    # Product/variant/sync endpoints - need both API Key and Merchant ID
                    elif any(x in str(operation_path) for x in ["/api/products", "/api/variants", "/api/sync"]):
                        operation["security"] = [{"ApiKeyAuth": [], "MerchantIdAuth": []}]
                    # Webhook management endpoints - API Key only
                    elif "webhooks/register" in str(operation_path) or "webhooks/list" in str(operation_path) or "webhooks/delete" in str(operation_path) or "webhooks/sync" in str(operation_path):
                        operation["security"] = [{"ApiKeyAuth": []}]

    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi

# CORS configuration - Restrict to specific origins in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=[
        "Content-Type", 
        "X-API-Key", 
        "X-Merchant-Id", 
        "Authorization", 
        "Accept",
        "X-Shopify-Hmac-SHA256",  # Required for webhook verification
        "X-Shopify-Shop-Domain",   # Required for webhook processing
        "X-Shopify-Topic"          # Required for webhook routing
    ],
    expose_headers=["Content-Type"],
    max_age=600,  # Cache preflight requests for 10 minutes
)


# Global API Key Authentication Middleware
@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    """
    Global middleware to verify API key for all endpoints except:
    - Health check endpoints (/, /health)
    - API documentation (/docs, /redoc, /openapi.json)
    - Shopify webhook callbacks (they use HMAC verification)
    """
    # List of paths that don't require API key authentication
    public_paths = [
        "/",
        "/health",
        "/docs",
        "/redoc",
        "/openapi.json",
    ]

    # Shopify webhook paths use HMAC verification instead of API key
    webhook_paths = [
        "/api/webhooks/products/create",
        "/api/webhooks/products/update",
        "/api/webhooks/products/delete",
        "/api/webhooks/compliance",  # Single endpoint for all compliance webhooks
        "/api/webhooks/customers/data_request",
        "/api/webhooks/customers/redact",
        "/api/webhooks/shop/redact",
    ]

    path = request.url.path

    # Handle OPTIONS requests (CORS preflight) immediately
    # Skip authentication and pass through to let CORS middleware handle it
    if request.method == "OPTIONS":
        response = await call_next(request)
        return response

    # Skip API key check for public and webhook paths
    if path in public_paths or path in webhook_paths:
        return await call_next(request)

    # Verify API key for all other endpoints
    api_key = request.headers.get("x-api-key")

    if not api_key:
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing X-API-Key header"}
        )

    # Use constant-time comparison to prevent timing attacks
    if not secrets.compare_digest(api_key, settings.API_KEY):
        return JSONResponse(
            status_code=403,
            content={"detail": "Invalid API Key"}
        )

    # API key is valid, proceed with the request
    response = await call_next(request)
    return response

# Include routers
app.include_router(oauth.router)
app.include_router(shopify_data.router)
app.include_router(webhooks.router)
app.include_router(variants.router)
app.include_router(sync.router)


@app.get("/")
async def root():
    return {
        "service": "Shopify Products API",
        "version": "1.0.0",
        "status": "running",
        "focus": "products_only",
        "docs": "/docs"
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "shopify-products-api"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=True
    )

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import engine, Base
from app.routers import oauth, shopify_data, webhooks, variants, sync
from app.config import settings
from app.services.scheduler import start_scheduler, stop_scheduler
from sqlalchemy import text
import logging

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


# Initialize FastAPI app
app = FastAPI(
    title="Shopify Products API",
    description="OAuth-based microservice for syncing Shopify products per merchant",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure this based on your needs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

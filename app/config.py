from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str

    # Shopify API
    SHOPIFY_API_KEY: str
    SHOPIFY_API_SECRET: str
    SHOPIFY_API_VERSION: str = "2024-01"
    SHOPIFY_SCOPES: str = "read_products,read_orders,read_customers"

    # Application
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_SECRET_KEY: str

    # OAuth
    OAUTH_REDIRECT_URL: str

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()

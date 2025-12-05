from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
import os


class Settings(BaseSettings):
    """
    Application settings with flexible sourcing from environment variables or secrets.
    Values can be provided either as:
    - Plain environment variables (for development/non-sensitive data)
    - Secrets from Google Cloud Secret Manager (for production/sensitive data)

    The application will use whichever is provided.
    """

    # Database - can be provided as env var or secret
    DB_DSN: str

    # Shopify API - can be provided as env vars or secrets
    # SHOPIFY_API_KEY: str  # Temporarily commented out - Key: 2d7b087dea3ffdfc52f7d01bc0111c27
    # SHOPIFY_API_SECRET: str  # Temporarily commented out - Secret: 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
    SHOPIFY_API_KEY: str = "2d7b087dea3ffdfc52f7d01bc0111c27"
    SHOPIFY_API_SECRET: str = "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"
    SHOPIFY_API_VERSION: str = "2024-01"
    SHOPIFY_SCOPES: str = "read_products,read_orders,read_customers"

    # Application (optional - have sensible defaults)
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    # Environment configuration
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    LOG_LEVEL: str = "DEBUG"

    # OAuth
    OAUTH_REDIRECT_URL: str

    # Security - Token Encryption
    # Generate key using: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    ENCRYPTION_KEY: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )


# Helper function to get config value with fallback
def get_config_value(primary_key: str, fallback_key: Optional[str] = None, default: Optional[str] = None) -> Optional[str]:
    """
    Get configuration value with fallback logic.
    Tries primary key first, then fallback key, then default.
    """
    value = os.getenv(primary_key)
    if value:
        return value
    if fallback_key:
        value = os.getenv(fallback_key)
        if value:
            return value
    return default


settings = Settings()

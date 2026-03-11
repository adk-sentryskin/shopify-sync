from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class ShopifyStoreBase(BaseModel):
    merchant_id: str
    shop_domain: str


class ShopifyStoreCreate(ShopifyStoreBase):
    pass


class ShopifyStoreResponse(ShopifyStoreBase):
    id: int
    is_active: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# Legacy aliases for backwards compatibility during migration
MerchantBase = ShopifyStoreBase
MerchantCreate = ShopifyStoreCreate
MerchantResponse = ShopifyStoreResponse


class OAuthGenerateURL(BaseModel):
    """Schema for generating OAuth authorization URL"""
    shop_domain: Optional[str] = Field(None, description="Shopify shop domain (e.g., mystore.myshopify.com). If omitted, Shopify will prompt the merchant to select their store.")
    merchant_id: str = Field(..., description="Unique merchant identifier")
    redirect_uri: Optional[str] = Field(None, description="Ignored — backend always uses OAUTH_REDIRECT_URL from config")


class OAuthComplete(BaseModel):
    """Schema for completing OAuth from frontend.

    The frontend receives these as query params in the Shopify callback URL and
    forwards them here. ``state`` is Shopify's name for the value we set in
    ``generate-url`` (which equals the merchant_id). Accept either name so the
    frontend can forward Shopify's params without renaming.
    """
    code: str = Field(..., description="Authorization code from Shopify")
    shop: str = Field(..., description="Shop domain returned by Shopify")
    state: Optional[str] = Field(None, description="State param from Shopify callback (equals merchant_id)")
    merchant_id: Optional[str] = Field(None, description="Merchant ID — use when state is not forwarded directly")
    hmac: str = Field(..., description="HMAC signature from Shopify")
    timestamp: str = Field(..., description="Timestamp from Shopify (required for replay attack prevention)")
    host: Optional[str] = Field(None, description="Host parameter from Shopify")


class ProductBase(BaseModel):
    shopify_product_id: int
    title: Optional[str] = None
    vendor: Optional[str] = None
    product_type: Optional[str] = None
    handle: Optional[str] = None
    status: Optional[str] = None


class ProductResponse(ProductBase):
    id: int
    merchant_id: int
    shopify_created_at: Optional[datetime] = None
    shopify_updated_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    synced_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ProductSyncStatus(BaseModel):
    synced_count: int
    created_count: int
    updated_count: int
    failed_count: int = 0

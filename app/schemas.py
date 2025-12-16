from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class MerchantBase(BaseModel):
    merchant_id: str
    shop_domain: str


class MerchantCreate(MerchantBase):
    pass


class MerchantResponse(MerchantBase):
    id: int
    is_active: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class OAuthGenerateURL(BaseModel):
    """Schema for generating OAuth authorization URL"""
    shop_domain: str = Field(..., description="Shopify shop domain (e.g., mystore.myshopify.com)")
    merchant_id: str = Field(..., description="Unique merchant identifier")
    redirect_uri: str = Field(..., description="Frontend callback URL where Shopify will redirect")


class OAuthComplete(BaseModel):
    """Schema for completing OAuth from frontend"""
    code: str = Field(..., description="Authorization code from Shopify")
    shop: str = Field(..., description="Shop domain")
    merchant_id: str = Field(..., description="Merchant ID (from state parameter)")
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

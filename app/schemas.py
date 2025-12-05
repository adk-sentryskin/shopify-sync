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


class OAuthInitiate(BaseModel):
    shop_domain: str = Field(..., description="Shopify shop domain (e.g., mystore.myshopify.com)")
    merchant_id: str = Field(..., description="Unique merchant identifier")


class OAuthCallback(BaseModel):
    code: str
    shop: str
    state: Optional[str] = None


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

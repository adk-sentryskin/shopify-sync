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

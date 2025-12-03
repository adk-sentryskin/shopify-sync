from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from app.database import Base


class Merchant(Base):
    __tablename__ = "merchants"
    __table_args__ = {'schema': 'shopify_sync'}

    id = Column(Integer, primary_key=True, index=True)
    merchant_id = Column(String(255), unique=True, index=True, nullable=False)
    shop_domain = Column(String(255), unique=True, nullable=False)
    access_token = Column(Text, nullable=True)
    scope = Column(String(500), nullable=True)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"<Merchant(merchant_id={self.merchant_id}, shop_domain={self.shop_domain})>"

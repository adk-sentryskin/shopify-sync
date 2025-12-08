from sqlalchemy import Column, Integer, String, DateTime, Text, BigInteger, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB
from app.database import Base
from app.utils.encryption import get_encryption


class Merchant(Base):
    __tablename__ = "merchants"
    __table_args__ = {'schema': 'shopify_sync'}

    id = Column(Integer, primary_key=True, index=True)
    merchant_id = Column(String(255), unique=True, index=True, nullable=False)
    shop_domain = Column(String(255), unique=True, nullable=False)
    _access_token = Column("access_token", Text, nullable=True)  # Stored encrypted
    scope = Column(String(500), nullable=True)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    @hybrid_property
    def access_token(self) -> str:
        """
        Get decrypted access token

        Returns:
            Decrypted access token
        """
        if not self._access_token:
            return None
        try:
            encryption = get_encryption()
            return encryption.decrypt(self._access_token)
        except Exception:
            # If decryption fails, return None (token may be corrupted or key changed)
            return None

    @access_token.setter
    def access_token(self, value: str):
        """
        Set access token (automatically encrypts before storing)

        Args:
            value: Plaintext access token
        """
        if not value:
            self._access_token = None
        else:
            encryption = get_encryption()
            self._access_token = encryption.encrypt(value)

    def __repr__(self):
        return f"<Merchant(merchant_id={self.merchant_id}, shop_domain={self.shop_domain})>"


class Product(Base):
    __tablename__ = "products"
    __table_args__ = {'schema': 'shopify_sync'}

    # Primary Keys
    id = Column(Integer, primary_key=True, autoincrement=True)
    shopify_product_id = Column(BigInteger, unique=True, index=True, nullable=False)
    merchant_id = Column(Integer, ForeignKey('shopify_sync.merchants.id'), nullable=False)

    # Searchable Fields
    title = Column(String(500))
    vendor = Column(String(255))
    product_type = Column(String(255))
    handle = Column(String(255))
    status = Column(String(50))  # active, draft, archived

    # Timestamps from Shopify
    shopify_created_at = Column(DateTime(timezone=True))
    shopify_updated_at = Column(DateTime(timezone=True))
    published_at = Column(DateTime(timezone=True))

    # Full Shopify data (for flexibility)
    raw_data = Column(JSONB)  # Complete Shopify product JSON

    # Local timestamps
    synced_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    merchant = relationship("Merchant", backref="products")

    def __repr__(self):
        return f"<Product(shopify_product_id={self.shopify_product_id}, title={self.title})>"


class Webhook(Base):
    """
    Tracks webhook subscriptions registered with Shopify

    """
    __tablename__ = "webhooks"
    __table_args__ = {'schema': 'shopify_sync'}

    # Primary Keys
    id = Column(Integer, primary_key=True, autoincrement=True)
    merchant_id = Column(Integer, ForeignKey('shopify_sync.merchants.id'), nullable=False)
    shopify_webhook_id = Column(BigInteger, unique=True, index=True, nullable=False)

    # Webhook Details
    topic = Column(String(100), nullable=False, index=True)  # e.g., "products/create"
    address = Column(String(500), nullable=False)  # Full webhook URL
    format = Column(String(20), default="json")  # json or xml

    # Status Tracking
    is_active = Column(Integer, default=1)  # 1=active, 0=deleted/inactive
    last_verified_at = Column(DateTime(timezone=True))  # Last time we verified it exists in Shopify

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    merchant = relationship("Merchant", backref="webhooks")

    def __repr__(self):
        return f"<Webhook(topic={self.topic}, merchant_id={self.merchant_id}, shopify_webhook_id={self.shopify_webhook_id})>"

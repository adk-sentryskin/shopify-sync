from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from sqlalchemy.ext.hybrid import hybrid_property
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

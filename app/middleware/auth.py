from fastapi import Header, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import Optional
from app.database import get_db
from app.models import Merchant
from app.config import settings
import secrets


async def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
    """Verify API Key for service-to-service authentication"""
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Key header"
        )

    if not secrets.compare_digest(x_api_key, settings.API_KEY):
        raise HTTPException(
            status_code=403,
            detail="Invalid API Key"
        )

    return True


async def get_merchant_from_header(
    x_merchant_id: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> Merchant:
    """Extract and validate merchant from X-Merchant-Id header"""
    if not x_merchant_id:
        raise HTTPException(
            status_code=400,
            detail="Missing X-Merchant-Id header. Required for merchant-specific operations."
        )

    merchant = db.query(Merchant).filter(
        Merchant.merchant_id == x_merchant_id,
        Merchant.is_active == 1
    ).first()

    if not merchant:
        raise HTTPException(
            status_code=404,
            detail="Merchant not found or inactive"
        )

    if not merchant.access_token:
        raise HTTPException(
            status_code=403,
            detail="Merchant has not completed OAuth. Please authenticate first."
        )

    return merchant


async def get_optional_merchant(
    x_merchant_id: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> Optional[Merchant]:
    """Optional merchant extraction for OAuth initiation"""
    if not x_merchant_id:
        return None

    merchant = db.query(Merchant).filter(
        Merchant.merchant_id == x_merchant_id
    ).first()

    return merchant

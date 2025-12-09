from fastapi import Header, HTTPException, Depends, Request
from sqlalchemy.orm import Session
from typing import Optional
from app.database import get_db
from app.models import Merchant
from app.config import settings
import secrets


async def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
    """
    Verify API Key for service-to-service authentication

    Args:
        x_api_key: API Key from X-API-Key header

    Returns:
        bool: True if valid

    Raises:
        HTTPException: If API key is missing or invalid
    """
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Key header"
        )

    expected_api_key = settings.API_KEY

    # Use constant-time comparison to prevent timing attacks
    if not secrets.compare_digest(x_api_key, expected_api_key):
        raise HTTPException(
            status_code=403,
            detail="Invalid API Key"
        )

    return True


async def get_merchant_from_header(
    x_merchant_id: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> Merchant:
    """
    Middleware to extract and validate merchant ID from request headers

    Note: API Key authentication is handled globally by middleware.
    This function validates the merchant-specific context for the request.

    Args:
        x_merchant_id: Merchant ID from X-Merchant-Id header
        db: Database session

    Returns:
        Merchant object

    Raises:
        HTTPException: If merchant ID is missing or invalid
    """
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
    """
    Optional merchant extraction - doesn't fail if header is missing
    Used for OAuth initiation endpoints
    """
    if not x_merchant_id:
        return None

    merchant = db.query(Merchant).filter(
        Merchant.merchant_id == x_merchant_id
    ).first()

    return merchant

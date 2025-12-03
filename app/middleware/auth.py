from fastapi import Header, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import Optional
from app.database import get_db
from app.models import Merchant


async def get_merchant_from_header(
    x_merchant_id: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> Merchant:
    """
    Middleware to extract and validate merchant ID from request headers

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
            status_code=401,
            detail="Missing X-Merchant-Id header"
        )

    merchant = db.query(Merchant).filter(
        Merchant.merchant_id == x_merchant_id,
        Merchant.is_active == 1
    ).first()

    if not merchant:
        raise HTTPException(
            status_code=404,
            detail=f"Merchant not found or inactive: {x_merchant_id}"
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

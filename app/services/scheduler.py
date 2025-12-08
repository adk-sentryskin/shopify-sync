"""
Scheduled Job Service for Product Reconciliation

This module handles automatic daily reconciliation of products
to ensure data consistency between Shopify and the local database.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timezone
from typing import Dict, List
import asyncio
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import Merchant
from app.services.product_reconciliation import reconcile_products
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler = None


async def run_daily_reconciliation_for_merchant(merchant_id: int):
    """
    Run reconciliation for a single merchant

    Args:
        merchant_id: Database ID of the merchant
    """
    db = SessionLocal()

    try:
        # Get merchant from database
        merchant = db.query(Merchant).filter(
            Merchant.id == merchant_id,
            Merchant.is_active == 1
        ).first()

        if not merchant:
            logger.warning(f"[Scheduler] Merchant {merchant_id} not found or inactive")
            return

        if not merchant.access_token:
            logger.warning(f"[Scheduler] Merchant {merchant.merchant_id} has no access token")
            return

        logger.info(f"[Scheduler] Starting reconciliation for merchant {merchant.merchant_id} ({merchant.shop_domain})")

        # Run reconciliation (without marking deleted products)
        results = await reconcile_products(
            db=db,
            merchant=merchant,
            shop_domain=merchant.shop_domain,
            access_token=merchant.access_token,
            mark_deleted=False  # Don't auto-delete on scheduled runs
        )

        # Log results
        if results['status'] == 'completed':
            logger.info(
                f"[Scheduler] Reconciliation completed for {merchant.merchant_id}: "
                f"{results['synced_count']} synced, "
                f"{results['missing_in_db']} missing, "
                f"{results['out_of_sync']} out of sync, "
                f"{results['deleted_in_shopify']} deleted in Shopify"
            )
        else:
            logger.error(
                f"[Scheduler] Reconciliation failed for {merchant.merchant_id}: "
                f"{results.get('error', 'Unknown error')}"
            )

    except Exception as e:
        logger.error(f"[Scheduler] Error during reconciliation for merchant {merchant_id}: {str(e)}")

    finally:
        db.close()


async def run_daily_reconciliation_for_all_merchants():
    """
    Run reconciliation for all active merchants

    This is the main scheduled job that runs daily.
    """
    logger.info("[Scheduler] Starting daily reconciliation for all merchants")

    db = SessionLocal()

    try:
        # Get all active merchants with access tokens
        merchants = db.query(Merchant).filter(
            Merchant.is_active == 1
        ).all()

        if not merchants:
            logger.info("[Scheduler] No active merchants found")
            return

        logger.info(f"[Scheduler] Found {len(merchants)} active merchants")

        # Run reconciliation for each merchant
        for merchant in merchants:
            if merchant.access_token:
                try:
                    await run_daily_reconciliation_for_merchant(merchant.id)
                    # Small delay between merchants to avoid overloading
                    await asyncio.sleep(5)
                except Exception as e:
                    logger.error(f"[Scheduler] Error reconciling merchant {merchant.merchant_id}: {str(e)}")
            else:
                logger.warning(f"[Scheduler] Skipping merchant {merchant.merchant_id} (no access token)")

        logger.info("[Scheduler] Daily reconciliation completed for all merchants")

    except Exception as e:
        logger.error(f"[Scheduler] Error in daily reconciliation job: {str(e)}")

    finally:
        db.close()


def start_scheduler():
    """
    Start the APScheduler with daily reconciliation job

    Runs reconciliation every day at 2 AM UTC
    """
    global scheduler

    if scheduler is not None:
        logger.warning("[Scheduler] Scheduler already running")
        return scheduler

    scheduler = AsyncIOScheduler()

    # Add daily reconciliation job (runs at 2 AM UTC every day)
    scheduler.add_job(
        run_daily_reconciliation_for_all_merchants,
        trigger=CronTrigger(hour=2, minute=0, timezone='UTC'),
        id='daily_reconciliation',
        name='Daily Product Reconciliation',
        replace_existing=True,
        max_instances=1  # Only one instance running at a time
    )

    scheduler.start()
    logger.info("[Scheduler] Started - Daily reconciliation scheduled for 2:00 AM UTC")

    return scheduler


def stop_scheduler():
    """
    Stop the scheduler gracefully
    """
    global scheduler

    if scheduler is not None:
        scheduler.shutdown(wait=True)
        scheduler = None
        logger.info("[Scheduler] Stopped")


def get_scheduler_status() -> Dict:
    """
    Get current scheduler status and job information

    Returns:
        Dictionary with scheduler status and job details
    """
    if scheduler is None:
        return {
            "running": False,
            "jobs": []
        }

    jobs = []
    for job in scheduler.get_jobs():
        next_run = job.next_run_time
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": next_run.isoformat() if next_run else None,
            "trigger": str(job.trigger)
        })

    return {
        "running": scheduler.running,
        "jobs": jobs
    }


async def trigger_manual_reconciliation(merchant_id: int = None) -> Dict:
    """
    Manually trigger reconciliation (bypasses schedule)

    Args:
        merchant_id: Optional merchant ID. If None, runs for all merchants.

    Returns:
        Dictionary with execution results
    """
    logger.info(f"[Scheduler] Manual reconciliation triggered for merchant {merchant_id or 'all'}")

    try:
        if merchant_id:
            await run_daily_reconciliation_for_merchant(merchant_id)
            return {
                "status": "completed",
                "message": f"Manual reconciliation completed for merchant {merchant_id}"
            }
        else:
            await run_daily_reconciliation_for_all_merchants()
            return {
                "status": "completed",
                "message": "Manual reconciliation completed for all merchants"
            }
    except Exception as e:
        logger.error(f"[Scheduler] Manual reconciliation failed: {str(e)}")
        return {
            "status": "failed",
            "error": str(e)
        }


def reschedule_job(hour: int = 2, minute: int = 0):
    """
    Reschedule the daily reconciliation job

    Args:
        hour: Hour of the day (0-23, default: 2)
        minute: Minute of the hour (0-59, default: 0)
    """
    if scheduler is None:
        raise Exception("Scheduler is not running")

    scheduler.reschedule_job(
        'daily_reconciliation',
        trigger=CronTrigger(hour=hour, minute=minute, timezone='UTC')
    )

    logger.info(f"[Scheduler] Rescheduled daily reconciliation to {hour:02d}:{minute:02d} UTC")

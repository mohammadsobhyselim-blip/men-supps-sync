"""
Discover new products from MFsupps that don't exist yet in Shopify,
then import them as published products tagged with custom.supplier = "MF".
"""

import logging
from config import settings
from core import shopify_client as shopify
from suppliers import mfsupps

logger = logging.getLogger(__name__)


def import_new_mfsupps_products(dry_run: bool = False,
                                existing_skus: set = None,
                                location_id: int = None) -> dict:
    """
    1. List all MFsupps products.
    2. Compare against SKUs already in Shopify.
    3. Create whatever is missing — published, with price/stock/images/desc/metafield.

    existing_skus and location_id can be passed in from sync_engine to avoid
    redundant API calls. If omitted, they are fetched here (standalone mode).
    """
    logger.info("─" * 60)
    logger.info(f"Discovering new MFsupps products {'(DRY RUN)' if dry_run else ''}...")
    logger.info("─" * 60)

    # 1. Get location for inventory (if not provided)
    if location_id is None:
        try:
            location_id = shopify.get_primary_location_id()
        except Exception as e:
            logger.error(f"Cannot get Shopify location: {e}")
            return {"error": str(e)}

    # 2. Get existing SKUs from Shopify (if not provided)
    if existing_skus is None:
        existing_skus = shopify.get_all_skus()

    # 3. Get all MFsupps products
    mf_products = mfsupps.get_all_products()
    if not mf_products:
        logger.warning("No products returned from MFsupps — check API.")
        return {"new": 0, "created": 0, "skipped": 0, "errors": 0}

    stats = {"new": 0, "created": 0, "skipped": 0, "errors": 0}

    for raw in mf_products:
        normalized = mfsupps.normalize_product(raw)
        sku = normalized["sku"]

        if not sku:
            logger.debug(f"  Skipping MF product with no SKU: {normalized.get('title')}")
            stats["skipped"] += 1
            continue

        # Already in Shopify? Then sync engine handles it. Skip here.
        if sku in existing_skus:
            continue

        stats["new"] += 1
        logger.info(f"  NEW: {normalized['title']} (SKU={sku}) — price={normalized['price']}, stock={normalized['stock']}")

        if dry_run:
            continue

        # Create it
        created = shopify.create_product_from_supplier(
            normalized=normalized,
            supplier_value=settings.SUPPLIER_MFSUPPS,
            location_id=location_id,
        )
        if created:
            stats["created"] += 1
            existing_skus.add(sku)  # Prevent double-create if SKU repeats
        else:
            stats["errors"] += 1

    logger.info("─" * 60)
    logger.info(f"Import done {'(DRY RUN) ' if dry_run else ''}| "
                f"new={stats['new']} | created={stats['created']} | "
                f"skipped={stats['skipped']} | errors={stats['errors']}")
    logger.info("─" * 60)
    return stats

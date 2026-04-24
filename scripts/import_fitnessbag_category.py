"""
One-time (or repeatable) script:
  Import all MyFitnessBag products from WooCommerce category 198
  into men-supps.com Shopify under the "Healthy Groceries" collection.

Usage:
  python scripts/import_fitnessbag_category.py            # live run
  python scripts/import_fitnessbag_category.py --dry-run  # simulate only
"""

import sys
import logging
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import colorlog
from config import settings
from core import shopify_client as shopify
from suppliers import fitnessbag

CATEGORY_ID       = 198
COLLECTION_TITLE  = "Healthy Groceries"
SUPPLIER_VALUE    = settings.SUPPLIER_FITNESSBAG   # "fitnessbag"


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    file_handler = logging.FileHandler("logs/sync.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(file_handler)


def run(dry_run: bool = False):
    logger = logging.getLogger(__name__)
    tag = " (DRY RUN)" if dry_run else ""

    logger.info("=" * 60)
    logger.info(f"FitnessBag category {CATEGORY_ID} → '{COLLECTION_TITLE}'{tag}")
    logger.info("=" * 60)

    # ── Shopify setup ──────────────────────────────────────────────
    try:
        location_id = shopify.get_primary_location_id()
        logger.info(f"Shopify location ID: {location_id}")
    except Exception as e:
        logger.error(f"Cannot get Shopify location: {e}")
        return

    if not dry_run:
        try:
            collection_id = shopify.get_or_create_custom_collection(COLLECTION_TITLE)
        except Exception as e:
            logger.error(f"Cannot get/create collection '{COLLECTION_TITLE}': {e}")
            return
    else:
        collection_id = None
        logger.info(f"[DRY RUN] Would create/find collection '{COLLECTION_TITLE}'")

    # ── Existing Shopify SKUs ──────────────────────────────────────
    existing_skus = shopify.get_all_skus()

    # ── Fetch FitnessBag category ──────────────────────────────────
    logger.info(f"Fetching FitnessBag category {CATEGORY_ID}...")
    wc_products = fitnessbag.get_products_by_category(CATEGORY_ID)
    if not wc_products:
        logger.warning("No products returned from FitnessBag category — check category ID or auth.")
        return

    stats = {"total": 0, "new": 0, "created": 0, "already_exists": 0,
             "added_to_collection": 0, "skipped": 0, "errors": 0}

    for raw in wc_products:
        product_type = raw.get("type", "simple")

        if product_type == "variable":
            # Import each variation as a separate Shopify product
            variations = fitnessbag.get_variations(raw["id"])
            if not variations:
                logger.warning(f"  Variable product '{raw.get('name')}' has no variations — skipping")
                stats["skipped"] += 1
                continue

            for var in variations:
                _process_item(
                    raw=raw,
                    sku=str(var.get("sku", "")).strip(),
                    price=var.get("price") or var.get("regular_price") or "0",
                    stock_qty=var.get("stock_quantity"),
                    in_stock=var.get("stock_status", "instock") == "instock",
                    title_suffix=_variation_title(var),
                    existing_skus=existing_skus,
                    location_id=location_id,
                    collection_id=collection_id,
                    dry_run=dry_run,
                    stats=stats,
                    logger=logger,
                )
        else:
            normalized = fitnessbag.normalize_product(raw)
            _process_item(
                raw=raw,
                sku=normalized["sku"],
                price=str(normalized["price"]),
                stock_qty=normalized["stock"],
                in_stock=True,
                title_suffix="",
                existing_skus=existing_skus,
                location_id=location_id,
                collection_id=collection_id,
                dry_run=dry_run,
                stats=stats,
                logger=logger,
                normalized=normalized,
            )

    logger.info("=" * 60)
    logger.info(
        f"Done{tag} | total={stats['total']} | new={stats['new']} | "
        f"created={stats['created']} | already_exists={stats['already_exists']} | "
        f"added_to_collection={stats['added_to_collection']} | "
        f"skipped={stats['skipped']} | errors={stats['errors']}"
    )
    logger.info("=" * 60)


def _process_item(raw, sku, price, stock_qty, in_stock, title_suffix,
                  existing_skus, location_id, collection_id,
                  dry_run, stats, logger, normalized=None):
    stats["total"] += 1

    if not sku:
        logger.warning(f"  Skipping '{raw.get('name')}' — no SKU")
        stats["skipped"] += 1
        return

    full_title = raw.get("name", "Untitled")
    if title_suffix:
        full_title = f"{full_title} – {title_suffix}"

    if stock_qty is None:
        stock_qty = 100 if in_stock else 0

    if sku in existing_skus:
        logger.info(f"  EXISTS  {sku} | {full_title}")
        stats["already_exists"] += 1

        if not dry_run and collection_id:
            # Product already in Shopify — find its ID and add to collection
            _add_existing_to_collection(sku, collection_id, logger, stats)
        return

    stats["new"] += 1
    logger.info(f"  NEW     {sku} | {full_title} | price={price} stock={stock_qty}")

    if dry_run:
        return

    if normalized is None:
        normalized = fitnessbag.normalize_product(raw)

    normalized["sku"]   = sku
    normalized["title"] = full_title
    normalized["price"] = float(price)
    normalized["stock"] = int(stock_qty)

    product = shopify.create_product_from_supplier(
        normalized=normalized,
        supplier_value=SUPPLIER_VALUE,
        location_id=location_id,
    )

    if product:
        stats["created"] += 1
        existing_skus.add(sku)
        if collection_id:
            ok = shopify.add_product_to_collection(collection_id, product["id"])
            if ok:
                stats["added_to_collection"] += 1
    else:
        stats["errors"] += 1


def _add_existing_to_collection(sku, collection_id, logger, stats):
    """Find an existing Shopify product by SKU and add it to the collection."""
    try:
        data = shopify._get("/products.json", params={"fields": "id,variants", "limit": 250})
        for p in data.get("products", []):
            for v in p.get("variants", []):
                if (v.get("sku") or "").strip() == sku:
                    ok = shopify.add_product_to_collection(collection_id, p["id"])
                    if ok:
                        stats["added_to_collection"] += 1
                    return
    except Exception as e:
        logger.error(f"  Could not add existing SKU {sku} to collection: {e}")


def _variation_title(var: dict) -> str:
    """Build a human-readable suffix from variation attributes."""
    attrs = var.get("attributes", [])
    parts = [a.get("option", "") for a in attrs if a.get("option")]
    return " / ".join(parts) if parts else str(var.get("id", ""))


if __name__ == "__main__":
    setup_logging()
    dry_run = "--dry-run" in sys.argv
    run(dry_run=dry_run)

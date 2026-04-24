import logging
from config import settings
from core import shopify_client as shopify
from core.importer import import_new_mfsupps_products
from suppliers import mfsupps, fitnessbag

logger = logging.getLogger(__name__)


def run_sync(dry_run=False, skip_import=False):
    """
    Full cycle:
      STEP 1 → discover & import new MFsupps products (adds to Shopify as published, supplier=MF)
      STEP 2 → sync price + stock for every product tagged with a known supplier

    dry_run=True  → logs what would change without writing anything.
    skip_import=True → only run price/stock sync, skip new product discovery.
    """
    logger.info("=" * 60)
    logger.info(f"Starting full sync {'(DRY RUN) ' if dry_run else ''}...")
    logger.info("=" * 60)

    # Get primary location for inventory
    try:
        location_id = shopify.get_primary_location_id()
        logger.info(f"Shopify location ID: {location_id}")
    except Exception as e:
        logger.error(f"Cannot get Shopify location: {e}")
        return

    # Fetch all products once — reused by both importer and sync loop
    logger.info("Fetching all Shopify products...")
    products = shopify.get_all_products_paginated()
    logger.info(f"Total products fetched: {len(products)}")

    # STEP 1: Import new MFsupps products
    if not skip_import:
        existing_skus = {
            (v.get("sku") or "").strip()
            for p in products
            for v in p.get("variants", [])
            if (v.get("sku") or "").strip()
        }
        try:
            import_new_mfsupps_products(dry_run=dry_run,
                                        existing_skus=existing_skus,
                                        location_id=location_id)
        except Exception as e:
            logger.error(f"Importer failed (continuing to sync): {e}")

    # STEP 2: Price + stock sync for all supplier-tagged products
    logger.info("")
    logger.info("─" * 60)
    logger.info("Syncing price + stock for existing products...")
    logger.info("─" * 60)

    stats = {"checked": 0, "updated": 0, "skipped": 0, "not_found": 0, "errors": 0}

    for product in products:
        product_id = product["id"]
        title = product.get("title", "?")

        # Resolve supplier: metafield first, then tag fallback
        try:
            supplier = shopify.get_product_metafield_supplier(product_id)
        except Exception as e:
            logger.error(f"[{title}] Failed to read metafield: {e}")
            stats["errors"] += 1
            continue

        if not supplier:
            # Tag-based fallback: fitnessbag tag → MFB supplier
            product_tags = [t.strip().lower() for t in product.get("tags", "").split(",")]
            if "fitnessbag" in product_tags:
                supplier = settings.SUPPLIER_FITNESSBAG
            else:
                logger.debug(f"[{title}] No supplier metafield or known tag — skipping")
                stats["skipped"] += 1
                continue

        # Route to correct supplier
        supplier_lower = supplier.strip().lower()

        if supplier_lower == settings.SUPPLIER_MFSUPPS.lower():
            fetch_by_sku  = mfsupps.get_product_by_sku
            fetch_by_name = None
            supplier_label = "MFsupps"
        elif supplier_lower == settings.SUPPLIER_FITNESSBAG.lower():
            fetch_by_sku  = fitnessbag.get_product_by_sku
            fetch_by_name = fitnessbag.get_product_by_name
            supplier_label = "FitnessBag"
        else:
            logger.debug(f"[{title}] Unknown supplier '{supplier}' — skipping")
            stats["skipped"] += 1
            continue

        logger.info(f"[{title}] supplier={supplier_label} | variants: {len(product['variants'])}")

        # Process each variant
        for variant in product.get("variants", []):
            sku = variant.get("sku", "").strip()
            variant_id = variant["id"]
            stats["checked"] += 1

            # Fetch from supplier — try SKU first, fall back to product title
            supplier_data = None
            if sku:
                supplier_data = fetch_by_sku(sku)
            if supplier_data is None and fetch_by_name:
                logger.debug(f"  SKU '{sku}' not found — trying name lookup for '{title}'")
                supplier_data = fetch_by_name(title)

            if supplier_data is None:
                logger.warning(f"  '{title}' (SKU={sku or 'none'}) not found at {supplier_label}")
                stats["not_found"] += 1
                continue

            new_price = supplier_data.get("price")
            new_stock = supplier_data.get("stock")

            # Check if anything actually changed
            current_price = float(variant.get("price", 0))
            price_changed = new_price is not None and abs(new_price - current_price) > 0.001

            log_parts = [f"  SKU={sku}"]
            if price_changed:
                log_parts.append(f"price {current_price} → {new_price}")
            if new_stock is not None:
                log_parts.append(f"stock → {new_stock}")

            logger.info(" | ".join(log_parts))

            if dry_run:
                stats["updated"] += 1
                continue

            # Apply updates
            try:
                shopify.update_variant_price_and_stock(
                    variant_id=variant_id,
                    product_id=product_id,
                    price=new_price if price_changed else None,
                    inventory_quantity=new_stock,
                    location_id=location_id,
                    inventory_item_id=variant.get("inventory_item_id"),
                )
                stats["updated"] += 1
            except Exception as e:
                logger.error(f"  Failed to update variant {variant_id} (SKU={sku}): {e}")
                stats["errors"] += 1

    # Summary
    logger.info("=" * 60)
    logger.info(f"Sync complete {'(DRY RUN) ' if dry_run else ''}| "
                f"checked={stats['checked']} | "
                f"updated={stats['updated']} | "
                f"not_found={stats['not_found']} | "
                f"skipped={stats['skipped']} | "
                f"errors={stats['errors']}")
    logger.info("=" * 60)

    return stats

import requests
import logging
from requests.auth import HTTPBasicAuth
from config import settings

logger = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.auth = HTTPBasicAuth(settings.MFSUPPS_CONSUMER_KEY, settings.MFSUPPS_CONSUMER_SECRET)
SESSION.headers.update({"Content-Type": "application/json"})


def get_product_by_sku(sku: str) -> dict | None:
    """
    Fetch a single product by SKU from MFsupps WooCommerce.
    WooCommerce returns a list; handles simple and variable products.
    """
    try:
        r = SESSION.get(
            f"{settings.MFSUPPS_API_URL}/wp-json/wc/v3/products",
            params={"sku": sku},
            timeout=15,
        )
        if r.status_code == 401:
            logger.error("[MFsupps] Auth failed — check Consumer Key/Secret")
            return None
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()

        if not isinstance(data, list) or len(data) == 0:
            logger.warning(f"[MFsupps] No product found for SKU: {sku}")
            return None

        product = data[0]
        if product.get("type") == "variable":
            return _get_variation_by_sku(product["id"], sku)

        price = product.get("price") or product.get("regular_price")
        if not price:
            logger.warning(f"[MFsupps] No price for SKU {sku}")
            return None

        stock_qty = product.get("stock_quantity")
        in_stock = product.get("stock_status", "instock") == "instock"
        if stock_qty is None:
            stock_qty = 100 if in_stock else 0

        return {"price": float(price), "stock": int(stock_qty), "sku": sku}

    except requests.RequestException as e:
        logger.error(f"[MFsupps] SKU lookup failed for {sku}: {e}")
        return None


def _get_variation_by_sku(product_id: int, sku: str) -> dict | None:
    """For variable WooCommerce products, search variations by SKU."""
    try:
        r = SESSION.get(
            f"{settings.MFSUPPS_API_URL}/wp-json/wc/v3/products/{product_id}/variations",
            params={"sku": sku},
            timeout=15,
        )
        r.raise_for_status()
        variations = r.json()
        if not variations:
            return None

        var = variations[0]
        price = var.get("price") or var.get("regular_price")
        if not price:
            return None

        stock_qty = var.get("stock_quantity")
        in_stock = var.get("stock_status", "instock") == "instock"
        if stock_qty is None:
            stock_qty = 100 if in_stock else 0

        return {"price": float(price), "stock": int(stock_qty), "sku": sku}

    except requests.RequestException as e:
        logger.error(f"[MFsupps] Failed to fetch variation for SKU {sku}: {e}")
        return None


def get_all_products() -> list[dict]:
    """Fetch ALL products from MFsupps WooCommerce (for discovering new ones to import)."""
    all_products = []
    page = 1
    per_page = 100

    while True:
        try:
            r = SESSION.get(
                f"{settings.MFSUPPS_API_URL}/wp-json/wc/v3/products",
                params={"page": page, "per_page": per_page, "status": "publish"},
                timeout=30,
            )
            r.raise_for_status()
            products = r.json()

            if not products:
                break

            all_products.extend(products)
            logger.info(f"[MFsupps] Fetched page {page} ({len(products)} products)")

            if len(products) < per_page:
                break
            page += 1

        except requests.RequestException as e:
            logger.error(f"[MFsupps] Failed to fetch page {page}: {e}")
            break

    logger.info(f"[MFsupps] Total products discovered: {len(all_products)}")
    return all_products


def get_variations(product_id: int) -> list[dict]:
    """Fetch all variations for a variable WooCommerce product."""
    all_vars = []
    page = 1
    while True:
        try:
            r = SESSION.get(
                f"{settings.MFSUPPS_API_URL}/wp-json/wc/v3/products/{product_id}/variations",
                params={"page": page, "per_page": 100},
                timeout=15,
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            all_vars.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        except requests.RequestException as e:
            logger.error(f"[MFsupps] Failed to fetch variations for product {product_id}: {e}")
            break
    return all_vars


def normalize_variation(var: dict) -> dict:
    """Convert a WooCommerce variation into normalized form for a Shopify variant."""
    price = var.get("price") or var.get("regular_price") or "0"
    stock_qty = var.get("stock_quantity")
    in_stock = var.get("stock_status", "instock") == "instock"
    if stock_qty is None:
        stock_qty = 100 if in_stock else 0
    attrs = {
        a["name"]: a["option"]
        for a in var.get("attributes", [])
        if a.get("name") and a.get("option")
    }
    return {
        "sku":        str(var.get("sku", "")).strip(),
        "price":      float(price),
        "stock":      int(stock_qty),
        "attributes": attrs,
    }


def normalize_product(raw: dict) -> dict:
    """Convert a WooCommerce product dict into the shape expected by Shopify importer."""
    product_type = raw.get("type", "simple")

    images = [img["src"] for img in raw.get("images", []) if img.get("src")]
    tags = [t["name"] for t in raw.get("tags", []) if t.get("name")]
    categories = [c["name"] for c in raw.get("categories", []) if c.get("name")]

    weight = raw.get("weight")
    try:
        weight = float(weight) * 1000 if weight else 0  # WC stores kg, Shopify wants g
    except (ValueError, TypeError):
        weight = 0

    if product_type == "variable":
        price, stock_qty = 0.0, 0
    else:
        price = raw.get("price") or raw.get("regular_price") or "0"
        stock_qty = raw.get("stock_quantity")
        in_stock = raw.get("stock_status", "instock") == "instock"
        if stock_qty is None:
            stock_qty = 100 if in_stock else 0
        price = float(price)
        stock_qty = int(stock_qty)

    return {
        "sku":         str(raw.get("sku", "")).strip(),
        "title":       raw.get("name", "Untitled"),
        "description": raw.get("description", ""),
        "price":       price,
        "stock":       stock_qty,
        "images":      images,
        "brand":       _get_brand(raw),
        "weight":      weight,
        "tags":        tags,
        "barcode":     "",
        "type":        product_type,
        "wc_id":       raw.get("id"),
        "categories":  categories,
        "variants":    [],  # filled by importer for variable products
    }


def _get_brand(raw: dict) -> str:
    """Extract brand from WooCommerce attributes or brands taxonomy."""
    for attr in raw.get("attributes", []):
        if attr.get("name", "").lower() in ("brand", "manufacturer"):
            options = attr.get("options", [])
            if options:
                return options[0]
    return raw.get("slug", "").split("-")[0].title()

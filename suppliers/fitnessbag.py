import requests
import logging
from requests.auth import HTTPBasicAuth
from config import settings

logger = logging.getLogger(__name__)

# WooCommerce REST API uses HTTP Basic Auth with Consumer Key + Secret
AUTH = HTTPBasicAuth(settings.FITNESSBAG_CONSUMER_KEY, settings.FITNESSBAG_CONSUMER_SECRET)

SESSION = requests.Session()
SESSION.auth = AUTH


def get_product_by_sku(sku: str) -> dict | None:
    """
    Fetch product data from MyFitnessBag (WooCommerce) by SKU.
    WooCommerce API: GET /wp-json/wc/v3/products?sku=XXX
    Returns dict with 'price' and 'stock' keys, or None if not found.
    """
    try:
        r = SESSION.get(
            f"{settings.FITNESSBAG_API_URL}/wp-json/wc/v3/products",
            params={"sku": sku},
            timeout=15,
        )

        if r.status_code == 401:
            logger.error("[FitnessBag] Auth failed — check Consumer Key/Secret")
            return None

        if r.status_code == 404:
            logger.warning(f"[FitnessBag] SKU not found: {sku}")
            return None

        r.raise_for_status()
        data = r.json()

        # WooCommerce always returns a list
        if not isinstance(data, list) or len(data) == 0:
            logger.warning(f"[FitnessBag] No product found for SKU: {sku}")
            return None

        product = data[0]

        # WooCommerce product can be simple or variable
        # For simple products: price + stock_quantity live on the product itself
        # For variable products: we need to check variations by SKU
        product_type = product.get("type", "simple")

        if product_type == "variable":
            return _get_variation_by_sku(product["id"], sku)

        price = product.get("price") or product.get("regular_price")
        if not price:
            logger.warning(f"[FitnessBag] No price for SKU {sku}")
            return None

        stock_qty = product.get("stock_quantity")
        in_stock = product.get("stock_status", "instock") == "instock"
        if stock_qty is None:
            stock_qty = 100 if in_stock else 0

        return {
            "price": float(price),
            "stock": int(stock_qty),
            "sku":   sku,
        }

    except requests.RequestException as e:
        logger.error(f"[FitnessBag] Request failed for SKU {sku}: {e}")
        return None


def _get_variation_by_sku(product_id: int, sku: str) -> dict | None:
    """
    For variable WooCommerce products, search variations by SKU.
    GET /wp-json/wc/v3/products/{id}/variations?sku=XXX
    """
    try:
        r = SESSION.get(
            f"{settings.FITNESSBAG_API_URL}/wp-json/wc/v3/products/{product_id}/variations",
            params={"sku": sku},
            timeout=15,
        )
        r.raise_for_status()
        variations = r.json()

        if not variations:
            logger.warning(f"[FitnessBag] No variation found for SKU {sku} in product {product_id}")
            return None

        var = variations[0]
        price = var.get("price") or var.get("regular_price")
        if not price:
            return None

        stock_qty = var.get("stock_quantity")
        in_stock = var.get("stock_status", "instock") == "instock"
        if stock_qty is None:
            stock_qty = 100 if in_stock else 0

        return {
            "price": float(price),
            "stock": int(stock_qty),
            "sku":   sku,
        }

    except requests.RequestException as e:
        logger.error(f"[FitnessBag] Failed to fetch variation for SKU {sku}: {e}")
        return None


def get_product_by_name(title: str) -> dict | None:
    """
    Fuzzy-match a product on MyFitnessBag by title (search).
    Returns {'price', 'stock'} or None.
    Falls back to partial match if exact match fails.
    """
    try:
        r = SESSION.get(
            f"{settings.FITNESSBAG_API_URL}/wp-json/wc/v3/products",
            params={"search": title, "status": "publish", "per_page": 5},
            timeout=15,
        )
        if r.status_code in (401, 404):
            return None
        r.raise_for_status()
        results = r.json()
        if not results:
            return None

        # Pick closest name match
        title_lower = title.lower()
        best = None
        for p in results:
            if p.get("name", "").lower() == title_lower:
                best = p
                break
        if best is None:
            # fallback: first result
            best = results[0]

        product_type = best.get("type", "simple")
        if product_type == "variable":
            vars_ = get_variations(best["id"])
            if not vars_:
                return None
            var = vars_[0]
            price = var.get("price") or var.get("regular_price")
            stock_qty = var.get("stock_quantity")
            in_stock = var.get("stock_status", "instock") == "instock"
        else:
            price = best.get("price") or best.get("regular_price")
            stock_qty = best.get("stock_quantity")
            in_stock = best.get("stock_status", "instock") == "instock"

        if not price:
            return None
        if stock_qty is None:
            stock_qty = 100 if in_stock else 0

        return {"price": float(price), "stock": int(stock_qty)}

    except requests.RequestException as e:
        logger.error(f"[FitnessBag] Name lookup failed for '{title}': {e}")
        return None


def get_products_by_category(category_id: int) -> list[dict]:
    """
    Fetch all published products under a WooCommerce category ID.
    Handles pagination automatically.
    """
    all_products = []
    page = 1
    per_page = 100

    while True:
        try:
            r = SESSION.get(
                f"{settings.FITNESSBAG_API_URL}/wp-json/wc/v3/products",
                params={
                    "category": category_id,
                    "status":   "publish",
                    "page":     page,
                    "per_page": per_page,
                },
                timeout=30,
            )
            if r.status_code == 401:
                logger.error("[FitnessBag] Auth failed — check Consumer Key/Secret")
                break
            r.raise_for_status()
            products = r.json()

            if not products:
                break

            all_products.extend(products)
            logger.info(f"[FitnessBag] Category {category_id} — page {page}: {len(products)} products")

            if len(products) < per_page:
                break
            page += 1

        except requests.RequestException as e:
            logger.error(f"[FitnessBag] Failed to fetch category {category_id} page {page}: {e}")
            break

    logger.info(f"[FitnessBag] Total products in category {category_id}: {len(all_products)}")
    return all_products


def normalize_product(raw: dict) -> dict:
    """
    Convert a WooCommerce product dict into the normalized shape
    expected by shopify_client.create_product_from_supplier().
    For variable products, uses the first variation's price/stock.
    """
    product_type = raw.get("type", "simple")
    sku = str(raw.get("sku", "")).strip()

    price = raw.get("price") or raw.get("regular_price") or "0"
    stock_qty = raw.get("stock_quantity")
    in_stock = raw.get("stock_status", "instock") == "instock"
    if stock_qty is None:
        stock_qty = 100 if in_stock else 0

    # For variable products with no top-level SKU, we'll import per-variation below
    images = [img["src"] for img in raw.get("images", []) if img.get("src")]
    tags   = [t["name"] for t in raw.get("tags", []) if t.get("name")]

    weight = raw.get("weight")
    try:
        weight = float(weight) * 1000 if weight else 0   # WC stores kg → Shopify wants g
    except (ValueError, TypeError):
        weight = 0

    return {
        "sku":         sku,
        "title":       raw.get("name", "Untitled"),
        "description": raw.get("description", ""),
        "price":       float(price),
        "stock":       int(stock_qty),
        "images":      images,
        "brand":       _get_brand(raw),
        "weight":      weight,
        "tags":        tags,
        "barcode":     "",
        "type":        product_type,
        "wc_id":       raw.get("id"),
    }


def get_variations(product_id: int) -> list[dict]:
    """Fetch all variations for a variable WooCommerce product."""
    all_vars = []
    page = 1
    while True:
        try:
            r = SESSION.get(
                f"{settings.FITNESSBAG_API_URL}/wp-json/wc/v3/products/{product_id}/variations",
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
            logger.error(f"[FitnessBag] Failed to fetch variations for product {product_id}: {e}")
            break
    return all_vars


def _get_brand(raw: dict) -> str:
    for attr in raw.get("attributes", []):
        if attr.get("name", "").lower() in ("brand", "manufacturer"):
            options = attr.get("options", [])
            if options:
                return options[0]
    return ""

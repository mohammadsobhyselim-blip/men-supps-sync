import requests
import logging
import time
from config import settings

logger = logging.getLogger(__name__)

BASE = f"https://{settings.SHOPIFY_STORE_URL}/admin/api/{settings.SHOPIFY_API_VERSION}"


def _headers() -> dict:
    """Build auth headers using the current token (loaded dynamically so OAuth can inject it)."""
    from oauth_server import load_token
    token = load_token()
    if not token:
        raise RuntimeError(
            "No Shopify access token found. "
            "Visit /install on the deployed app to complete OAuth."
        )
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }


def _get(endpoint, params=None):
    """GET with basic retry on 429."""
    url = f"{BASE}{endpoint}"
    for attempt in range(3):
        r = requests.get(url, headers=_headers(), params=params, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 2))
            logger.warning(f"Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise Exception(f"Failed after retries: GET {endpoint}")


def _put(endpoint, payload):
    """PUT with basic retry on 429."""
    url = f"{BASE}{endpoint}"
    for attempt in range(3):
        r = requests.put(url, headers=_headers(), json=payload, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 2))
            logger.warning(f"Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise Exception(f"Failed after retries: PUT {endpoint}")


def get_all_products_paginated():
    """
    Proper cursor-based pagination for Shopify products.
    Returns list of all products with their variants.
    """
    products = []
    url = f"{BASE}/products.json"
    params = {"limit": 250, "fields": "id,title,tags,variants,status"}

    while url:
        r = requests.get(url, headers=_headers(), params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 2)))
            continue
        r.raise_for_status()

        batch = r.json().get("products", [])
        products.extend(batch)
        logger.info(f"  → fetched {len(batch)} products (total: {len(products)})")

        # Next page from Link header
        url, params = None, None
        link_header = r.headers.get("Link", "")
        if 'rel="next"' in link_header:
            for part in link_header.split(","):
                if 'rel="next"' in part:
                    page_info = part.split("page_info=")[1].split(">")[0].strip()
                    url = f"{BASE}/products.json"
                    params = {"limit": 250, "fields": "id,title,tags,variants,status", "page_info": page_info}
                    break

    return products


def get_product_metafield_supplier(product_id):
    """
    Read the custom.supplier metafield for a given product.
    Returns the supplier string or None.
    """
    data = _get(f"/products/{product_id}/metafields.json")
    for mf in data.get("metafields", []):
        if mf["namespace"] == settings.SUPPLIER_METAFIELD_NAMESPACE and \
           mf["key"] == settings.SUPPLIER_METAFIELD_KEY:
            return mf["value"]
    return None


def get_metafields_bulk(product_ids):
    """
    Fetch supplier metafield for multiple products.
    Returns dict: {product_id: supplier_value}
    """
    result = {}
    for pid in product_ids:
        try:
            supplier = get_product_metafield_supplier(pid)
            if supplier:
                result[pid] = supplier
        except Exception as e:
            logger.error(f"Failed to get metafield for product {pid}: {e}")
    return result


def update_variant_price_and_stock(variant_id, product_id, price=None, inventory_quantity=None,
                                    location_id=None, inventory_item_id=None):
    """
    Update a variant's price and/or inventory quantity.

    Pass inventory_item_id when already available (from the product list) to avoid
    an extra GET. If omitted, it is fetched from the API.
    """
    updated = {}

    # Update price
    if price is not None:
        payload = {"variant": {"id": variant_id, "price": str(round(price, 2))}}
        result = _put(f"/variants/{variant_id}.json", payload)
        updated["price"] = result.get("variant", {}).get("price")
        logger.debug(f"  Updated price for variant {variant_id} → {price}")

    # Update inventory (requires inventory_item_id + location_id)
    if inventory_quantity is not None and location_id:
        if inventory_item_id is None:
            variant_data = _get(f"/variants/{variant_id}.json")
            inventory_item_id = variant_data["variant"]["inventory_item_id"]

        inv_payload = {
            "location_id":       location_id,
            "inventory_item_id": inventory_item_id,
            "available":         inventory_quantity,
        }
        r = requests.post(
            f"{BASE}/inventory_levels/set.json",
            headers=_headers(),
            json=inv_payload,
            timeout=30,
        )
        r.raise_for_status()
        updated["inventory"] = inventory_quantity
        logger.debug(f"  Updated stock for variant {variant_id} → {inventory_quantity}")

    return updated


def get_primary_location_id():
    """Get the first/primary location ID for inventory updates."""
    data = _get("/locations.json")
    locations = data.get("locations", [])
    if not locations:
        raise Exception("No locations found in Shopify store")
    return locations[0]["id"]


def _post(endpoint, payload):
    """POST with retry on 429."""
    url = f"{BASE}{endpoint}"
    for attempt in range(3):
        r = requests.post(url, headers=_headers(), json=payload, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 2))
            logger.warning(f"Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise Exception(f"Failed after retries: POST {endpoint}")


def get_or_create_custom_collection(title: str) -> int:
    """
    Return the ID of a custom collection with the given title, creating it if needed.
    """
    data = _get("/custom_collections.json", params={"title": title, "limit": 5})
    for col in data.get("custom_collections", []):
        if col["title"].strip().lower() == title.strip().lower():
            logger.info(f"Collection '{title}' already exists (id={col['id']})")
            return col["id"]

    result = _post("/custom_collections.json", {"custom_collection": {"title": title, "published": True}})
    col_id = result["custom_collection"]["id"]
    logger.info(f"Created collection '{title}' (id={col_id})")
    return col_id


def add_product_to_collection(collection_id: int, product_id: int) -> bool:
    """Add a product to a custom collection via a Collect object."""
    try:
        _post("/collects.json", {"collect": {"collection_id": collection_id, "product_id": product_id}})
        return True
    except Exception as e:
        # 422 = already in collection, that's fine
        if "422" in str(e):
            return True
        logger.error(f"Failed to add product {product_id} to collection {collection_id}: {e}")
        return False


def set_product_metafield(product_id: int, namespace: str, key: str, value: str) -> bool:
    """
    Create or update a single metafield on a product.
    Uses POST (Shopify upserts by namespace+key if it already exists on same resource).
    """
    try:
        _post(f"/products/{product_id}/metafields.json", {
            "metafield": {
                "namespace": namespace,
                "key":       key,
                "type":      "single_line_text_field",
                "value":     value,
            }
        })
        return True
    except Exception as e:
        logger.error(f"Failed to set metafield {namespace}.{key} on product {product_id}: {e}")
        return False


def bulk_set_supplier_on_last_n(n: int, supplier_value: str, dry_run: bool = False) -> dict:
    """
    Fetch ALL products sorted by ID ascending, take the last `n` (highest IDs = most recently
    created), and set custom.supplier = supplier_value on each.

    This is the right approach when you know 'the last N products are all from supplier X'
    but they may not share a consistent tag.

    Returns stats dict: {'tagged', 'skipped', 'errors'}.
    """
    stats = {"tagged": 0, "skipped": 0, "errors": 0}

    # Collect all products via cursor pagination (ascending by ID = Shopify default)
    all_products = []
    url = f"{BASE}/products.json"
    params = {"limit": 250, "fields": "id,title"}

    while url:
        r = requests.get(url, headers=_headers(), params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 2)))
            continue
        r.raise_for_status()
        batch = r.json().get("products", [])
        all_products.extend(batch)

        url, params = None, None
        link_header = r.headers.get("Link", "")
        if 'rel="next"' in link_header:
            for part in link_header.split(","):
                if 'rel="next"' in part:
                    page_info = part.split("page_info=")[1].split(">")[0].strip()
                    url = f"{BASE}/products.json"
                    params = {"limit": 250, "fields": "id,title", "page_info": page_info}
                    break

    # Sort by ID ascending, take last n
    all_products.sort(key=lambda p: p["id"])
    targets = all_products[-n:]
    logger.info(f"Total products in store: {len(all_products)} — targeting last {n} (IDs {targets[0]['id']} → {targets[-1]['id']})")

    for p in targets:
        if dry_run:
            logger.info(f"[DRY RUN] Would set supplier={supplier_value} on '{p['title']}' (id={p['id']})")
            stats["tagged"] += 1
            continue

        ok = set_product_metafield(
            p["id"],
            settings.SUPPLIER_METAFIELD_NAMESPACE,
            settings.SUPPLIER_METAFIELD_KEY,
            supplier_value,
        )
        if ok:
            logger.info(f"  ✓ '{p['title']}' (id={p['id']}) → supplier={supplier_value}")
            stats["tagged"] += 1
        else:
            stats["errors"] += 1

    logger.info(f"bulk_set_supplier_on_last_n done — tagged={stats['tagged']} errors={stats['errors']}")
    return stats


def get_all_skus() -> set[str]:
    """
    Return the set of every SKU currently in Shopify.
    Used to detect which MFsupps products are new.
    """
    skus = set()
    products = get_all_products_paginated()
    for p in products:
        for v in p.get("variants", []):
            sku = (v.get("sku") or "").strip()
            if sku:
                skus.add(sku)
    logger.info(f"Shopify currently has {len(skus)} SKUs")
    return skus


def create_product_from_supplier(normalized: dict, supplier_value: str, location_id: int) -> dict | None:
    """
    Create a new published product in Shopify from a normalized supplier product.

    For simple products: normalized must have sku, price, stock.
    For variable products: normalized must have variants list (each with sku, price, stock, attributes).
    Categories in normalized["categories"] are mapped to Shopify collections.
    Brand in normalized["brand"] becomes the Shopify vendor.
    """
    title        = normalized["title"]
    product_type = normalized.get("type", "simple")
    wc_variants  = normalized.get("variants") or []

    metafield_entry = {
        "namespace": settings.SUPPLIER_METAFIELD_NAMESPACE,
        "key":       settings.SUPPLIER_METAFIELD_KEY,
        "type":      "single_line_text_field",
        "value":     supplier_value,
    }

    if product_type == "variable" and wc_variants:
        # Collect unique option names across all variations (max 3 in Shopify)
        option_names = []
        for var in wc_variants:
            for attr_name in var.get("attributes", {}).keys():
                if attr_name not in option_names:
                    option_names.append(attr_name)
        if not option_names:
            option_names = ["Variant"]

        shopify_variants = []
        for var in wc_variants:
            sv = {
                "sku":                  var["sku"],
                "price":                str(round(float(var["price"]), 2)),
                "inventory_management": "shopify",
                "inventory_policy":     "deny",
                "weight":               normalized.get("weight") or 0,
                "weight_unit":          "g",
            }
            attrs = var.get("attributes", {})
            for i, opt_name in enumerate(option_names[:3]):
                sv[f"option{i + 1}"] = attrs.get(opt_name, "Default")
            shopify_variants.append(sv)

        payload = {
            "product": {
                "title":      title,
                "body_html":  normalized.get("description", ""),
                "vendor":     normalized.get("brand") or "",
                "status":     "active",
                "published":  True,
                "options":    [{"name": n} for n in option_names[:3]],
                "variants":   shopify_variants,
                "images":     [{"src": u} for u in normalized.get("images", []) if u],
                "tags":       ",".join(normalized.get("tags", []) or []),
                "metafields": [metafield_entry],
            }
        }
    else:
        sku = normalized["sku"]
        payload = {
            "product": {
                "title":      title,
                "body_html":  normalized.get("description", ""),
                "vendor":     normalized.get("brand") or "",
                "status":     "active",
                "published":  True,
                "variants": [{
                    "sku":                  sku,
                    "price":                str(round(float(normalized["price"]), 2)),
                    "inventory_management": "shopify",
                    "inventory_policy":     "deny",
                    "barcode":              normalized.get("barcode") or "",
                    "weight":               normalized.get("weight") or 0,
                    "weight_unit":          "g",
                }],
                "images":     [{"src": u} for u in normalized.get("images", []) if u],
                "tags":       ",".join(normalized.get("tags", []) or []),
                "metafields": [metafield_entry],
            }
        }

    try:
        result = _post("/products.json", payload)
        product = result["product"]
        logger.info(f"  ✓ Created '{title}' (type={product_type}) — id={product['id']}, variants={len(product['variants'])}")

        # Set inventory for every variant
        for i, shopify_var in enumerate(product["variants"]):
            if product_type == "variable" and i < len(wc_variants):
                stock = int(wc_variants[i].get("stock", 0))
            else:
                stock = int(normalized.get("stock", 0))

            r = requests.post(
                f"{BASE}/inventory_levels/set.json",
                headers=_headers(),
                json={
                    "location_id":       location_id,
                    "inventory_item_id": shopify_var["inventory_item_id"],
                    "available":         stock,
                },
                timeout=30,
            )
            r.raise_for_status()

        # Assign to Shopify collections based on WooCommerce categories
        for cat_name in normalized.get("categories", []):
            try:
                col_id = get_or_create_custom_collection(cat_name)
                add_product_to_collection(col_id, product["id"])
            except Exception as e:
                logger.warning(f"  Could not assign collection '{cat_name}': {e}")

        return product

    except requests.HTTPError as e:
        logger.error(f"  ✗ Failed to create '{title}': {e.response.text if e.response else e}")
        return None
    except Exception as e:
        logger.error(f"  ✗ Failed to create '{title}': {e}")
        return None

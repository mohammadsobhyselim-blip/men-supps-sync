# men-supps sync engine

Python service that keeps **men-supps.com** (Shopify) in sync with multiple suppliers.
Designed to run continuously on Railway, scheduled via APScheduler.

---

## What this project does

`men-supps.com` is a Shopify store that aggregates products from several suppliers.
Each product on Shopify is tagged with a `custom.supplier` metafield that tells
this service where to fetch the live price and stock from.

### Full cycle (runs every N hours, default 6)

**Step 1 — Discover new MFsupps products**
- Pull the full product list from MFsupps.
- Compare against every SKU currently in Shopify.
- Anything missing → create a new **published** Shopify product with:
  - same price as MF
  - current MF stock
  - all images
  - description, brand, weight, barcode, tags
  - `custom.supplier = "MF"` metafield set automatically

**Step 2 — Sync price + stock for every supplier-tagged product**
- Loop over every Shopify product.
- Read `custom.supplier`.
  - `"MF"` → fetch via MFsupps API by SKU
  - `"fitnessbag"` → fetch via MyFitnessBag WooCommerce REST API by SKU
  - anything else → skip
- If price or stock differs, update the Shopify variant.

Shopify is never the source of truth for price/stock on these products —
suppliers are. Existing non-supplier products are never touched.

---

## Architecture

```
┌──────────────┐        ┌──────────────────┐
│ MFsupps API  │        │ MyFitnessBag     │
│ (existing)   │        │ WooCommerce API  │
└──────┬───────┘        └────────┬─────────┘
       │                         │
       ▼                         ▼
┌──────────────┐        ┌──────────────────┐
│ mfsupps.py   │        │ fitnessbag.py    │
│ (SKU lookup  │        │ (WC REST, handles│
│  + discovery)│        │  simple+variable)│
└──────┬───────┘        └────────┬─────────┘
       │                         │
       └────────┬────────────────┘
                ▼
       ┌─────────────────┐          ┌─────────────┐
       │ sync_engine.py  │◀────────▶│ importer.py │
       │ (routes by      │          │ (adds new MF│
       │  supplier field)│          │  products)  │
       └────────┬────────┘          └─────────────┘
                ▼
       ┌─────────────────────┐
       │ shopify_client.py   │
       │ (products, variants,│
       │  metafields, inv)   │
       └────────┬────────────┘
                ▼
       ┌─────────────────────┐
       │ men-supps.myshopify │
       └─────────────────────┘
```

### File layout

```
men-supps-sync/
├── main.py                    # entry point: scheduler + CLI flags
├── requirements.txt
├── Procfile                   # Railway: worker process
├── railway.toml               # Railway build + restart policy
├── .env.example               # template — copy to .env locally
├── .gitignore                 # excludes .env and logs
│
├── config/
│   └── settings.py            # loads env vars
│
├── core/
│   ├── shopify_client.py      # Shopify Admin API wrapper
│   ├── importer.py            # step 1: discover + create new MF products
│   └── sync_engine.py         # step 2: price/stock sync + orchestrates import
│
└── suppliers/
    ├── mfsupps.py             # MFsupps API: SKU lookup + full product list
    └── fitnessbag.py          # MyFitnessBag WooCommerce client
```

---

## Running it

### Local (for testing)

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in real credentials in .env
python main.py --dry-run        # simulate, never writes to Shopify
```

### CLI flags

| Command                        | Does                                                   |
|--------------------------------|--------------------------------------------------------|
| `python main.py`               | Scheduler: runs full cycle every `SYNC_INTERVAL_HOURS` |
| `python main.py --now`         | Full cycle once (import + sync)                        |
| `python main.py --dry-run`     | Full cycle once, simulate only                         |
| `python main.py --import-only` | Just discover+add new MF products                      |
| `python main.py --sync-only`   | Just price/stock sync                                  |

### Railway

- Connect GitHub repo → Railway auto-detects `railway.toml`
- Set env vars in Railway dashboard (never commit `.env`)
- `Procfile` declares this as a `worker` (no web port needed)

---

## Environment variables

See `.env.example` for the full template.

| Variable                       | Purpose                                 |
|--------------------------------|------------------------------------------|
| `SHOPIFY_STORE_URL`            | e.g. `men-supps.myshopify.com`           |
| `SHOPIFY_API_TOKEN`            | Admin API access token (`shpat_…`)       |
| `SHOPIFY_API_VERSION`          | `2024-01`                                |
| `MFSUPPS_API_URL`              | MFsupps API base                         |
| `MFSUPPS_API_KEY`              | Bearer token                             |
| `FITNESSBAG_API_URL`           | `https://myfitnessbag.com`               |
| `FITNESSBAG_CONSUMER_KEY`      | WooCommerce REST: `ck_…`                 |
| `FITNESSBAG_CONSUMER_SECRET`   | WooCommerce REST: `cs_…`                 |
| `SYNC_INTERVAL_HOURS`          | default `6`                              |
| `LOG_LEVEL`                    | `INFO` / `DEBUG`                         |

---

## Shopify setup (one-time)

1. Create a **custom metafield definition** on products:
   - Namespace: `custom`
   - Key: `supplier`
   - Type: Single line text
2. For every existing MFsupps product, set `custom.supplier = "MF"`.
3. For products from MyFitnessBag, set `custom.supplier = "fitnessbag"`.
4. Create an **Admin API access token** with these scopes:
   - `read_products`, `write_products`
   - `read_inventory`, `write_inventory`
   - `read_locations`

---

## What's confirmed vs what needs verification

### ✅ Confirmed
- MyFitnessBag is WooCommerce → uses standard `/wp-json/wc/v3/products` endpoint
  with HTTP Basic Auth (Consumer Key + Secret). This is stable.
- Shopify API endpoints, metafield structure, inventory_levels flow. Stable.
- Logic for simple vs variable WooCommerce products (fitnessbag.py handles both).

### ⚠️ Needs verification with first `--dry-run`
- **MFsupps API response shape.** `suppliers/mfsupps.py` currently tries several
  common shapes (direct object / list / nested `product`). Once we see the real
  response, trim it to the exact one.
  - Fields probed: `sku`, `price`, `stock`/`quantity`, `name`/`title`,
    `description`/`body_html`, `images`/`image_urls`, `brand`/`vendor`,
    `weight`, `tags`, `barcode`/`upc`.
- **MFsupps pagination.** Currently assumes `?page=N&per_page=100`. May be cursor-based.

---

## Design decisions

- **Metafield-driven routing, not category/product-type.** User rejected
  category-based routing — `custom.supplier` is the single source of truth.
- **Published-on-create for MF.** New MF products go live immediately, no draft review.
- **Price mirrored 1:1 from MF.** No markup applied at import.
- **Non-supplier products are invisible to this service.** Any product with no
  `custom.supplier` is skipped; the service will never edit/delete it.
- **Cairo timezone** for scheduler (user is in Egypt).

---

## Next steps for Claude Code

1. Run `python main.py --dry-run` against a staging store to verify MFsupps
   response shape matches `normalize_product()` in `suppliers/mfsupps.py`.
2. If MFsupps fields are named differently, adjust the `.get(...)` fallbacks.
3. Push to a fresh GitHub repo, connect to Railway, set env vars.
4. Run once with `--import-only --dry-run` before enabling full cycle.

### Possible improvements to consider
- **Metafield caching.** Currently `get_product_metafield_supplier()` does one
  API call per product. For large catalogs, fetch metafields in the initial
  product query (using GraphQL) or batch.
- **Change detection.** Currently updates price whenever it differs. Could
  track a last-synced hash to reduce writes.
- **Alerting.** Add webhook/email on errors > threshold.
- **MF deletions.** What happens if a product is removed from MF? Currently
  the sync just fails to find the SKU and logs a warning. Decide: auto-archive
  on Shopify, or leave alone?

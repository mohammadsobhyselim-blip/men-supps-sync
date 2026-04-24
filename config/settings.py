import os
from dotenv import load_dotenv

load_dotenv()

# Shopify
SHOPIFY_STORE_URL      = os.getenv("SHOPIFY_STORE_URL")
SHOPIFY_CLIENT_ID      = os.getenv("SHOPIFY_CLIENT_ID")
SHOPIFY_CLIENT_SECRET  = os.getenv("SHOPIFY_CLIENT_SECRET")
SHOPIFY_ACCESS_TOKEN   = os.getenv("SHOPIFY_ACCESS_TOKEN")
SHOPIFY_API_VERSION    = os.getenv("SHOPIFY_API_VERSION", "2025-01")

# Suppliers
MFSUPPS_API_URL           = os.getenv("MFSUPPS_API_URL")
MFSUPPS_CONSUMER_KEY      = os.getenv("MFSUPPS_CONSUMER_KEY")
MFSUPPS_CONSUMER_SECRET   = os.getenv("MFSUPPS_CONSUMER_SECRET")

FITNESSBAG_API_URL         = os.getenv("FITNESSBAG_API_URL")         # https://myfitnessbag.com
FITNESSBAG_CONSUMER_KEY    = os.getenv("FITNESSBAG_CONSUMER_KEY")    # ck_xxxx
FITNESSBAG_CONSUMER_SECRET = os.getenv("FITNESSBAG_CONSUMER_SECRET") # cs_xxxx

# Sync
SYNC_INTERVAL_HOURS = int(os.getenv("SYNC_INTERVAL_HOURS", 6))
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO")

# Metafield that tells us which supplier owns this product
SUPPLIER_METAFIELD_NAMESPACE = "custom"
SUPPLIER_METAFIELD_KEY       = "supplier"

# Supplier name values (as stored in the metafield)
SUPPLIER_MFSUPPS     = "MF"
SUPPLIER_FITNESSBAG  = "MFB"

# Validate required env vars at import time so failures are obvious
_REQUIRED = {
    "SHOPIFY_STORE_URL":          SHOPIFY_STORE_URL,
    "SHOPIFY_CLIENT_ID":          SHOPIFY_CLIENT_ID,
    "SHOPIFY_CLIENT_SECRET":      SHOPIFY_CLIENT_SECRET,
    "MFSUPPS_API_URL":            MFSUPPS_API_URL,
    "MFSUPPS_CONSUMER_KEY":       MFSUPPS_CONSUMER_KEY,
    "MFSUPPS_CONSUMER_SECRET":    MFSUPPS_CONSUMER_SECRET,
    "FITNESSBAG_API_URL":         FITNESSBAG_API_URL,
    "FITNESSBAG_CONSUMER_KEY":    FITNESSBAG_CONSUMER_KEY,
    "FITNESSBAG_CONSUMER_SECRET": FITNESSBAG_CONSUMER_SECRET,
}
_missing = [k for k, v in _REQUIRED.items() if not v]
if _missing:
    raise EnvironmentError(f"Missing required env vars: {', '.join(_missing)}")

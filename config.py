"""Central settings. Override any value with an environment variable."""
import os

SITE_URL = os.environ.get("SITE_URL", "https://analyst-assessment-production.up.railway.app").rstrip("/")
API_URL = os.environ.get("API_URL", SITE_URL + "/api/v1").rstrip("/")
CRM_TOKEN = os.environ.get("CRM_TOKEN", "")

PARENT_NAME = "Bellhaven Senior Living (Parent Account)"

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
PROPOSALS_FILE = os.path.join(DATA_DIR, "proposals.json")
DECISIONS_FILE = os.path.join(DATA_DIR, "decisions.json")
LOCATIONS_FILE = os.path.join(DATA_DIR, "website_locations.json")
CRM_SNAPSHOT_FILE = os.path.join(DATA_DIR, "crm_snapshot.json")
RUN_LOG_FILE = os.path.join(DATA_DIR, "run_log.jsonl")

# Safety: abort a run if the scrape returns far fewer locations than last time
# (a broken scrape must never turn into "every facility left Bellhaven").
MIN_SCRAPE_RATIO = 0.8
MIN_LOCATIONS = 10

REVIEW_PORT = int(os.environ.get("REVIEW_PORT", "8000"))

# Website care offerings -> CRM care_type picklist values
CARE_TYPE_MAP = {
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "short-term rehabilitation and nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "independent living": "Independent Living",
}

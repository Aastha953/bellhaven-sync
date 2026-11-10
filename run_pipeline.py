"""Daily job: scrape website -> pull CRM -> match -> write review queue.

Never writes to the CRM. Writes happen only from the review app after approval.
Safe to re-run: items already approved/rejected are never re-proposed.
"""
import sys
from collections import Counter
from datetime import datetime, timezone

import config
import storage
from crm import CRM
from matcher import build_proposals
from scraper import scrape


def main():
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{started}] Bellhaven sync starting")
    if not config.CRM_TOKEN:
        print("  ERROR: CRM_TOKEN is not set. Set it as an environment variable (locally) "
              "or a repository secret (GitHub Actions).")
        return 2

    locations, meta = scrape()
    print(f"  website: {meta}")

    # Safety gate: a partial scrape must not create false "not on website" flags
    prev = storage.load(config.LOCATIONS_FILE, [])
    floor = max(config.MIN_LOCATIONS, int(len(prev) * config.MIN_SCRAPE_RATIO))
    if len(locations) < floor:
        msg = f"ABORT: scraped {len(locations)} locations, expected >= {floor}. Queue left unchanged."
        print("  " + msg)
        storage.append_jsonl(config.RUN_LOG_FILE, {"at": started, "status": "aborted", "reason": msg, "meta": meta})
        return 1
    if meta.get("directory_says") and meta["directory_says"] > meta["unique_links"]:
        print(f"  WARNING: directory says {meta['directory_says']} but only {meta['unique_links']} links found")

    accounts = CRM().list_accounts()
    print(f"  CRM: {len(accounts)} accounts")

    decisions = storage.load(config.DECISIONS_FILE, {})
    decided = frozenset(k for k, v in decisions.items() if v.get("decision") in ("approved", "rejected"))
    proposals = build_proposals(locations, accounts, decided)

    storage.save(config.LOCATIONS_FILE, locations)
    storage.save(config.CRM_SNAPSHOT_FILE, accounts)
    storage.save(config.PROPOSALS_FILE, {"generated_at": started, "website_meta": meta, "proposals": proposals})

    counts = Counter(p["type"] for p in proposals)
    storage.append_jsonl(config.RUN_LOG_FILE, {"at": started, "status": "ok", "locations": len(locations),
                                               "accounts": len(accounts), "pending": len(proposals),
                                               "already_decided": len(decided), "by_type": counts})
    print(f"  {len(proposals)} proposals pending review ({len(decided)} previously decided, skipped)")
    for t, n in counts.most_common():
        print(f"    {t:<16} {n}")
    print(f"  Review them: python review_app.py  ->  http://localhost:{config.REVIEW_PORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Independent audit of the CRM end state against the live website.

Does not trust decisions.json or proposals: it re-scrapes the site, pulls every
account, and checks the rules directly. Run after reviewing:

    python verify_crm.py            (writes data/verification_report.txt)
"""
import sys
from collections import Counter
from difflib import SequenceMatcher

import config
import storage
from crm import CRM
from matcher import (address_match, build_proposals, is_parent_account, money, norm_name)
from scraper import scrape


def audit(locations, accounts, decisions):
    by_id = {a["account_id"]: a for a in accounts}
    parent = next(a for a in accounts if (a.get("name") or "").strip() == config.PARENT_NAME)
    pid = parent["account_id"]
    facilities = [a for a in accounts if not is_parent_account(a)]
    live = [a for a in facilities if not a.get("duplicate_of_account") and not a.get("chow_current_account")]
    ok, issues, info = [], [], []

    # 1. every website community -> exactly one live account, under Bellhaven, current name
    matched = set()
    for loc in locations:
        hits = [a for a in live if address_match(loc, a) >= 0.8 or (
            (a.get("billing_city") or "").lower() == loc["city"].lower()
            and (a.get("billing_state") or "").upper() == loc["state"]
            and SequenceMatcher(None, norm_name(a.get("name")), norm_name(loc["name"])).ratio() >= 0.75)]
        matched.update(a["account_id"] for a in hits)
        tag = f"{loc['name']} ({loc['city']}, {loc['state']})"
        if len(hits) != 1:
            issues.append(f"{tag}: {len(hits)} live accounts {[a['account_id'] for a in hits]} (expected 1)")
            continue
        a = hits[0]
        probs = []
        if a.get("parent_id") != pid:
            probs.append(f"parent is {a.get('parent_name') or '(none)'}")
        if (a.get("name") or "").strip() != loc["name"]:
            probs.append(f"name is '{a.get('name')}'")
        if a.get("status") != "Active":
            probs.append(f"status {a.get('status')}")
        (issues if probs else ok).append(f"{tag} -> {a['account_id']}" + (f": {'; '.join(probs)}" if probs else ""))

    # 2. CHOW-preserved accounts: untouched parent, valid pointer, SOP condition actually met
    for a in facilities:
        t = a.get("chow_current_account")
        if not t:
            continue
        tgt = by_id.get(t)
        line = f"CHOW {a['name']} ({a['account_id']}) -> {t}"
        if not tgt:
            issues.append(line + ": target account does not exist")
        elif not (money(a.get("lifetime_revenue")) > 0 and money(a.get("outstanding_ar")) > 0):
            issues.append(line + ": SOP not met (needs revenue AND AR > 0) - should have been re-parented")
        else:
            ok.append(line + f" [{tgt['name']} under {tgt.get('parent_name')}]; old parent kept: {a.get('parent_name')}")

    # 3. duplicate losers: Inactive, survivor exists and is itself live
    for a in facilities:
        s = a.get("duplicate_of_account")
        if not s:
            continue
        sv = by_id.get(s)
        line = f"Duplicate {a['name']} ({a['account_id']}) -> {s}"
        if not sv or sv.get("duplicate_of_account"):
            issues.append(line + ": survivor missing or itself a duplicate")
        elif a.get("status") != "Inactive":
            issues.append(line + f": status is {a.get('status')}, expected Inactive")
        else:
            ok.append(line + f" [{sv['name']}]")

    # 4. Bellhaven-parented accounts not on the website
    rejected_ids = {v.get("account_id") for v in decisions.values() if v.get("decision") == "rejected"}
    for a in live:
        if a.get("parent_id") == pid and a["account_id"] not in matched:
            line = f"Not on website: {a['name']} ({a['account_id']}) status={a.get('status')}"
            if a.get("status") == "Needs Review":
                ok.append(line)
            elif a["account_id"] in rejected_ids:
                info.append(line + " (proposal deliberately rejected by reviewer)")
            else:
                issues.append(line + " (unresolved)")

    # 5. decision ledger + what a fresh run would still propose
    states = Counter(v.get("decision") for v in decisions.values())
    stuck = [v.get("title") for v in decisions.values() if v.get("decision") == "applying" or v.get("error")]
    for t in stuck:
        issues.append(f"Write not fully verified: {t}")
    decided = frozenset(k for k, v in decisions.items() if v.get("decision") in ("approved", "rejected"))
    pending = build_proposals(locations, accounts, decided)
    for p in pending:
        issues.append(f"Still pending: {p['title']}")

    summary = {
        "website_communities": len(locations), "crm_accounts": len(accounts),
        "created_by_pipeline": sum(1 for a in accounts if a.get("created_by_candidate")),
        "approved": states.get("approved", 0), "rejected": states.get("rejected", 0),
        "unverified_writes": len(stuck), "still_pending": len(pending),
        "checks_passed": len(ok), "issues": len(issues),
    }
    return summary, ok, issues, info


def main():
    locations, _ = scrape()
    accounts = CRM().list_accounts()
    decisions = storage.load(config.DECISIONS_FILE, {})
    summary, ok, issues, info = audit(locations, accounts, decisions)

    lines = ["BELLHAVEN CRM END-STATE AUDIT", "=" * 30]
    lines += [f"{k:<22} {v}" for k, v in summary.items()]
    lines += ["", f"ISSUES ({len(issues)})"] + [f"  ! {x}" for x in issues]
    lines += ["", f"NOTES ({len(info)})"] + [f"  - {x}" for x in info]
    lines += ["", f"PASSED ({len(ok)})"] + [f"  ok {x}" for x in ok]
    report = "\n".join(lines)
    print(report)
    path = config.DATA_DIR + "/verification_report.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(f"\nSaved to {path}")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())

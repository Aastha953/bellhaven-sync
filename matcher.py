"""Match website locations to CRM accounts and turn findings into proposals.

Every proposal carries:
  key       deterministic id -> re-runs never re-propose a decided item
  evidence  human-readable reasons shown to the reviewer
  changes   field-level before/after for display
  ops       the exact API calls to run if (and only if) a reviewer approves
"""
import hashlib
import json
import re
from datetime import date
from difflib import SequenceMatcher

import config

# ---------------------------------------------------------------- normalizers
STREET_ABBR = {
    "street": "st", "avenue": "ave", "av": "ave", "boulevard": "blvd", "road": "rd",
    "drive": "dr", "lane": "ln", "court": "ct", "place": "pl", "parkway": "pkwy",
    "highway": "hwy", "pk": "pike", "circle": "cir", "terrace": "ter", "way": "way", "pike": "pike",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
    "saint": "st", "suite": "ste",
}


def norm_street(s):
    s = re.sub(r"[.,#]", " ", (s or "").lower())
    return " ".join(STREET_ABBR.get(t, t) for t in s.split())


def street_number(s):
    m = re.match(r"\s*(\d+)", s or "")
    return m.group(1) if m else ""


def norm_name(s):
    s = (s or "").lower().replace("&", " and ")
    s = s.replace("centre", "center").replace("healthcare", "health care")
    s = re.sub(r"\brehabilitation\b", "rehab", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in {"the"})


def sim(a, b):
    return SequenceMatcher(None, a, b).ratio()


def money(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def make_key(*parts):
    raw = json.dumps(parts, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def care_types_for(loc):
    return [config.CARE_TYPE_MAP.get(o.lower(), o) for o in loc.get("care_offerings", [])]


# ---------------------------------------------------------------- helpers
def is_parent_account(a):
    return "(parent account)" in (a.get("name") or "").lower()


def is_retired(a):
    """Accounts we already resolved: duplicate losers and CHOW-preserved originals."""
    return bool(a.get("duplicate_of_account")) or bool(a.get("chow_current_account"))


def address_match(loc, a):
    if not loc["zip"] or (a.get("billing_zip") or "")[:5] != loc["zip"]:
        return 0.0
    if street_number(a.get("billing_street")) != street_number(loc["street"]) or not street_number(loc["street"]):
        return 0.0
    return sim(norm_street(a.get("billing_street")), norm_street(loc["street"]))


def address_equal(loc, a):
    return (norm_street(a.get("billing_street")) == norm_street(loc["street"])
            and (a.get("billing_city") or "").strip().lower() == loc["city"].lower()
            and (a.get("billing_state") or "").strip().upper() == loc["state"]
            and (a.get("billing_zip") or "")[:5] == loc["zip"])


def snapshot(a):
    keys = ["account_id", "name", "parent_id", "parent_name", "billing_street", "billing_city",
            "billing_state", "billing_zip", "care_type", "status", "phone",
            "lifetime_revenue", "outstanding_ar", "chow_current_account", "duplicate_of_account", "note"]
    return {k: a.get(k) for k in keys}


def append_note(existing, text):
    stamp = f"[Bellhaven sync {date.today().isoformat()}] {text}"
    return f"{existing.strip()} | {stamp}" if (existing or "").strip() else stamp


def pick_survivor(accounts, loc, parent_id):
    """Keep the record billing/sales depends on most; deterministic tie-break."""
    def rank(a):
        filled = sum(1 for k in ("billing_street", "billing_city", "billing_zip", "phone", "care_type") if a.get(k))
        return (
            money(a.get("lifetime_revenue")) > 0 or money(a.get("outstanding_ar")) > 0,
            a.get("parent_id") == parent_id,
            a.get("status") == "Active",
            loc is not None and (a.get("name") or "").strip().lower() == loc["name"].lower(),
            loc is not None and address_equal(loc, a),
            filled,
        )
    return sorted(accounts, key=lambda a: (rank(a), a["account_id"]), reverse=True)[0]


def near_misses(loc, facilities, limit=2):
    """Closest non-matching accounts, shown so the reviewer can rule them out."""
    scored = []
    strip = lambda n: " ".join(t for t in norm_name(n).split() if t not in {"bellhaven", "of", "at"})
    for a in facilities:
        s = sim(strip(a.get("name")), strip(loc["name"]))
        same_city = (a.get("billing_city") or "").strip().lower() == loc["city"].lower()
        if s >= 0.6 or (same_city and s >= 0.4):
            scored.append((s + (0.2 if same_city else 0), a))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [f"Ruled out: {a['name']} ({a['account_id']}) at {a.get('billing_street')}, {a.get('billing_city')}, "
            f"{a.get('billing_state')} {a.get('billing_zip')} - different address." for _, a in scored[:limit]]


# ---------------------------------------------------------------- main
def build_proposals(locations, accounts, decided_keys=frozenset()):
    by_id = {a["account_id"]: a for a in accounts}
    parent = next((a for a in accounts if (a.get("name") or "").strip() == config.PARENT_NAME), None)
    if not parent:
        raise RuntimeError(f"Parent account '{config.PARENT_NAME}' not found in CRM")
    pid = parent["account_id"]

    facilities = [a for a in accounts if not is_parent_account(a) and not is_retired(a)]
    proposals = []

    def propose(p):
        if p["key"] not in decided_keys:
            proposals.append(p)

    # ---- 1. match each website location to CRM accounts
    matches = {}  # slug -> list of (account, score, how)
    for loc in locations:
        found = []
        for a in facilities:
            s_addr = address_match(loc, a)
            s_name = sim(norm_name(a.get("name")), norm_name(loc["name"]))
            same_city = ((a.get("billing_city") or "").strip().lower() == loc["city"].lower()
                         and (a.get("billing_state") or "").strip().upper() == loc["state"])
            if s_addr >= 0.8:
                found.append((a, s_addr, "address"))
            elif same_city and s_name >= 0.75:
                found.append((a, s_name, "name+city"))
        matches[loc["slug"]] = found

    # an account may only belong to one location: keep its best match
    best_for_acc = {}
    for slug, found in matches.items():
        for a, score, how in found:
            cur = best_for_acc.get(a["account_id"])
            rank = (how == "address", score)
            if cur is None or rank > cur[0]:
                best_for_acc[a["account_id"]] = (rank, slug)
    for slug in matches:
        matches[slug] = [m for m in matches[slug] if best_for_acc[m[0]["account_id"]][1] == slug]

    matched_ids = set()
    for loc in locations:
        found = matches[loc["slug"]]
        evidence_site = (f"Website: {loc['name']} - {loc['street']}, {loc['city']}, {loc['state']} {loc['zip']} "
                         f"(listed on {', '.join(loc.get('found_on', []))})")

        # ---- no CRM account -> create under Bellhaven
        if not found:
            body = {
                "name": loc["name"], "parent_id": pid, "billing_street": loc["street"],
                "billing_city": loc["city"], "billing_state": loc["state"], "billing_zip": loc["zip"],
                "care_type": (care_types_for(loc) or [""])[0], "status": "Active", "phone": loc.get("phone", ""),
                "note": append_note("", f"Created from Bellhaven website ({loc['url']}); no matching CRM account found."),
            }
            propose({
                "key": make_key("create", loc["slug"]), "type": "create", "confidence": "high",
                "title": f"Create account: {loc['name']}",
                "account_id": None, "location": loc, "crm": None,
                "evidence": [evidence_site,
                             "No CRM account at this address (zip + street number + street name) "
                             "and no similarly named account in the same city.",
                             *near_misses(loc, facilities)],
                "changes": [{"field": k, "from": "", "to": v} for k, v in body.items() if k != "note"],
                "ops": [{"op": "create", "body": body, "ref": "new"}],
            })
            continue

        accs = [m[0] for m in found]
        how = {m[0]["account_id"]: f"{m[2]} match ({m[1]:.0%})" for m in found}
        survivor = pick_survivor(accs, loc, pid)
        matched_ids.update(a["account_id"] for a in accs)
        conf = "high" if any(m[2] == "address" for m in found if m[0] is survivor) else "medium"
        base_ev = [evidence_site, f"CRM: {survivor['name']} ({survivor['account_id']}) - "
                                  f"{survivor.get('billing_street')}, {survivor.get('billing_city')}, "
                                  f"{survivor.get('billing_state')} {survivor.get('billing_zip')} "
                                  f"[{how[survivor['account_id']]}]"]

        # ---- duplicates
        for loser in accs:
            if loser is survivor:
                continue
            both_billed = all(money(x.get("lifetime_revenue")) > 0 for x in (loser, survivor))
            propose({
                "key": make_key("duplicate", loser["account_id"], survivor["account_id"]),
                "type": "duplicate", "confidence": "medium" if both_billed else "high",
                "title": f"Duplicate: {loser['name']} ({loser['account_id']}) -> keep {survivor['account_id']}",
                "account_id": loser["account_id"], "location": loc, "crm": snapshot(loser),
                "survivor": snapshot(survivor),
                "evidence": [evidence_site,
                             f"Both {loser['account_id']} and {survivor['account_id']} match this location "
                             f"({how[loser['account_id']]} / {how[survivor['account_id']]}).",
                             f"Survivor chosen by: billing history, already under Bellhaven, Active, "
                             f"exact website name/address, completeness.",
                             *(["WARNING: both copies have billing history - confirm with billing before approving."]
                               if both_billed else [])],
                "changes": [{"field": "duplicate_of_account", "from": loser.get("duplicate_of_account") or "", "to": survivor["account_id"]},
                            {"field": "status", "from": loser.get("status"), "to": "Inactive"}],
                "ops": [{"op": "patch", "account_id": loser["account_id"], "body": {
                    "duplicate_of_account": survivor["account_id"], "status": "Inactive",
                    "note": append_note(loser.get("note"), f"Duplicate of {survivor['account_id']} ({survivor['name']}).")}}],
            })

        a = survivor
        # ---- wrong parent -> SOP: CHOW vs direct re-parent
        if a.get("parent_id") != pid:
            rev, ar = money(a.get("lifetime_revenue")), money(a.get("outstanding_ar"))
            sop = (f"SOP check: lifetime_revenue={rev:,.0f}, outstanding_ar={ar:,.0f}. ")
            old_parent = a.get("parent_name") or "(no parent)"
            if rev > 0 and ar > 0:
                body = {
                    "name": loc["name"], "parent_id": pid, "billing_street": loc["street"],
                    "billing_city": loc["city"], "billing_state": loc["state"], "billing_zip": loc["zip"],
                    "care_type": a.get("care_type") or (care_types_for(loc) or [""])[0],
                    "status": "Active", "phone": a.get("phone") or loc.get("phone", ""),
                    "note": append_note("", f"CHOW: new account for {loc['name']} under Bellhaven. "
                                            f"Prior-owner account {a['account_id']} ({old_parent}) preserved for billing."),
                }
                propose({
                    "key": make_key("chow", a["account_id"], pid), "type": "chow", "confidence": conf,
                    "title": f"Change of ownership (CHOW): {a['name']} -> new account under Bellhaven",
                    "account_id": a["account_id"], "location": loc, "crm": snapshot(a),
                    "evidence": base_ev + [f"Currently under {old_parent}; website lists it as a Bellhaven community.",
                                           sop + "Revenue AND open AR -> old account preserved untouched; "
                                                 "new account created under Bellhaven, old.chow_current_account -> new."],
                    "changes": [{"field": "NEW account", "from": "", "to": f"{loc['name']} under Bellhaven"},
                                {"field": "old.chow_current_account", "from": "", "to": "<new account id>"},
                                {"field": "old.parent_id", "from": old_parent, "to": "unchanged (SOP)"}],
                    "ops": [{"op": "create", "body": body, "ref": "new"},
                            {"op": "patch", "account_id": a["account_id"], "body": {"chow_current_account": "$new"}}],
                })
                continue  # old account must stay exactly as is: no other fixes
            propose({
                "key": make_key("reparent", a["account_id"], pid), "type": "reparent", "confidence": conf,
                "title": f"Re-parent: {a['name']} ({old_parent} -> Bellhaven)",
                "account_id": a["account_id"], "location": loc, "crm": snapshot(a),
                "evidence": base_ev + [f"Currently under {old_parent}; website lists it as a Bellhaven community.",
                                       sop + "Not both > 0 -> re-parent the existing account directly."],
                "changes": [{"field": "parent_id", "from": f"{a.get('parent_id') or ''} {old_parent}".strip(),
                             "to": f"{pid} {config.PARENT_NAME}"}],
                "ops": [{"op": "patch", "account_id": a["account_id"], "body": {
                    "parent_id": pid,
                    "note": append_note(a.get("note"), f"Re-parented from {old_parent} to Bellhaven "
                                                       f"(revenue {rev:,.0f}, AR {ar:,.0f})."),
                }}],
            })

        # ---- outdated name
        if (a.get("name") or "").strip().lower() != loc["name"].strip().lower():
            propose({
                "key": make_key("rename", a["account_id"], loc["name"]), "type": "rename", "confidence": conf,
                "title": f"Rename: {a['name']} -> {loc['name']}",
                "account_id": a["account_id"], "location": loc, "crm": snapshot(a),
                "evidence": base_ev + ["Website is the source of truth for the community's current name."],
                "changes": [{"field": "name", "from": a.get("name"), "to": loc["name"]}],
                "ops": [{"op": "patch", "account_id": a["account_id"], "body": {"name": loc["name"]}}],
            })

        # ---- outdated / wrong address (ignores pure abbreviation differences)
        if not address_equal(loc, a):
            new = {"billing_street": loc["street"], "billing_city": loc["city"],
                   "billing_state": loc["state"], "billing_zip": loc["zip"]}
            propose({
                "key": make_key("address", a["account_id"], new), "type": "address", "confidence": conf,
                "title": f"Update address: {a['name']}",
                "account_id": a["account_id"], "location": loc, "crm": snapshot(a),
                "evidence": base_ev + ["Address differs from the website beyond abbreviation differences."],
                "changes": [{"field": k, "from": a.get(k), "to": v} for k, v in new.items() if (a.get(k) or "") != v],
                "ops": [{"op": "patch", "account_id": a["account_id"], "body": new}],
            })

        # ---- care type no longer offered
        offered = care_types_for(loc)
        if offered and a.get("care_type") not in offered:
            propose({
                "key": make_key("care_type", a["account_id"], offered[0]), "type": "care_type", "confidence": "medium",
                "title": f"Update care type: {a['name']}",
                "account_id": a["account_id"], "location": loc, "crm": snapshot(a),
                "evidence": base_ev + [f"Website care offerings: {', '.join(loc['care_offerings'])} "
                                       f"(-> CRM picklist: {', '.join(offered)})."],
                "changes": [{"field": "care_type", "from": a.get("care_type"), "to": offered[0]}],
                "ops": [{"op": "patch", "account_id": a["account_id"], "body": {"care_type": offered[0]}}],
            })

        # ---- inactive but operating
        if a.get("status") != "Active":
            propose({
                "key": make_key("reactivate", a["account_id"]), "type": "reactivate", "confidence": conf,
                "title": f"Reactivate: {a['name']}",
                "account_id": a["account_id"], "location": loc, "crm": snapshot(a),
                "evidence": base_ev + [f"CRM status is {a.get('status')} but the community is live on the website."],
                "changes": [{"field": "status", "from": a.get("status"), "to": "Active"}],
                "ops": [{"op": "patch", "account_id": a["account_id"], "body": {"status": "Active"}}],
            })

    # ---- 2. Bellhaven accounts that are not on the website
    orphans = [a for a in facilities if a.get("parent_id") == pid and a["account_id"] not in matched_ids]

    # duplicates among orphans (same normalized address)
    groups = {}
    for a in orphans:
        k = ((a.get("billing_zip") or "")[:5], norm_street(a.get("billing_street")))
        if k[0] and k[1]:
            groups.setdefault(k, []).append(a)
    losers = set()
    for grp in groups.values():
        if len(grp) < 2:
            continue
        surv = pick_survivor(grp, None, pid)
        for loser in grp:
            if loser is surv:
                continue
            losers.add(loser["account_id"])
            propose({
                "key": make_key("duplicate", loser["account_id"], surv["account_id"]),
                "type": "duplicate", "confidence": "high",
                "title": f"Duplicate: {loser['name']} -> keep {surv['account_id']}",
                "account_id": loser["account_id"], "location": None, "crm": snapshot(loser),
                "survivor": snapshot(surv),
                "evidence": [f"{loser['account_id']} and {surv['account_id']} share the same normalized address "
                             f"({loser.get('billing_street')} / {surv.get('billing_street')}, {loser.get('billing_zip')}).",
                             "Survivor chosen by billing history, Active status, completeness, then id."],
                "changes": [{"field": "duplicate_of_account", "from": "", "to": surv["account_id"]},
                            {"field": "status", "from": loser.get("status"), "to": "Inactive"}],
                "ops": [{"op": "patch", "account_id": loser["account_id"], "body": {
                    "duplicate_of_account": surv["account_id"], "status": "Inactive",
                    "note": append_note(loser.get("note"), f"Duplicate of {surv['account_id']} ({surv['name']}).")}}],
            })

    for a in orphans:
        if a["account_id"] in losers or a.get("status") in ("Needs Review", "Inactive"):
            continue
        billed = money(a.get("lifetime_revenue")) > 0 or money(a.get("outstanding_ar")) > 0
        rev, ar = money(a.get("lifetime_revenue")), money(a.get("outstanding_ar"))

        # Sold to another operator? Another parent already has an account at this exact address.
        pseudo = {"street": a.get("billing_street") or "", "zip": (a.get("billing_zip") or "")[:5]}
        new_owner = [b for b in facilities
                     if b["account_id"] != a["account_id"] and b.get("parent_id") and b.get("parent_id") != pid
                     and address_match(pseudo, b) >= 0.8]
        if new_owner:
            n = pick_survivor(new_owner, None, None)
            owner = n.get("parent_name") or n.get("parent_id")
            ev = [f"Not on the Bellhaven website, and {n['name']} ({n['account_id']}) under {owner} "
                  f"is at the same address ({n.get('billing_street')}, {n.get('billing_zip')}) -> facility changed hands.",
                  f"SOP check: lifetime_revenue={rev:,.0f}, outstanding_ar={ar:,.0f}."]
            if rev > 0 and ar > 0:
                propose({
                    "key": make_key("chow_existing", a["account_id"], n["account_id"]), "type": "chow", "confidence": "high",
                    "title": f"Change of ownership (CHOW): {a['name']} sold -> {n['name']}",
                    "account_id": a["account_id"], "location": None, "crm": snapshot(a), "survivor": snapshot(n),
                    "evidence": ev + ["Revenue AND open AR -> old Bellhaven account preserved untouched; the new owner's "
                                      "account already exists, so link it via chow_current_account (no new account needed)."],
                    "changes": [{"field": "chow_current_account", "from": "", "to": f"{n['account_id']} ({n['name']})"},
                                {"field": "parent_id / other fields", "from": "", "to": "unchanged (SOP)"}],
                    "ops": [{"op": "patch", "account_id": a["account_id"], "body": {"chow_current_account": n["account_id"]}}],
                })
            else:
                propose({
                    "key": make_key("duplicate", a["account_id"], n["account_id"]), "type": "duplicate", "confidence": "high",
                    "title": f"Sold: {a['name']} ({a['account_id']}) -> duplicate of {n['name']}",
                    "account_id": a["account_id"], "location": None, "crm": snapshot(a), "survivor": snapshot(n),
                    "evidence": ev + ["No revenue+AR to preserve -> retire the stale Bellhaven copy in favour of the "
                                      "new owner's account."],
                    "changes": [{"field": "duplicate_of_account", "from": "", "to": n["account_id"]},
                                {"field": "status", "from": a.get("status"), "to": "Inactive"}],
                    "ops": [{"op": "patch", "account_id": a["account_id"], "body": {
                        "duplicate_of_account": n["account_id"], "status": "Inactive",
                        "note": append_note(a.get("note"), f"Sold to {owner}; current account is {n['account_id']}.")}}],
                })
            continue

        propose({
            "key": make_key("not_on_website", a["account_id"]), "type": "not_on_website", "confidence": "medium",
            "title": f"Not on Bellhaven website: {a['name']}",
            "account_id": a["account_id"], "location": None, "crm": snapshot(a),
            "evidence": [f"Under the Bellhaven parent in CRM, but no website community matches its address "
                         f"({a.get('billing_street')}, {a.get('billing_city')}, {a.get('billing_state')} {a.get('billing_zip')}) or name.",
                         "Likely sold, closed, or mis-parented. The new owner is unknown, so we flag it for "
                         "Needs Review instead of guessing a new parent or deactivating it.",
                         *(["Has billing history - do NOT deactivate without billing sign-off."] if billed else [])],
            "changes": [{"field": "status", "from": a.get("status"), "to": "Needs Review"},
                        {"field": "note", "from": a.get("note") or "", "to": "(+ explanation)"}],
            "ops": [{"op": "patch", "account_id": a["account_id"], "body": {
                "status": "Needs Review",
                "note": append_note(a.get("note"), "No longer listed on bellhaven website; verify current owner "
                                                   "before re-parenting or deactivating.")}}],
        })

    order = {"chow": 0, "reparent": 1, "create": 2, "duplicate": 3, "rename": 4, "address": 5,
             "reactivate": 6, "care_type": 7, "not_on_website": 8}
    proposals.sort(key=lambda p: (order.get(p["type"], 9), p["title"]))
    return proposals

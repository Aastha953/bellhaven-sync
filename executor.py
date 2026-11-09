"""Apply an approved proposal's ops to the CRM, resumably, then verify.

Each op moves through two recorded states:
  written   - the API call succeeded (a retry must NOT repeat it, or a create would duplicate)
  verified  - a fresh read-back confirmed every written field

A proposal is only marked "approved" once every op is verified. If verification
fails, the item stays in the queue with the error; Approve again re-checks the
read-back (without re-sending the write) and re-sends only if the value is still
wrong on a PATCH, which is idempotent. Creates are never re-sent.
"""
from datetime import datetime, timezone

import config
import storage
from crm import CRM


def _resolve(body, refs):
    return {k: (refs[v[1:]] if isinstance(v, str) and v.startswith("$") and v[1:] in refs else v)
            for k, v in body.items()}


def _mismatches(crm, account_id, body):
    """Re-read the account and list fields that did not stick (notes/phone are free text, skipped)."""
    acc = crm.get_account(account_id)
    return [k for k, v in body.items()
            if k not in ("note", "phone") and str(acc.get(k) or "").strip() != str(v or "").strip()]


def apply(proposal, decisions, reviewer_note=""):
    key = proposal["key"]
    state = decisions.get(key) or {}
    state.setdefault("decision", "applying")
    state.setdefault("written", state.pop("done", []))   # back-compat with the earlier field name
    state.setdefault("verified", [])
    state.setdefault("targets", {})
    state.setdefault("refs", {})
    state.setdefault("results", [])
    state["ops"] = proposal["ops"]                         # keep the exact calls for later audit
    decisions[key] = state
    crm = CRM()

    def save():
        storage.save(config.DECISIONS_FILE, decisions)

    try:
        for i, op in enumerate(proposal["ops"]):
            si = str(i)
            body = _resolve(op["body"], state["refs"])

            if i not in state["written"]:
                if op["op"] == "create":
                    acc = crm.create_account(body)
                    state["refs"][op.get("ref", "new")] = acc["account_id"]
                    state["targets"][si] = acc["account_id"]
                else:
                    crm.update_account(op["account_id"], body)
                    state["targets"][si] = op["account_id"]
                state["written"].append(i)
                save()                                     # recorded before verifying: never re-create

            if i in state["verified"]:
                continue
            target = state["targets"].get(si) or op.get("account_id")
            bad = _mismatches(crm, target, body)
            if bad and op["op"] == "patch":
                crm.update_account(target, body)           # PATCH is idempotent: safe to re-send once
                bad = _mismatches(crm, target, body)
            if bad:
                raise RuntimeError(f"{op['op']} {target} written but read-back differs on {bad}; "
                                   f"item left pending - check it in the CRM browser")
            state["verified"].append(i)
            state["results"].append(f"{op['op']} {target}: verified by read-back")
            save()

        if len(state["verified"]) != len(proposal["ops"]):
            raise RuntimeError("not every operation was verified")
        state.update(decision="approved", at=_now(), reviewer_note=reviewer_note, title=proposal["title"],
                     type=proposal["type"], account_id=proposal.get("account_id"))
        state.pop("error", None)
        save()
        return True, state["results"]
    except Exception as e:  # noqa: BLE001 - surface any failure to the reviewer
        state["decision"] = "applying"
        state["error"] = str(e)
        save()
        return False, [str(e)]


def reject(proposal, decisions, reviewer_note=""):
    decisions[proposal["key"]] = {"decision": "rejected", "at": _now(), "reviewer_note": reviewer_note,
                                  "title": proposal["title"], "type": proposal["type"],
                                  "account_id": proposal.get("account_id")}
    storage.save(config.DECISIONS_FILE, decisions)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

"""Apply an approved proposal's ops to the CRM, resumably, then verify.

Progress is saved after every op, so if op 2 fails after op 1 created an
account, a retry continues at op 2 instead of creating a second account.
"""
from datetime import datetime, timezone

import config
import storage
from crm import CRM


def _resolve(body, refs):
    return {k: (refs[v[1:]] if isinstance(v, str) and v.startswith("$") and v[1:] in refs else v)
            for k, v in body.items()}


def _verify(crm, account_id, body):
    """Re-read the account and confirm every field we wrote actually stuck."""
    acc = crm.get_account(account_id)
    bad = [k for k, v in body.items()
           if k not in ("note", "phone") and str(acc.get(k) or "").strip() != str(v or "").strip()]
    return acc, bad


def apply(proposal, decisions, reviewer_note=""):
    key = proposal["key"]
    state = decisions.get(key) or {"decision": "applying", "done": [], "refs": {}, "results": []}
    decisions[key] = state
    crm = CRM()
    try:
        for i, op in enumerate(proposal["ops"]):
            if i in state["done"]:
                continue
            body = _resolve(op["body"], state["refs"])
            if op["op"] == "create":
                acc = crm.create_account(body)
                state["refs"][op.get("ref", "new")] = acc["account_id"]
                target = acc["account_id"]
            else:
                crm.update_account(op["account_id"], body)
                target = op["account_id"]
            # record the write before verifying, so a retry never repeats it
            state["done"].append(i)
            storage.save(config.DECISIONS_FILE, decisions)
            acc, bad = _verify(crm, target, body)
            if bad:
                raise RuntimeError(f"wrote {target} but read-back differs on {bad}; check it in the CRM browser")
            state["results"].append(f"{op['op']} {target}: verified by read-back")
            storage.save(config.DECISIONS_FILE, decisions)
        state.update(decision="approved", at=_now(), reviewer_note=reviewer_note, title=proposal["title"],
                     type=proposal["type"], account_id=proposal.get("account_id"))
        state.pop("error", None)
        storage.save(config.DECISIONS_FILE, decisions)
        return True, state["results"]
    except Exception as e:  # noqa: BLE001 - surface any failure to the reviewer
        state["error"] = str(e)
        storage.save(config.DECISIONS_FILE, decisions)
        return False, [str(e)]


def reject(proposal, decisions, reviewer_note=""):
    decisions[proposal["key"]] = {"decision": "rejected", "at": _now(), "reviewer_note": reviewer_note,
                                  "title": proposal["title"], "type": proposal["type"],
                                  "account_id": proposal.get("account_id")}
    storage.save(config.DECISIONS_FILE, decisions)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

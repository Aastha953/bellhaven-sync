# Bellhaven Ownership Sync

Keeps the CRM's picture of which facilities Bellhaven Senior Living owns in line with Bellhaven's public website.
Every day it scrapes the website, compares it to the CRM, and queues proposed fixes. A human approves or rejects
each one in a small review app. **Nothing is written to the CRM without approval.**

```
website ──scrape──┐
                  ├─► matcher ─► review queue (proposals.json) ─► review app ─► approve ─► CRM API (+ read-back check)
CRM API ──read────┘                                                     └─► reject ──► decisions.json (never re-proposed)
```

## Run it

Requires Python 3.10+ and `requests` (`python -m pip install --user requests`). Everything else is the standard library.

```bash
python run_pipeline.py      # scrape + match -> builds the review queue (read-only against the CRM)
python review_app.py        # open http://localhost:8000 and approve / reject
```

The token can be set with `CRM_TOKEN` (an environment variable). `config.py` falls back to the sandbox token.

| File | Purpose |
|---|---|
| `scraper.py` | Crawls every page of `/communities?page=N` **and** community links on the homepage/About page. Parses name, street, city, state, zip, care offerings, administrator, phone. |
| `matcher.py` | Links each location to CRM accounts, classifies, and builds proposals with evidence and the exact API calls. |
| `review_app.py` | Local web app: website-vs-CRM side by side, before/after diff, evidence, Approve / Reject with a reviewer note. |
| `executor.py` | Runs the approved API calls, re-reads each account to verify the write landed, and can resume after a failure. |
| `run_pipeline.py` | The daily job. |
| `schedule/` and `.github/workflows/` | Cron entry and GitHub Actions workflow (daily at 6:17 AM ET). |
| `data/decisions.json` | Ledger of every approve/reject decision. This is what makes re-runs safe. |

## How matching works

1. **Normalize.** Street suffixes and directions are abbreviated (`Boulevard→blvd`, `West→w`, `Northwest→nw`, `Saint→st`). In names, `&`, `Centre/Center`, `Healthcare/Health Care`, `Rehabilitation/Rehab`, and a leading `The` are treated as the same.
2. **Address match (high confidence).** Same 5-digit zip, same street number, and normalized street similarity of at least 80%.
3. **Name + city fallback (medium confidence).** Same city and state, with name similarity of at least 75%. This catches records whose address is stale or wrong.
4. **One location per account.** If an account matches two locations, it keeps its best match.
5. **Excluded from matching:** parent accounts, accounts already marked as duplicates (`duplicate_of_account` set), and old accounts preserved by a CHOW (`chow_current_account` set). After an approval these drop out naturally, so the next run doesn't flag them again.

## What gets proposed

| Finding | Action |
|---|---|
| On website, no CRM account | **Create** under the Bellhaven parent, with website data and a note. |
| Under the wrong parent or no parent | **SOP check.** If `lifetime_revenue > 0` **and** `outstanding_ar > 0`: **CHOW**. The old account is left exactly as is (no parent, name, or note change), a new account is created under Bellhaven, and `old.chow_current_account` is set to the new id. Otherwise: **re-parent** the existing account. |
| Outdated name | **Rename** to the website name. The website is the source of truth for the name. |
| Address differs (beyond abbreviations) | **Update address.** |
| CRM care type not offered per the website | **Update care type**, mapped to the CRM picklist (Memory Support → Memory Care, Short-Term Rehab & Nursing → Skilled Nursing). |
| Inactive or Needs Review, but live on the website | **Reactivate.** |
| Two or more accounts for one facility | **Duplicate.** The loser gets `duplicate_of_account = survivor` and `status = Inactive`, plus a note. The survivor is chosen by billing history, then already under Bellhaven, then Active, then exact website name/address, then completeness. If both copies have billing history, it's flagged medium confidence with a warning. |
| Under Bellhaven, not on the website, **and another operator has an account at the same address** | The facility was sold. Same SOP: if revenue **and** AR are above 0, the Bellhaven account stays untouched and `chow_current_account` points to the new owner's *existing* account (no new account is needed). Otherwise the stale Bellhaven copy becomes a duplicate of the new owner's account (Inactive). |
| Under Bellhaven but not on the website (no new owner found) | **Status → Needs Review** with an explanatory note. |

### Judgment calls

- **"Not on website" → Needs Review, not Inactive or un-parented.** A facility missing from the site may have been sold, closed, or was never Bellhaven's (some of these accounts have non-Bellhaven names). We don't know the new owner. Removing the parent would claim it's independent, and Inactive would hide a facility that may still be operating. Needs Review keeps it visible and routes it to a human. Parent and billing fields are untouched.
- **CHOW keeps the old account untouched.** The SOP says "exactly as it is", so the only field written on the old account is `chow_current_account`. The explanation goes in the note on the new account.
- **Bellhaven-named accounts under another parent that are *not* on the website** are left alone. Their current parent may well be correct.
- **Phone numbers** are shown as evidence only. Matching never relies on them.

## Safety and re-runs

- **Read-only pipeline.** Only an approval in the app writes to the CRM.
- **Idempotent.** Each proposal has a deterministic key (type + account + target). Approved and rejected keys are stored in `decisions.json` and never proposed again. Approved fixes also change the CRM, so the issue simply disappears on the next run.
- **Resumable writes.** Progress is saved after every API call, so a CHOW whose create succeeded but whose patch failed won't create a second account on retry.
- **Read-back verification.** After every write the account is fetched again and each written field is compared. Any mismatch is shown to the reviewer.
- **Scrape guard.** If a run scrapes fewer than 80% of the previous run's locations (or fewer than 10), it aborts without changing the queue. A broken website scrape must never turn into "every facility left Bellhaven".
- **Audit trail.** Notes on changed accounts are stamped `[Bellhaven sync YYYY-MM-DD]`. `data/run_log.jsonl` records every run, and the Decided tab shows who did what and the write result.

## How AI was used

I used Claude to scaffold the scraper, matcher, and review app from my spec. I verified the work by:
- running the scraper parser against saved copies of the real directory and detail pages,
- running the full pipeline against a mock CRM seeded with the real 121-account export, covering CHOW, re-parent, create, duplicate, and rename cases,
- approving and rejecting items, then confirming the old CHOW account kept its parent and note while only `chow_current_account` changed,
- re-running the pipeline to confirm decided items weren't re-proposed and that newly created accounts didn't trigger duplicate flags.

## With more time

- Slack digest of new proposals after each daily run.
- A bulk "approve all high-confidence renames" action.
- Store decisions in a database instead of a JSON file, for multi-reviewer use.
- Use contacts (administrator names from the website) as an additional matching signal.

**Time spent:** ~2.5 hours (build, real-data QA, review, demo)

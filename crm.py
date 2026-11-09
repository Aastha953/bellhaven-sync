"""Thin client for the CRM sandbox API."""
import requests
import config


class CRMError(Exception):
    pass


def _unwrap(payload):
    """API may return the account directly or wrapped in {"data": {...}}."""
    if isinstance(payload, dict) and "account_id" not in payload and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


class CRM:
    def __init__(self, api_url=config.API_URL, token=config.CRM_TOKEN):
        self.api = api_url
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    def _req(self, method, path, **kw):
        r = self.s.request(method, self.api + path, timeout=30, **kw)
        if r.status_code >= 400:
            raise CRMError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.text else {}

    def list_accounts(self):
        out, page = [], 1
        while True:
            data = self._req("GET", "/accounts", params={"page": page, "page_size": 100})
            batch = data.get("data", []) if isinstance(data, dict) else data
            out.extend(batch)
            total = data.get("total") if isinstance(data, dict) else None
            if not batch or (total is not None and len(out) >= total):
                break
            page += 1
            if page > 100:
                raise CRMError("pagination did not terminate")
        # de-dupe by id in case pages overlap
        return list({a["account_id"]: a for a in out}.values())

    def get_account(self, account_id):
        return _unwrap(self._req("GET", f"/accounts/{account_id}"))

    def update_account(self, account_id, body):
        return _unwrap(self._req("PATCH", f"/accounts/{account_id}", json=body))

    def create_account(self, body):
        acc = _unwrap(self._req("POST", "/accounts", json=body))
        if not isinstance(acc, dict) or not acc.get("account_id"):
            raise CRMError(f"create returned no account_id: {acc}")
        return acc

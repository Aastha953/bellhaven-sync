"""Local review app: approve / reject each proposed CRM change.

Run:  python review_app.py   then open http://localhost:8000
Nothing is written to the CRM until a reviewer clicks Approve.
"""
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config
import executor
import storage

TYPE_LABEL = {"chow": "CHOW", "reparent": "Re-parent", "create": "New account", "duplicate": "Duplicate",
              "rename": "Rename", "address": "Address", "reactivate": "Reactivate",
              "care_type": "Care type", "not_on_website": "Not on website"}
FLASH = {"msg": "", "ok": True}

CSS = """
:root{--bg:#f6f4ef;--card:#fff;--ink:#22231f;--muted:#6b6a63;--line:#e3dfd4;--green:#2e5d50;--red:#b23a3a;
--gold:#b8891a;--chip:#efe9d8}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{background:var(--green);color:#fff;padding:14px 24px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
header h1{margin:0;font-size:18px;font-weight:600}header a{color:#fff;margin-left:16px;text-decoration:none;opacity:.85}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 80px}
.stats{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.stats a{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:4px 12px;color:var(--ink);text-decoration:none;font-size:13px}
.stats a.on{background:var(--green);color:#fff;border-color:var(--green)}
.flash{padding:10px 14px;border-radius:6px;margin-bottom:16px;background:#e5f1ea;border:1px solid #b7d8c4}
.flash.err{background:#f8e5e5;border-color:#e2b5b5}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px 18px;margin-bottom:14px}
.top{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap}
.card h2{margin:0;font-size:16px}
.tag{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;background:var(--chip);margin-right:6px;font-weight:600}
.tag.chow{background:#f6e3c2;color:#7a5200}.tag.high{background:#dcefe3;color:#1d5a3a}.tag.medium{background:#fbeccd;color:#7a5200}
table{border-collapse:collapse;width:100%;margin-top:10px;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;width:22%}
td.from{color:var(--red);text-decoration:line-through}td.to{color:var(--green);font-weight:600}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:10px}
.cols h3{margin:0 0 4px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.cols div.box{background:#faf9f6;border:1px solid var(--line);border-radius:6px;padding:10px;font-size:13px}
ul.ev{margin:10px 0 0;padding-left:18px;color:#3c3b36}ul.ev li{margin:2px 0}
form{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
input[type=text]{flex:1;min-width:200px;padding:7px 10px;border:1px solid var(--line);border-radius:6px}
button{padding:7px 16px;border-radius:6px;border:0;font-weight:600;cursor:pointer}
.ok{background:var(--green);color:#fff}.no{background:#fff;color:var(--red);border:1px solid var(--red)}
.muted{color:var(--muted)}.err{color:var(--red)}
@media(max-width:700px){.cols{grid-template-columns:1fr}}
"""


def e(x):
    return html.escape("" if x is None else str(x))


def load_state():
    data = storage.load(config.PROPOSALS_FILE, {"proposals": [], "generated_at": None})
    decisions = storage.load(config.DECISIONS_FILE, {})
    return data, decisions


def pending(data, decisions):
    return [p for p in data["proposals"]
            if decisions.get(p["key"], {}).get("decision") not in ("approved", "rejected")]


def kv_box(title, rows):
    body = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in rows if v not in (None, ""))
    return f"<div><h3>{e(title)}</h3><div class='box'><table>{body}</table></div></div>"


def card(p, decisions):
    loc, crm = p.get("location"), p.get("crm")
    site_rows = [] if not loc else [("Name", loc["name"]), ("Address", f"{loc['street']}, {loc['city']}, {loc['state']} {loc['zip']}"),
                                    ("Care", ", ".join(loc["care_offerings"])), ("Phone", loc.get("phone")),
                                    ("Found on", ", ".join(loc.get("found_on", []))), ("URL", loc["url"])]
    crm_rows = [] if not crm else [("Account", crm["account_id"]), ("Name", crm["name"]), ("Parent", crm.get("parent_name") or "(none)"),
                                   ("Address", f"{crm.get('billing_street')}, {crm.get('billing_city')}, {crm.get('billing_state')} {crm.get('billing_zip')}"),
                                   ("Care type", crm.get("care_type")), ("Status", crm.get("status")),
                                   ("Lifetime revenue", f"{float(crm.get('lifetime_revenue') or 0):,.0f}"),
                                   ("Outstanding AR", f"{float(crm.get('outstanding_ar') or 0):,.0f}"), ("Note", crm.get("note"))]
    cols = ""
    if site_rows or crm_rows:
        cols = "<div class='cols'>" + (kv_box("Website (source of truth)", site_rows) if site_rows else "<div></div>") \
               + (kv_box("CRM today", crm_rows) if crm_rows else kv_box("CRM today", [("", "No matching account")])) + "</div>"
    if p.get("survivor"):
        s = p["survivor"]
        cols += f"<p class='muted'>Surviving record: <b>{e(s['name'])}</b> ({e(s['account_id'])}), parent {e(s.get('parent_name') or '(none)')}, revenue {float(s.get('lifetime_revenue') or 0):,.0f}</p>"
    changes = "".join(f"<tr><th>{e(c['field'])}</th><td class='from'>{e(c['from'])}</td><td class='to'>{e(c['to'])}</td></tr>"
                      for c in p["changes"])
    ev = "".join(f"<li>{e(x)}</li>" for x in p["evidence"])
    err = decisions.get(p["key"], {}).get("error")
    err_html = f"<p class='err'>Last attempt failed: {e(err)} - click Approve to retry (completed steps are not repeated).</p>" if err else ""
    return f"""<div class="card" id="{e(p['key'])}">
  <div class="top"><h2>{e(p['title'])}</h2>
  <div><span class="tag {e(p['type'])}">{e(TYPE_LABEL.get(p['type'], p['type']))}</span><span class="tag {e(p['confidence'])}">{e(p['confidence'])} confidence</span></div></div>
  <table><tr><th>Proposed change</th><th>Before</th><th>After</th></tr>{changes}</table>
  {cols}
  <ul class="ev">{ev}</ul>{err_html}
  <form method="post" action="/decide"><input type="hidden" name="key" value="{e(p['key'])}">
    <input type="text" name="note" placeholder="Reviewer note (optional)">
    <button class="ok" name="action" value="approve">Approve &amp; write to CRM</button>
    <button class="no" name="action" value="reject">Reject</button></form>
</div>"""


def page(body, title="Bellhaven CRM Sync - Review"):
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><style>{CSS}</style></head><body>
<header><h1>Bellhaven ownership sync &middot; review queue</h1><nav><a href="/">Pending</a><a href="/decided">Decided</a></nav></header>
<div class="wrap">{body}</div></body></html>"""


def render_pending(qs):
    data, decisions = load_state()
    items = pending(data, decisions)
    want = qs.get("type", [""])[0]
    counts = {}
    for p in items:
        counts[p["type"]] = counts.get(p["type"], 0) + 1
    chips = f"<a href='/' class='{'on' if not want else ''}'>All {len(items)}</a>" + "".join(
        f"<a href='/?type={t}' class='{'on' if want == t else ''}'>{e(TYPE_LABEL.get(t, t))} {n}</a>" for t, n in counts.items())
    flash = ""
    if FLASH["msg"]:
        flash = f"<div class='flash {'' if FLASH['ok'] else 'err'}'>{FLASH['msg']}</div>"
        FLASH["msg"] = ""
    shown = [p for p in items if not want or p["type"] == want]
    meta = data.get("website_meta") or {}
    head = (f"<p class='muted'>Queue generated {e(data.get('generated_at'))} &middot; {e(meta.get('scraped'))} website "
            f"communities scraped &middot; {len(decisions)} decisions on file</p>")
    cards = "".join(card(p, decisions) for p in shown) or "<p>Nothing pending. 🎉 Run <code>python run_pipeline.py</code> to refresh.</p>"
    return page(flash + head + f"<div class='stats'>{chips}</div>" + cards)


def render_decided():
    _, decisions = load_state()
    rows = sorted(decisions.items(), key=lambda kv: kv[1].get("at", ""), reverse=True)
    trs = "".join(
        f"<tr><td>{e(v.get('at'))}</td><td><b>{e(v.get('decision'))}</b></td><td>{e(TYPE_LABEL.get(v.get('type'), v.get('type')))}</td>"
        f"<td>{e(v.get('title'))}</td><td>{e(v.get('reviewer_note'))}</td><td>{e('; '.join(v.get('results', [])) or v.get('error', ''))}</td></tr>"
        for _, v in rows)
    return page("<h2>Decision log</h2><table><tr><th>When</th><th>Decision</th><th>Type</th><th>Item</th><th>Note</th><th>Result</th></tr>"
                + trs + "</table>", "Decision log")


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, status=200):
        b = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(render_pending(parse_qs(u.query)))
        elif u.path == "/decided":
            self._send(render_decided())
        else:
            self._send("not found", 404)

    def do_POST(self):
        if urlparse(self.path).path != "/decide":
            return self._send("not found", 404)
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
        key, action, note = form.get("key", [""])[0], form.get("action", [""])[0], form.get("note", [""])[0]
        data, decisions = load_state()
        p = next((x for x in data["proposals"] if x["key"] == key), None)
        if not p or decisions.get(key, {}).get("decision") in ("approved", "rejected"):
            FLASH.update(msg="That item was already decided.", ok=False)
        elif action == "approve":
            ok, results = executor.apply(p, decisions, note)
            FLASH.update(ok=ok, msg=(f"Approved and written: <b>{e(p['title'])}</b> &mdash; " if ok else
                                     f"Write FAILED for <b>{e(p['title'])}</b>: ") + e("; ".join(results)))
        elif action == "reject":
            executor.reject(p, decisions, note)
            FLASH.update(ok=True, msg=f"Rejected: <b>{e(p['title'])}</b> (will not be re-proposed)")
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"Review app running at http://localhost:{config.REVIEW_PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", config.REVIEW_PORT), Handler).serve_forever()

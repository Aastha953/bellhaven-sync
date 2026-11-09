"""Scrape every Bellhaven community from the public website.

Crawls the paginated directory (/communities?page=N) AND any community links on
the homepage / About page, because a location can be linked from one place but
not the other (e.g. a newly acquired community announced on the homepage).
"""
import html
import re
import time
from urllib.parse import urljoin

import requests
import config

COMMUNITY_LINK = re.compile(r'href="(/communities/[^"#?]+)"')
NEXT_LINK = re.compile(r'<a href="([^"]*\?page=\d+)">\s*Next', re.I)
H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
DT_DD = re.compile(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", re.S)
BADGE = re.compile(r'<span class="badge">(.*?)</span>', re.S)
CITY_LINE = re.compile(r"^(.*?),\s*([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?$")


def _text(fragment):
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).strip()


def _clean(s):
    return re.sub(r"\s+", " ", s).strip()


def _get(session, url, tries=3):
    for i in range(tries):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def parse_detail(page_html, url):
    m = H1.search(page_html)
    loc = {"name": _clean(_text(m.group(1))) if m else "", "url": url,
           "slug": url.rstrip("/").split("/")[-1],
           "street": "", "city": "", "state": "", "zip": "",
           "care_offerings": [], "administrator": "", "phone": ""}
    for dt, dd in DT_DD.findall(page_html):
        label = _text(dt).lower()
        if label == "address":
            lines = [_clean(_text(x)) for x in re.split(r"<br\s*/?>", dd) if _text(x)]
            if lines:
                loc["street"] = lines[0]
            if len(lines) > 1:
                cm = CITY_LINE.match(lines[-1])
                if cm:
                    loc["city"], loc["state"], loc["zip"] = cm.group(1).strip(), cm.group(2).upper(), cm.group(3)
                else:
                    loc["city"] = lines[-1]
        elif label.startswith("care"):
            loc["care_offerings"] = [_clean(html.unescape(b)) for b in BADGE.findall(dd)]
        elif label == "administrator":
            loc["administrator"] = _clean(_text(dd))
        elif label == "phone":
            loc["phone"] = _clean(_text(dd))
    return loc


def scrape(site_url=config.SITE_URL):
    """Returns (locations, meta). Raises on network failure so the run aborts."""
    s = requests.Session()
    s.headers["User-Agent"] = "bellhaven-crm-sync/1.0"
    links, sources = [], {}

    def add(path, source):
        if path not in sources:
            links.append(path)
            sources[path] = []
        if source not in sources[path]:
            sources[path].append(source)

    # 1) paginated directory
    url, pages, listed_total = site_url + "/communities", 0, None
    seen_pages = set()
    while url and url not in seen_pages:
        seen_pages.add(url)
        page = _get(s, url)
        pages += 1
        m = re.search(r"(\d+)\s+communities listed", page)
        if m:
            listed_total = int(m.group(1))
        for p in COMMUNITY_LINK.findall(page):
            add(p, "directory")
        nm = NEXT_LINK.search(page)
        url = urljoin(site_url + "/", html.unescape(nm.group(1))) if nm else None

    # 2) homepage + about page (announcements, newly acquired communities)
    for extra in ["/", "/about"]:
        try:
            for p in COMMUNITY_LINK.findall(_get(s, site_url + extra)):
                add(p, "homepage" if extra == "/" else "about")
        except requests.RequestException:
            pass

    # 3) detail pages
    locations = []
    for path in links:
        loc = parse_detail(_get(s, site_url + path), site_url + path)
        loc["found_on"] = sources[path]
        locations.append(loc)

    meta = {"directory_pages": pages, "directory_says": listed_total,
            "unique_links": len(links), "scraped": len(locations)}
    return locations, meta


if __name__ == "__main__":
    import json
    locs, meta = scrape()
    print(json.dumps(meta, indent=2))
    for l in locs:
        print(f"{l['name']:<55} {l['street']}, {l['city']}, {l['state']} {l['zip']}  {l['care_offerings']}  {l['found_on']}")

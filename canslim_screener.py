#!/usr/bin/env python3
"""
CAN SLIM screener.in scraper -> canslim_screen.json (+ optional GitHub push)

Runs the CAN SLIM earnings query on screener.in with your login, walks every
result page, maps each stock to its Upstox instrument key (NSE first, BSE
fallback) and writes a JSON file that CAN SLIM Desk loads.

Setup:
    pip install requests beautifulsoup4

Environment variables:
    SCREENER_USER, SCREENER_PASS      screener.in login (required)
    GITHUB_TOKEN                      only for --push (fine-grained token, Contents: read & write)

Usage:
    python canslim_screener.py
    python canslim_screener.py --push --repo donald/market-data --path canslim/canslim_screen.json
"""
import argparse
import base64
import datetime as dt
import gzip
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

BASE = "https://www.screener.in"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

QUERY = (
    "EPS latest quarter > 1.2 * EPS preceding year quarter AND "
    "EPS latest quarter > EPS preceding quarter AND "
    "YOY Quarterly sales growth > 20 AND "
    "YOY Quarterly profit growth > 20 AND "
    "Profit growth > 20 AND "
    "Sales growth > 20 AND "
    "Return on equity > 20 AND "
    "Return on capital employed > 15 AND "
    "Market Capitalization > 500"
)

# Always-true conditions: screener shows a column for every field used in a query,
# so these bring promoter, pledge and FII/DII data into the results for free.
HOLDING_FIELDS = (
    " AND Promoter holding >= 0"
    " AND Change in promoter holding > -100"
    " AND Pledged percentage >= 0"
    " AND Change in FII holding > -100"
    " AND Change in DII holding > -100"
)

UPSTOX_NSE = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
UPSTOX_BSE = "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz"

# Screener column header -> field name used by CAN SLIM Desk
COLS = [
    ("name", r"^(name|company)$"),
    ("price", r"^cmp|current price"),
    ("pe", r"^p/e|price to earning"),
    ("mcap", r"mar(ket)? ?cap"),
    ("roe", r"^roe|return on equity"),
    ("roce", r"^roce|return on capital"),
    ("qp", r"qtr profit var|quarterly profit growth"),
    ("qs", r"qtr sales var|quarterly sales growth"),
    ("industry", r"industry|sector"),
    ("hi52", r"52\s*w(eek)?\s*high"),
    ("dma50", r"^50\s*dma"),
    ("dma200", r"^200\s*dma"),
    ("ret6m", r"6\s*m(on)?th\s*return"),
    ("eps12m", r"^eps\s*12m"),
    ("promoter", r"^(?!.*(chg|change)).*prom"),
    ("prom_chg", r"(chg|change).*prom"),
    ("pledge", r"pledg"),
    ("fii_chg", r"(chg|change).*fii"),
    ("dii_chg", r"(chg|change).*dii"),
]


def log(msg):
    print(f"[{dt.datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


STATUS_PATH = "canslim/screen_status.json"


def write_status(ok, code, message, **extra):
    """Small file the app reads to show why a run failed (or that it worked)."""
    d = os.path.dirname(STATUS_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump({"ok": ok, "code": code, "message": message,
                   "at": dt.datetime.now(IST).isoformat(timespec="seconds"), **extra}, f, indent=1)


def fail(code, message):
    log("ERROR: " + message)
    write_status(False, code, message)
    sys.exit(1)


def num(v):
    try:
        return float(re.sub(r"[,₹%\s]", "", str(v)))
    except ValueError:
        return None


# ---------------------------------------------------------------- screener.in
def login(user, password):
    s = requests.Session()
    s.headers["User-Agent"] = UA
    try:
        r0 = s.get(f"{BASE}/login/", timeout=30)
    except requests.RequestException as e:
        fail("unreachable", f"Could not reach screener.in from GitHub ({e.__class__.__name__}). Try again later.")
    if r0.status_code in (403, 429, 503):
        fail("blocked", f"screener.in blocked the request from GitHub's servers (HTTP {r0.status_code}).")
    token = s.cookies.get("csrftoken")
    if not token:
        fail("blocked", "screener.in did not return its login form. It may be blocking GitHub's servers.")
    r = s.post(
        f"{BASE}/login/",
        data={"csrfmiddlewaretoken": token, "username": user, "password": password, "next": "/"},
        headers={"Referer": f"{BASE}/login/"},
        timeout=30,
    )
    if r.status_code in (403, 429, 503):
        fail("blocked", f"screener.in blocked the login from GitHub's servers (HTTP {r.status_code}).")
    if "sessionid" not in s.cookies:
        page = r.text.lower()
        if "correct" in page or "errorlist" in page or "invalid" in page or "/login/" in r.url:
            fail("wrong_password", "Wrong password for screener.in (or wrong email). "
                 "Fix the SCREENER_USER / SCREENER_PASS secrets in the repo settings.")
        fail("login_failed", f"screener.in login did not complete (HTTP {r.status_code}).")
    log("Logged in to screener.in")
    return s


def parse_page(html):
    """Return (headers, rows) from a screener results page.

    Rows are found by their /company/ link, so this works even if the table's
    class names or header labels change. row["Name"] is always the link text."""
    soup = BeautifulSoup(html, "html.parser")
    link_re = re.compile(r"^/company/")
    tables = [t for t in soup.find_all("table") if t.find("a", href=link_re)]
    if not tables:
        return [], []
    table = max(tables, key=lambda t: len(t.find_all("a", href=link_re)))
    headers, rows = [], []
    for tr in table.find_all("tr"):
        ths = tr.find_all("th")
        if ths and not headers:
            headers = [" ".join(th.get_text(" ", strip=True).split()) for th in ths]
            continue
        link = tr.find("a", href=link_re)
        tds = tr.find_all("td")
        if not link or not tds:
            continue
        cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tds]
        row = dict(zip(headers, cells)) if headers else {}
        row = {("Name" if re.match(r"(name|company)\b", k, re.I) else k): v for k, v in row.items()}
        row["Name"] = " ".join(link.get_text(" ", strip=True).split()) or row.get("Name", "")
        m = re.match(r"^/company/([^/]+)/", link["href"])
        row["_slug"] = m.group(1) if m else None
        rows.append(row)
    return headers, rows


def save_debug_page(r, page):
    """Save the results page (navigation, scripts and e-mail addresses removed) so the layout can be checked."""
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "svg", "form"]):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    html = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", str(main))
    path = os.path.join(os.path.dirname(STATUS_PATH) or ".", f"debug_page{page}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<!-- url: {r.url} | status: {r.status_code} -->\n" + html[:400000])
    text = " ".join(main.get_text(" ", strip=True).split())
    log(f"Page {page} url: {r.url} | HTTP {r.status_code} | tables: {len(soup.find_all('table'))}")
    preview = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", text[:300])
    log("Page text starts: " + preview)
    log(f"Saved {path}")
    return text


def fetch_results(s, query, page, limit=True):
    params = {"query": query, "page": page}
    if limit:
        params["limit"] = 50
    r = s.get(f"{BASE}/screen/raw/", params=params, timeout=45)
    if r.status_code == 429:
        log("Rate limited by screener.in, waiting 20 s")
        time.sleep(20)
        r = s.get(f"{BASE}/screen/raw/", params=params, timeout=45)
    return r


def run_query(s, query, max_pages=60):
    all_rows, seen, headers = [], set(), []
    use_limit = True
    for page in range(1, max_pages + 1):
        r = fetch_results(s, query, page, use_limit)
        if "/login/" in r.url:
            fail("session_expired", "screener.in logged us out mid-run. Run the screen again.")
        if r.status_code >= 400:
            fail("query_failed", f"screener.in refused the query on page {page} (HTTP {r.status_code}).")
        if page == 1 and not parse_page(r.text)[1]:
            log("Page 1 had no stock rows; retrying without the page-size setting")
            r2 = fetch_results(s, query, 1, limit=False)
            if parse_page(r2.text)[1]:
                r, use_limit = r2, False
            else:
                text = save_debug_page(r2, 1)
                m = re.search(r"(\d[\d,]*)\s+results?\s+found", text, re.I)
                if m and m.group(1).replace(",", "") == "0" or re.search(r"no (results|companies) found", text, re.I):
                    fail("no_results", "The query ran but no stocks pass it today.")
                err = re.search(r"(invalid[^.]{0,120}|did you mean[^.]{0,120}|error[^.]{0,120})", text, re.I)
                fail("parse_failed", "Logged in, but no stocks could be read from the results page"
                     + (f" (screener.in says: {err.group(1).strip()})" if err else "")
                     + ". A copy of the page was saved as canslim/debug_page1.html.")
        h, rows = parse_page(r.text)
        headers = headers or h
        new = 0
        for row in rows:
            name = row.get("Name", "")
            if not name or name.lower().startswith("median"):
                continue
            key = row.get("_slug") or name
            if key in seen:
                continue
            seen.add(key)
            all_rows.append(row)
            new += 1
        log(f"Page {page}: {new} new stocks")
        if new == 0:
            break
        time.sleep(1.5)  # be polite
    return headers, all_rows


# ------------------------------------------------------------------- upstox
def load_upstox_master(url):
    r = requests.get(url, timeout=90, headers={"User-Agent": UA})
    r.raise_for_status()
    return json.loads(gzip.decompress(r.content))


def build_maps():
    log("Downloading Upstox instrument masters")
    nse, bse = {}, {}
    for it in load_upstox_master(UPSTOX_NSE):
        if it.get("segment") == "NSE_EQ":
            sym = it.get("trading_symbol")
            # prefer the EQ series when a symbol appears more than once
            if sym and (sym not in nse or it.get("instrument_type") == "EQ"):
                nse[sym] = {"key": it["instrument_key"], "isin": it.get("isin")}
    for it in load_upstox_master(UPSTOX_BSE):
        if it.get("segment") == "BSE_EQ":
            tok = str(it.get("exchange_token", ""))
            if tok:
                bse[tok] = {"key": it["instrument_key"], "isin": it.get("isin"), "symbol": it.get("trading_symbol")}
    log(f"Upstox: {len(nse)} NSE and {len(bse)} BSE equities")
    return nse, bse


# --------------------------------------------------------------------- news
GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
STOPWORDS = {"ltd", "limited", "india", "industries", "the", "and", "co", "corp", "company", "inds"}


def clean_name(name):
    """Screener shortens long names ("Sunlite Recycli."); drop the cut-off last word."""
    words = name.split()
    if len(words) > 1 and words[-1].endswith("."):
        words = words[:-1]
    return " ".join(w.strip(".") for w in words)


def news_query(rec):
    name = clean_name(rec["name"])
    parts = [f'"{name}"']
    sym = rec.get("nse") or ""
    if re.fullmatch(r"[A-Z]{4,}", sym) and sym.lower() not in name.lower().replace(" ", ""):
        parts.append(f'"{sym}"')
    q = "(" + " OR ".join(parts) + ")"
    if len(name.split()) == 1:
        # one-word names ("BSE", "Tips") are common words: ask for stock-market context
        q += " (shares OR stock OR results OR NSE)"
    return q + " when:30d"


def parse_rss(xml_text, rec, limit=8):
    """Headlines that actually mention the company, newest first."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    name = clean_name(rec["name"]).lower()
    keys = [w for w in re.findall(r"[a-z0-9]+", name) if w not in STOPWORDS and len(w) > 1][:2]
    sym = (rec.get("nse") or "").lower()
    items, seen = [], set()
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3].strip()
        low = title.lower()
        if not title or low in seen:
            continue
        if keys and not (all(k in low for k in keys) or (sym and len(sym) > 3 and sym in low)):
            continue  # skip look-alike companies
        seen.add(low)
        try:
            when = parsedate_to_datetime(it.findtext("pubDate")).astimezone(IST).isoformat(timespec="minutes")
        except Exception:
            when = None
        items.append({"t": title, "s": src, "d": when, "u": (it.findtext("link") or "").strip()})
    items.sort(key=lambda x: x["d"] or "", reverse=True)
    return items[:limit]


def fetch_news(rec):
    url = GNEWS.format(q=quote_plus(news_query(rec)))
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": UA})
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(3 * (attempt + 1))
                continue
            return parse_rss(r.text, rec) if r.ok else []
        except requests.RequestException:
            time.sleep(2)
    return []


def add_news(records, workers=6):
    log(f"Fetching news headlines for {len(records)} stocks")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for rec, items in zip(records, pool.map(fetch_news, records)):
            rec["news"] = items
    with_news = sum(1 for r in records if r["news"])
    log(f"Headlines found for {with_news} of {len(records)} stocks")


# ------------------------------------------------------------------ assemble
def to_record(row, nse, bse):
    rec = {}
    for field, pat in COLS:
        h = next((k for k in row if not k.startswith("_") and re.search(pat, k, re.I)), None)
        if h is None:
            continue
        rec[field] = row[h] if field in ("name", "industry") else num(row[h])
    slug = row.get("_slug") or ""
    rec["nse"] = slug if slug and not slug.isdigit() else None
    rec["bse"] = slug if slug.isdigit() else None
    hit = nse.get(rec["nse"]) if rec["nse"] else None
    if not hit and rec["bse"]:
        hit = bse.get(rec["bse"])
    rec["instrument_key"] = hit["key"] if hit else None
    rec["isin"] = hit.get("isin") if hit else None
    rec["raw"] = {k: v for k, v in row.items() if not k.startswith("_")}
    return rec


def push_github(repo, path, branch, content_bytes, token):
    api = f"https://api.github.com/repos/{repo}/contents/{path}"
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    sha = None
    r = requests.get(api, headers=h, params={"ref": branch}, timeout=30)
    if r.status_code == 200:
        sha = r.json().get("sha")
    body = {
        "message": f"CAN SLIM screen {dt.datetime.now(IST):%Y-%m-%d %H:%M} IST",
        "content": base64.b64encode(content_bytes).decode(),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    r = requests.put(api, headers=h, json=body, timeout=60)
    if r.status_code not in (200, 201):
        sys.exit(f"GitHub push failed: {r.status_code} {r.text[:300]}")
    log(f"Pushed to github.com/{repo}/blob/{branch}/{path}")


def main():
    global STATUS_PATH
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="canslim_screen.json")
    ap.add_argument("--query", default=QUERY)
    ap.add_argument("--push", action="store_true", help="also commit the JSON to GitHub")
    ap.add_argument("--repo", help="owner/name for --push")
    ap.add_argument("--path", default="canslim/canslim_screen.json")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--status", default=STATUS_PATH, help="where to write the run status file")
    ap.add_argument("--no-news", action="store_true", help="skip the Google News headlines step")
    a = ap.parse_args()
    STATUS_PATH = a.status

    user, pw = os.environ.get("SCREENER_USER"), os.environ.get("SCREENER_PASS")
    if not user or not pw:
        fail("no_secrets", "The SCREENER_USER / SCREENER_PASS secrets are missing. Add them in Settings → Secrets and variables → Actions.")

    s = login(user, pw)
    try:
        headers, rows = run_query(s, a.query + HOLDING_FIELDS)
    except SystemExit:
        log("screener.in did not accept the holding fields; running the plain query")
        headers, rows = run_query(s, a.query)
    if not rows:
        fail("no_results", "The query ran but returned no stocks today.")
    log(f"{len(rows)} stocks passed the screen")

    try:
        nse, bse = build_maps()
    except Exception as e:
        log(f"Upstox instrument masters failed ({e}); saving the list without instrument keys")
        nse, bse = {}, {}
    records = [to_record(r, nse, bse) for r in rows]
    if not a.no_news:
        try:
            add_news(records)
        except Exception as e:
            log(f"News step failed ({e}); saving the list without headlines")
    missing = [r["name"] for r in records if not r["instrument_key"]]
    if missing:
        log(f"No Upstox key for {len(missing)}: {', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}")

    payload = {
        "generated_at": dt.datetime.now(IST).isoformat(timespec="seconds"),
        "source": "screener.in",
        "query": a.query,
        "columns": headers,
        "count": len(records),
        "news_source": None if a.no_news else "Google News RSS, last 30 days",
        "rows": records,
    }
    data = json.dumps(payload, ensure_ascii=False, indent=1).encode()
    with open(a.out, "wb") as f:
        f.write(data)
    log(f"Wrote {a.out}")
    write_status(True, "ok", f"{len(records)} stocks passed the screen.", count=len(records))

    if a.push:
        tok = os.environ.get("GITHUB_TOKEN")
        if not (a.repo and tok):
            sys.exit("--push needs --repo and GITHUB_TOKEN.")
        push_github(a.repo, a.path, a.branch, data, tok)


if __name__ == "__main__":
    main()
          

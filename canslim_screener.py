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

UPSTOX_NSE = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
UPSTOX_BSE = "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz"

# Screener column header -> field name used by CAN SLIM Desk
COLS = [
    ("name", r"^name$"),
    ("price", r"^cmp|current price"),
    ("pe", r"^p/e|price to earning"),
    ("mcap", r"mar(ket)? ?cap"),
    ("roe", r"^roe|return on equity"),
    ("roce", r"^roce|return on capital"),
    ("qp", r"qtr profit var|quarterly profit growth"),
    ("qs", r"qtr sales var|quarterly sales growth"),
    ("industry", r"industry|sector"),
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
    """Return (headers, rows). Each row: dict of header -> text, plus _slug from the company link."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.data-table") or soup.find("table")
    if not table:
        return [], []
    headers, rows = [], []
    for tr in table.find_all("tr"):
        ths = tr.find_all("th")
        if ths and not headers:
            headers = [" ".join(th.get_text(" ", strip=True).split()) for th in ths]
            continue
        tds = tr.find_all("td")
        if not tds or not headers:
            continue
        cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tds]
        row = dict(zip(headers, cells))
        link = tr.find("a", href=re.compile(r"^/company/"))
        if link:
            m = re.match(r"^/company/([^/]+)/", link["href"])
            row["_slug"] = m.group(1) if m else None
        rows.append(row)
    return headers, rows


def run_query(s, query, max_pages=60):
    all_rows, seen, headers = [], set(), []
    for page in range(1, max_pages + 1):
        r = s.get(f"{BASE}/screen/raw/", params={"query": query, "limit": 50, "page": page}, timeout=45)
        if r.status_code == 429:
            log("Rate limited by screener.in, waiting 20 s")
            time.sleep(20)
            r = s.get(f"{BASE}/screen/raw/", params={"query": query, "limit": 50, "page": page}, timeout=45)
        if "/login/" in r.url:
            fail("session_expired", "screener.in logged us out mid-run. Run the screen again.")
        if r.status_code >= 400:
            fail("query_failed", f"screener.in refused the query on page {page} (HTTP {r.status_code}).")
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
    a = ap.parse_args()
    STATUS_PATH = a.status

    user, pw = os.environ.get("SCREENER_USER"), os.environ.get("SCREENER_PASS")
    if not user or not pw:
        fail("no_secrets", "The SCREENER_USER / SCREENER_PASS secrets are missing. Add them in Settings → Secrets and variables → Actions.")

    s = login(user, pw)
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
    missing = [r["name"] for r in records if not r["instrument_key"]]
    if missing:
        log(f"No Upstox key for {len(missing)}: {', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}")

    payload = {
        "generated_at": dt.datetime.now(IST).isoformat(timespec="seconds"),
        "source": "screener.in",
        "query": a.query,
        "columns": headers,
        "count": len(records),
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

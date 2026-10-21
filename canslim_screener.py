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
import statistics
import threading
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
        row["_href"] = link["href"]
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


def run_query(s, query, max_pages=60, allow_empty=False):
    all_rows, seen, headers = [], set(), []
    use_limit = True
    for page in range(1, max_pages + 1):
        r = fetch_results(s, query, page, use_limit)
        if allow_empty and page == 1 and r.status_code < 400 and not parse_page(r.text)[1]:
            return [], []
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


# ------------------------------------------------- company pages (free, screener.in)
# Each stock's screener page gives: sector / industry, shareholding (promoter, FII, DII,
# public by quarter) and the auto pros / cons. The peers table gives the industry median
# P/E, and the P/E chart gives the stock's 10, 5 and 1-year median P/E. None of this
# depends on which columns are switched on in your screener account.
_rate_lock = threading.Lock()
_last_call = [0.0]
_debug_saved = [False]
PE_WINDOWS = {"y10": 3652, "y5": 1826, "y1": 365}


def sget(s, url, xhr=False, tries=4, **kw):
    """GET with a shared ~2.5 requests/second limit and back-off on 429 / 5xx."""
    headers = {"X-Requested-With": "XMLHttpRequest"} if xhr else {}
    for attempt in range(tries):
        with _rate_lock:
            wait = _last_call[0] + 0.4 - time.time()
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
        try:
            r = s.get(url, headers=headers, timeout=30, **kw)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(10 * (attempt + 1))
            continue
        return r
    return None


def _txt(el):
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def save_debug_company(html, href):
    if _debug_saved[0]:
        return
    _debug_saved[0] = True
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "svg", "form"]):
        tag.decompose()
    body = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", str(soup.find("main") or soup.body or soup))
    path = os.path.join(os.path.dirname(STATUS_PATH) or ".", "debug_company.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<!-- {href} -->\n" + body[:400000])
    log(f"Company page layout not recognised for {href}; saved {path}")


def parse_company(html):
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    info = soup.select_one("#company-info") or soup.find(attrs={"data-warehouse-id": True})
    if info is not None:
        out["cid"] = info.get("data-company-id")
        out["wid"] = info.get("data-warehouse-id")
        out["consolidated"] = str(info.get("data-consolidated", "")).lower() == "true"
    if not out.get("wid"):
        m = re.search(r'data-warehouse-id="(\d+)"', html)
        out["wid"] = m.group(1) if m else None
    if not out.get("cid"):
        m = re.search(r'data-company-id="(\d+)"', html)
        out["cid"] = m.group(1) if m else None

    ratios = {}
    for li in soup.select("#top-ratios li"):
        name = _txt(li.select_one(".name")).lower()
        val = num(_txt(li.select_one(".number")) or _txt(li.select_one(".value")))
        if name:
            ratios[name] = val
    out["ratios"] = ratios

    # sector / industry from the peer-comparison breadcrumb
    peers_sec = soup.select_one("#peers") or soup
    levels = {}
    links = [a for a in peers_sec.find_all("a", href=True) if a["href"].startswith("/market/")]
    for a in links:
        title = (a.get("title") or "").strip().lower()
        if title in ("broad sector", "sector", "broad industry", "industry"):
            levels[title] = _txt(a)
    if not levels and links:
        names = [_txt(a) for a in links]
        keys = ["broad sector", "sector", "broad industry", "industry"][-len(names):] if len(names) <= 4 else None
        if keys:
            levels = dict(zip(keys, names[-len(keys):]))
    if not levels:
        t = _txt(peers_sec)
        m1, m2 = re.search(r"Sector:\s*(.+?)\s+Industry:", t), re.search(r"Industry:\s*(.+?)(\s{2,}|$|\s+Part of)", t)
        if m1:
            levels["sector"] = m1.group(1)
        if m2:
            levels["industry"] = m2.group(1)
    out["levels"] = levels

    # shareholding pattern, latest quarter and change vs the quarter before
    shp = {}
    sec = soup.select_one("#quarterly-shp") or soup.select_one("#shareholding")
    table = sec.find("table") if sec else None
    if table:
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 2:
                continue
            label = _txt(cells[0]).lower()
            vals = [num(_txt(c)) for c in cells[1:]]
            vals = [v for v in vals if v is not None]
            for key, pat in (("promoter", r"^promoter"), ("fii", r"^fii|foreign"), ("dii", r"^dii|domestic"), ("public", r"^public"), ("govt", r"^gov")):
                if re.match(pat, label) and vals:
                    shp[key] = vals[-1]
                    shp[key + "_chg"] = round(vals[-1] - vals[-2], 2) if len(vals) > 1 else None
        heads = [_txt(th) for th in table.find_all("th")]
        shp["quarter"] = heads[-1] if heads else None
    out["shp"] = shp

    out["pros"] = [_txt(li) for li in soup.select(".pros li")][:3]
    out["cons"] = [_txt(li) for li in soup.select(".cons li")][:3]
    about = soup.select_one(".company-profile .about p") or soup.select_one(".about p")
    out["about"] = _txt(about)[:300] if about else None
    return out


def parse_peers(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return None, []
    heads = [_txt(th).lower() for th in table.find_all("th")]
    pe_i = next((i for i, h in enumerate(heads) if re.match(r"p/e|price to earning", h)), None)
    mc_i = next((i for i, h in enumerate(heads) if "mar cap" in h or "market cap" in h), None)
    median, peers = None, []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        cells = [_txt(td) for td in tds]
        off = len(heads) - len(cells)          # header row may include a column the data rows lack
        get = lambda i: num(cells[i - off]) if i is not None and 0 <= i - off < len(cells) else None
        if "median" in " ".join(cells[:3]).lower():
            median = get(pe_i)
            continue
        a = tr.find("a", href=re.compile(r"^/company/"))
        if a:
            peers.append({"href": a["href"], "pe": get(pe_i), "mcap": get(mc_i)})
    if median is None:
        pes = [p["pe"] for p in peers if p["pe"] and p["pe"] > 0]
        median = round(statistics.median(pes), 2) if pes else None
    return median, peers


def pe_history(s, cid, consolidated):
    """Median P/E of the stock over the last 10, 5 and 1 years from screener's P/E chart."""
    if not cid:
        return {}
    r = sget(s, f"{BASE}/api/company/{cid}/chart/", xhr=True, params={
        "q": "Price to Earning-Median PE-EPS", "days": 3652, "consolidated": "true" if consolidated else "false"})
    if not r or not r.ok:
        return {}
    try:
        sets = r.json().get("datasets", [])
    except ValueError:
        return {}
    ds = next((d for d in sets if re.search(r"earning|^pe$", (d.get("metric") or d.get("label") or ""), re.I)), None)
    if not ds:
        return {}
    pts = []
    for v in ds.get("values", []):
        try:
            d, x = dt.date.fromisoformat(str(v[0])[:10]), float(v[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 < x < 1000:
            pts.append((d, x))
    if not pts:
        return {}
    end = max(d for d, _ in pts)
    out = {}
    for k, days in PE_WINDOWS.items():
        xs = [x for d, x in pts if (end - d).days <= days]
        if xs and (k == "y1" or (end - min(d for d, _ in pts)).days >= days * 0.8):
            out[k] = round(statistics.median(xs), 1)
    return out


_page_cache, _hist_cache = {}, {}


def company(s, href):
    if href not in _page_cache:
        r = sget(s, BASE + href)
        _page_cache[href] = parse_company(r.text) if r is not None and r.ok else None
        if _page_cache[href] is not None and not _page_cache[href].get("wid"):
            save_debug_company(r.text, href)
    return _page_cache[href]


def history(s, href):
    if href not in _hist_cache:
        c = company(s, href)
        _hist_cache[href] = pe_history(s, c.get("cid"), c.get("consolidated")) if c else {}
    return _hist_cache[href]


def enrich_one(s, rec):
    href = rec.get("_href")
    c = company(s, href) if href else None
    if not c:
        return
    lv = c["levels"]
    rec["broad_sector"] = lv.get("broad sector")
    rec["sector"] = lv.get("sector") or lv.get("broad sector")
    rec["industry"] = lv.get("industry") or lv.get("broad industry") or rec.get("industry")
    rt = c["ratios"]
    if rt.get("stock p/e") is not None:
        rec["pe"] = rt["stock p/e"]
    for name, key in (("pledged percentage", "pledge"), ("industry pe", "ind_pe")):
        if rt.get(name) is not None:
            rec[key] = rt[name]
    for k, v in c["shp"].items():
        if v is not None and k != "quarter":
            rec[k] = v
    if "promoter_chg" in rec:
        rec["prom_chg"] = rec.pop("promoter_chg")
    rec["shp_quarter"] = c["shp"].get("quarter")
    rec["pros"], rec["cons"], rec["about"] = c["pros"], c["cons"], c["about"]
    if c.get("wid"):
        r = sget(s, f"{BASE}/api/company/{c['wid']}/peers/", xhr=True)
        if r is not None and r.ok:
            med, peers = parse_peers(r.text)
            if rec.get("ind_pe") is None:
                rec["ind_pe"] = med
            rec["_peers"] = peers
    rec["pe_hist"] = history(s, href)


def add_fundamentals(s, records, budget_s=900):
    t0 = time.time()
    log(f"Reading screener company pages for {len(records)} stocks (sector, shareholding, P/E history)")
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda r: enrich_one(s, r), records))
    ok = sum(1 for r in records if r.get("sector") or r.get("promoter") is not None)
    log(f"Company data for {ok} of {len(records)} stocks ({int(time.time() - t0)} s)")

    # Industry P/E history = median of the top 5 peers' own 10 / 5 / 1-year medians
    groups = {}
    for r in records:
        if r.get("industry") and r.get("_peers"):
            groups.setdefault(r["industry"], r["_peers"])
    ind_hist = {}

    def one_industry(item):
        name, peers = item
        if time.time() - t0 > budget_s:
            return name, None
        top = sorted([p for p in peers if p.get("href")], key=lambda p: -(p.get("mcap") or 0))[:5]
        hs = [history(s, p["href"]) for p in top]
        res = {}
        for k in PE_WINDOWS:
            xs = [h[k] for h in hs if h.get(k)]
            if len(xs) >= 2:
                res[k] = round(statistics.median(xs), 1)
        return name, res

    log(f"Building industry P/E history for {len(groups)} industries")
    with ThreadPoolExecutor(max_workers=3) as pool:
        for name, res in pool.map(one_industry, groups.items()):
            ind_hist[name] = res
    skipped = sum(1 for v in ind_hist.values() if v is None)
    for r in records:
        r["ind_pe_hist"] = ind_hist.get(r.get("industry")) or {}
        r.pop("_peers", None)
    log(f"Industry P/E history done ({int(time.time() - t0)} s total{', ' + str(skipped) + ' industries skipped for time' if skipped else ''})")


def add_pledge_bands(s, query, records):
    """Pledge % via the query itself: screener filters on 'Pledged percentage' even when the column isn't shown."""
    def ids(q):
        return {r.get("_slug") for r in run_query(s, q, allow_empty=True)[1]}
    known = ids(query + " AND Pledged percentage >= 0")
    if not known:
        log("Pledge data not available from screener.in; skipping pledge bands")
        return
    over = {t: ids(f"{query} AND Pledged percentage > {t}") for t in (0, 5, 10, 25, 50)}
    for r in records:
        slug = r.get("_slug")
        if r.get("pledge") is not None or slug not in known:
            continue
        lo, hi = 0, 0
        for t, lo2, hi2 in ((0, 0, 5), (5, 5, 10), (10, 10, 25), (25, 25, 50), (50, 50, 100)):
            if slug in over[t]:
                lo, hi = lo2, hi2
        r["pledge_lo"], r["pledge_hi"] = lo, hi
        r["pledge_band"] = "0%" if hi == 0 else f"{lo}–{hi}%"
    pledged = sum(1 for r in records if r.get("pledge_hi"))
    log(f"Pledge checked: {pledged} of {len(records)} stocks have pledged shares")


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
    rec["_slug"], rec["_href"] = row.get("_slug"), row.get("_href")
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
    ap.add_argument("--no-fundamentals", action="store_true", help="skip company pages, shareholding and P/E history")
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
    if not a.no_fundamentals:
        try:
            add_pledge_bands(s, a.query, records)
        except (Exception, SystemExit) as e:
            log(f"Pledge step failed ({e}); continuing")
        try:
            add_fundamentals(s, records)
        except Exception as e:
            log(f"Company-page step failed ({e}); saving the list without it")
    if not a.no_news:
        try:
            add_news(records)
        except Exception as e:
            log(f"News step failed ({e}); saving the list without headlines")
    for r in records:
        r.pop("_slug", None)
        r.pop("_href", None)
        r.pop("_peers", None)
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

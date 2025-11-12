#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Seats.aero → HTML daily report for Qatar (QR) Avios premium awards (NZ → EU use case)

Key behaviour:
- Scan hub legs only (DOH→EU outbound; EU→DOH return) to stay within QR-operated inventory.
- Pair into itineraries (open-jaw allowed) with return 28–35 days after outbound, ±3 days flex.
- Business/First only; exclude mixed-cabin.
- Rank by Avios then cash (taxes); sweet-spot highlight ≤ 90k Avios one-way.
- Group output by month (NZ date format dd/mm/yy). Writes out.html and prints a short preview.
- Optional: POST to a webhook (e.g., from GitHub Actions) via WEBHOOK_URL. DRY_RUN skips delivery.
- Optional: 7-day dedup cache in .seen_hits.json (disable via DEDUP_DAYS=0).

Environment (sane defaults shown where appropriate):
    SEATSAERO_API_KEY   = pro_************************ (required)
    SEATSAERO_AVAIL_URL = https://seats.aero/partnerapi/availability
    SEATSAERO_ROUTES_URL= https://seats.aero/partnerapi/routes
    USE_HUB_MODE        = 1          # keep on; forces DOH hub scan
    SCAN_ORIGIN         = AKL        # your real origin for links/summary only
    USE_DYNAMIC_ROUTES  = 0          # use static EU list (recommended)
    RATE_LIMIT_MS       = 300
    SCAN_MONTHS         = 15         # months from today
    FLEX_DAYS           = 3          # ± days around travel dates
    MIN_RET_DAYS        = 28
    MAX_RET_DAYS        = 35
    DEDUP_DAYS          = 7
    DRY_RUN             = 1          # write/print only
    DEBUG               = 0
    WEBHOOK_URL         = (optional) # if set, will POST {"subject","html","alert":false}
"""

import os
import sys
import json
import time
import math
import html
import gzip
import ssl
import datetime as dt
import urllib.parse
import urllib.request
from collections import defaultdict

# -------- Config / Env --------
SEATSAERO_API_KEY   = os.getenv("SEATSAERO_API_KEY", "").strip()
AVAIL_URL           = os.getenv("SEATSAERO_AVAIL_URL", "https://seats.aero/partnerapi/availability").strip()
ROUTES_URL          = os.getenv("SEATSAERO_ROUTES_URL", "https://seats.aero/partnerapi/routes").strip()

USE_HUB_MODE        = os.getenv("USE_HUB_MODE", "1") not in ("0", "false", "False")
SCAN_ORIGIN         = os.getenv("SCAN_ORIGIN", "AKL").strip().upper()
SCAN_HUB            = "DOH"  # Qatar hub

USE_DYNAMIC_ROUTES  = os.getenv("USE_DYNAMIC_ROUTES", "0") not in ("0", "false", "False")
RATE_LIMIT_MS       = int(os.getenv("RATE_LIMIT_MS", "300"))
SCAN_MONTHS         = int(os.getenv("SCAN_MONTHS", "15"))
FLEX_DAYS           = int(os.getenv("FLEX_DAYS",  "3"))
MIN_RET_DAYS        = int(os.getenv("MIN_RET_DAYS", "28"))
MAX_RET_DAYS        = int(os.getenv("MAX_RET_DAYS", "35"))
DEDUP_DAYS          = int(os.getenv("DEDUP_DAYS",  "7"))

WEBHOOK_URL         = os.getenv("WEBHOOK_URL", "").strip()
DRY_RUN             = os.getenv("DRY_RUN", "0") not in ("0", "false", "False")
DEBUG               = os.getenv("DEBUG", "0") not in ("0", "false", "False")

OUT_HTML            = os.getenv("OUT_HTML", "out.html")
CACHE_PATH          = os.getenv("CACHE_PATH", ".seen_hits.json")

# Static EU set (seed). We’ll use this if dynamic fails or is disabled.
STATIC_EU = [
    "AMS","ATH","BCN","BER","BRU","BUD","CPH","DUB","DUS","FCO",
    "FRA","HEL","LIS","LYS","MAD","MUC","MXP","NCE","OTP","PRG",
    "SOF","VCE","VIE","WAW","ZAG"
]

# -------- Helpers --------
def today_utc_date():
    return dt.datetime.now(dt.UTC).date()

def nz_date(d: dt.date) -> str:
    # dd/mm/yy (NZ)
    return d.strftime("%d/%m/%y")

def month_key(d: dt.date) -> str:
    # "November 2025"
    return d.strftime("%B %Y")

def _sleep_ms(ms: int):
    time.sleep(max(0, ms) / 1000.0)

def _retry_delays():
    # Simple backoff schedule (seconds)
    return [0.5, 1.0, 2.0, 3.0]

def _headers():
    return {
        "Partner-Authorization": SEATSAERO_API_KEY,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": "avios-nz/1.0"
    }

def _get(url: str, timeout=30):
    if DEBUG:
        print(f"[GET] {url}")
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    ctx = ssl.create_default_context()
    last_err = None
    for i, delay in enumerate([0] + _retry_delays()):
        if i > 0:
            _sleep_ms(int(delay * 1000))
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            # retry on 429 / 5xx
            if e.code in (429, 500, 502, 503, 504) and i < 4:
                last_err = e
                continue
            raise
        except Exception as e:
            last_err = e
            if i < 4:
                continue
            raise
    raise last_err

def ensure_env():
    req = ["SEATSAERO_API_KEY", "SEATSAERO_AVAIL_URL"]
    for k in req:
        if not os.getenv(k):
            print(f"❌ Required env var missing: {k}")
            sys.exit(1)

def routes_dynamic(static):
    if not USE_DYNAMIC_ROUTES:
        return sorted(static)
    try:
        params = urllib.parse.urlencode({"carrier": "QR"})
        url = f"{ROUTES_URL}?{params}"
        payload = _get(url)
        eu = set()
        for r in (payload.get("data") if isinstance(payload, dict) else payload or []):
            if not isinstance(r, dict):
                continue
            route = r.get("Route") or r
            if not isinstance(route, dict):
                continue
            if route.get("DestinationRegion") not in ("Europe", "EU"):
                continue
            # Carrier filter: only QR
            if route.get("Carrier") and route.get("Carrier") != "QR":
                continue
            dest = route.get("DestinationAirport")
            if dest and len(dest) == 3:
                eu.add(dest)
        # Keep list sane
        if len(eu) > 60:
            eu = eu.intersection(static) or set(static)
        return sorted(eu) if eu else sorted(static)
    except Exception as e:
        if DEBUG:
            print(f"[routes] dynamic failed: {e}; falling back to static")
        return sorted(static)

def seats_availability(origin, dest, start_date, end_date):
    qs = urllib.parse.urlencode({
        "carrier": "QR",
        "origin": origin,
        "dest": dest,
        "start": start_date.isoformat(),
        "end":   end_date.isoformat(),
    })
    url = f"{AVAIL_URL}?{qs}"
    return _get(url)

def normalize_rows(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    if isinstance(payload, list):
        return payload
    return []

def premium_qr_only(row):
    # Enforce Business/First only; reject mixed-cabin
    if not isinstance(row, dict):
        return False
    # Some feeds: booleans Y/W/J/F flags; others include cabin array
    j = bool(row.get("JAvailable"))
    f = bool(row.get("FAvailable"))
    if not (j or f):
        return False
    # Mixed-cabin flag (if provided)
    if row.get("MixedCabin") is True:
        return False
    # Carrier check if exposed
    c = row.get("Carrier") or (row.get("Route", {}) if isinstance(row.get("Route"), dict) else {}).get("Carrier")
    if c and c != "QR":
        return False
    return True

def extract_price(row):
    # Try to read Avios / Taxes from common keys; default high for sorting
    avios = row.get("Avios") or row.get("MilesCost") or row.get("Points") or 10**9
    taxes = row.get("Taxes") or row.get("Cash") or row.get("YQ") or 10**9
    try:
        avios = int(avios)
    except Exception:
        avios = 10**9
    try:
        taxes = int(taxes)
    except Exception:
        taxes = 10**9
    return avios, taxes

def outbound_dates_between(start: dt.date, months: int):
    # produce month buckets of candidate days across the window
    end_date = (start.replace(day=1) + dt.timedelta(days=32*months))
    # cap to start + months with some buffer
    hard_end = start + dt.timedelta(days=30*months + 14)
    end = min(end_date, hard_end)
    return start, end

def flex_dates(d: dt.date, flex: int):
    return [d + dt.timedelta(days=off) for off in range(-flex, flex+1)]

def build_pairs(out_rows, ret_rows):
    # Index return availability by date for quick lookup
    ret_dates = defaultdict(list)  # date -> rows
    for r in ret_rows:
        try:
            d = dt.date.fromisoformat(r.get("Date"))
            ret_dates[d].append(r)
        except Exception:
            continue

    pairs = []
    for o in out_rows:
        try:
            d_out = dt.date.fromisoformat(o.get("Date"))
        except Exception:
            continue
        # return 28–35 days later, ± flex
        for delta in range(MIN_RET_DAYS, MAX_RET_DAYS + 1):
            target = d_out + dt.timedelta(days=delta)
            for d in flex_dates(target, FLEX_DAYS):
                if d in ret_dates:
                    for r in ret_dates[d]:
                        pairs.append((o, r))
    return pairs

def sweet_tag(avios):
    if avios <= 90000:
        return "green"
    elif avios < 100000:
        return "orange"
    return "grey"

def ymd(s):
    # lenient parse for either "YYYY-MM-DD" or already date
    if isinstance(s, dt.date):
        return s
    return dt.date.fromisoformat(str(s))

def month_groups(rows):
    groups = defaultdict(list)
    for row in rows:
        d = ymd(row.get("Date"))
        groups[month_key(d)].append(row)
    # sort groups by date
    ordered_keys = sorted(groups.keys(), key=lambda k: dt.datetime.strptime(k, "%B %Y"))
    return [(k, groups[k]) for k in ordered_keys]

def format_money(n):
    if n >= 10**9:
        return "—"
    return f"{n:,}"

def html_escape(s):
    return html.escape(str(s or ""))

def booking_link(origin, dest, date_iso):
    # Shallow deeplink helper (placeholder to seats.aero search)
    qs = urllib.parse.urlencode({
        "carrier": "QR",
        "origin": origin,
        "dest": dest,
        "date": date_iso,
    })
    return f"https://seats.aero/availability?{qs}"

def render_html(pairs, subject):
    # Group by outbound month, NZ date format; one table per month
    # Prepare rows with ranking: min(out_avios, ret_avios) then min taxes
    cooked = []
    for (o, r) in pairs:
        ao, to = extract_price(o)
        ar, tr = extract_price(r)
        ra = min(ao, ar)
        rt = min(to, tr)
        try:
            d_out = ymd(o.get("Date"))
            d_ret = ymd(r.get("Date"))
        except Exception:
            continue
        cooked.append({
            "rank": (ra, rt),
            "out_date": d_out,
            "ret_date": d_ret,
            "out_orig": (o.get("Route", {}) or {}).get("OriginAirport") or o.get("Origin") or "DOH",
            "out_dest": (o.get("Route", {}) or {}).get("DestinationAirport") or o.get("Dest") or "",
            "ret_orig": (r.get("Route", {}) or {}).get("OriginAirport") or r.get("Origin") or "",
            "ret_dest": (r.get("Route", {}) or {}).get("DestinationAirport") or r.get("Dest") or "DOH",
            "out_avios": ao, "out_taxes": to,
            "ret_avios": ar, "ret_taxes": tr,
        })

    cooked.sort(key=lambda x: x["rank"])

    # Monthly buckets by outbound date
    months = defaultdict(list)
    for row in cooked:
        months[month_key(row["out_date"])].append(row)

    # Summary banner
    best = cooked[0]["rank"][0] if cooked else None
    if best is None:
        summary_html = (
            f"<p>No Business/First availability found for {html_escape(SCAN_ORIGIN)} ↔ EU in the configured window.</p>"
        )
        color = "grey"
    else:
        color = sweet_tag(best)
        summary_html = (
            f"<p><strong>Best one-way:</strong> "
            f"<span style='color:{'green' if color=='green' else ('#cc8400' if color=='orange' else '#666')}'>"
            f"{best:,} Avios</span> (lowest taxes next). "
            f"Open-jaw allowed. Ranked by Avios → taxes.</p>"
        )

    # Build per-month tables
    def th(s): return f"<th style='text-align:left;padding:8px;border-bottom:1px solid #ddd'>{html_escape(s)}</th>"
    def td(s): return f"<td style='padding:8px;border-bottom:1px solid #eee'>{s}</td>"

    tables = []
    for mon in sorted(months.keys(), key=lambda k: dt.datetime.strptime(k, "%B %Y")):
        rows = months[mon]
        header = (
            f"<h2 style='margin:16px 0 6px 0;font-family:Arial,Helvetica,sans-serif'>{html_escape(mon)}</h2>"
            "<table style='border-collapse:collapse;width:100%;font-family:Arial,Helvetica,sans-serif;font-size:14px'>"
            "<tr>"
            f"{th('Route')}{th('Dates (Depart & Return)')}{th('Cabin')}{th('Avios')}{th('Taxes')}{th('Availability')}{th('Booking Link')}"
            "</tr>"
        )
        body = []
        for row in rows:
            # choose better side (out vs ret) for Avios/Taxes display
            if row["out_avios"] < row["ret_avios"] or (row["out_avios"] == row["ret_avios"] and row["out_taxes"] <= row["ret_taxes"]):
                show_avios, show_taxes = row["out_avios"], row["out_taxes"]
                cabin = "J/F"
                dep = row["out_date"]
                ret = row["ret_date"]
                route = f"{row['out_orig']}→{row['out_dest']} / {row['ret_orig']}→{row['ret_dest']}"
                link = booking_link(row["out_orig"], row["out_dest"], row["out_date"].isoformat())
            else:
                show_avios, show_taxes = row["ret_avios"], row["ret_taxes"]
                cabin = "J/F"
                dep = row["out_date"]
                ret = row["ret_date"]
                route = f"{row['out_orig']}→{row['out_dest']} / {row['ret_orig']}→{row['ret_dest']}"
                link = booking_link(row["ret_orig"], row["ret_dest"], row["ret_date"].isoformat())

            tag = sweet_tag(show_avios)
            badge = {
                "green":  "#008000",
                "orange": "#cc8400",
                "grey":   "#666666"
            }[tag]

            row_html = (
    "<tr>"
    f"{td(html_escape(route))}"
    f"{td(nz_date(dep) + ' → ' + nz_date(ret))}"
    f"{td(html_escape(cabin))}"
    f"{td('<span style=\"color:{}\">{}</span>'.format(badge, format_money(show_avios)))}"
    f"{td(format_money(show_taxes))}"
    f"{td('Yes')}"
    f"{td('<a href=\"{}\">Search</a>'.format(html_escape(link)))}"
    "</tr>"
)
            body.append(row_html)
        tables.append(header + "".join(body) + "</table>")

    html_body = (
        f"<h1 style='font-family:Arial,Helvetica,sans-serif;'>"
        f"{html_escape(subject)}</h1>"
        f"{summary_html}"
        + ("" if tables else "<p>No qualifying pairs found.</p>")
        + "".join(tables)
    )
    return html_body

def persist_cache(new_pairs):
    if DEDUP_DAYS <= 0:
        return new_pairs
    now = today_utc_date()
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        cache = {}

    # build key per pair (route+dates+rank-ish)
    def key_for(p):
        o, r = p
        kd = [
            (o.get("Route",{}) or {}).get("OriginAirport") or o.get("Origin") or "",
            (o.get("Route",{}) or {}).get("DestinationAirport") or o.get("Dest") or "",
            o.get("Date",""),
            (r.get("Route",{}) or {}).get("OriginAirport") or r.get("Origin") or "",
            (r.get("Route",{}) or {}).get("DestinationAirport") or r.get("Dest") or "",
            r.get("Date",""),
        ]
        return "|".join(kd)

    # prune old entries
    out_cache = {}
    for k, stamp in cache.items():
        try:
            dt0 = dt.date.fromisoformat(stamp)
            if (now - dt0).days <= DEDUP_DAYS:
                out_cache[k] = stamp
        except Exception:
            continue

    # filter pairs that are new/changed
    filtered = []
    seen_now = {}
    for p in new_pairs:
        k = key_for(p)
        if k not in out_cache:
            filtered.append(p)
        seen_now[k] = now.isoformat()

    out_cache.update(seen_now)
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(out_cache, f)
    except Exception:
        pass
    return filtered

def post_webhook(subject, html_body):
    if not WEBHOOK_URL:
        return True, "skipped (no WEBHOOK_URL)"
    payload = json.dumps({"subject": subject, "html": html_body, "alert": False}).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL, data=payload,
        headers={"Content-Type":"application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return (200 <= r.status < 300), f"http {r.status}"
    except Exception as e:
        return False, str(e)

def main():
    ensure_env()

    # Determine scan window
    start = today_utc_date()
    start, stop = outbound_dates_between(start, SCAN_MONTHS)
    # For returns we’ll compute per-pair but also gather a big return window to index
    start_ret = start + dt.timedelta(days=MIN_RET_DAYS - FLEX_DAYS)
    stop_ret  = stop  + dt.timedelta(days=MAX_RET_DAYS + FLEX_DAYS)

    # Destinations
    EU_DESTS = routes_dynamic(STATIC_EU)
    print(f"EU destinations: {len(EU_DESTS)} ({', '.join(EU_DESTS[:12])}…)" )

    # Outbound: DOH -> EU only (hub mode enforced)
    out_rows = []
    if USE_HUB_MODE:
        out_origins = [SCAN_HUB]
    else:
        out_origins = [SCAN_ORIGIN]

    for dest in EU_DESTS:
        for orig in out_origins:
            try:
                payload = seats_availability(orig, dest, start, stop)
                rows = normalize_rows(payload)
                rows = [r for r in rows if premium_qr_only(r)]
                out_rows.extend(rows)
            except urllib.error.HTTPError as e:
                print(f"HTTP {e.code} on {orig}->{dest}: {e.reason}")
            except Exception as e:
                print(f"ERR on {orig}->{dest}: {e}")
            _sleep_ms(RATE_LIMIT_MS)

    # Return: EU -> DOH only (hub mode), NOT EU -> AKL
    ret_rows = []
    ret_dest = SCAN_HUB if USE_HUB_MODE else SCAN_ORIGIN
    for orig in EU_DESTS:
        try:
            payload = seats_availability(orig, ret_dest, start_ret, stop_ret)
            rows = normalize_rows(payload)
            rows = [r for r in rows if premium_qr_only(r)]
            ret_rows.extend(rows)
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code} on {orig}->{ret_dest}: {e.reason}")
        except Exception as e:
            print(f"ERR on {orig}->{ret_dest}: {e}")
        _sleep_ms(RATE_LIMIT_MS)

    print(f"Scanned rows: OUT={len(out_rows)} ; RET={len(ret_rows)}")

    # Pair (open-jaw okay)
    raw_pairs = build_pairs(out_rows, ret_rows)
    print(f"Paired (open-jaw allowed): {len(raw_pairs)}")

    # Dedup last N days
    pairs = persist_cache(raw_pairs)
    if DEDUP_DAYS > 0:
        print(f"After {DEDUP_DAYS}-day dedup: {len(pairs)}")
    else:
        print("Dedup disabled.")

    # Render HTML (NZ date grouping per month)
    subject = f"Daily Qatar Avios EU Search – {today_utc_date().isoformat()}"
    html_body = render_html(pairs, subject)

    # Write out.html
    try:
        with open(OUT_HTML, "w", encoding="utf-8") as f:
            f.write(html_body)
        print(f"Wrote {OUT_HTML}")
    except Exception as e:
        print(f"❌ Failed to write HTML: {e}")

    # Delivery
    if DRY_RUN:
        print("DRY_RUN=1: skip webhook/email delivery.")
    else:
        ok, info = post_webhook(subject, html_body)
        if not ok:
            print(f"❌ Webhook delivery failed: {info}")
        else:
            print(f"✅ Webhook delivered: {info}")

    # Console preview
    print(subject, end="\n\n")
    # Keep output short in console
    preview = html_body if len(html_body) < 2000 else html_body[:2000] + "…"
    print(preview)

if __name__ == "__main__":
    if not SEATSAERO_API_KEY:
        print("❌ Required env var missing: SEATSAERO_API_KEY")
        sys.exit(1)
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)

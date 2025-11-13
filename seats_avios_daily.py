#!/usr/bin/env python3
# -*- coding: utf-8 -*-

DRY_RUN = int(os.getenv("DRY_RUN", "1"))
DEBUG   = int(os.getenv("DEBUG", "0"))

if DEBUG:
    print(f"[DEBUG] DRY_RUN={DRY_RUN} (0=send email), TO_EMAIL={os.getenv('TO_EMAIL')}, FROM_EMAIL={os.getenv('FROM_EMAIL')}")

"""
Seats.aero → HTML email for Qatar Avios EU search, grouped by month (NZ date).
- Hub scan via DOH to avoid QR codeshare/region quirks.
- Pairs with real origin for link generation (e.g., AKL).
- Highlights ≤90k business sweet spots.
- Brevo REST delivery (optional). DRY_RUN=1 to skip email.

Env (recommended in .env.avios):
  SEATSAERO_API_KEY=pro_xxx
  SEATSAERO_AVAIL_URL=https://seats.aero/partnerapi/availability
  SEATSAERO_ROUTES_URL=https://seats.aero/partnerapi/routes
  USE_HUB_MODE=1
  SCAN_ORIGIN=AKL
  USE_DYNAMIC_ROUTES=0
  RATE_LIMIT_MS=300
  SCAN_MONTHS=15
  FLEX_DAYS=3
  MIN_RET_DAYS=28
  MAX_RET_DAYS=35
  DEDUP_DAYS=7
  DRY_RUN=1
  DEBUG=1
  BREVO_API_KEY=xkeysib-...
  FROM_EMAIL=alert@imoffto.xyz
  FROM_NAME=Avios Bot
  TO_EMAIL=milo@imoffto.xyz
"""

import os
import sys
import json
import time
import math
import ssl
import urllib.parse
import urllib.request
import datetime as dt
from collections import defaultdict

# ---------------- friendly airport names ----------------
AIRPORT_NAMES = {
    "AKL": "Auckland",
    "DOH": "Doha",
    "LHR": "London Heathrow",
    "CDG": "Paris Charles de Gaulle",
    "AMS": "Amsterdam",
    "ATH": "Athens",
    "BCN": "Barcelona",
    "BER": "Berlin",
    "BRU": "Brussels",
    "BUD": "Budapest",
    "CPH": "Copenhagen",
    "DUB": "Dublin",
    "DUS": "Düsseldorf",
    "FCO": "Rome",
    "FRA": "Frankfurt",
    "HEL": "Helsinki",
    "LIS": "Lisbon",
    "LYS": "Lyon",
    "MAD": "Madrid",
    "MUC": "Munich",
    "MXP": "Milan",
    "NCE": "Nice",
    "OTP": "Bucharest",
    "PRG": "Prague",
    "SOF": "Sofia",
    "VCE": "Venice",
    "VIE": "Vienna",
    "WAW": "Warsaw",
    "ZAG": "Zagreb",
}
def airport_name(code: str) -> str:
    return AIRPORT_NAMES.get(code, code)

def pretty_route(orig: str, dest: str) -> str:
    # "AKL → DOH (Auckland → Doha)"
    return "{} \u2192 {} ({} \u2192 {})".format(
        orig, dest, airport_name(orig), airport_name(dest)
    )

# ---------------- config / constants ----------------
def env_bool(key, default=False):
    v = os.getenv(key)
    if v is None:
        return default
    return str(v).strip() not in ("0", "false", "False", "")

def env_int(key, default):
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return default

def env_required(key):
    val = os.getenv(key, "").strip()
    if not val:
        raise SystemExit("❌ Required env var missing: {}".format(key))
    return val

SEATSAERO_API_KEY = env_required("SEATSAERO_API_KEY")
SEATSAERO_AVAIL_URL = env_required("SEATSAERO_AVAIL_URL")
SEATSAERO_ROUTES_URL = env_required("SEATSAERO_ROUTES_URL")

USE_HUB_MODE     = env_bool("USE_HUB_MODE", True)
SCAN_ORIGIN      = os.getenv("SCAN_ORIGIN", "AKL").strip().upper()
SCAN_HUB         = "DOH"  # Qatar hub

USE_DYNAMIC_ROUTES = env_bool("USE_DYNAMIC_ROUTES", False)
RATE_LIMIT_MS      = env_int("RATE_LIMIT_MS", 300)
SCAN_MONTHS        = env_int("SCAN_MONTHS", 15)
FLEX_DAYS          = env_int("FLEX_DAYS", 3)
MIN_RET_DAYS       = env_int("MIN_RET_DAYS", 28)
MAX_RET_DAYS       = env_int("MAX_RET_DAYS", 35)
DEDUP_DAYS         = env_int("DEDUP_DAYS", 7)

DRY_RUN            = env_bool("DRY_RUN", True)
DEBUG              = env_bool("DEBUG", False)

FROM_EMAIL         = os.getenv("FROM_EMAIL", "")
FROM_NAME          = os.getenv("FROM_NAME", "Avios Bot")
TO_EMAIL           = os.getenv("TO_EMAIL", "")
BREVO_API_KEY      = os.getenv("BREVO_API_KEY", "")

# Static EU set (baseline)
EU_STATIC = [
    "AMS", "ATH", "BCN", "BER", "BRU", "BUD", "CPH", "DUB", "DUS",
    "FCO", "FRA", "HEL", "LIS", "LYS", "MAD", "MUC", "MXP", "NCE",
    "OTP", "PRG", "SOF", "VCE", "VIE", "WAW", "ZAG"
]

# ---------------- utils ----------------
def today_utc_date():
    # Keep naive date, used only for ranges
    return dt.datetime.utcnow().date()

def nz_date(d: dt.date) -> str:
    # dd/mm/yy
    return "{:02d}/{:02d}/{:02d}".format(d.day, d.month, d.year % 100)

def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )

def format_money(v):
    try:
        n = int(v)
        return "{:,}".format(n)
    except Exception:
        try:
            f = float(v)
            return "{:,.0f}".format(f)
        except Exception:
            return str(v)

def tr(inner):
    return "<tr>{}</tr>".format(inner)

def td(inner):
    return "<td style='padding:6px 8px;border-bottom:1px solid #eee;font-size:13px'>{}</td>".format(inner)

def th(inner):
    return "<th style='padding:8px 8px;border-bottom:2px solid #ccc;text-align:left;font-size:13px'>{}</th>".format(inner)

def table(inner, caption=None):
    cap = ""
    if caption:
        cap = "<caption style='text-align:left;font-weight:600;margin:6px 0 2px 0'>{}</caption>".format(html_escape(caption))
    return (
        "<table style='border-collapse:collapse;width:100%;margin:10px 0'>"
        "{}"
        "{}"
        "</table>".format(cap, inner)
    )

def _get(url, headers, timeout=30):
    if DEBUG:
        print("[GET] {}".format(url))
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        body = r.read()
        if r.status != 200:
            raise urllib.error.HTTPError(url, r.status, r.reason, r.headers, None)
        return json.loads(body.decode("utf-8"))

def seats_routes_qr_eu(static_list):
    # try dynamic route discovery; fallback to static
    if not USE_DYNAMIC_ROUTES:
        return list(static_list)

    hdr = {
        "Accept": "application/json",
        "Partner-Authorization": SEATSAERO_API_KEY
    }
    qs = urllib.parse.urlencode({"carrier": "QR"})
    url = "{}?{}".format(SEATSAERO_ROUTES_URL, qs)
    try:
        payload = _get(url, hdr)
        data = payload.get("data") if isinstance(payload, dict) else payload
        eu = set()
        if isinstance(data, list):
            for r in data:
                if not isinstance(r, dict):
                    continue
                route = r.get("Route") or r
                if not isinstance(route, dict):
                    continue
                if route.get("DestinationRegion") != "Europe":
                    continue
                dest = route.get("DestinationAirport")
                if dest and len(dest) == 3:
                    eu.add(dest)
        # Avoid ballooning to hundreds; intersect if needed
        if len(eu) > 60:
            eu = eu.intersection(static_list) or set(static_list)
        return sorted(eu) if eu else list(static_list)
    except Exception as e:
        if DEBUG:
            print("[routes] dynamic failed: {}; falling back to static".format(e))
        return list(static_list)

def seats_availability(origin, dest, start_date, end_date):
    hdr = {
        "Accept": "application/json",
        "Partner-Authorization": SEATSAERO_API_KEY
    }
    qs = urllib.parse.urlencode({
        "carrier": "QR",
        "origin": origin,
        "dest": dest,
        "start": start_date.isoformat(),
        "end": end_date.isoformat()
    })
    url = "{}?{}".format(SEATSAERO_AVAIL_URL, qs)
    resp = _get(url, hdr)
    if isinstance(resp, dict) and "data" in resp:
        return resp["data"]
    return resp if isinstance(resp, list) else []

def is_premium_row(row):
    # Only Business (J) / First (F)
    return bool(row.get("JAvailable") or row.get("FAvailable"))

def row_date(row):
    # parsed as date
    s = row.get("ParsedDate") or row.get("Date")
    if not s:
        return None
    try:
        # "2025-11-12T00:00:00Z" or "2025-11-12"
        s2 = s.split("T")[0]
        parts = s2.split("-")
        return dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return None

def row_avios(row):
    # Try common fields; default high if missing to push down list
    for k in ("Avios", "AviosPrice", "AviosCost", "Points", "AwardCost"):
        if k in row:
            try:
                return int(row[k])
            except Exception:
                try:
                    return int(float(row[k]))
                except Exception:
                    pass
    return 10**9

def row_taxes(row):
    for k in ("Taxes", "Cash", "CashCost"):
        if k in row:
            try:
                return int(row[k])
            except Exception:
                try:
                    return int(float(row[k]))
                except Exception:
                    pass
    return 10**9

def nz_month_key(d: dt.date):
    # "2025-11" for grouping
    return "{}-{:02d}".format(d.year, d.month)

def sweet_badge(avios):
    if avios <= 90000:
        return "green"
    if avios < 100000:
        return "darkorange"
    return "inherit"

def booking_link(orig, dest, dep_date, ret_date, origin_home):
    # Deep links change often; provide a stable search anchor to Qatar or BA.
    # Use Qatar origin_home for outward and EU for return; users will refine dates.
    base = "https://www.qatarairways.com/search"
    params = {
        "tripType": "RT" if ret_date else "OW",
        "origin": origin_home,
        "destination": dest,
        "departureDate": dep_date.isoformat(),
        "returnDate": ret_date.isoformat() if ret_date else ""
    }
    return "{}?{}".format(base, urllib.parse.urlencode(params))

def load_seen(path=".seen_hits.json", max_age_days=7):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    # prune by age
    cutoff = today_utc_date() - dt.timedelta(days=max_age_days)
    out = {}
    for k, v in data.items():
        try:
            ts = dt.date.fromisoformat(v.get("date", "1970-01-01"))
            if ts >= cutoff:
                out[k] = v
        except Exception:
            pass
    return out

def save_seen(d, path=".seen_hits.json"):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass

def hit_key(orig, dest, dep, ret, cabin, avios, cash):
    return "{}|{}|{}|{}|{}|{}|{}".format(orig, dest, dep.isoformat(), ret.isoformat() if ret else "", cabin, avios, cash)

def render_html(grouped_rows, scan_origin, subject_date):
    # Header + legend
    parts = []
    parts.append("<h1 style='font-family:Arial,Helvetica,sans-serif;'>Daily Qatar Avios EU Search \u2013 {}</h1>".format(subject_date))
    parts.append("<p style='font-family:Arial,Helvetica,sans-serif;margin:6px 0;'>Origin: {} ({}). Business & First only. Ranked by Avios then cash.</p>".format(
        scan_origin, airport_name(scan_origin)
    ))
    parts.append("<p style='font-family:Arial,Helvetica,sans-serif;margin:6px 0;'><span style='color:green;'>Green \u2264 90,000 Avios</span> \u2022 <span style='color:darkorange;'>Amber &lt; 100,000</span></p>")

    if not grouped_rows:
        parts.append("<p>No Business/First availability found for {} \u2194 EU in the configured window.</p>".format(html_escape(scan_origin)))
        return "\n".join(parts)

    # Per-month sections
    month_keys = sorted(grouped_rows.keys())
    for mk in month_keys:
        rows = grouped_rows[mk]
        # month heading like "November 2025"
        y, m = mk.split("-")
        month_name = dt.date(int(y), int(m), 1).strftime("%B %Y")
        parts.append("<h2 style='font-family:Arial,Helvetica,sans-serif;margin:12px 0 6px 0;'>{}</h2>".format(html_escape(month_name)))

        header = (
            "<thead><tr>"
            "{}{}{}{}{}{}{}"
            "</tr></thead>".format(
                th("Route"),
                th("Dates (Depart \u2192 Return)"),
                th("Cabin"),
                th("Avios"),
                th("Taxes (NZD)"),
                th("Seats"),
                th("Booking Link"),
            )
        )
        body = []
        for r in rows:
            orig = r["orig"]
            dest = r["dest"]
            dep = r["dep"]
            ret = r["ret"]
            cabin = r["cabin"]
            avios = r["avios"]
            cash = r["cash"]
            seats = r["seats"]
            link = r["link"]

            badge_color = sweet_badge(avios)
            route_text = pretty_route(orig, dest)
            dates_text = "{} \u2192 {}".format(nz_date(dep), nz_date(ret) if ret else "")
            avios_html = "<span style='color:{}'>{}</span>".format(badge_color, format_money(avios))
            link_html = "<a href='{}'>Search</a>".format(html_escape(link))

            cells = []
            cells.append(td(html_escape(route_text)))
            cells.append(td(html_escape(dates_text)))
            cells.append(td(html_escape(cabin)))
            cells.append(td(avios_html))
            cells.append(td(format_money(cash)))
            cells.append(td(str(seats)))
            cells.append(td(link_html))
            body.append(tr("".join(cells)))

        parts.append(table(header + "<tbody>{}</tbody>".format("".join(body))))

    return "\n".join(parts)

if DRY_RUN:
    print("[DEBUG] DRY_RUN=1 – skipping Brevo send, writing HTML only")
    # don’t call send_email(...)
else:
    print("[DEBUG] DRY_RUN=0 – sending Brevo email now…")
    send_email(...)  # whatever your function is

def send_brevo_rest(subject, html_body):
    if not BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY not set")
    if not FROM_EMAIL or not TO_EMAIL:
        raise RuntimeError("FROM_EMAIL/TO_EMAIL not set")

    payload = {
        "sender": {"email": FROM_EMAIL, "name": FROM_NAME or "Avios Bot"},
        "to": [{"email": TO_EMAIL}],
        "subject": subject,
        "htmlContent": html_body,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=data,
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": BREVO_API_KEY
        },
        method="POST"
    )
    if DEBUG:
        print("[POST] Brevo /v3/smtp/email ({} bytes)".format(len(data)))
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read()
        if r.status // 100 != 2:
            raise RuntimeError("Brevo non-2xx: {} {}".format(r.status, body.decode("utf-8")))
        return body.decode("utf-8")

def main():
    subject_date = today_utc_date().isoformat()
    today = today_utc_date()
    end = today + dt.timedelta(days=SCAN_MONTHS * 31)  # loose 15-month span

    # outbound window ± FLEX around dates within the span
    out_start = today - dt.timedelta(days=FLEX_DAYS)
    out_end   = end   + dt.timedelta(days=FLEX_DAYS)

    # return window offsets
    ret_min = MIN_RET_DAYS
    ret_max = MAX_RET_DAYS

    # choose EU list
    eu_list = seats_routes_qr_eu(EU_STATIC)
    print("EU destinations: {} ({}…)".format(len(eu_list), ", ".join(eu_list[:12])))

    out_rows_raw = []
    ret_rows_raw = []

    # OUTBOUND: hub -> EU (QR metal)
    out_origins = [SCAN_HUB] if USE_HUB_MODE else [SCAN_ORIGIN]
    for dest in eu_list:
        for orig in out_origins:
            try:
                rows = seats_availability(orig, dest, out_start, out_end)
                out_rows_raw.extend(rows)
            except urllib.error.HTTPError as e:
                print("HTTP {} on {}->{}: {}".format(e.code, orig, dest, e.reason))
            except Exception as ex:
                print("Error {}->{}: {}".format(orig, dest, ex))
            time.sleep(RATE_LIMIT_MS / 1000.0)
    print("  … outbound queries complete")

    # RETURN: EU -> hub (QR metal)
    ret_dest = SCAN_HUB if USE_HUB_MODE else SCAN_ORIGIN
    for orig in eu_list:
        try:
            ret_start = out_start + dt.timedelta(days=ret_min)
            ret_end   = out_end   + dt.timedelta(days=ret_max)
            rows = seats_availability(orig, ret_dest, ret_start, ret_end)
            ret_rows_raw.extend(rows)
        except urllib.error.HTTPError as e:
            print("HTTP {} on {}->{}: {}".format(e.code, orig, ret_dest, e.reason))
        except Exception as ex:
            print("Error {}->{}: {}".format(orig, ret_dest, ex))
        time.sleep(RATE_LIMIT_MS / 1000.0)
    print("  … return queries complete")

    # Normalize / filter premium only
    out_rows = []
    for r in out_rows_raw:
        if not is_premium_row(r):
            continue
        d = row_date(r)
        if not d:
            continue
        cabin = "First" if r.get("FAvailable") else "Business"
        out_rows.append({
            "orig": r.get("Route", {}).get("OriginAirport", r.get("OriginAirport", out_origins[0])),
            "dest": r.get("Route", {}).get("DestinationAirport", r.get("DestinationAirport")),
            "date": d,
            "cabin": cabin,
            "avios": row_avios(r),
            "cash": row_taxes(r),
            "seats": int(r.get("Seats", 1))
        })

    ret_rows = []
    for r in ret_rows_raw:
        if not is_premium_row(r):
            continue
        d = row_date(r)
        if not d:
            continue
        cabin = "First" if r.get("FAvailable") else "Business"
        ret_rows.append({
            "orig": r.get("Route", {}).get("OriginAirport", r.get("OriginAirport")),
            "dest": r.get("Route", {}).get("DestinationAirport", r.get("DestinationAirport", ret_dest)),
            "date": d,
            "cabin": cabin,
            "avios": row_avios(r),
            "cash": row_taxes(r),
            "seats": int(r.get("Seats", 1))
        })

    print("Scanned rows: OUT={} ; RET={}".format(len(out_rows), len(ret_rows)))

    # Pairing logic (open-jaw allowed): we only require both legs exist within 28–35 days window
    pairs = []
    # Build index by destination for outbound and origin for return
    out_by_dest = defaultdict(list)
    for o in out_rows:
        out_by_dest[o["dest"]].append(o)

    ret_by_orig = defaultdict(list)
    for r in ret_rows:
        ret_by_orig[r["orig"]].append(r)

    for eu in eu_list:
        outs = out_by_dest.get(eu, [])
        rets = ret_by_orig.get(eu, [])
        if not outs or not rets:
            continue
        for o in outs:
            for r in rets:
                # return between min/max days after outbound
                delta = (r["date"] - o["date"]).days
                if delta < MIN_RET_DAYS or delta > MAX_RET_DAYS:
                    continue
                # cabin must match or prefer the lower cabin label (exclude mixed)
                if o["cabin"] != r["cabin"]:
                    continue
                total_avios = o["avios"] + r["avios"]
                total_cash = o["cash"] + r["cash"]
                seats = min(o["seats"], r["seats"])
                link = booking_link(SCAN_HUB, eu, o["date"], r["date"], SCAN_ORIGIN)
                pairs.append({
                    "orig": SCAN_ORIGIN,
                    "dest": eu,
                    "dep": o["date"],
                    "ret": r["date"],
                    "cabin": o["cabin"],
                    "avios": total_avios,
                    "cash": total_cash,
                    "seats": seats,
                    "link": link
                })

    # Sort by avios then cash
    pairs.sort(key=lambda x: (x["avios"], x["cash"]))
    print("Paired (open-jaw allowed): {}".format(len(pairs)))

    # De-dup (7-day memory)
    seen = load_seen(max_age_days=DEDUP_DAYS)
    fresh = []
    today_iso = today.isoformat()
    for p in pairs:
        k = hit_key(p["orig"], p["dest"], p["dep"], p["ret"], p["cabin"], p["avios"], p["cash"])
        if k in seen:
            continue
        seen[k] = {"date": today_iso}
        fresh.append(p)

    print("After {}-day dedup (if enabled): {}".format(DEDUP_DAYS, len(fresh)))

    # Group by month (NZ)
    grouped = defaultdict(list)
    for p in fresh:
        mk = nz_month_key(p["dep"])
        grouped[mk].append(p)

    # Build HTML
    subject = "Daily Qatar Avios EU Search \u2013 {}".format(subject_date)
    html = render_html(grouped, SCAN_ORIGIN, subject_date)

    # Always write a local preview
    try:
        with open("out.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("Wrote out.html")
    except Exception as e:
        print("Failed to write out.html: {}".format(e))

    # Save dedup memory
    save_seen(seen)

    # Delivery
    if DRY_RUN:
        print("DRY_RUN=1: skip email delivery.")
        print(subject)
        print()
        print(html)
        return

    # Brevo REST
    try:
        resp = send_brevo_rest(subject, html)
        if DEBUG:
            print("Brevo response: {}".format(resp))
    except Exception as e:
        print("❌ Brevo email failed: {}".format(e))
        raise

if __name__ == "__main__":
    try:
        main()
    except SystemExit as se:
        print(se)
        sys.exit(1)
    except urllib.error.HTTPError as he:
        print("HTTP {} {}: {}".format(he.code, he.reason, getattr(he, "url", "")))
        sys.exit(1)
    except Exception as ex:
        print("💥 Error:", ex)
        sys.exit(1)

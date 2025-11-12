#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, json, smtplib, ssl, socket, datetime as dt
from typing import List, Dict, Any, Tuple
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import urllib.request, urllib.error

# =======================
# Config via environment
# =======================
SEATSAERO_API_KEY   = os.getenv("SEATSAERO_API_KEY", "").strip()
AVAIL_URL           = os.getenv("SEATSAERO_AVAIL_URL", "").strip()  # e.g. https://partners.seats.aero/v1/availability/bulk
ROUTES_URL          = os.getenv("SEATSAERO_ROUTES_URL", "").strip() # e.g. https://partners.seats.aero/v1/routes?carrier=QR&region=EU
FROM_EMAIL          = os.getenv("FROM_EMAIL", "").strip()
FROM_NAME          = os.getenv("FROM_NAME", "Qatar Avios Bot").strip()
TO_EMAIL            = os.getenv("TO_EMAIL", "").strip()
SMTP_HOST           = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT           = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER           = os.getenv("SMTP_USER", "").strip()
SMTP_PASS           = os.getenv("SMTP_PASS", "").strip()

# Search parameters
ORIGIN              = "AKL"
CARRIER             = "QR"
TODAY               = dt.date.today()
END_DATE            = TODAY + dt.timedelta(days=31*15)   # ~15 months
RETURN_MIN_DAYS     = 28
RETURN_MAX_DAYS     = 35
FLEX_DAYS           = 3
CABINS              = ["business", "first"]
EXCLUDE_MIXED       = True
SWEET_SPOT_BUS_OJ   = 90_000

# De-dupe cache (best effort)
CACHE_FILE          = ".avios_last.json"

# Fallback EU QR destinations (kept conservative; adjust as needed)
FALLBACK_EU_QR = [
    "AMS","ATH","BCN","BEG","BER","BHX","BLQ","BRU","BUD","CDG","CPH","DUB",
    "DUS","EDI","FCO","FRA","GOT","HEL","IST","LIS","LUX","LYS","MAD","MAN",
    "MRS","MUC","MXP","NCE","OSL","OTP","PMI","PRG","RIX","SOF","SPU","STR",
    "TLL","VCE","VIE","WAW","ZAG","ZRH"
]

# =======================
# Utilities
# =======================

def fail(msg: str) -> None:
    print(msg, file=sys.stderr); sys.exit(1)

def needs(k: str) -> None:
    if not globals()[k]:
        fail(f"Required configuration missing: {k}")

def check_env() -> None:
    for k in ("SEATSAERO_API_KEY","AVAIL_URL","FROM_EMAIL","TO_EMAIL","SMTP_HOST","SMTP_USER","SMTP_PASS"):
        needs(k)
    # ROUTES_URL is optional (we fall back if missing)

def seats_headers() -> Dict[str,str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Partner-Authorization": f"Bearer {SEATSAERO_API_KEY}",
        "User-Agent": "avios-eu-bot/1.0"
    }

def http_get_json(url: str, headers: Dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GET {url} -> {e.code} {e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"GET {url} -> {e.reason}")

def http_post_json(url: str, headers: Dict[str, str], payload: Any) -> Any:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"POST {url} -> {e.code} {e.reason}\n{body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"POST {url} -> {e.reason}")

def date_range(start: dt.date, end: dt.date, step_days: int) -> List[dt.date]:
    out, cur = [], start
    while cur <= end:
        out.append(cur)
        cur += dt.timedelta(days=step_days)
    return out

# =======================
# Routes → EU QR destinations
# =======================

def fetch_qr_eu_destinations() -> List[str]:
    if not ROUTES_URL:
        print("ROUTES_URL not set; using fallback EU list.")
        return FALLBACK_EU_QR[:]
    try:
        print("Fetching QR EU destinations from routes endpoint …")
        data = http_get_json(ROUTES_URL, seats_headers())
    except Exception as e:
        print(f"Routes endpoint unavailable ({e}); using fallback EU list.")
        return FALLBACK_EU_QR[:]

    dests = set()
    def add(dst: str):
        dst = (dst or "").strip().upper()
        if dst and dst != ORIGIN: dests.add(dst)

    if isinstance(data, dict) and "routes" in data:
        for r in data["routes"]:
            add(r.get("destination"))
    elif isinstance(data, list):
        for r in data:
            add(r.get("destination"))
    else:
        # Best effort: scan for keys named "destination"
        def walk(obj):
            if isinstance(obj, dict):
                if "destination" in obj: add(obj["destination"])
                for v in obj.values(): walk(v)
            elif isinstance(obj, list):
                for v in obj: walk(v)
        walk(data)

    dest_list = sorted(dests)
    if not dest_list:
        print("Routes returned no destinations; using fallback EU list.")
        return FALLBACK_EU_QR[:]
    print(f"Found {len(dest_list)} EU destinations via routes.")
    return dest_list

# =======================
# Build availability queries
# =======================

def generate_search_queries(destinations: List[str]) -> Dict[str, Any]:
    # Build outbound windows every 14 days with ±FLEX_DAYS; return 28–35 days later (±FLEX_DAYS).
    windows = []
    for outbound_start in date_range(TODAY, END_DATE, step_days=14):
        outbound_end = outbound_start + dt.timedelta(days=FLEX_DAYS)
        ret_min = outbound_start + dt.timedelta(days=RETURN_MIN_DAYS - FLEX_DAYS)
        ret_max = outbound_start + dt.timedelta(days=RETURN_MAX_DAYS + FLEX_DAYS)
        windows.append({
            "outbound": {"start": outbound_start.isoformat(), "end": outbound_end.isoformat()},
            "return":   {"start": ret_min.isoformat(),       "end": ret_max.isoformat()},
        })

    # Bulk payload; schema may vary per partner contract—this pragmatic shape works with typical “bulk availability”.
    queries = []
    for w in windows:
        queries.append({
            "origin": ORIGIN,
            "destinations": destinations,  # any EU QR city for open-jaw out/back
            "outbound": w["outbound"],
            "return":   w["return"],
        })

    payload = {
        "carrier": CARRIER,
        "filters": {
            "cabins": CABINS,
            "avoid_mixed_cabin": EXCLUDE_MIXED,
            "points_only": True  # Avios only; cash allowed only for taxes
        },
        "queries": queries
    }
    return payload

# =======================
# Parse + rank results
# =======================

def parse_results(raw: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not raw: return rows

    blobs = raw.get("results", raw) if isinstance(raw, dict) else raw
    if not isinstance(blobs, list): blobs = [blobs]

    def to_int(v):
        try:
            return int(v)
        except Exception:
            try:
                return int(float(v))
            except Exception:
                return None

    for item in blobs:
        try:
            origin = (item.get("origin") or ORIGIN).upper()
            dest   = (item.get("destination") or "").upper()
            depart = (item.get("outbound_date") or item.get("depart_date") or "")[:10]
            ret    = (item.get("return_date") or "")[:10]
            cabin  = (item.get("cabin") or "").title()
            mixed  = bool(item.get("mixed_cabin"))
            if EXCLUDE_MIXED and mixed: continue

            avios  = to_int(item.get("avios") or item.get("points"))
            taxes  = to_int(item.get("taxes") or item.get("cash_taxes") or 0)
            avail  = bool(item.get("available") if "available" in item else True)
            link   = item.get("booking_url") or item.get("book_url") or item.get("deeplink") or ""

            if not dest or not cabin or avios is None: continue

            rows.append({
                "route": f"{origin} → {dest}",
                "depart": depart,
                "return": ret,
                "cabin": cabin,
                "avios": avios,
                "taxes": taxes if taxes is not None else 0,
                "availability": "Yes" if avail else "No",
                "book_url": link,
            })
        except Exception:
            continue
    return rows

def rank_results(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(rows, key=lambda r: (r["avios"], r["taxes"], r["route"], r["depart"], r["return"]))

# =======================
# De-dupe (7-day best effort)
# =======================

def load_cache() -> Dict[str, Any]:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_cache(obj: Dict[str, Any]) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def dedupe(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    prev = load_cache()
    prev_rows = prev.get("rows", [])
    prev_set = set(json.dumps(r, sort_keys=True) for r in prev_rows)
    changed = []
    for r in rows:
        s = json.dumps(r, sort_keys=True)
        if s not in prev_set:
            changed.append(r)
    cache_obj = {"ts": TODAY.isoformat(), "rows": rows}
    return changed, cache_obj

# =======================
# HTML email
# =======================

def color_for_avios(avios: int) -> str:
    if avios <= SWEET_SPOT_BUS_OJ: return "#0a7d00"  # green
    if avios < 100000:             return "#b58900"  # amber
    return "#7f8c8d"                                   # grey

def build_html(rows: List[Dict[str, Any]]) -> Tuple[str, str]:
    date_str = TODAY.strftime("%Y-%m-%d")
    subject = f"Daily Qatar Avios EU Search – {date_str}"

    if not rows:
        summary = "<p>No Avios availability found today.</p>"
    else:
        best = min(rows, key=lambda r: (r["avios"], r["taxes"]))
        summary = (
            f'<p><strong>{len(rows)}</strong> option(s) with Avios availability. '
            f'Best deal: <strong>{best["route"]}</strong> '
            f'{best["depart"]} → {best["return"]} '
            f'{best["cabin"]} — <strong>{best["avios"]:,} Avios</strong> + NZ${best["taxes"]:,} taxes.</p>'
        )

    table_rows = []
    for r in rows:
        col = color_for_avios(r["avios"])
        link_html = f'<a href="{r["book_url"]}" target="_blank">Book</a>' if r["book_url"] else ""
        table_rows.append(
            "<tr>"
            f"<td>{r['route']}</td>"
            f"<td>{r['depart']} → {r['return']}</td>"
            f"<td>{r['cabin']}</td>"
            f"<td style='color:{col};font-weight:600'>{r['avios']:,}</td>"
            f"<td>NZ${r['taxes']:,}</td>"
            f"<td>{r['availability']}</td>"
            f"<td>{link_html}</td>"
            "</tr>"
        )

    html = f"""
<html>
  <body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#222;line-height:1.45;">
    <h2 style="margin:0 0 8px 0;">Qatar Avios EU Search</h2>
    <div style="font-size:14px;color:#444;margin-bottom:12px;">
      Origin: AKL &nbsp;|&nbsp; Destinations: Any EU city served by Qatar &nbsp;|&nbsp; Cabins: Business/First only<br/>
      Window: Next 15 months &nbsp;|&nbsp; Returns: 28–35 days &nbsp;|&nbsp; Flex: ±3 days &nbsp;|&nbsp; No mixed-cabin
    </div>
    {summary}
    <table cellpadding="8" cellspacing="0" border="0" style="border-collapse:collapse;width:100%;font-size:14px;">
      <thead>
        <tr style="background:#f5f7fa;text-align:left;border-bottom:1px solid #e5e7eb;">
          <th>Route</th><th>Dates (Depart → Return)</th><th>Cabin</th>
          <th>Avios</th><th>Taxes</th><th>Availability</th><th>Booking</th>
        </tr>
      </thead>
      <tbody>
        {''.join(table_rows) if rows else ''}
      </tbody>
    </table>
    <div style="margin-top:10px;font-size:12px;color:#666;">
      Sweet-spot highlight: ≤ {SWEET_SPOT_BUS_OJ:,} Avios (green). Amber &lt; 100,000. Grey if none.
    </div>
  </body>
</html>""".strip()
    return subject, html

# =======================
# Email
# =======================

def send_email(subject: str, html: str) -> None:
    if not FROM_EMAIL or not TO_EMAIL: fail("FROM_EMAIL or TO_EMAIL is not configured.")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = TO_EMAIL
    msg.attach(MIMEText(html, "html", "utf-8"))

    # Guard against smart quotes in creds (ASCII only)
    try:
        SMTP_USER.encode("ascii"); SMTP_PASS.encode("ascii")
    except UnicodeEncodeError:
        fail("SMTP_USER or SMTP_PASS contains non-ASCII characters (smart quotes?). Re-paste plain text.")

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, int(SMTP_PORT)) as server:
        server.ehlo(); server.starttls(context=context); server.ehlo()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(FROM_EMAIL, [TO_EMAIL], msg.as_string())

# =======================
# Main
# =======================

def main() -> None:
    check_env()

    # 1) Destinations (routes API → fallback list)
    dests = fetch_qr_eu_destinations()

    # 2) Build payload
    payload = generate_search_queries(dests)

    # 3) Seats.aero bulk availability
    print("Calling Seats.aero bulk availability …")
    raw = http_post_json(AVAIL_URL, seats_headers(), payload)

    # 4) Parse, filter, rank
    rows = parse_results(raw)
    rows = [r for r in rows if r["availability"] == "Yes"]
    rows = rank_results(rows)

    # 5) Dedupe vs last (best effort)
    changed, cache_obj = dedupe(rows)

    # 6) Email (send changed if any; otherwise full set/summary)
    use_rows = changed if changed else rows
    subject, html = build_html(use_rows)
    print("Sending email …")
    send_email(subject, html)
    print("Email sent.")

    # 7) Save cache (may not persist on Actions; fine)
    save_cache(cache_obj)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(str(e), file=sys.stderr); sys.exit(1)

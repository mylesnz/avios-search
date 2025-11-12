#!/usr/bin/env python3
import os
import sys
import json
import time
import datetime as dt
import urllib.request
import urllib.parse
from typing import Any, Dict, List, Optional

# -----------------------------
# Configuration from env (with safe defaults)
# -----------------------------

def getenv_stripped(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.getenv(name, default)
    if val is None:
        return None
    return val.strip()

SEATSAERO_API_KEY = getenv_stripped("SEATSAERO_API_KEY")
BREVO_API_KEY     = getenv_stripped("BREVO_API_KEY")
FROM_EMAIL        = getenv_stripped("FROM_EMAIL")
FROM_NAME         = getenv_stripped("FROM_NAME", "Avios Daily")
TO_EMAIL          = getenv_stripped("TO_EMAIL")

# Defaults so the workflow doesn't fail just because URLs aren't set
AVAIL_URL  = getenv_stripped("SEATSAERO_AVAIL_URL",  "https://seats.aero/partnerapi/availability")
ROUTES_URL = getenv_stripped("SEATSAERO_ROUTES_URL", "https://seats.aero/partnerapi/routes")

# Query window
DAYS_AHEAD = int(getenv_stripped("DAYS_AHEAD", "60") or "60")

# Origin we check from (Qatar Airways hub)
QR_ORIGIN = getenv_stripped("QR_ORIGIN", "DOH")

# Fallback list of EU airports if /routes is locked out
EU_DESTS_FALLBACK = [
    "LHR","LGW","MAN","EDI","GLA","BHX","DUB",
    "CDG","ORY","AMS","FRA","MUC","BER","DUS","HAM","STR",
    "ZRH","GVA","VIE","PRG","WAW","CPH","ARN","OSL","HEL",
    "MAD","BCN","PMI","LIS","OPO","BRU","ATH","MXP","FCO","VCE","NAP","PSA",
    "BUD","OTP"
]

# -----------------------------
# Basic guardrails
# -----------------------------

def require_env(name: str, val: Optional[str]) -> None:
    if not val:
        print(f"❌ Required env var missing: {name}", file=sys.stderr)
        sys.exit(1)

for must in ("SEATSAERO_API_KEY", "BREVO_API_KEY", "FROM_EMAIL", "TO_EMAIL"):
    require_env(must, globals()[must])

# -----------------------------
# HTTP helpers
# -----------------------------

def http_get_json(url: str, headers: Dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        ctype = resp.headers.get("Content-Type","")
        if "application/json" in ctype:
            return json.loads(body.decode("utf-8"))
        # Try to parse anyway
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {"raw": body.decode("utf-8")}

def http_post_json(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Any:
    data = json.dumps(payload).encode("utf-8")
    h = {**headers, "Content-Type":"application/json"}
    req = urllib.request.Request(url, headers=h, method="POST", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        ctype = resp.headers.get("Content-Type","")
        if "application/json" in ctype:
            return json.loads(body.decode("utf-8"))
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {"status": resp.status, "raw": body.decode("utf-8")}

# -----------------------------
# Seats.aero partner API
# -----------------------------

def seats_headers() -> Dict[str, str]:
    return {
        "Partner-Authorization": SEATSAERO_API_KEY,
        "Accept": "application/json",
        "User-Agent": "avios-daily/1.0 (+github actions)"
    }

def get_qr_eu_dests_from_routes() -> Optional[List[str]]:
    """
    Try to read QR routes and collect EU destinations where origin is DOH.
    If unauthorized or unexpected, return None so we can fall back.
    """
    try:
        # Many partner accounts require no params; some allow filtering.
        # We'll fetch and filter client-side for carrier=QR & OriginAirport=DOH & DestinationRegion=Europe
        url = ROUTES_URL
        print(f"Fetching QR EU destinations from routes endpoint …")
        obj = http_get_json(url, headers=seats_headers())
        data = obj.get("data", []) if isinstance(obj, dict) else []
        dests = set()
        for r in data:
            try:
                carrier = r.get("Carrier") or r.get("carrier")  # schema leniency
                origin  = r.get("OriginAirport") or r.get("origin")
                dest    = r.get("DestinationAirport") or r.get("dest")
                dest_rg = r.get("DestinationRegion") or r.get("destinationRegion")
                if (carrier == "QR") and (origin == QR_ORIGIN) and (dest_rg == "Europe") and dest:
                    dests.add(dest)
            except Exception:
                continue
        if dests:
            out = sorted(dests)
            print(f"Routes endpoint returned {len(out)} EU destinations.")
            return out
        print("Routes endpoint returned 0 EU destinations after filtering; will fall back.")
        return None
    except urllib.error.HTTPError as e:
        print(f"Routes endpoint unavailable (GET {ROUTES_URL} -> {e.code} {e.reason}); using fallback EU list.")
        return None
    except Exception as e:
        print(f"Routes endpoint error: {e}; using fallback EU list.")
        return None

def availability(origin: str, dest: str, start: str, end: str) -> List[Dict[str, Any]]:
    """
    GET /availability?carrier=QR&origin=DOH&dest=XXX&start=YYYY-MM-DD&end=YYYY-MM-DD
    Returns a list under 'data' (or raw list).
    """
    qs = urllib.parse.urlencode({
        "carrier": "QR",
        "origin": origin,
        "dest": dest,
        "start": start,
        "end": end,
    })
    url = f"{AVAIL_URL}?{qs}"
    obj = http_get_json(url, headers=seats_headers())
    if isinstance(obj, dict) and "data" in obj:
        return obj["data"]
    if isinstance(obj, list):
        return obj
    # Unexpected
    return []

def any_available(row: Dict[str, Any]) -> bool:
    return bool(row.get("YAvailable") or row.get("WAvailable") or row.get("JAvailable") or row.get("FAvailable"))

# -----------------------------
# Brevo sender (REST)
# -----------------------------

def send_email_brevo(subject: str, html: str, text: Optional[str] = None) -> None:
    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "api-key": BREVO_API_KEY,
        "accept": "application/json"
    }
    payload = {
        "sender": {"email": FROM_EMAIL, "name": FROM_NAME or "Avios Daily"},
        "to": [{"email": TO_EMAIL}],
        "subject": subject,
        "htmlContent": html
    }
    if text:
        payload["textContent"] = text
    try:
        resp = http_post_json(url, headers, payload)
        message_id = resp.get("messageId") or resp.get("messageIds") or resp
        print(f"📧 Brevo accepted message: {message_id}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"❌ Brevo email failed: {e.code} {e.reason}\n{body}", file=sys.stderr)
        sys.exit(1)
    except Exception as ex:
        print(f"❌ Brevo email error: {ex}", file=sys.stderr)
        sys.exit(1)

# -----------------------------
# HTML rendering
# -----------------------------

def html_escape(s: Any) -> str:
    return (
        str(s)
        .replace("&","&amp;")
        .replace("<","&lt;")
        .replace(">","&gt;")
    )

def render_rows_html(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "<p>No availability found today.</p>"
    header = """
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:14px;">
  <thead style="background:#f3f4f6">
    <tr>
      <th>Date</th>
      <th>Origin</th>
      <th>Destination</th>
      <th>Y</th>
      <th>W</th>
      <th>J</th>
      <th>F</th>
      <th>Source</th>
    </tr>
  </thead>
  <tbody>
"""
    body = []
    for r in rows:
        route = r.get("Route") or {}
        o = route.get("OriginAirport") or r.get("Origin") or QR_ORIGIN
        d = route.get("DestinationAirport") or r.get("Destination") or "?"
        y = "✓" if r.get("YAvailable") else ""
        w = "✓" if r.get("WAvailable") else ""
        j = "✓" if r.get("JAvailable") else ""
        f = "✓" if r.get("FAvailable") else ""
        src = (route.get("Source") or "").upper()
        body.append(
            f"<tr>"
            f"<td>{html_escape(r.get('Date',''))}</td>"
            f"<td>{html_escape(o)}</td>"
            f"<td>{html_escape(d)}</td>"
            f"<td style='text-align:center'>{y}</td>"
            f"<td style='text-align:center'>{w}</td>"
            f"<td style='text-align:center'>{j}</td>"
            f"<td style='text-align:center'>{f}</td>"
            f"<td>{html_escape(src)}</td>"
            f"</tr>"
        )
    tail = """
  </tbody>
</table>
"""
    return header + "\n".join(body) + tail

# -----------------------------
# Main
# -----------------------------

def main() -> None:
    print("🚀 Running Seats.aero → Brevo integration...")

    today = dt.date.today()
    start = today.isoformat()
    end   = (today + dt.timedelta(days=DAYS_AHEAD)).isoformat()

    # 1) Get destination list
    dests = get_qr_eu_dests_from_routes()
    if not dests:
        dests = EU_DESTS_FALLBACK
        print(f"Using fallback EU list ({len(dests)} destinations).")

    # 2) Query availability DOH -> each EU destination
    all_rows: List[Dict[str, Any]] = []
    hit_rows: List[Dict[str, Any]] = []

    print(f"Checking QR {QR_ORIGIN} → EU from {start} to {end} across {len(dests)} destinations...")
    for i, dest in enumerate(dests, start=1):
        try:
            rows = availability(QR_ORIGIN, dest, start, end)
            all_rows.extend(rows)
            avail = [r for r in rows if any_available(r)]
            if avail:
                hit_rows.extend(avail)
            if i % 10 == 0:
                print(f"  … {i}/{len(dests)} destinations scanned")
            time.sleep(0.1)  # be polite
        except urllib.error.HTTPError as e:
            print(f"  {QR_ORIGIN}->{dest} HTTP {e.code} {e.reason}")
        except Exception as ex:
            print(f"  {QR_ORIGIN}->{dest} error: {ex}")

    print(f"✅ Total rows: {len(all_rows)} ; with availability: {len(hit_rows)}")

    # 3) Build email
    subject = f"QR → EU award seats ({len(hit_rows)} hits) – {today.isoformat()}"
    preamble = (
        f"<p>Window: <b>{html_escape(start)}</b> to <b>{html_escape(end)}</b><br>"
        f"Origin: <b>{html_escape(QR_ORIGIN)}</b> • Destinations scanned: <b>{len(dests)}</b></p>"
    )
    html = (
        f"<div style='font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif'>"
        f"<h2 style='margin:0 0 12px'>Qatar Airways → Europe (Avios) – Daily Scan</h2>"
        f"{preamble}"
        + (render_rows_html(hit_rows) if hit_rows else "<p><b>No availability found.</b></p>")
        + "<p style='color:#6b7280;font-size:12px;margin-top:16px'>"
          "Data from Seats.aero Partner API. Times in API are per-day granularity."
          "</p>"
        f"</div>"
    )

    text = f"QR → EU award seats\nWindow: {start} to {end}\nHits: {len(hit_rows)}\n"

    # 4) Send via Brevo
    send_email_brevo(subject=subject, html=html, text=text)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)

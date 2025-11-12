#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Qatar Avios EU finder + emailer.

Environment:
  # Seats.aero
  SEATSAERO_API_KEY      = pro_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  SEATSAERO_ROUTES_URL   = https://seats.aero/partnerapi/routes?carrier=QR&region=EU
  SEATSAERO_AVAIL_URL    = https://seats.aero/partnerapi/availability

  # Query window (optional; defaults = today → +45 days)
  OUTBOUND_START         = 2026-02-01
  OUTBOUND_END           = 2026-02-28
  ORIGIN                 = AKL
  CARRIER                = QR

  # Email (Brevo REST preferred)
  FROM_EMAIL             = you@imoffto.xyz
  TO_EMAIL               = you@example.com
  FROM_NAME              = Avios Alerts
  BREVO_API_KEY          = xkeysib_...   # <-- REST API key (works even if SMTP blocked)

  # Optional SMTP fallback (only if you insist)
  SMTP_HOST              = smtp-relay.brevo.com
  SMTP_PORT              = 587
  SMTP_USER              = apikey
  SMTP_PASS              = xsmtpsib-...  # <-- SMTP key, different from xkeysib
"""

import json
import os
import ssl
import sys
import time
import smtplib
import urllib.parse
import urllib.request
from datetime import date, timedelta

# ---------- Helpers ----------

def getenv_str(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and (v is None or str(v).strip() == ""):
        die(f"Required env var missing: {name}")
    return v.strip() if isinstance(v, str) else v

def die(msg, code=1):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(code)

def log(msg):
    print(msg, flush=True)

def is_partner_api(url: str) -> bool:
    # Seats.aero Partner API lives under /partnerapi/...
    return "/partnerapi/" in url

def auth_headers(url: str, api_key: str) -> dict:
    # Partner API uses 'Partner-Authorization: Bearer <key>'
    if is_partner_api(url):
        return {
            "Accept": "application/json",
            "Partner-Authorization": f"Bearer {api_key}",
            "User-Agent": "avios-search/1.0"
        }
    # Generic (non-partner) APIs (kept for completeness)
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "avios-search/1.0"
    }

def http_get_json(url: str, headers: dict, timeout=30, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status != 200:
                    body = r.read().decode("utf-8", "ignore")
                    raise RuntimeError(f"HTTP {r.status} → {body[:200]}")
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last

def http_post_json(url: str, headers: dict, payload: dict, timeout=30, retries=1):
    last = None
    data = json.dumps(payload).encode("utf-8")
    hdrs = dict(headers)
    hdrs.setdefault("Content-Type", "application/json")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status not in (200, 201, 202):
                    body = r.read().decode("utf-8", "ignore")
                    raise RuntimeError(f"HTTP {r.status} → {body[:200]}")
                txt = r.read().decode("utf-8") if r.length else "{}"
                return json.loads(txt) if txt else {}
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last

# ---------- Seats.aero logic ----------

FALLBACK_EU_DESTS = [
    # Common EU hubs; routes endpoint can refine when available
    "LHR", "LGW", "MAN", "CDG", "AMS", "FRA", "MUC", "DUS",
    "MAD", "BCN", "ZRH", "VIE", "CPH", "ARN", "OSL", "BRU",
    "DUB", "LIS", "PRG", "WAW", "ATH", "MXP", "FCO"
]

def load_window():
    # default: today → +45 days
    start = getenv_str("OUTBOUND_START")
    end = getenv_str("OUTBOUND_END")
    def fmt(d): return d.strftime("%Y-%m-%d")
    if not start:
        start = fmt(date.today())
    if not end:
        end = fmt(date.today() + timedelta(days=45))
    return start, end

def fetch_eu_destinations(routes_url: str, api_key: str) -> list[str]:
    if not routes_url:
        log("ℹ️ No SEATSAERO_ROUTES_URL set; using fallback EU list.")
        return FALLBACK_EU_DESTS
    headers = auth_headers(routes_url, api_key)
    try:
        log("🔎 Fetching EU destinations from routes endpoint …")
        data = http_get_json(routes_url, headers)
        # expected shape: [{"origin":"DOH","destination":"CDG","carrier":"QR",...}, ...]
        dests = sorted({row.get("destination","").upper() for row in data if row.get("destination")})
        if not dests:
            raise ValueError("Routes response empty")
        log(f"✅ Got {len(dests)} EU destinations from routes")
        return dests
    except Exception as e:
        log(f"⚠️ Routes fetch failed ({e}); falling back to static EU list.")
        return FALLBACK_EU_DESTS

def query_availability(avail_url: str, api_key: str, carrier: str, origin: str,
                       destinations: list[str], start: str, end: str) -> list[dict]:
    """
    Partner API supports GET with query params:
      /partnerapi/availability?carrier=QR&origin=AKL&dest=CDG&start=YYYY-MM-DD&end=YYYY-MM-DD
    We'll loop destinations (keeps responses small & avoids CF issues).
    """
    headers = auth_headers(avail_url, api_key)
    results = []
    for dest in destinations:
        q = {
            "carrier": carrier,
            "origin":  origin,
            "dest":    dest,
            "start":   start,
            "end":     end
        }
        url = f"{avail_url}?{urllib.parse.urlencode(q)}"
        try:
            data = http_get_json(url, headers)
            # expected shape: list of flights / objects; we pass through fields we care about
            for row in data or []:
                row["destination"] = dest  # just in case
            results.extend(data or [])
            time.sleep(0.1)  # be polite to CF
        except Exception as e:
            log(f"⚠️ Availability GET failed for {dest}: {e}")
    return results

# ---------- Email (Brevo REST preferred) ----------

def send_email_brevo_rest(subject: str, html: str) -> None:
    api_key = getenv_str("BREVO_API_KEY")
    if not api_key:
        raise RuntimeError("BREVO_API_KEY not set (needed for REST send).")

    from_email = getenv_str("FROM_EMAIL", required=True)
    from_name  = getenv_str("FROM_NAME", "Avios Alerts")
    to_email   = getenv_str("TO_EMAIL", required=True)

    payload = {
        "sender": {"email": from_email, "name": from_name},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html
    }
    log("📮 Sending via Brevo REST /v3/smtp/email …")
    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "api-key": api_key
    }
    # Using our http_post_json helper
    _ = http_post_json(url, headers, payload, timeout=30, retries=0)
    log("✅ Brevo REST send accepted")

def send_email_smtp(subject: str, html: str) -> None:
    host = getenv_str("SMTP_HOST", required=True)
    port = int(getenv_str("SMTP_PORT", "587"))
    user = getenv_str("SMTP_USER", required=True)
    pw   = getenv_str("SMTP_PASS", required=True)
    from_email = getenv_str("FROM_EMAIL", required=True)
    from_name  = getenv_str("FROM_NAME", "Avios Alerts")
    to_email   = getenv_str("TO_EMAIL", required=True)

    msg = f"From: {from_name} <{from_email}>\r\n" \
          f"To: <{to_email}>\r\n" \
          f"Subject: {subject}\r\n" \
          f"MIME-Version: 1.0\r\n" \
          f"Content-Type: text/html; charset=UTF-8\r\n\r\n{html}"

    ctx = ssl.create_default_context()
    log(f"📮 Sending via SMTP {host}:{port} as {user!r} …")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=25) as s:
                s.login(user, pw)
                s.sendmail(from_email, [to_email], msg.encode("utf-8"))
        else:
            with smtplib.SMTP(host, port, timeout=25) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(user, pw)
                s.sendmail(from_email, [to_email], msg.encode("utf-8"))
        log("✅ SMTP send OK")
    except smtplib.SMTPAuthenticationError as e:
        die(f"SMTP auth failed: {e}")
    except Exception as e:
        die(f"SMTP send failed: {e}")

def send_email(subject: str, html: str) -> None:
    # Prefer REST if available (avoids “535 auth failed” headaches)
    if getenv_str("BREVO_API_KEY"):
        send_email_brevo_rest(subject, html)
        return
    # Else fall back to SMTP if configured
    if getenv_str("SMTP_HOST") and getenv_str("SMTP_USER") and getenv_str("SMTP_PASS"):
        send_email_smtp(subject, html)
        return
    die("No email method configured: set BREVO_API_KEY (preferred) or SMTP_* envs.")

# ---------- HTML ----------

def build_html(results: list[dict], start: str, end: str, origin: str, destinations: list[str]) -> str:
    if not results:
        body = f"<p>No Qatar Avios seats found from <b>{origin}</b> to EU between <b>{start}</b> and <b>{end}</b>.</p>"
    else:
        rows = []
        # We don't know exact schema; try to render common fields defensively
        for r in results:
            d = r.get("dest") or r.get("destination") or ""
            o = r.get("origin") or origin
            dt = r.get("date") or r.get("outboundDate") or r.get("departure") or ""
            cabin = r.get("cabin") or r.get("class") or ""
            seats = r.get("seats") or r.get("available") or r.get("availability") or ""
            flight = r.get("flight") or r.get("flightNumber") or ""
            price = r.get("avios") or r.get("miles") or r.get("points") or ""
            rows.append(
                f"<tr>"
                f"<td>{o}</td><td>{d}</td><td>{dt}</td>"
                f"<td>{flight}</td><td>{cabin}</td><td>{seats}</td><td>{price}</td>"
                f"</tr>"
            )
        table = (
            "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
            "<thead><tr>"
            "<th>Origin</th><th>Dest</th><th>Date/Time</th>"
            "<th>Flight</th><th>Cabin</th><th>Seats</th><th>Avios/Miles</th>"
            "</tr></thead><tbody>"
            + "".join(rows) +
            "</tbody></table>"
        )
        body = f"<p>Found <b>{len(results)}</b> options from <b>{origin}</b> to EU ({len(destinations)} destinations) " \
               f"between <b>{start}</b> and <b>{end}</b>.</p>{table}"

    return f"""<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Qatar Avios EU</title></head>
  <body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
    <h2>Qatar Avios EU Availability</h2>
    {body}
    <p style="color:#999;margin-top:16px">Generated {date.today().isoformat()}.</p>
  </body>
</html>
"""

# ---------- Main ----------

def main():
    # Required config
    api_key   = getenv_str("SEATSAERO_API_KEY", required=True)
    avail_url = getenv_str("SEATSAERO_AVAIL_URL", required=True)
    routes_url= getenv_str("SEATSAERO_ROUTES_URL", "")  # optional

    carrier = getenv_str("CARRIER", "QR")
    origin  = getenv_str("ORIGIN",  "AKL")
    start, end = load_window()

    log("🚀 Running daily Avios scan …")
    log(f"   carrier={carrier} origin={origin} range={start} → {end}")
    log(f"   routes_url={routes_url or '(none)'}")
    log(f"   avail_url={avail_url}")

    # Sanity on auth header type
    if not is_partner_api(avail_url):
        log("⚠️ SEATSAERO_AVAIL_URL does not look like Partner API. Expect failures unless this is intentional.")
    if routes_url and not is_partner_api(routes_url):
        log("⚠️ SEATSAERO_ROUTES_URL does not look like Partner API. Expect failures unless this is intentional.")

    # Destinations
    dests = fetch_eu_destinations(routes_url, api_key)

    # Query availability
    log(f"🔎 Querying availability for {len(dests)} EU destinations …")
    results = query_availability(avail_url, api_key, carrier, origin, dests, start, end)
    log(f"✅ Availability fetch complete: {len(results)} rows")

    # Email
    subject = f"[Avios] {carrier} {origin}→EU {start}–{end} ({len(results)} hits)"
    html = build_html(results, start, end, origin, dests)
    send_email(subject, html)
    log("🎉 Done.")

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        die(f"Unhandled error: {e!r}")

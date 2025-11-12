#!/usr/bin/env python3
import os
import sys
import datetime as dt
import json
import urllib.request
import urllib.error

# --- Configuration & env defaults ---
SEATSAERO_API_KEY = os.getenv("SEATSAERO_API_KEY")
BREVO_API_KEY     = os.getenv("BREVO_API_KEY")
FROM_EMAIL        = os.getenv("FROM_EMAIL")
FROM_NAME         = os.getenv("FROM_NAME", "AviosBot")
TO_EMAIL          = os.getenv("TO_EMAIL")

AVAIL_URL  = os.getenv("SEATSAERO_AVAIL_URL",  "https://seats.aero/partnerapi/availability").strip()
ROUTES_URL = os.getenv("SEATSAERO_ROUTES_URL", "https://seats.aero/partnerapi/routes").strip()

def require_env(key, val):
    if not val:
        print(f"❌ Required env var missing: {key}")
        sys.exit(1)

for k, v in {
    "SEATSAERO_API_KEY": SEATSAERO_API_KEY,
    "BREVO_API_KEY":     BREVO_API_KEY,
    "FROM_EMAIL":        FROM_EMAIL,
    "TO_EMAIL":          TO_EMAIL,
}.items():
    require_env(k, v)

# --- Utility: HTTP GET wrapper ---
def http_get(url):
    try:
        req = urllib.request.Request(url, headers={
            "Partner-Authorization": SEATSAERO_API_KEY,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"❌ HTTP {e.code} {e.reason}")
        try:
            print(e.read().decode())
        except Exception:
            pass
        sys.exit(1)
    except Exception as ex:
        print("💥 GET failed:", ex)
        sys.exit(1)

# --- API Helpers ---
def availability(origin, dest, start, end):
    qs = f"carrier=QR&origin={origin}&dest={dest}&start={start}&end={end}"
    return http_get(f"{AVAIL_URL}?{qs}")

def normalize_rows(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload if isinstance(payload, list) else []

def any_available(row):
    return any([row.get("YAvailable"), row.get("WAvailable"),
                row.get("JAvailable"), row.get("FAvailable")])

# --- Brevo mailer ---
def send_email(subject, html_body):
    try:
        payload = {
            "sender": {"email": FROM_EMAIL, "name": FROM_NAME},
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
                "api-key": BREVO_API_KEY,
            },
        )
        with urllib.request.urlopen(req) as resp:
            code = resp.getcode()
            print(f"📤 Email sent (HTTP {code})")
    except Exception as e:
        print("💥 Email send failed:", e)

# --- Main ---
def main():
    today = dt.date.today().isoformat()
    origin, dest = "AKL", "LHR"
    print(f"🔍 Checking {origin}→{dest} for {today}")

    payload = availability(origin, dest, today, today)
    rows = normalize_rows(payload)
    hits = [r for r in rows if any_available(r)]

    print(f"✅ Total rows: {len(rows)}, Available: {len(hits)}")

    if hits:
        html = "<h3>Available seats found</h3><ul>"
        for r in hits[:10]:
            route = r.get("Route", {})
            html += (f"<li>{r.get('Date')} {origin}→{route.get('DestinationAirport')} "
                     f"Y/W/J/F={int(bool(r.get('YAvailable')))}/"
                     f"{int(bool(r.get('WAvailable')))}/"
                     f"{int(bool(r.get('JAvailable')))}/"
                     f"{int(bool(r.get('FAvailable')))}</li>")
        html += "</ul>"
        send_email(f"Avios Availability {origin}→{dest} ({today})", html)
    else:
        print("ℹ️ No seats available today — no email sent.")

if __name__ == "__main__":
    main()

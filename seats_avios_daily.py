#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Qatar / Avios-ish daily scanner
"""

import os
import json
import time
import datetime as dt
import urllib.request
import urllib.error
import urllib.parse
import html

# ---------------------------------------------------------
# Environment
# ---------------------------------------------------------

def env_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Environment variable {name} is required but missing.")
    return v

SEATSAERO_API_KEY = env_required("SEATSAERO_API_KEY")
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "")
TO_EMAIL = os.getenv("TO_EMAIL", "")
FROM_NAME = os.getenv("FROM_NAME", "Avios Bot")

DRY_RUN = int(os.getenv("DRY_RUN", "1"))
DEBUG = int(os.getenv("DEBUG", "1"))

# ---------------------------------------------------------
# Constants
# ---------------------------------------------------------

SEATSAERO_AVAIL_URL = "https://seats.aero/partnerapi/availability"
ORIGIN = "AKL"

STATIC_EU = [
    "AMS","ATH","BCN","BER","BRU","BUD","CPH","DUB","DUS","FCO","FRA","HEL",
    "LIS","LYS","MAD","MUC","MXP","NCE","OTP","PRG","SOF","VCE","VIE","WAW","ZAG"
]

IATA_NAMES = {
    "AKL":"Auckland","AMS":"Amsterdam","ATH":"Athens","BCN":"Barcelona","BER":"Berlin",
    "BRU":"Brussels","BUD":"Budapest","CPH":"Copenhagen","DUB":"Dublin","DUS":"Dusseldorf",
    "FCO":"Rome","FRA":"Frankfurt","HEL":"Helsinki","LIS":"Lisbon","LYS":"Lyon",
    "MAD":"Madrid","MUC":"Munich","MXP":"Milan","NCE":"Nice","OTP":"Bucharest",
    "PRG":"Prague","SOF":"Sofia","VCE":"Venice","VIE":"Vienna","WAW":"Warsaw","ZAG":"Zagreb",
}

SCAN_MONTHS = 9   # tomorrow → +9 months

# ---------------------------------------------------------
# Utils
# ---------------------------------------------------------

def log(x):
    if DEBUG:
        print(x)

def fmt_http_date(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def fmt_html_date(d: dt.date) -> str:
    return d.strftime("%d/%m/%Y")

def first_non_zero(row, keys):
    for k in keys:
        v = row.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return v
    return None

# ---------------------------------------------------------
# Seats.aero
# ---------------------------------------------------------

def seats_get(params):
    q = urllib.parse.urlencode(params)
    url = f"{SEATSAERO_AVAIL_URL}?{q}"
    req = urllib.request.Request(url, headers={"Partner-Authorization": SEATSAERO_API_KEY})
    log(f"[GET] {url}")

    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log(f"HTTP {e.code} on {params.get('origin')}->{params.get('dest')}: {e.reason}")
        return {}
    except Exception as e:
        log(f"ERR on {params.get('origin')}->{params.get('dest')}: {e}")
        return {}

def availability(origin, dest, start, end, sample=False):
    params = {
        "origin": origin,
        "dest": dest,
        "start": fmt_http_date(start),
        "end": fmt_http_date(end),
    }
    resp = seats_get(params)
    data = resp.get("response", {}).get("data", [])
    log(f"{origin}->{dest}: {len(data)} rows")

    if sample:
        try:
            with open("seats_sample.json", "w", encoding="utf-8") as f:
                json.dump(resp, f, indent=2)
            log("Wrote seats_sample.json")
        except:
            pass

    return data

# ---------------------------------------------------------
# Convert raw rows to Business / First cabin candidates
# ---------------------------------------------------------

def row_to_cabin_candidates(row):
    route = row.get("Route", {})
    origin = route.get("OriginAirport")
    dest = route.get("DestinationAirport")

    if origin != ORIGIN or dest not in STATIC_EU:
        return []

    try:
        dep = dt.date.fromisoformat(row.get("Date"))
    except:
        return []

    currency = row.get("TaxesCurrency", "")

    out = []

    def build(code, name):
        if code == "J":
            miles = first_non_zero(row, ["JDirectMileageCostRaw","JMileageCostRaw"])
            taxes = first_non_zero(row, ["JDirectTotalTaxesRaw","JTotalTaxesRaw"])
            seats = first_non_zero(row, ["JDirectRemainingSeatsRaw","JRemainingSeatsRaw"]) or 0
            airlines = row.get("JDirectAirlinesRaw") or row.get("JAirlinesRaw") or ""
        else:
            miles = first_non_zero(row, ["FDirectMileageCostRaw","FMileageCostRaw"])
            taxes = first_non_zero(row, ["FDirectTotalTaxesRaw","FTotalTaxesRaw"])
            seats = first_non_zero(row, ["FDirectRemainingSeatsRaw","FRemainingSeatsRaw"]) or 0
            airlines = row.get("FDirectAirlinesRaw") or row.get("FAirlinesRaw") or ""

        if not miles or miles <= 0:
            return None

        return {
            "origin": origin,
            "dest": dest,
            "date": dep,
            "cabin": name,
            "miles": miles,
            "taxes": taxes or 0,
            "seats": seats,
            "currency": currency,
            "airlines": airlines,
        }

    for code, name in (("J","Business"),("F","First")):
        c = build(code, name)
        if c:
            out.append(c)

    return out

# ---------------------------------------------------------
# Scan
# ---------------------------------------------------------

def scan():
    today = dt.date.today()
    start = today + dt.timedelta(days=1)
    end = today + dt.timedelta(days=SCAN_MONTHS * 30)

    log(f"Scan window: {fmt_html_date(start)} → {fmt_html_date(end)}")
    log(f"EU destinations: {STATIC_EU}")

    all_raw = []
    for idx, dest in enumerate(STATIC_EU):
        sample = (idx == 0)
        rows = availability(ORIGIN, dest, start, end, sample)
        all_raw.extend(rows)
        time.sleep(0.3)

    log(f"Total raw rows: {len(all_raw)}")

    cands = []
    for r in all_raw:
        cands.extend(row_to_cabin_candidates(r))

    log(f"Total Business/First candidates: {len(cands)}")

    business = sorted([c for c in cands if c["cabin"] == "Business"],
                      key=lambda x: (x["miles"], x["taxes"]))

    first = sorted([c for c in cands if c["cabin"] == "First"],
                   key=lambda x: (x["miles"], x["taxes"]))

    selected = business[:10] + first[:5]
    log(f"Selected {len(selected)} rows")

    return selected

# ---------------------------------------------------------
# HTML
# ---------------------------------------------------------

def build_html(items):
    today = dt.date.today()
    header_date = fmt_html_date(today)

    if not items:
        return f"""
<h1>Daily Avios AKL → EU – {header_date}</h1>
<p>No Business/First availability found.</p>
"""

    rows = ""
    for it in items:
        d = fmt_html_date(it["date"])
        miles = it["miles"]
        cabin = it["cabin"]

        if miles <= 90000:
            color = "#2e7d32"
        elif miles < 100000:
            color = "#e6a700"
        else:
            color = "#666666"

        route = f"{it['origin']} ({IATA_NAMES[it['origin']]}) → {it['dest']} ({IATA_NAMES[it['dest']]})"

        rows += (
            "<tr>"
            f"<td>{html.escape(route)}</td>"
            f"<td>{html.escape(d)}</td>"
            f"<td>{html.escape(cabin)}</td>"
            f"<td style='color:{color};font-weight:bold;'>{miles}</td>"
            f"<td>{it['taxes']} {html.escape(it['currency'])}</td>"
            f"<td>{it['seats']}</td>"
            f"<td>{html.escape(it['airlines'])}</td>"
            "</tr>"
        )

    return f"""
<h1>Daily Avios AKL → EU – {header_date}</h1>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Route</th><th>Date</th><th>Cabin</th><th>Miles</th><th>Taxes</th><th>Seats</th><th>Airlines</th></tr>
{rows}
</table>
"""

# ---------------------------------------------------------
# Email (Brevo)
# ---------------------------------------------------------

def send_email(subject, body):
    if DRY_RUN == 1:
        log("DRY_RUN=1 — not sending email")
        return

    if not BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY missing")

    payload = json.dumps({
        "sender": {"email": FROM_EMAIL, "name": FROM_NAME},
        "to": [{"email": TO_EMAIL}],
        "subject": subject,
        "htmlContent": body
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=payload,
        method="POST",
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": BREVO_API_KEY,
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log(f"Email sent OK: {r.status}")
    except Exception as e:
        log(f"Brevo error: {e}")

# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

if __name__ == "__main__":
    items = scan()
    html_body = build_html(items)

    outpath = os.path.join(os.getcwd(), "out.html")
    with open(outpath, "w", encoding="utf-8") as f:
        f.write(html_body)

    print(f"Wrote {outpath}")

    subject = f"Daily Avios AKL → EU – {fmt_html_date(dt.date.today())}"
    send_email(subject, html_body)

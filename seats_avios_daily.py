import os, smtplib, ssl, json, sys, time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone

# ====== CONFIG YOU EDIT (env vars preferred) ======
SEATSAERO_API_KEY = os.getenv("SEATSAERO_API_KEY", "").strip()
SEATSAERO_AVAIL_URL = os.getenv("SEATSAERO_AVAIL_URL", "").strip()  # from Seats.aero partner panel
SEATSAERO_ROUTES_URL = os.getenv("SEATSAERO_ROUTES_URL", "")        # optional; can be blank

FROM_EMAIL = os.getenv("FROM_EMAIL", "").strip()         # e.g. your Gmail address
FROM_NAME  = os.getenv("FROM_NAME", "Qatar Avios Bot")
TO_EMAIL   = os.getenv("TO_EMAIL", "Myles.Richardson@gmail.com")

SMTP_HOST  = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT  = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER  = os.getenv("SMTP_USER", FROM_EMAIL)
SMTP_PASS  = os.getenv("SMTP_PASS", "")                  # Gmail App Password or SMTP password

NZT = timezone(timedelta(hours=13))  # Pacific/Auckland (NZDT). Adjust if DST changes.
RUN_DATE = datetime.now(NZT).strftime("%Y-%m-%d")

# Business rules
ORIGIN = "AKL"
EU_STATIC = [
    "AMS","ATH","BCN","BER","BRU","BUD","CPH","DUB","DUS",
    "FCO","FRA","HEL","LIS","LYS","MAD","MUC","MXP","NCE",
    "OTP","PRG","SOF","VCE","VIE","WAW","ZAG"
]
FLEX_DAYS = 3
STAY_MIN, STAY_MAX = 28, 35
WINDOW_MONTHS = 15
AVIOS_SWEET_SPOT = 90000

# ====== SAFETY CHECKS ======
for varname, val in [
    ("SEATSAERO_API_KEY", SEATSAERO_API_KEY),
    ("SEATSAERO_AVAIL_URL", SEATSAERO_AVAIL_URL),
    ("FROM_EMAIL", FROM_EMAIL),
    ("SMTP_USER", SMTP_USER),
    ("SMTP_PASS", SMTP_PASS),
]:
    if not val:
        print(f"Missing required config: {varname}", file=sys.stderr)

# ====== HELPERS ======
def iso(d): return d.strftime("%Y-%m-%d")
def add_days(d, n): return d + timedelta(days=n)

def build_date_anchors():
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=WINDOW_MONTHS * 30)
    anchors = []
    d = start
    while d <= end:
        anchors.append(d)
        d = add_days(d, 3)  # step every 3 days to limit calls
    return anchors

def get_destinations():
    # Start with static list; optionally union with dynamic routes endpoint if provided
    dests = set(EU_STATIC)
    if SEATSAERO_ROUTES_URL:
        try:
            import urllib.request
            req = urllib.request.Request(SEATSAERO_ROUTES_URL, headers={
                "Partner-Authorization": f"Bearer {SEATSAERO_API_KEY}"
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            # Try common shapes: {routes:[{destination:"CDG"}]} or {airports:["CDG",...]}
            if isinstance(data, dict) and "routes" in data and isinstance(data["routes"], list):
                for r in data["routes"]:
                    dest = (r.get("destination") or r.get("to") or "").strip().upper()
                    if dest: dests.add(dest)
            elif isinstance(data, dict) and "airports" in data and isinstance(data["airports"], list):
                for dest in data["airports"]:
                    dests.add(str(dest).strip().upper())
        except Exception as e:
            # Fallback: static only
            pass
    # Exclude obvious non-EU if they sneak in
    non_eu = {"LHR","LGW","MAN","EDI","ZRH","GVA","OSL","BGO","KEF"}
    return sorted([d for d in dests if d not in non_eu])

def fetch_availability(destinations):
    """Calls the Seats.aero bulk availability endpoint in ranges.
       Expects SEATSAERO_AVAIL_URL to accept a JSON POST with standard fields."""
    import urllib.request
    headers = {
        "Partner-Authorization": f"Bearer {SEATSAERO_API_KEY}",
        "Content-Type": "application/json",
    }
    items = []

    anchors = build_date_anchors()
    for anchor in anchors:
        depart_from = iso(add_days(anchor, -FLEX_DAYS))
        depart_to   = iso(add_days(anchor,  FLEX_DAYS))
        for stay in (STAY_MIN, STAY_MAX):
            ret_from = iso(add_days(anchor, stay - FLEX_DAYS))
            ret_to   = iso(add_days(anchor, stay + FLEX_DAYS))
            payload = {
                "origin": ORIGIN,
                "destinations": destinations,
                "cabin": ["BUSINESS","FIRST"],
                "program": "QR",
                "operator": "QR",
                "exclude_mixed_cabin": True,
                "date_from": depart_from,
                "date_to": depart_to,
                "return_from": ret_from,
                "return_to": ret_to,
                "allow_open_jaw": True
            }
            req = urllib.request.Request(SEATSAERO_AVAIL_URL,
                                         data=json.dumps(payload).encode("utf-8"),
                                         headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                bucket = data.get("items") or data.get("results") or (data if isinstance(data, list) else [])
                for x in bucket:
                    items.append(normalize_item(x))
            except Exception as e:
                # soft-fail; keep scanning
                continue
            # be gentle
            time.sleep(0.25)
    return items

def normalize_item(x):
    # Adapt to common key names; API may use slightly different ones.
    dest   = (x.get("dest") or x.get("destination") or x.get("to") or x.get("arrival") or (x.get("route",{})).get("to") or "").upper()
    depart = x.get("depart") or x.get("departure") or x.get("outbound_date")
    ret    = x.get("ret") or x.get("return") or x.get("inbound_date")
    cabin  = str(x.get("cabin") or x.get("class") or "BUSINESS").upper()
    cabin  = "First" if "FIRST" in cabin else "Business"
    avios  = int(float(x.get("avios") or x.get("miles") or x.get("points") or 0))
    taxes  = float(x.get("taxes") or x.get("fees") or 0)
    avail  = bool(x.get("available") or x.get("availability") or (x.get("seats") or 0) > 0)
    wait   = bool(x.get("waitlist") or False)
    link   = x.get("link") or x.get("url") or "https://www.qatarairways.com/"
    openj  = bool(x.get("open_jaw") or x.get("openJaw") or True)
    return {
        "dest": dest, "depart": depart, "ret": ret, "cabin": cabin,
        "avios": avios, "taxes": taxes, "available": avail, "waitlist": wait,
        "openJaw": openj, "link": link
    }

def build_html(items):
    items = [i for i in items if i["dest"] and i["depart"] and i["ret"] and i["avios"] > 0]
    items.sort(key=lambda i: (i["avios"], i["taxes"]))
    summary = ("No Qatar-operated Avios Business/First availability found today within the 15-month window."
               if not items else
               f'{len(set(i["dest"] for i in items))} cities found. Best: {items[0]["dest"]} {items[0]["avios"]:,} Avios + NZ$ {round(items[0]["taxes"])}.')

    def row(i):
        color = "green" if i["avios"] <= AVIOS_SWEET_SPOT else ("orange" if i["avios"] < 100000 else "inherit")
        route = f'{ORIGIN} → DOH → {i["dest"]}{"<br><em>Open-jaw return</em>" if i["openJaw"] else ""}'
        dates = f'Depart {i["depart"]} • Return {i["ret"]}'
        taxes = f'NZ$ {round(i["taxes"])}'
        avail = "Yes" if i["available"] else ("Waitlist" if i["waitlist"] else "No")
        return f'''<tr>
          <td>{route}</td>
          <td>{dates}</td>
          <td>{i["cabin"]}</td>
          <td><span style="color:{color};font-weight:bold;">{i["avios"]:,}</span></td>
          <td>{taxes}</td>
          <td>{avail}</td>
          <td><a href="{i["link"]}" target="_blank" rel="noopener">Book/Check</a></td>
        </tr>'''

    rows = "\n".join(row(i) for i in items) or '<tr><td colspan="7" style="color:#666;">No results</td></tr>'

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Daily Qatar Avios EU Search – {RUN_DATE}</title></head>
<body style="font-family:Arial,Helvetica,sans-serif;">
  <h2>Daily Qatar Avios EU Search – {RUN_DATE}</h2>
  <p><strong>Summary:</strong> {summary}</p>
  <table border="1" cellpadding="6" cellspacing="0" width="100%">
    <thead><tr>
      <th>Route</th><th>Dates (Depart & Return)</th><th>Cabin</th><th>Avios</th><th>Taxes</th><th>Availability</th><th>Booking Link</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="margin-top:14px;">
    Legend: <span style="color:green;font-weight:bold;">GREEN ≤ 90,000 Avios</span>,
    <span style="color:orange;font-weight:bold;">AMBER &lt; 100,000</span>,
    <span style="color:#666;">GREY = none</span>
  </p>
</body></html>"""
    subject = f"Daily Qatar Avios EU Search – {RUN_DATE}"
    return subject, html

def send_email(subject, html):
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = TO_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(FROM_EMAIL, [TO_EMAIL], msg.as_string())

def main():
    # Destinations: static + optional dynamic
    dests = get_destinations()
    # Query Seats.aero availability
    if not SEATSAERO_API_KEY or not SEATSAERO_AVAIL_URL:
        raise RuntimeError("Seats.aero API key or availability URL not configured.")
    items = fetch_availability(dests)
    subject, html = build_html(items)
    send_email(subject, html)
    print("OK")

if __name__ == "__main__":
    main()

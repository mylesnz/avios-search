# ✈️ Avios Search — "I’m Off To..." Automation

Tired of checking Qatar Airways reward space every morning before your coffee?  
This little automation does it for you — quietly, reliably, and without judgment.

---

## 💡 What It Does

Every day, **Avios Search**:
- Talks to the **Seats.aero Partner API** to find Qatar Airways award seats  
- Focuses on **Business and First Class** routes from **Auckland (AKL)** to **Europe**  
- Filters out the boring stuff (no codeshares, no mixed cabins, no sky-tax nightmares)  
- Ranks results by *fewest Avios first*, *least cash second*  
- Builds a tidy HTML summary highlighting sweet spots (≤ 90 000 Avios = 💚)  
- Emails you the results via **Brevo REST API**

All automated by GitHub Actions — so while you sleep, your next Euro escape plan refreshes itself.

---

## 🧠 Architecture Overview

```
              ┌──────────────────────────────┐
              │  GitHub Actions (Scheduler)  │
              │  🕑 Runs Daily at 07:00 UTC   │
              └──────────────┬───────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │   seats_avios_daily.py       │
              │   🔍 Queries Seats.aero API   │
              │   ✨ Builds HTML Results      │
              └──────────────┬───────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │        Brevo REST / SMTP      │
              │        📧 Sends Daily Email    │
              └──────────────┬───────────────┘
                             │
                             ▼
                      📬 Inbox (milo@imoffto.xyz)
```

---

## 🧩 Tech Stack

| Layer | What It Does |
|-------|---------------|
| **Python Script** | Calls Seats.aero Partner API, filters Qatar-only results, formats HTML |
| **GitHub Actions** | Automates daily execution and manages secrets |
| **Brevo REST / SMTP** | Sends the daily summary email |
| **Seats.aero API** | Provides live Avios availability data |
| **Secrets Management** | Uses `gh secret set` for all keys and credentials |

---

## ⚙️ Setup

1. **Clone this repo**
   ```bash
   git clone https://github.com/mylesnz/avios-search.git
   cd avios-search
   ```

2. **Add your secrets**
   ```bash
   gh secret set SEATSAERO_API_KEY -b"pro_xxxxx"
   gh secret set SEATSAERO_AVAIL_URL -b"https://seats.aero/partnerapi/availability"
   gh secret set SEATSAERO_ROUTES_URL -b"https://seats.aero/partnerapi/routes"
   gh secret set BREVO_API_KEY -b"xkeysib-xxxxx"
   gh secret set FROM_EMAIL -b"alert@imoffto.xyz"
   gh secret set TO_EMAIL -b"milo@imoffto.xyz"
   gh secret set FROM_NAME -b"Avios Bot"
   ```

3. **Trigger a manual run**
   ```bash
   gh workflow run "Avios Daily" --ref main
   ```

4. **Check the logs**
   ```bash
   gh run view $(gh run list --workflow "Avios Daily" --limit 1 --json databaseId -q '.[0].databaseId') --log
   ```

---

## 🧪 Local Dry Run (No Emails)

You can safely test everything without sending an actual email.  
This will hit the Seats.aero API, generate the HTML, but skip Brevo delivery.

```bash
export SEATSAERO_API_KEY='pro_XXXX'
export SEATSAERO_AVAIL_URL='https://seats.aero/partnerapi/availability'
export SEATSAERO_ROUTES_URL='https://seats.aero/partnerapi/routes'
export DRY_RUN=1
python3 seats_avios_daily.py
```

Expected output:
```
🚀 Running Seats.aero → Brevo integration...
✅ Data fetched successfully
📭 DRY_RUN enabled — no email sent
🎉 Done.
```

---

## 🛫 Output Example

| Route | Dates | Cabin | Avios | Taxes | Availability | Link |
|:------|:------|:------|:------|:------|:-------------|:-----|
| AKL → DOH → LHR | 12 Nov 2025 → 10 Dec 2025 | J | 87 500 | NZD 450 | ✅ | [Book](#) |

If you see a table like that in your inbox — congratulations, automation is now your travel agent.

---

## ⚠️ Notes

- Seats.aero Partner API key **must** start with `pro_`
- Brevo REST API key **must** start with `xkeysib_`
- This script will **not** book your flights (yet)
- Newlines in API keys are evil — check with `od -t x1` if in doubt
- No responsibility for spontaneous Avios burn sessions at 2 a.m.

---

## 🔮 Future Integration (Optional Zapier Webhook)

You can extend this automation to send JSON payloads to Zapier, Slack, Notion, or Telegram:  

```json
{
  "subject": "Daily Qatar Avios EU Search – 2025-11-13",
  "html": "<full HTML email body>",
  "alert": false
}
```

If delivery fails, a follow-up alert payload can be triggered automatically — because travel data deserves retries too.

---

## 🍸 Credits

Built by **Myles Richardson**, who decided travel should start with automation, not frustration.  
If something breaks, it’s either a rate limit or a missing export. Possibly both.

---

> “Travel is the only thing you buy that makes you richer.  
> But automating it makes you *smarter*, too.” ☕️✈️

---

![Python](https://img.shields.io/badge/Made%20with-Python-3572A5?logo=python&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-Automated-success?logo=githubactions&logoColor=white)

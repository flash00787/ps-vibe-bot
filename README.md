# PS Vibe Bot System

Two Telegram bots for a PlayStation gaming café (Myanmar).

## Files

| File | Purpose |
|---|---|
| `main.py` | Staff Bot — daily sales, members, consoles, stock, KPI, finance |
| `customer_bot.py` | Customer Bot — booking, console status, waitlist, balance |
| `api_server/api_server.js` | Node.js REST API — Google Sheets bridge, receipts, bookings, finance |

## Stack

- Python 3.11 + `python-telegram-bot` v20 (async)
- Node.js 18+ + Express 5
- Google Sheets (primary database via `gspread` / `googleapis`)
- JSON flat-files for bookings & waitlist

## Required Secrets

```
BOT_TOKEN=
CUSTOMER_BOT_TOKEN=
SHEET_ID=
STAFF_NOTIFY_CHAT=
ADMIN_USER_IDS=
STOCK_PIN=
API_BASE_URL=https://ps-vibe.com
LOW_BALANCE_THRESHOLD=120
N8N_SESSION_WEBHOOK=
N8N_BOOKING_WEBHOOK=
```

> `.env` and `service_account.json` are excluded from version control.

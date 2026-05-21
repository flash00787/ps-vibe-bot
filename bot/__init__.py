import os
import re
import sys
import json
import time
import fcntl
import signal
import asyncio
import logging
from pathlib import Path
import gspread
from gspread.exceptions import APIError
import functools

# ─────────────────────────────────────────
#  GOOGLE SHEETS RETRY WRAPPER (Phase 2 - Data Safety)
#  Retries on 429/500/503 with exponential backoff
# ─────────────────────────────────────────
_SHEETS_RETRY_CODES = (429, 500, 503)
_SHEETS_MAX_RETRIES = 3
_SHEETS_BASE_DELAY  = 1  # seconds

def _sheets_retry(func):
    """Decorator: retry gspread calls on transient API errors (429/500/503)."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(_SHEETS_MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except APIError as e:
                code = e.response.status_code if hasattr(e, 'response') else 0
                if code in _SHEETS_RETRY_CODES and attempt < _SHEETS_MAX_RETRIES:
                    delay = _SHEETS_BASE_DELAY * (2 ** attempt)
                    logging.warning(
                        "Sheets API %d error (attempt %d/%d), retrying in %ds: %s",
                        code, attempt + 1, _SHEETS_MAX_RETRIES, delay,
                        str(e)[:100]
                    )
                    time.sleep(delay)
                    last_exc = e
                else:
                    raise
            except (ConnectionError, TimeoutError, OSError) as e:
                if attempt < _SHEETS_MAX_RETRIES:
                    delay = _SHEETS_BASE_DELAY * (2 ** attempt)
                    logging.warning(
                        "Sheets network error (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, _SHEETS_MAX_RETRIES, delay,
                        str(e)[:100]
                    )
                    time.sleep(delay)
                    last_exc = e
                else:
                    raise
        raise last_exc
    return wrapper
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timezone, timedelta

# Myanmar Time — GMT+6:30
MMT = timezone(timedelta(hours=6, minutes=30))
def now_mmt() -> datetime:
    return datetime.now(MMT)

BOT_VERSION = "2026.05.05-r1"   # Console double-booking conflict check (409 guard)
try:
    from keep_alive import keep_alive
except ImportError:
    keep_alive = None
from telegram import BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_file_handler  = logging.FileHandler("bot_status.log", encoding="utf-8")
_file_handler.setFormatter(_log_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_file_handler, _console_handler],
)

# ─────────────────────────────────────────
#  SHEET AUTH
# ─────────────────────────────────────────
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
creds       = ServiceAccountCredentials.from_json_keyfile_name("service_account.json", scope)
gc          = gspread.authorize(creds)
wb          = gc.open_by_key(os.environ["SHEET_ID"])
sales_sh    = wb.worksheet("Sales_Daily")
setting_sh  = wb.worksheet("Setting")
member_sh   = wb.worksheet("Card_Wallet")
stock_sh    = wb.worksheet("Stock_Out")
stock_in_sh = wb.worksheet("Stock_In")
topup_sh    = wb.worksheet("TopUp_Log")
inv_sh      = wb.worksheet("Inventory")

# ─────────────────────────────────────────
#  Apply retry wrapper to gspread Worksheet methods
# ─────────────────────────────────────────
_GSPREAD_METHODS_TO_WRAP = [
    'get_all_values', 'get_all_records', 'col_values', 'row_values',
    'append_row', 'append_rows', 'update', 'update_cell', 'update_cells',
    'delete_rows', 'delete_columns', 'get', 'batch_get', 'batch_update',
    'acell', 'cell', 'find', 'findall',
]

for _method_name in _GSPREAD_METHODS_TO_WRAP:
    _orig = getattr(gspread.Worksheet, _method_name, None)
    if _orig and not getattr(_orig, '_sheets_retry_wrapped', False):
        _wrapped = _sheets_retry(_orig)
        _wrapped._sheets_retry_wrapped = True
        setattr(gspread.Worksheet, _method_name, _wrapped)

# Also wrap Spreadsheet.add_worksheet
if hasattr(gspread.Spreadsheet, 'add_worksheet'):
    _orig_add = gspread.Spreadsheet.add_worksheet
    if not getattr(_orig_add, '_sheets_retry_wrapped', False):
        _wrapped_add = _sheets_retry(_orig_add)
        _wrapped_add._sheets_retry_wrapped = True
        gspread.Spreadsheet.add_worksheet = _wrapped_add

logging.info("Google Sheets retry wrapper applied (max %d retries, backoff 1s/2s/4s)", _SHEETS_MAX_RETRIES)

def get_att_sh():
    """Return (or create) the Attendance_Log worksheet."""
    try:
        return wb.worksheet("Attendance_Log")
    except Exception:
        sh = wb.add_worksheet("Attendance_Log", rows=200, cols=6)
        sh.update("A1:E1", [["Month", "Staff", "Leave_Days", "Late_Count", "Late_Deduct_Ks"]])
        return sh


def get_booking_sh():
    """Return (or create) the Console_Booking worksheet.
    Columns: A=BookingID, B=Date, C=ConsoleID, D=MemberID,
             E=StartTime, F=EndTime, G=Status, H=Staff, I=Notes
    """
    try:
        return wb.worksheet("Console_Booking")
    except Exception:
        sh = wb.add_worksheet("Console_Booking", rows=1000, cols=9)
        sh.update("A1:I1", [["BookingID", "Date", "ConsoleID", "MemberID",
                              "StartTime", "EndTime", "Status", "Staff", "Notes"]])
        return sh


def fetch_console_status() -> list[dict]:
    """Return list of console dicts with live status.
    Reads cached console_multipliers for H/I/J info; Console_Booking for live sessions.
    """
    today = today_str()
    # Use cached console_multipliers if available, fallback to direct Sheets read
    cfg = _get_cfg()
    cached_mults = cfg.get("console_multipliers", {})
    if cached_mults:
        names = list(cached_mults.keys())
        types = [""] * len(names)
        mults = [cached_mults[n] for n in names]
    else:
        names  = setting_sh.col_values(8)[1:]   # H
        types  = setting_sh.col_values(9)[1:]   # I (console type)
        mults  = setting_sh.col_values(10)[1:]  # J (multiplier)
    consoles = []
    for i, name in enumerate(names):
        if not name.strip():
            continue
        try:
            mult = float(str(mults[i] if i < len(mults) else "1").replace(",", "").strip()) or 1.0
        except (ValueError, IndexError):
            mult = 1.0
        ctype = (types[i] if i < len(types) else "").strip()
        consoles.append({"id": name.strip(), "type": ctype, "mult": mult,
                         "status": "Free", "member": None, "start": None, "staff": None, "booking_id": None})

    # Overlay active bookings — cached 30 s
    try:
        global _BK_ROWS, _BK_TS
        if not _BK_ROWS or (time.time() - _BK_TS) > _BK_TTL:
            _BK_ROWS = get_booking_sh().get_all_values()
            _BK_TS   = time.time()
        for row in _BK_ROWS[1:]:
            if len(row) < 7:
                continue
            bk_date   = row[1].strip()
            bk_cid    = row[2].strip()
            bk_status = row[6].strip()
            if bk_date == today and bk_status in ("Active", "Scheduled"):
                for c in consoles:
                    if c["id"] == bk_cid:
                        c["status"]     = bk_status
                        c["member"]     = row[3].strip() or "Guest"
                        c["start"]      = row[4].strip()
                        c["staff"]      = row[7].strip() if len(row) > 7 else ""
                        c["booking_id"] = row[0].strip()
                        break
    except Exception:
        pass
    return consoles


def create_booking(console_id: str, member_id: str, staff: str, notes: str = "") -> str:
    """Append a row to Console_Booking and return the BookingID."""
    sh     = get_booking_sh()
    now    = now_mmt()
    date   = now.strftime("%-m/%-d/%Y")
    time_s = now.strftime("%H:%M")
    seq    = now.strftime("%H%M")
    bk_id  = f"BK-{now.strftime('%Y%m%d')}-{console_id.replace(' ','').replace('-','')}-{seq}"
    sh.append_row([bk_id, date, console_id, member_id, time_s, "", "Active", staff, notes],
                  value_input_option="USER_ENTERED")
    return bk_id


def end_booking(booking_id: str) -> bool:
    """Mark a booking as Done and fill EndTime. Returns True if found."""
    try:
        sh   = get_booking_sh()
        rows = sh.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if row and row[0].strip() == booking_id:
                now = now_mmt()
                sh.update(f"F{i}", [[now.strftime("%H:%M")]])
                sh.update(f"G{i}", [["Done"]])
                return True
    except Exception:
        pass
    return False


def get_salary_adv_sh():
    """Return (or create) the Salary_Advance worksheet.
    Columns: A=Date, B=Staff, C=Amount, D=Payment (Cash/KPay), E=Note
    """
    try:
        return wb.worksheet("Salary_Advance")
    except Exception:
        sh = wb.add_worksheet("Salary_Advance", rows=500, cols=5)
        sh.update("A1:E1", [["Date", "Staff", "Amount", "Payment", "Note"]])
        return sh


def get_game_lib_sh():
    """Return (or create) the Game_Library worksheet.
    Columns: A=Title, B=Platform, C=Genre, D=Players, E=Status, F=Notes
    """
    try:
        return wb.worksheet("Game_Library")
    except Exception:
        sh = wb.add_worksheet("Game_Library", rows=500, cols=6)
        sh.update("A1:F1", [["Title", "Platform", "Genre", "Players", "Status", "Notes"]])
        return sh


def fetch_games() -> list[dict]:
    """Return all games from Game_Library sheet (cached 10 min).
    Sheet columns: A=No, B=Game Name, C=Final Status, D=Available Discs,
                   E=Total Copies, F=In Use, G-P=C-01..C-10, Q-S=SSD cols.
    Filters out garbage/metadata rows (empty title or non-game status).
    """
    try:
        global _GAME_ROWS, _GAME_TS
        if not _GAME_ROWS or (time.time() - _GAME_TS) > _GAME_TTL:
            _GAME_ROWS = get_game_lib_sh().get_all_values()
            _GAME_TS   = time.time()
        rows = _GAME_ROWS
        if len(rows) < 2:
            return []
        games = []
        for i, row in enumerate(rows[1:], start=2):
            if not row:
                continue
            title  = row[1].strip() if len(row) > 1 else ""
            status = row[2].strip() if len(row) > 2 else ""
            if not title:
                continue
            # Skip garbage/metadata rows: only keep rows with valid game status
            is_not_installed = status.lower() == "not installed"
            has_console      = "C -" in status or "c -" in status.lower()
            if not (is_not_installed or has_console):
                continue
            games.append({
                "row":    i,
                "title":  title,
                "status": status,
                "discs":  row[3].strip() if len(row) > 3 else "",
            })
        return games
    except Exception:
        return []


def set_game_disc_count(row_num: int, count: int) -> bool:
    """Update column D (Available Discs) for a game row in Game_Library. Returns True on success."""
    global _GAME_ROWS, _GAME_TS
    try:
        sh = get_game_lib_sh()
        sh.update_cell(row_num, 4, count)   # col D = index 4
        _GAME_ROWS = None                   # invalidate cache
        _GAME_TS   = 0
        return True
    except Exception:
        return False


def get_console_games_sh():
    """Return (or create) the Console_Games worksheet.
    Columns: A=Console_ID, B=Game_Title, C=Install_Type, D=Date, E=Notes
    """
    try:
        return wb.worksheet("Console_Games")
    except Exception:
        sh = wb.add_worksheet("Console_Games", rows=1000, cols=5)
        sh.update("A1:E1", [["Console_ID", "Game_Title", "Install_Type", "Date", "Notes"]])
        return sh


def fetch_console_games() -> list[dict]:
    """Return all console-game installation records (cached 5 min)."""
    try:
        global _CGAME_ROWS, _CGAME_TS
        if not _CGAME_ROWS or (time.time() - _CGAME_TS) > _CGAME_TTL:
            _CGAME_ROWS = get_console_games_sh().get_all_values()
            _CGAME_TS   = time.time()
        rows = _CGAME_ROWS
        if len(rows) < 2:
            return []
        return [
            {
                "row":          i,
                "console_id":   row[0].strip() if len(row) > 0 else "",
                "game_title":   row[1].strip() if len(row) > 1 else "",
                "install_type": row[2].strip() if len(row) > 2 else "",
                "date":         row[3].strip() if len(row) > 3 else "",
                "notes":        row[4].strip() if len(row) > 4 else "",
            }
            for i, row in enumerate(rows[1:], start=2)
            if row and row[0].strip()
        ]
    except Exception:
        return []


def get_games_on_console(console_id: str) -> list[str]:
    """Return list of game titles installed on a specific console."""
    return [
        r["game_title"] for r in fetch_console_games()
        if r["console_id"].upper() == console_id.upper() and r["game_title"]
    ]


def get_consoles_with_game(game_title: str) -> list[str]:
    """Return list of console IDs that have a specific game installed."""
    gl = game_title.strip().lower()
    return [
        r["console_id"] for r in fetch_console_games()
        if r["game_title"].strip().lower() == gl
    ]


def add_console_game(console_id: str, game_title: str, install_type: str, notes: str = "") -> bool:
    """Add a game installation record. Returns True on success."""
    global _CGAME_ROWS
    try:
        sh   = get_console_games_sh()
        date = now_mmt().strftime("%-m/%-d/%Y")
        sh.append_row([console_id, game_title, install_type, date, notes],
                      value_input_option="USER_ENTERED")
        _CGAME_ROWS = None   # invalidate cache so next fetch reads fresh data
        return True
    except Exception:
        return False


def remove_console_game(console_id: str, game_title: str) -> bool:
    """Remove a game installation record. Returns True if found and removed."""
    global _CGAME_ROWS
    try:
        sh   = get_console_games_sh()
        rows = sh.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if (len(row) >= 2
                    and row[0].strip().upper() == console_id.upper()
                    and row[1].strip().lower() == game_title.strip().lower()):
                sh.delete_rows(i)
                _CGAME_ROWS = None   # invalidate cache
                return True
    except Exception:
        pass
    return False


def _norm_cid(cid: str) -> str:
    """Normalise console ID for comparison: remove spaces, uppercase. 'C - 01' → 'C-01'."""
    return cid.replace(" ", "").upper()


def update_game_library_install(game_title: str, console_id: str, installed: bool) -> bool:
    """Set TRUE/FALSE in Game_Library for (game_title, console_id) intersection.
    Column B = Game Name; columns G:S = console headers (C-01 … SD2).
    Returns True on success.
    """
    try:
        sh   = wb.worksheet("Game_Library")
        rows = sh.get_all_values()
        if not rows:
            return False

        header_row = rows[0]  # row 1

        # Find console column index (G onwards = index 6)
        cid_norm = _norm_cid(console_id)
        col_idx  = None
        for i, h in enumerate(header_row):
            if _norm_cid(h) == cid_norm:
                col_idx = i
                break
        if col_idx is None:
            return False  # console column not found in sheet

        # Find game row (col B = index 1 = "Game Name")
        game_lower = game_title.strip().lower()
        row_idx    = None
        for i, row in enumerate(rows[1:], start=2):
            cell_val = row[1].strip().lower() if len(row) > 1 else ""
            if cell_val == game_lower:
                row_idx = i
                break
        if row_idx is None:
            return False  # game not found

        # Convert col_idx to A1 column letter(s)
        col_letter = ""
        n = col_idx + 1  # 1-indexed
        while n > 0:
            n, r = divmod(n - 1, 26)
            col_letter = chr(65 + r) + col_letter
        cell_addr = f"{col_letter}{row_idx}"

        sh.update(cell_addr, [[True if installed else ""]])
        return True
    except Exception:
        return False


def calc_duration(start_time_str: str) -> tuple[int, str]:
    """Calculate elapsed minutes from HH:MM start string. Returns (minutes, 'Xh Ym')."""
    try:
        from datetime import timedelta
        now   = now_mmt()
        h, m  = map(int, start_time_str.strip().split(":"))
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if start > now:
            start -= timedelta(days=1)
        total_mins = int((now - start).total_seconds() // 60)
        hrs  = total_mins // 60
        mins = total_mins % 60
        fmt  = (f"{hrs}h {mins}m" if hrs > 0 else f"{mins}m")
        return total_mins, fmt
    except Exception:
        return 0, "?"


def cancel_booking(booking_id: str) -> bool:
    """Mark a booking as Cancelled. Returns True if found."""
    try:
        sh   = get_booking_sh()
        rows = sh.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if row and row[0].strip() == booking_id:
                sh.update(f"G{i}", [["Cancelled"]])
                return True
    except Exception:
        pass
    return False


def add_console_to_setting(console_id: str, ctype: str, multiplier: float) -> bool:
    """Append a new console to Setting!H:J. Returns True on success."""
    try:
        names    = setting_sh.col_values(8)      # includes header row
        next_row = len(names) + 1
        setting_sh.update(f"H{next_row}:J{next_row}",
                          [[console_id, ctype, str(multiplier)]],
                          value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        logging.error("add_console_to_setting error: %s", e)
        return False


def remove_console_from_setting(console_id: str) -> bool:
    """Clear a console row from Setting!H:J. Returns True if found."""
    try:
        names = setting_sh.col_values(8)
        for i, name in enumerate(names):
            if name.strip() == console_id.strip():
                row = i + 1
                setting_sh.update(f"H{row}:J{row}", [["", "", ""]])
                return True
    except Exception:
        pass
    return False


def get_consoles_from_setting() -> list[dict]:
    """Return all consoles from Setting!H:J as list of dicts."""
    try:
        names = setting_sh.col_values(8)[1:]
        types = setting_sh.col_values(9)[1:]
        mults = setting_sh.col_values(10)[1:]
        result = []
        for i, name in enumerate(names):
            if not name.strip():
                continue
            result.append({
                "id":   name.strip(),
                "type": types[i].strip() if i < len(types) else "",
                "mult": mults[i].strip() if i < len(mults) else "1",
            })
        return result
    except Exception:
        return []

# ─────────────────────────────────────────
#  STATES
# ─────────────────────────────────────────
(
    MAIN_MENU,
    # ── Daily Sales flow ──
    MEMBER, CONSOLE, MINS, FOOD_MENU, FOOD_QTY, CONFIRM_SUMMARY, DISCOUNT, KPAY_AMT, SALE_CONFIRM,
    # ── Member Management menu ──
    MM_MENU,
    # ── First Purchase (New Member) flow ──
    NM_NAME, NM_PHONE, NM_EMAIL, NM_ID, NM_AMT, NM_KPAY, NM_CONFIRM, NM_GIFT_PIN,
    # ── Top Up (Existing Member) flow ──
    TU_MEMBER, TU_AMT, TU_KPAY, TU_CONFIRM,
    # ── Check Member lookup ──
    MM_LOOKUP,
    # ── Stock Update PIN entry ──
    STOCK_PIN,
    # ── Stock Update sub-menu ──
    STOCK_MENU,
    # ── Stock Out flow ──
    STOCK_ITEM, STOCK_QTY,
    # ── Stock In (Restock) flow ──
    SI_ITEM, SI_QTY, SI_COST, SI_CART, SI_PAY, SI_CONFIRM,
    # ── Staff selection ──
    STAFF_SELECT,
    # ── New Member staff selection ──
    NM_STAFF,
    # ── Attendance (Leave/Late) wizard ──
    ATTEND_STAFF, ATTEND_LEAVE, ATTEND_LATE, ATTEND_DEDUCT,
    # ── Admin Panel ──
    ADMIN_PIN, ADMIN_MENU,
    # ── Stock In payment split ──
    SI_PAY_SPLIT,
    # ── Salary Advance flow ──
    SAL_ADV_STAFF, SAL_ADV_AMT, SAL_ADV_PAY, SAL_ADV_CONFIRM,
    # ── Console Booking flow ──
    BOOK_CONSOLE, BOOK_MEMBER,
    # ── Console Management submenu ──
    CONSOLE_MENU,
    # ── End Session flow ──
    END_SESSION_SELECT,
    # ── Game Library flows ──
    GAME_MENU, GAME_ADD_TITLE, GAME_ADD_PLATFORM, GAME_ADD_GENRE, GAME_ADD_STATUS, GAME_DEL_SELECT,
    # ── Console CRUD flows ──
    CON_MGMT_MENU, CON_ADD_ID, CON_ADD_TYPE, CON_ADD_MULT, CON_DEL_SELECT,
    # ── Session → Daily Sales bridge ──
    SESSION_SHORTFALL,
    # ── Daily Sales in-session conflict checks ──
    DS_MEMBER_IN_SESSION, DS_CONSOLE_IN_SESSION,
    # ── Booking: member already in session warning ──
    BOOK_DUP_WARN,
    # ── Booking: game selection ──
    BOOK_GAME,
    # ── Booking: planned play duration (for timer) ──
    BOOK_MINS,
    # ── Game change for active session ──
    GAME_CHANGE_CONS, GAME_CHANGE_GAME,
    # ── Staff Advance Booking flow ──
    SBK_CONSOLE, SBK_CUST_NAME, SBK_DATE, SBK_TIME, SBK_DUR, SBK_GAME, SBK_CONFIRM,
    # ── Console-Game Install tracking ──
    GINST_MENU, GINST_VIEW_CONS, GINST_ADD_CONS, GINST_ADD_GAME, GINST_ADD_TYPE,
    GINST_DEL_CONS, GINST_DEL_GAME,
    # ── External SSD Management ──
    SSD_MENU, SSD_VIEW_SSD, SSD_ADD_SSD, SSD_ADD_GAME, SSD_ADD_TYPE,
    SSD_DEL_SSD, SSD_DEL_GAME,
    SSD_XFER_SSD, SSD_XFER_GAME, SSD_XFER_CONS,
    SSD_RET_CONS, SSD_RET_GAME,
    # ── Game Discs Record ──
    DISC_SELECT, DISC_SET_QTY,
    # ── Finance module ──
    FINANCE_MENU,
    OPEX_CAT, OPEX_DESC, OPEX_AMT, OPEX_ACCT, OPEX_PAY, OPEX_CONFIRM,
    ASSET_NAME, ASSET_CAT, ASSET_DATE, ASSET_COST, ASSET_QTY, ASSET_LIFE, ASSET_SALVAGE, ASSET_PAY, ASSET_CONFIRM,
    ASSET_DISPOSE_SEL, ASSET_DISPOSE_DATE, ASSET_DISPOSE_QTY, ASSET_DISPOSE_PROCEEDS, ASSET_DISPOSE_CONFIRM,
    PREPAID_DESC, PREPAID_CAT, PREPAID_AMT, PREPAID_ACCT, PREPAID_START, PREPAID_END, PREPAID_CONFIRM,
    ACCT_TRF_FROM, ACCT_TRF_TO, ACCT_TRF_AMT, ACCT_TRF_NOTE, ACCT_TRF_CONFIRM,
    PAY_VENDOR, PAY_DESC, PAY_AMT, PAY_DUE, PAY_ACCT, PAY_CONFIRM,
    REC_CUST, REC_DESC, REC_AMT, REC_DUE, REC_ACCT, REC_CONFIRM,
    FIN_REPORT_MENU,
    # ── Initial Capital flow ──
    CAP_ACCT, CAP_AMT, CAP_CONFIRM,
    # ── Shareholders flow ──
    SHARE_NAME, SHARE_ROLE, SHARE_CAP, SHARE_OWN, SHARE_CONFIRM,
    # ── Settle flows ──
    PAY_SETTLE_LIST, PAY_SETTLE_ACCT, PAY_SETTLE_CONFIRM,
    REC_SETTLE_LIST, REC_SETTLE_ACCT, REC_SETTLE_CONFIRM,
    # ── Advance Payment flow ──
    ADVPAY_PARTY, ADVPAY_DESC, ADVPAY_AMT, ADVPAY_ACCT,
    ADVPAY_DUE, ADVPAY_NOTE, ADVPAY_CONFIRM,
    ADVPAY_LIST, ADVPAY_SETTLE_CONFIRM,
) = range(167)

# ─────────────────────────────────────────
#  BUTTON LABELS
# ─────────────────────────────────────────
BTN_BACK         = "⬅️ ပြန်သွား"
BTN_BACK_MAIN    = "⬅️ Main Menu သို့ပြန်"
BTN_DONE         = "Done ✅"
BTN_YES          = "Yes ✅"
BTN_SAVE         = "သိမ်းမည် ✅"
BTN_NEW_SALE     = "📝 New Sale"
BTN_CANCEL       = "❌ Cancel"
BTN_CONFIRM_SAVE = "✅ Confirm & Save"

NAV_ROW = [BTN_BACK, BTN_CANCEL]   # appended to every wizard keyboard

VALID_CONSOLES = {
    "C - 01", "C - 02", "C - 03", "C - 04", "C - 05",
    "C - 06", "C - 07", "C - 08", "C - 09", "C - 10",
}

# Main menu
BTN_DAILY_SALES  = "📝 Daily Sales"
BTN_MEMBER_MGMT  = "💳 Member Management"
BTN_TODAY_REPORT = "📊 Today's Report"
BTN_STOCK_UPDATE = "📦 Stock Update"
BTN_STAFF_KPI          = "📈 Staff KPI"
BTN_PAYROLL            = "💰 Payroll"
BTN_FINANCIAL_REPORT   = "💹 Financial Report"
BTN_ADMIN              = "🔧 Admin Panel"
BTN_ADMIN_ATTEND  = "📅 Attendance"
BTN_ADMIN_PNL     = "📊 Monthly P&L"
BTN_ADMIN_CF      = "💵 Cash Flow"
BTN_ADMIN_LIB     = "💳 Card Liability"
BTN_ADMIN_BOOK    = "📋 Pending Bookings"
BTN_ADMIN_SAL_ADV = "💸 Salary Advance"
BTN_CONSOLE_STATUS = "🕹️ Console Status"
BTN_CONSOLE_BOOK   = "📋 New Booking"
# Console Management submenu
BTN_CONSOLES        = "🕹️ Consoles"
BTN_START_SESSION   = "▶️ Session စတင်"
BTN_END_SESSION     = "⏹️ Session ဆုံး"
BTN_STATUS_BOARD    = "📊 Status ကြည့်"
BTN_GAME_LIB_MENU   = "🎮 Game Library"
BTN_CON_MANAGE      = "⚙️ Console စီမံ"
# Game Library
BTN_ADD_GAME        = "➕ ဂိမ်းထည့်"
BTN_VIEW_GAMES      = "📋 ဂိမ်းစာရင်း"
BTN_DEL_GAME        = "🗑️ ဂိမ်းဖျက်"
# Console CRUD
BTN_ADD_CONSOLE     = "➕ Console ထည့်"
BTN_LIST_CONSOLE    = "📋 Console စာရင်း"
BTN_DEL_CONSOLE     = "🗑️ Console ဖျက်"
# Confirm/End
BTN_YES_END         = "✅ Yes — ဆုံးမည်"
BTN_NO_BACK         = "❌ No — ပြန်"
BTN_SI_SPLIT     = "💰 ခွဲပေး (Cash + KPay)"
BTN_STOCK_OUT        = "📦 Stock Out (ထုတ်ယူ)"
BTN_STOCK_IN_M       = "📥 Stock In (ဝယ်ယူ)"
BTN_INVENTORY_VIEW   = "📊 Inventory ကြည့်ရှု"
BTN_SKIP_DISC        = "⏩ Skip (Discount မထည့်)"
# Session → Daily Sales bridge
BTN_CASH_DOWN        = "💵 Cash Down (ချက်ချင်းပေး)"
BTN_TOPUP_SESSION    = "💳 Top Up ပြီး ဆက်"
BTN_SKIP_SALES       = "⏭ Skip (မမှတ်တမ်းတင်)"
BTN_YES_END_SESSION  = "✅ Session ကို End မည်"
BTN_NO_RESELECT      = "❌ ပြန်ရွေး"
BTN_BOOK_PROCEED     = "⚠️ ဒါပဲ ဆက်ဖွင့်မည်"
BTN_SKIP_TIMER       = "⏭ Skip (Timer မလိုပါ)"
BTN_STAFF_BOOK       = "📅 Customer Booking"
BTN_CANCEL_BOOKING   = "🚫 Cancel Booking"
BTN_SBK_TODAY        = "📅 ယနေ့"
BTN_SBK_TOMORROW     = "📅 မနက်ဖြန်"
BTN_SBK_CUSTOM       = "✏️ ရက်ထည့်"
BTN_SBK_SKIP_PHONE   = "⏭ Phone မထည့်"
BTN_SBK_SKIP_GAME    = "⏭ Game မထည့်"
BTN_SBK_CONFIRM_BOOK = "✅ Booking ဖန်တီးမည်"
BTN_SBK_NEW          = "➕ New Booking"
BTN_SBK_CONFIRMED    = "✅ Confirmed Bookings"
BTN_CONSOLE_INSTALL  = "🖥️ Console Install"
BTN_GINST_VIEW       = "📋 ဘယ် Console မှာ ဘာ ရှိသလဲ"
BTN_GINST_ADD        = "➕ Install မှတ်သား"
BTN_GINST_REMOVE     = "❌ Install ဖျက်"
BTN_GINST_HDD        = "💾 HDD (Internal)"
BTN_GINST_DISC       = "💿 Disc"
BTN_GINST_SSD        = "🔌 Portable SSD"
# External SSD Management
BTN_SKIP_GAME    = "⏭ ဂိမ်း မထည့်"
BTN_CHANGE_GAME  = "🔄 Game ပြောင်း"
BTN_SSD_MANAGE   = "📀 External SSD"
BTN_SSD_VIEW     = "📋 SSD ထဲ ဘာ ရှိသလဲ"
BTN_SSD_ADD      = "➕ SSD ထဲ ဂိမ်း ထည့်"
BTN_SSD_REMOVE   = "❌ SSD မှ ဂိမ်း ဖျက်"
BTN_SSD_TRANSFER = "🔄 SSD → Console (Transfer)"
BTN_SSD_RETURN   = "↩️ Console → SSD (Return)"
BTN_SSD_T1       = "Samsung T1 Shield"
BTN_SSD_BLUE     = "Sandisk Extreme (Blue)"
BTN_SSD_GREY     = "Sandisk Extreme (Grey)"
# Game Discs Record
BTN_DISC_RECORD  = "💿 Game Discs"

# ── Finance module buttons ──
BTN_FINANCE          = "💼 Finance"
BTN_FIN_OPEX         = "📝 OPEX"
BTN_FIN_ASSET        = "🏢 Asset"
BTN_FIN_PREPAID      = "📅 Prepaid"
BTN_FIN_TRANSFER     = "💸 Transfer"
BTN_FIN_PAYABLE      = "📤 Payable"
BTN_FIN_RECEIVABLE   = "📥 Receivable"
BTN_FIN_REPORT       = "📊 Reports"
BTN_FIN_SETUP        = "⚙️ Sheet Setup"
BTN_FIN_PNL          = "📊 P&L Report"
BTN_FIN_BS           = "🏦 Balance Sheet"
BTN_FIN_ACCTS        = "💰 Accounts"
BTN_FIN_DEPR         = "📉 Depreciation"
BTN_FIN_ASSET_DISPOSE = "🔄 Dispose Asset"
BTN_FIN_PROFIT_SHARE = "💸 Profit Sharing"
BTN_FIN_CAPITAL      = "🏦 Capital"
BTN_FIN_SHAREHOLDER  = "👥 Partners"
BTN_FIN_SETTLE_PAY   = "✅ Settle Pay"
BTN_FIN_SETTLE_REC   = "✅ Settle Rec"
BTN_FIN_ADVPAY       = "💵 Advance"
BTN_FIN_SETTLE_ADVPAY= "✅ Settle Adv"
BTN_FIN_BACK         = "⬅️ Finance Menu"

fetch_game_library  = fetch_games            # alias used in SSD management
write_console_game  = add_console_game       # alias used in SSD management
delete_console_game = remove_console_game    # alias used in SSD management


def _delete_session_game(console_id: str) -> None:
    """Remove any 'Session' type entry for a console from Console_Games."""
    try:
        sh   = get_console_games_sh()
        rows = sh.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if (len(row) >= 3
                    and row[0].strip().upper() == console_id.strip().upper()
                    and row[2].strip() == "Session"):
                sh.delete_rows(i)
                # Invalidate cache
                global _CGAME_ROWS, _CGAME_TS
                _CGAME_TS = 0
                return
    except Exception:
        pass

SSD_NAMES: dict[str, str] = {
    "SSD-T1":   "Samsung T1 Shield",
    "SSD-Blue": "Sandisk Extreme (Blue)",
    "SSD-Grey": "Sandisk Extreme (Grey)",
}
SSD_BTN_TO_ID: dict[str, str] = {v: k for k, v in SSD_NAMES.items()}

STOCK_ACCESS_PIN    = os.environ.get("STOCK_PIN", "1234")
CUSTOMER_BOT_TOKEN  = os.environ.get("CUSTOMER_BOT_TOKEN", "")
STAFF_NOTIFY_CHAT   = os.environ.get("STAFF_NOTIFY_CHAT", "")   # group chat ID for booking notifications
# Comma-separated Telegram user IDs allowed to use /broadcast (e.g. "12345678,87654321")
_BROADCAST_ADMIN_IDS: set[str] = {
    s.strip() for s in os.environ.get("ADMIN_USER_IDS", "").split(",") if s.strip()
}

# n8n Phase 2 — Session reminder webhook (restart-proof timer)
# Test URL  : https://psvibe.app.n8n.cloud/webhook-test/session-reminder
# Production : https://psvibe.app.n8n.cloud/webhook/session-reminder
N8N_SESSION_WEBHOOK  = os.environ.get("N8N_SESSION_WEBHOOK", "")
N8N_BOOKING_WEBHOOK  = os.environ.get("N8N_BOOKING_WEBHOOK", "")

# Member Management sub-menu
BTN_FIRST_PURCHASE = "🆕 New Member"
BTN_TOP_UP         = "💰 Top Up"
BTN_CHECK_MEMBER   = "🔍 Check Member"
BTN_VIEW_RANKS     = "📋 Rank Bonuses"
BTN_CONFIRM_ID     = "✅ Confirm ID"
BTN_NM_CUSTOM      = "✏️ Enter Different Amount"
BTN_NM_GIFT        = "🎁 Gift / Free Card"
BTN_SKIP_PHONE     = "⏩ Skip"
BTN_SKIP_EMAIL     = "⏩ Email မထည့်"
BTN_CLEAR_CART     = "🗑️ Clear Cart"
BTN_SI_ADD         = "➕ Item ထပ်ထည့်"
BTN_SI_FINISH      = "💳 Payment & Save All"


# ═════════════════════════════════════════
#  SHEET HELPERS
# ═════════════════════════════════════════

def _int(val):
    """Safe int from sheet value — strips commas, 'Ks', spaces, handles floats."""
    try:
        cleaned = str(val).replace(",", "").replace("Ks", "").strip()
        return int(float(cleaned))
    except (ValueError, TypeError):
        return 0


def next_voucher():
    col = sales_sh.col_values(2)
    ids = [v for v in col[1:] if v.upper().startswith("V-")]
    if ids:
        try:
            return f"V-{int(ids[-1].split('-')[1]) + 1:03d}"
        except (IndexError, ValueError):
            pass
    return "V-001"


def fetch_members():
    raw = member_sh.col_values(2)[1:]
    return [m.strip() for m in raw if m.strip()]


def fetch_attendance(month_str: str) -> dict[str, dict]:
    """Read Attendance_Log for given month. Returns {staff: {leave, late, deduct_per_late}}."""
    result: dict[str, dict] = {}
    try:
        rows = get_att_sh().get_all_values()
        for row in rows[1:]:
            if len(row) < 4:
                continue
            if row[0].strip() != month_str:
                continue
            staff = row[1].strip()
            if not staff:
                continue
            result[staff] = {
                "leave_days":     int(row[2].strip() or 0) if len(row) > 2 else 0,
                "late_count":     int(row[3].strip() or 0) if len(row) > 3 else 0,
                "deduct_per_late": int(row[4].strip() or 500) if len(row) > 4 and row[4].strip() else 500,
            }
    except Exception as e:
        logging.warning("fetch_attendance: %s", e)
    return result


def save_attendance(month_str: str, staff: str, leave_days: int, late_count: int, deduct_per_late: int):
    """Insert or update row in Attendance_Log for given month+staff."""
    try:
        sh   = get_att_sh()
        rows = sh.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if row[0].strip() == month_str and row[1].strip() == staff:
                sh.update(f"A{i}:E{i}", [[month_str, staff, leave_days, late_count, deduct_per_late]])
                return
        sh.append_row([month_str, staff, leave_days, late_count, deduct_per_late])
    except Exception as e:
        logging.warning("save_attendance: %s", e)


def fetch_staff() -> list[str]:
    """Read staff names from Setting!S2:S10 (col 19)."""
    try:
        vals = setting_sh.col_values(19)[1:]  # col S = index 19 (1-based)
        return [v.strip() for v in vals if v.strip()]
    except Exception:
        return ["Staff A", "Staff B"]


def fetch_base_salaries() -> dict[str, int]:
    """Read base salaries from Setting!T2:T10 (col 20). Returns {staff_name: salary}."""
    try:
        staff   = setting_sh.col_values(19)[1:]   # S = staff names
        salaries = setting_sh.col_values(20)[1:]  # T = base salaries
        result: dict[str, int] = {}
        for i, name in enumerate(staff):
            name = name.strip()
            if not name:
                continue
            sal_str = salaries[i].strip() if i < len(salaries) else "0"
            result[name] = int(sal_str.replace(",", "")) if sal_str.replace(",", "").isdigit() else 0
        return result
    except Exception:
        return {}


def ensure_sheet_headers():
    """Write column headers for new staff-tracking columns (idempotent)."""
    try:
        if not sales_sh.cell(1, 15).value:
            sales_sh.update_cell(1, 15, "Staff")
        if not member_sh.cell(1, 11).value:
            member_sh.update_cell(1, 11, "Reg_Staff")
        if not topup_sh.cell(1, 10).value:
            topup_sh.update_cell(1, 10, "Staff")
        if not setting_sh.cell(1, 19).value:
            setting_sh.update_cell(1, 19, "Staff Names")
        if not setting_sh.cell(1, 20).value:
            setting_sh.update_cell(1, 20, "Base Salary")
        existing = [v.strip() for v in setting_sh.col_values(19)[1:] if v.strip()]
        if not existing:
            setting_sh.update("S2:T3", [["Staff A", "0"], ["Staff B", "0"]])
    except Exception as e:
        logging.warning("ensure_sheet_headers: %s", e)


# ─────────────────────────────────────────
#  BOT-LEVEL CONFIG + MEMBER CACHE
#  Eliminates ~8 Sheets API calls per user interaction.
#  Config refreshes every 5 min; member rows every 2 min.
# ─────────────────────────────────────────
_CFG:      dict  = {}
_CFG_TS:   float = 0.0
_CFG_TTL   = 300   # 5 minutes

_MBR_ROWS: list  = []
_MBR_TS:   float = 0.0
_MBR_TTL   = 180   # 3 minutes

# ── Console_Booking rows (live session overlay) ───────────────────────────────
_BK_ROWS:    list  = []
_BK_TS:      float = 0.0
_BK_TTL      = 30          # 30 s  — active sessions change frequently

# ── Game_Library rows ─────────────────────────────────────────────────────────
_GAME_ROWS:  list  = []
_GAME_TS:    float = 0.0
_GAME_TTL    = 600         # 10 min

# ── Console_Games rows ───────────────────────────────────────────────────────
_CGAME_ROWS: list  = []
_CGAME_TS:   float = 0.0
_CGAME_TTL   = 300         # 5 min


def _cfg_fresh() -> bool:
    return bool(_CFG) and (time.time() - _CFG_TS) < _CFG_TTL


def _replit_get(path: str, timeout: int = 30):
    """GET JSON from API server. Returns parsed dict or None on error."""
    base = _api_base()
    if not base:
        return None
    try:
        import urllib.request as _req
        _rg_req = _req.Request(f"{base}/api/{path}", headers={"X-API-Key": _API_KEY})
        with _req.urlopen(_rg_req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logging.warning("API GET /%s failed: %s", path, e)
        return None


def _load_cfg() -> None:
    global _CFG, _CFG_TS
    data = _replit_get("sheets/config")
    if data and "base_rate" in data:
        _CFG    = data
        _CFG_TS = time.time()
        logging.info("Config cache refreshed (base_rate=%s)", data.get("base_rate"))


def _get_cfg() -> dict:
    if not _cfg_fresh():
        _load_cfg()
    return _CFG


def _mbr_fresh() -> bool:
    return bool(_MBR_ROWS) and (time.time() - _MBR_TS) < _MBR_TTL


def _load_members() -> None:
    global _MBR_ROWS, _MBR_TS
    try:
        _MBR_ROWS = member_sh.get_all_values()
        _MBR_TS   = time.time()
    except Exception as e:
        logging.warning("Member cache refresh failed: %s", e)


def _get_member_rows() -> list:
    if not _mbr_fresh():
        _load_members()
    return _MBR_ROWS


async def _bg_cache_refresh() -> None:
    """Background asyncio task — refresh config + member cache every 5 min."""
    while True:
        await asyncio.sleep(300)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _load_cfg)
            await loop.run_in_executor(None, _load_members)
            logging.info("Background cache refresh done")
        except Exception as e:
            logging.warning("Background cache refresh error: %s", e)


def fetch_wallet_mins(member_id):
    for row in _get_member_rows()[1:]:
        if len(row) > 1 and row[1].strip() == member_id.strip():
            return _int(row[7]) if len(row) > 7 and row[7].strip() else None
    return None


def fetch_base_rate():
    cfg = _get_cfg()
    if cfg.get("base_rate"):
        return cfg["base_rate"]
    return _int(setting_sh.cell(2, 2).value)


def fetch_new_member_defaults():
    """Return (card_price, base_mins) from Setting!B20 and Setting!B21."""
    cfg = _get_cfg()
    if cfg.get("new_member_card_price") is not None:
        return cfg["new_member_card_price"], cfg.get("new_member_base_mins", 0)
    try:
        price = _int(setting_sh.cell(20, 2).value)
        mins  = _int(setting_sh.cell(21, 2).value)
        return price, mins
    except Exception:
        return 0, 0


def fetch_food_prices():
    cfg = _get_cfg()
    if cfg.get("food_prices"):
        return dict(cfg["food_prices"])
    names  = setting_sh.col_values(4)[1:]
    prices = setting_sh.col_values(5)[1:]
    return {n.strip(): _int(p) for n, p in zip(names, prices) if n and p}


def fetch_food_costs():
    cfg = _get_cfg()
    if cfg.get("food_costs"):
        return dict(cfg["food_costs"])
    names = setting_sh.col_values(4)[1:]
    costs = setting_sh.col_values(6)[1:]
    return {n.strip(): (_int(c) if str(c).strip() else 0) for n, c in zip(names, costs) if n.strip()}


def fetch_console_multiplier(console_id):
    cfg = _get_cfg()
    mults = cfg.get("console_multipliers", {})
    if mults:
        return float(mults.get(console_id.strip(), 1.0)) or 1.0
    try:
        console_names = setting_sh.col_values(8)[1:]
        multipliers   = setting_sh.col_values(10)[1:]
        for name, mult in zip(console_names, multipliers):
            if name.strip() == console_id.strip():
                val = float(str(mult).replace(",", "").strip())
                return val if val > 0 else 1.0
    except Exception:
        pass
    return 1.0


def fetch_rank_thresholds():
    cfg = _get_cfg()
    if cfg.get("master_threshold") is not None:
        return cfg["master_threshold"], cfg.get("immortal_threshold", 0)
    try:
        master   = _int(setting_sh.cell(3, 13).value)
        immortal = _int(setting_sh.cell(4, 13).value)
        return master, immortal
    except Exception:
        return 0, 0


def fetch_member_total_spend(member_id):
    """Return member's ranking net spend (Col F) — uses cached member rows."""
    try:
        for row in _get_member_rows()[1:]:
            if len(row) > 1 and row[1].strip() == member_id.strip():
                return _int(row[5]) if len(row) > 5 and row[5].strip() else 0
    except Exception:
        pass
    return 0


def fetch_member_phone(member_id):
    """Return phone (Col D) — uses cached member rows."""
    try:
        for row in _get_member_rows()[1:]:
            if len(row) > 1 and row[1].strip() == member_id.strip():
                return row[3].strip() if len(row) > 3 and row[3].strip() else "-"
    except Exception:
        pass
    return "-"


def fetch_member_data(member_id):
    """Single Card_Wallet read (cached) returning all commonly-needed member fields.
    Card_Wallet columns: A=row_no, B=member_id, C=name, D=phone,
    E=lifetime_spend, F=ranking_net_spend, G=rank_tier, H=wallet_mins,
    K=reg_staff, L=effective_rate, M=email"""
    try:
        for row in _get_member_rows()[1:]:
            if len(row) > 1 and row[1].strip() == member_id.strip():
                name        = row[2].strip()  if len(row) > 2  else "-"
                phone       = row[3].strip()  if len(row) > 3  else "-"
                net_spend   = _int(row[5])    if len(row) > 5  and row[5].strip() else 0
                wallet_mins = _int(row[7])    if len(row) > 7  and row[7].strip() else None
                email       = row[12].strip() if len(row) > 12 else ""
                # Always compute rank from spend so sheet stale values don't mislead
                mt, it   = fetch_rank_thresholds()
                rank_raw = get_member_rank(net_spend, mt, it)
                return {
                    "name":        name or "-",
                    "phone":       phone or "-",
                    "email":       email,
                    "net_spend":   net_spend,
                    "rank_raw":    rank_raw,
                    "wallet_mins": wallet_mins,
                }
    except Exception:
        pass
    return {"name": "-", "phone": "-", "email": "", "net_spend": 0, "rank_raw": "Warrior", "wallet_mins": None}


def fetch_balance_mins(member_id: str) -> int:
    """Read current wallet balance (minutes) from Card_Wallet column H — bypasses cache (must be live)."""
    try:
        rows = member_sh.get_all_values()
        for row in rows[1:]:
            if len(row) > 1 and row[1].strip() == member_id.strip():
                return _int(row[7]) if len(row) > 7 and row[7].strip() else 0
    except Exception:
        pass
    return 0


def fetch_member_effective_rate(member_id: str) -> float:
    """Read stored per-member effective rate from Card_Wallet col L."""
    try:
        for row in _get_member_rows()[1:]:
            if len(row) > 1 and row[1].strip() == member_id.strip():
                val = row[11].strip() if len(row) > 11 else ""
                if val:
                    return float(val)
    except Exception as e:
        logging.warning("fetch_member_effective_rate %s: %s", member_id, e)
    return 0.0


def update_member_effective_rate(member_id: str, new_rate: float) -> None:
    """Write per-member effective rate to Card_Wallet col L (1-based col 12)."""
    try:
        rows = member_sh.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if len(row) > 1 and row[1].strip() == member_id.strip():
                member_sh.update_cell(i, 12, round(new_rate, 4))
                # Invalidate member cache so next read picks up the change
                global _MBR_TS
                _MBR_TS = 0.0
                return
        logging.warning("update_member_effective_rate: member %s not found", member_id)
    except Exception as e:
        logging.warning("update_member_effective_rate %s: %s", member_id, e)


def build_member_rate_dict() -> dict[str, float]:
    """Return {member_id: effective_rate} for all members with a stored rate in col L."""
    result: dict[str, float] = {}
    try:
        for row in _get_member_rows()[1:]:
            if len(row) > 1 and row[1].strip():
                m_id = row[1].strip()
                val  = row[11].strip() if len(row) > 11 else ""
                if val:
                    try:
                        result[m_id] = float(val)
                    except ValueError:
                        pass
    except Exception as e:
        logging.warning("build_member_rate_dict: %s", e)
    return result


def fetch_member_rank_from_sheet(member_id):
    """Read the member's rank label directly from Card_Wallet Column G (cached)."""
    try:
        for row in _get_member_rows()[1:]:
            if len(row) > 1 and row[1].strip() == member_id.strip():
                rank_val = row[6].strip() if len(row) > 6 else ""
                if rank_val:
                    return rank_val
                rank_progress = _int(row[5]) if len(row) > 5 else 0
                master, immortal = fetch_rank_thresholds()
                return get_member_rank(rank_progress, master, immortal)
    except Exception:
        pass
    return "New Member"


def fetch_member_tier(member_id: str) -> str:
    """Return the member's current tier label from Card_Wallet Column G (cached)."""
    try:
        for row in _get_member_rows()[1:]:
            if len(row) > 1 and row[1].strip() == member_id.strip():
                tier = row[6].strip() if len(row) > 6 else ""
                return tier if tier else "Warrior"
    except Exception:
        pass
    return "New Member"


def get_member_rank(total_spend, master_thresh, immortal_thresh):
    """Return rank label based on net Top-Up spend (Column E).
    0 spend = 'Warrior' (all registered members start as Warrior).
    Otherwise Warrior/Master/Immortal."""
    if immortal_thresh > 0 and total_spend >= immortal_thresh:
        return "Immortal"
    if master_thresh > 0 and total_spend >= master_thresh:
        return "Master"
    return "Warrior"


def display_rank(rank):
    """Normalise rank label for display — 'New Member' maps to 'Warrior'
    since all registered members hold at least Warrior status."""
    return "Warrior" if rank in ("New Member", "", None) else rank


RANK_EMOJI = {"Warrior": "⚔️", "Master": "🏅", "Immortal": "💎"}


def rank_emoji(rank):
    return RANK_EMOJI.get(display_rank(rank), "⚔️")


def build_rank_bonus_lines(rank, bonus_table):
    """Return formatted lines showing each bonus tier for the member's rank."""
    rank_col = {"Warrior": 1, "Master": 2, "Immortal": 3}
    eff_rank = display_rank(rank)
    col      = rank_col.get(eff_rank, 1)
    lines    = []
    for (threshold, w, m, i) in sorted(bonus_table, key=lambda x: x[0]):
        bonus = (w, m, i)[col - 1]
        if threshold > 0:
            lines.append(f"  • {threshold:,} Ks  →  +{bonus} mins")
    return lines


def fetch_bonus_table():
    """Fetch bonus table from cache (or Setting!O2:R5 as fallback).
    Returns list of (threshold, warrior_bonus, master_bonus, immortal_bonus)."""
    cfg = _get_cfg()
    if cfg.get("bonus_table"):
        return [tuple(row) for row in cfg["bonus_table"]]
    try:
        rows = setting_sh.get("O2:R5")
        result = []
        for row in rows:
            if len(row) < 4:
                continue
            try:
                threshold = _int(row[0])
                w_bonus   = _int(row[1])
                m_bonus   = _int(row[2])
                i_bonus   = _int(row[3])
                if threshold > 0 or any([w_bonus, m_bonus, i_bonus]):
                    result.append((threshold, w_bonus, m_bonus, i_bonus))
            except (ValueError, TypeError):
                continue
        return result
    except Exception:
        return []


def get_bonus_mins(rank, amount, bonus_table):
    """Return bonus mins for the given rank and top-up amount.
    Finds the row with the highest threshold that is still <= amount."""
    if not bonus_table:
        return 0
    rank_col = {"Warrior": 1, "Master": 2, "Immortal": 3}
    col = rank_col.get(display_rank(rank), 1)
    matched_bonus     = 0
    matched_threshold = -1
    for (threshold, w, m, i) in bonus_table:
        if amount >= threshold and threshold > matched_threshold:
            matched_threshold = threshold
            matched_bonus     = (w, m, i)[col - 1]
    return matched_bonus


def next_member_row_no():
    """Return the next sequential row number for Card_Wallet Column A (No).
    Reads all values in col A, finds the last integer, and returns +1."""
    try:
        col_a = member_sh.col_values(1)[1:]   # skip header
        nums  = []
        for v in col_a:
            try:
                nums.append(int(str(v).strip()))
            except (ValueError, TypeError):
                pass
        return (max(nums) + 1) if nums else 1
    except Exception:
        return 1


def next_write_row(worksheet):
    """Return the next empty row number for a worksheet.
    Uses Column B (always written by the bot, never a formula) as the anchor
    so ARRAYFORMULA-filled columns don't inflate the count."""
    return len(worksheet.col_values(2)) + 1


def next_member_id():
    """Auto-increment the last member ID in Card_Wallet Column B.
    Handles any trailing digits: 'PSV_A_003' → 'PSV_A_004'.
    Returns 'PSV_A_001' when no members exist yet."""
    try:
        ids = [v.strip() for v in member_sh.col_values(2)[1:] if v.strip()]
        if not ids:
            return "PSV_A_001"
        last = ids[-1]
        m = re.search(r'(\d+)$', last)
        if m:
            prefix = last[:m.start()]
            num    = int(m.group(1)) + 1
            width  = len(m.group(1))
            return f"{prefix}{num:0{width}d}"
        return last + "_1"
    except Exception:
        return "PSV_A_001"


def fetch_rank_table_display():
    """Fetch Setting!O1:R5 and return a formatted string table.
    Row 1 = headers, rows 2-5 = data tiers."""
    try:
        rows = setting_sh.get("O1:R5")
        if not rows:
            return "_(data မရှိပါ)_"
        # Build header from first row
        header = rows[0] if rows else ["Amount", "Warrior", "Master", "Immortal"]
        # Pad header to 4 cols
        while len(header) < 4:
            header.append("-")
        lines = [
            f"{'Amount (Ks)':<14} {'⚔️ Warrior':>10} {'🏅 Master':>10} {'💎 Immortal':>11}",
            "─" * 48,
        ]
        for row in rows[1:]:
            if len(row) < 4:
                continue
            try:
                amt = _int(row[0])
                if amt == 0:
                    continue
                lines.append(
                    f"{amt:>14,}  {_int(row[1]):>9,}  {_int(row[2]):>9,}  {_int(row[3]):>10,}"
                )
            except Exception:
                continue
        return "```\n" + "\n".join(lines) + "\n```"
    except Exception:
        return "_(fetch error)_"


def get_top_up_suggestion(rank, bonus_table):
    """Return (suggested_amount, bonus_mins) for the given rank — highest bonus tier."""
    rank_col = {"Warrior": 1, "Master": 2, "Immortal": 3}
    col      = rank_col.get(display_rank(rank), 1)
    best_amt, best_bonus = 0, 0
    for (threshold, w, m, i) in bonus_table:
        bonus = (w, m, i)[col - 1]
        if bonus > best_bonus:
            best_bonus = bonus
            best_amt   = threshold
    return best_amt, best_bonus


def today_str():
    return now_mmt().strftime("%-m/%-d/%Y")


def step_hdr(step: int, total: int, label: str) -> str:
    """Return a Form Wizard progress header for every prompt message."""
    filled = "▰" * step
    empty  = "▱" * (total - step)
    return f"*{label}*\n`{filled}{empty}` _({step}/{total})_\n━━━━━━━━━━━━━━━━━━\n"


# ─────────────────────────────────────────
#  RECEIPT HELPERS
# ─────────────────────────────────────────
RECEIPTS_DIR = Path(__file__).parent / "receipts"
RECEIPTS_DIR.mkdir(exist_ok=True)


_API_KEY = os.environ.get("API_KEY", "")

def _api_base() -> str:
    """Return the API server base URL (no trailing slash), or empty string if not configured."""
    return os.environ.get("API_BASE_URL", "").rstrip("/")


def save_receipt_json(voucher_id: str, data: dict) -> None:
    """Persist receipt data locally and push to API server."""
    safe_id = voucher_id.replace("/", "-").replace("\\", "-")
    path = RECEIPTS_DIR / f"{safe_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    base = _api_base()
    if not base:
        return
    try:
        import urllib.request
        secret = os.environ.get("RECEIPT_SECRET", "")
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/receipt",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-receipt-secret": secret,
                "X-API-Key": _API_KEY,
            },
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logging.warning("Failed to push receipt to API server: %s", e)


def get_receipt_url(voucher_id: str) -> str:
    """Return the public receipt URL or empty string if API_BASE_URL not set."""
    base = _api_base()
    if not base:
        return ""
    safe_id = voucher_id.replace("/", "-").replace("\\", "-")
    return f"{base}/api/receipt/{safe_id}"


def get_receipt_kb(voucher_id: str):
    """Return InlineKeyboardMarkup with a 🧾 Print Receipt button, or None if no domain set."""
    url = get_receipt_url(voucher_id)
    if not url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("🧾 Print Receipt", url=url)]])



# ── Import all handlers so they're accessible from bot package ──
from bot.handlers import *  # noqa: F401,F403,E402
from bot.app import main as main  # noqa: F401,E402

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


# ═════════════════════════════════════════
#  MAIN MENU
# ═════════════════════════════════════════

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    now      = now_mmt()
    date_str = now.strftime("%-d %b %Y")
    hour     = now.hour
    if hour < 12:
        greet = "🌅 မင်္ဂလာနံနက်ခင်း"
    elif hour < 18:
        greet = "☀️ မင်္ဂလာနေ့လည်"
    else:
        greet = "🌙 မင်္ဂလာညနေ"
    kb = [
        [BTN_DAILY_SALES,      BTN_MEMBER_MGMT],
        [BTN_CONSOLES,         BTN_TODAY_REPORT],
        [BTN_STAFF_BOOK,       BTN_INVENTORY_VIEW],
        [BTN_FINANCIAL_REPORT, BTN_ADMIN],
    ]
    await update.message.reply_text(
        f"🎮 *PS Vibe — Staff Bot*\n"
        f"{greet} | _{date_str}_\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Menu ရွေးပါ ↓",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return MAIN_MENU


async def step_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text

    if choice in (BTN_DAILY_SALES, BTN_NEW_SALE):
        context.user_data["v_no"]  = next_voucher()
        context.user_data["staff"] = ""
        return await prompt_member(update, context)

    if choice == BTN_MEMBER_MGMT:
        return await show_mm_menu(update, context)

    if choice == BTN_INVENTORY_VIEW:
        return await cmd_inventory(update, context)

    if choice == BTN_TODAY_REPORT:
        return await cmd_today_report(update, context)

    if choice == BTN_CONSOLES:
        return await show_console_menu(update, context)

    if choice == BTN_STAFF_BOOK:
        return await cmd_staff_book_hub(update, context)

    if choice == BTN_SBK_NEW:
        return await cmd_staff_booking(update, context)

    if choice == BTN_SBK_CONFIRMED:
        return await cmd_confirmed_bookings(update, context)

    if choice == BTN_GAME_LIB_MENU:
        return await show_game_menu(update, context)

    if choice == BTN_FINANCIAL_REPORT:
        return await cmd_financial_report(update, context)

    if choice == BTN_ADMIN:
        await update.message.reply_text(
            "🔐 *Admin Panel — PIN လိုအပ်သည်*\n\nPIN နံပါတ် ထည့်ပါ:",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ADMIN_PIN

    # Any back from sub-states that lands here
    if choice == BTN_BACK_MAIN:
        return await show_main_menu(update, context)

    return await show_main_menu(update, context)


# ═════════════════════════════════════════
#  STAFF SELECTION (Daily Sales first step)
# ═════════════════════════════════════════

async def prompt_staff_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    staff_list = fetch_staff()
    kb = [[s] for s in staff_list] + [NAV_ROW]
    await update.message.reply_text(
        step_hdr(1, 7, "Staff Selection") +
        "👤 ဘယ် Staff လဲ ရွေးပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return STAFF_SELECT


async def step_staff_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_main_menu(update, context)

    staff_list = fetch_staff()
    if text not in staff_list:
        await update.message.reply_text("⚠️ ပြသောစာရင်းမှ ရွေးပေးပါ -")
        return STAFF_SELECT

    context.user_data["staff"] = text
    return await prompt_member(update, context)


# ═════════════════════════════════════════
#  MEMBER MANAGEMENT SUB-MENU
# ═════════════════════════════════════════

async def show_mm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [BTN_FIRST_PURCHASE, BTN_TOP_UP],
        [BTN_CHECK_MEMBER,   BTN_VIEW_RANKS],
        [BTN_BACK_MAIN],
    ]
    await update.message.reply_text(
        "💳 *Member Management*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Option ရွေးပါ ↓",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return MM_MENU


async def show_rank_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch and display the full Rank Bonus table from Setting!O1:R5."""
    table = fetch_rank_table_display()
    master_thresh, immortal_thresh = fetch_rank_thresholds()
    await update.message.reply_text(
        f"📋 *Rank Bonus Table*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏅 *Master* threshold: {master_thresh:,} Ks total spend\n"
        f"💎 *Immortal* threshold: {immortal_thresh:,} Ks total spend\n\n"
        f"*Bonus Mins by Top-Up Amount:*\n"
        f"{table}",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True),
    )
    return MM_MENU


async def step_mm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text

    if choice == BTN_FIRST_PURCHASE:
        context.user_data["nm_staff"] = ""
        return await prompt_nm_name(update, context)

    if choice == BTN_TOP_UP:
        return await prompt_tu_member(update, context)

    if choice == BTN_CHECK_MEMBER:
        return await prompt_mm_lookup(update, context)

    if choice == BTN_VIEW_RANKS:
        return await show_rank_info(update, context)

    if choice in (BTN_BACK_MAIN, BTN_BACK):
        return await show_main_menu(update, context)

    return await show_mm_menu(update, context)


# ─────────────────────────────────────────
#  CHECK MEMBER LOOKUP FLOW
# ─────────────────────────────────────────

async def prompt_mm_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           search_results: list | None = None, query: str = ""):
    members = fetch_members()
    if search_results is not None:
        display = search_results
        hint    = f"🔍 *\"{query}\"* — {len(display)} ရလဒ် တွေ့သည်\n" if display else f"❌ *\"{query}\"* — မတွေ့ပါ — ထပ်ရှာပါ\n"
    else:
        display = members
        hint    = "🔍 _ID/Name ရိုက်ပြီး ရှာနိုင်သည်_\n" if len(members) > 5 else ""
    kb = [[BTN_BACK]] + [[m] for m in display]
    await update.message.reply_text(
        "🔍 *Check Member*\n\n"
        f"{hint}"
        "ကြည့်ရှုလိုသော Member ID ကို ရွေးပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return MM_LOOKUP


async def step_mm_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_BACK:
        return await show_mm_menu(update, context)

    member_id = text.strip()
    members   = fetch_members()
    if member_id not in members:
        # Treat input as search query — filter by substring match
        q       = member_id.lower()
        results = [m for m in members if q in m.lower()]
        if len(results) == 1:
            # Single match → auto-select
            member_id = results[0]
        else:
            return await prompt_mm_lookup(update, context, search_results=results, query=member_id)

    data                           = fetch_member_data(member_id)
    master_thresh, immortal_thresh = fetch_rank_thresholds()
    r    = display_rank(data["rank_raw"])
    r_em = rank_emoji(r)
    net  = data["net_spend"]

    # Progress to next tier
    if r == "Warrior" and master_thresh > 0:
        remaining    = max(master_thresh - net, 0)
        progress_ln  = f"📊 Master ရရန်: *{remaining:,} Ks* လိုသေးသည်"
    elif r == "Master" and immortal_thresh > 0:
        remaining    = max(immortal_thresh - net, 0)
        progress_ln  = f"📊 Immortal ရရန်: *{remaining:,} Ks* လိုသေးသည်"
    else:
        progress_ln  = "🏆 _Top Rank — Immortal!_"

    wallet = data["wallet_mins"]
    wallet_ln = f"💰 Wallet Mins: *{wallet:,} mins*" if wallet is not None else "💰 Wallet Mins: _ဒေတာမရှိ_"

    mm_kb = [
        [BTN_FIRST_PURCHASE], [BTN_TOP_UP],
        [BTN_CHECK_MEMBER], [BTN_VIEW_RANKS],
        [BTN_BACK_MAIN],
    ]
    email     = data.get("email", "")
    email_ln  = f"📧 Email: *{email}*\n" if email else ""
    await update.message.reply_text(
        f"🔍 *Member Profile*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🪪 ID: *{member_id}*\n"
        f"👤 Name: *{data['name']}*\n"
        f"📞 Phone: *{data['phone']}*\n"
        f"{email_ln}"
        f"🎖 Rank: *{r_em} {r}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 Ranking Progress: *{net:,} Ks*\n"
        f"{wallet_ln}\n"
        f"{progress_ln}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"_(Menu ကို ဆက်လုပ်နိုင်သည်)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(mm_kb, resize_keyboard=True),
    )
    return MM_MENU


# ═════════════════════════════════════════
#  NEW MEMBER — STAFF SELECTION (first step)
# ═════════════════════════════════════════

async def prompt_nm_staff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    staff_list = fetch_staff()
    kb = [[s] for s in staff_list] + [NAV_ROW]
    await update.message.reply_text(
        step_hdr(1, 6, "Staff Selection") +
        "👤 ဘယ် Staff က Register လုပ်ပေးသလဲ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return NM_STAFF


async def step_nm_staff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_mm_menu(update, context)

    staff_list = fetch_staff()
    if text not in staff_list:
        await update.message.reply_text("⚠️ ပြသောစာရင်းမှ ရွေးပေးပါ -")
        return NM_STAFF

    context.user_data["nm_staff"] = text
    return await prompt_nm_name(update, context)


# ═════════════════════════════════════════
#  FIRST PURCHASE (NEW MEMBER) FLOW
# ═════════════════════════════════════════

async def prompt_nm_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        step_hdr(1, 6, "Member Name") +
        "👤 Member Name ရိုက်ပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return NM_NAME


async def step_nm_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_mm_menu(update, context)

    context.user_data["nm_name"] = text.strip()
    return await prompt_nm_phone(update, context)


async def prompt_nm_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data.get("nm_name", "")
    await update.message.reply_text(
        step_hdr(2, 6, "Phone Number") +
        f"👤 Name: *{name}*\n\n"
        "📞 Phone Number ရိုက်ပါ (မရှိလျှင် ⏩ Skip နှိပ်ပါ) -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [[BTN_SKIP_PHONE], NAV_ROW], resize_keyboard=True
        ),
    )
    return NM_PHONE


async def step_nm_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        context.user_data.pop("nm_name", None)
        return await prompt_nm_name(update, context)

    if text == BTN_SKIP_PHONE:
        context.user_data["nm_phone"] = "-"
    else:
        phone_input = text.strip()
        digits_only = re.sub(r'[\s\-\+\(\)]', '', phone_input)
        if not digits_only.isdigit() or len(digits_only) < 7:
            await update.message.reply_text(
                "⚠️ Phone number မမှန်ပါ — ဂဏန်း ၇ လုံးနှင့်အထက် ရိုက်ပါ\n"
                "_(မသိလျှင် ⏩ Skip နှိပ်ပါ)_",
                parse_mode="Markdown",
            )
            return NM_PHONE
        context.user_data["nm_phone"] = phone_input

    # Next → email step
    return await prompt_nm_email(update, context)


async def prompt_nm_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name  = context.user_data.get("nm_name", "")
    phone = context.user_data.get("nm_phone", "-")
    await update.message.reply_text(
        step_hdr(3, 6, "Email Address") +
        f"👤 Name: *{name}*  |  📞 Phone: *{phone}*\n\n"
        "📧 Email ရိုက်ပါ\n"
        "_(n8n မှတဆင့် wallet alert ပို့ရာတွင် သုံးမည်)_\n\n"
        "မရှိလျှင် 'Email မထည့်' နှိပ်ပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [[BTN_SKIP_EMAIL], NAV_ROW], resize_keyboard=True
        ),
    )
    return NM_EMAIL


async def step_nm_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        context.user_data.pop("nm_phone", None)
        return await prompt_nm_phone(update, context)

    if text == BTN_SKIP_EMAIL:
        context.user_data["nm_email"] = ""
    else:
        # Basic email validation
        email = text.strip().lower()
        if "@" not in email or "." not in email.split("@")[-1]:
            await update.message.reply_text(
                "⚠️ Email format မမှန်ပါ (e.g. name@gmail.com)\n"
                "ထပ်ရိုက်ပါ သို့မဟုတ် ⏩ Skip နှိပ်ပါ -"
            )
            return NM_EMAIL
        context.user_data["nm_email"] = email

    # Auto-generate the member ID now, then show it for confirmation
    context.user_data["nm_id"] = next_member_id()
    return await prompt_nm_id(update, context)


async def prompt_nm_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show auto-generated ID for staff confirmation. Staff can also type a custom ID."""
    name   = context.user_data.get("nm_name", "")
    phone  = context.user_data.get("nm_phone", "-")
    gen_id = context.user_data.get("nm_id", "")
    await update.message.reply_text(
        step_hdr(4, 6, "Member ID") +
        f"👤 Name: *{name}*  |  📞 Phone: *{phone}*\n"
        f"🪪 Auto ID: *{gen_id}*\n\n"
        f"ID မှန်ကန်ပါက ✅ Confirm ID နှိပ်ပါ။\n"
        f"ပြောင်းလဲလိုပါက ID အသစ် ရိုက်ပေးပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [[BTN_CONFIRM_ID], NAV_ROW], resize_keyboard=True
        ),
    )
    return NM_ID


async def step_nm_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        context.user_data.pop("nm_id", None)
        return await prompt_nm_email(update, context)

    if text != BTN_CONFIRM_ID:
        # Staff typed a custom ID — accept it
        context.user_data["nm_id"] = text.strip()

    # BTN_CONFIRM_ID keeps the auto-generated ID already stored
    return await prompt_nm_amt(update, context)


async def prompt_nm_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show default card price from Setting!B20/B21 with a one-tap confirm button."""
    price, base_mins = fetch_new_member_defaults()
    context.user_data["nm_default_price"] = price
    context.user_data["nm_default_mins"]  = base_mins

    # Build a button with the exact default price so step can detect it unambiguously
    default_btn = f"✅ {price:,} Ks (Default)" if price else BTN_NM_CUSTOM
    context.user_data["nm_default_btn"] = default_btn

    price_line = f"*{price:,} Ks*" if price else "_(Setting!B20 မရှိပါ)_"
    mins_line  = f"*{base_mins:,} mins*" if base_mins else "_(Setting!B21 မရှိပါ)_"

    d    = context.user_data
    name = d.get("nm_name", "")
    m_id = d.get("nm_id", "")
    kb   = [[default_btn], [BTN_NM_CUSTOM], [BTN_NM_GIFT], NAV_ROW]
    await update.message.reply_text(
        step_hdr(5, 6, "Card Amount") +
        f"👤 *{name}*  |  🪪 *{m_id}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💵 Card Price  : {price_line}\n"
        f"⏱️ Base Mins   : {mins_line}\n\n"
        f"ဤပမာဏ ကောက်ခံမည်လား?\n"
        f"_(ကွဲပြားသော ပမာဏ ✏️ | မဲဖောက်/Influencer/Gift ဆိုလျှင် 🎁 Gift နှိပ်ပါ)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return NM_AMT


async def step_nm_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text
    d       = context.user_data
    default_btn = d.get("nm_default_btn", "")

    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        d.pop("nm_id", None)
        return await prompt_nm_id(update, context)

    # Staff confirmed the default price
    if text == default_btn and default_btn:
        d["nm_amt"]  = d["nm_default_price"]
        d["nm_mins"] = d["nm_default_mins"]
        d.pop("nm_is_gift", None)
        return await prompt_nm_kpay(update, context)

    # Gift / Free card — PIN verify first
    if text == BTN_NM_GIFT:
        d["nm_gift_pending"] = True
        await update.message.reply_text(
            "🔐 *Gift Card — PIN လိုအပ်သည်*\n\n"
            "Admin PIN ထည့်ပါ -",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
        )
        return NM_GIFT_PIN

    # Staff wants to type a custom amount — re-prompt for free text input
    if text == BTN_NM_CUSTOM:
        default_price = d.get("nm_default_price", 0)
        d["nm_custom_mode"] = True
        d.pop("nm_is_gift", None)
        await update.message.reply_text(
            step_hdr(4, 5, "Card Amount — Custom") +
            f"ကောက်ခံမည့် ပမာဏ (Ks) ရိုက်ပါ -\n"
            f"_(Default: {default_price:,} Ks)_",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
        )
        return NM_AMT

    # Free-text entry (custom amount typed by staff)
    try:
        amt = int(text.replace(",", "").strip())
    except ValueError:
        await update.message.reply_text("⚠️ ဂဏန်းသက်သက် ရိုက်ပေးပါ -")
        return NM_AMT

    if amt < 0:
        await update.message.reply_text("⚠️ ပမာဏ 0 ထက်ကြီးရမည် -")
        return NM_AMT

    # Custom amount uses the same base mins from Setting!B21 (not recalculated)
    d["nm_amt"]  = amt
    d["nm_mins"] = d.get("nm_default_mins", 0)
    d.pop("nm_custom_mode", None)
    d.pop("nm_is_gift", None)
    return await prompt_nm_kpay(update, context)


async def step_nm_gift_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify admin PIN before allowing Gift / Free Card."""
    text = update.message.text.strip()
    d    = context.user_data
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        d.pop("nm_gift_pending", None)
        return await prompt_nm_amt(update, context)

    # Always delete the PIN message to keep it private
    try:
        await update.message.delete()
    except Exception:
        pass

    if text != STOCK_ACCESS_PIN:
        await update.message.reply_text(
            "❌ *PIN မမှန်ကန်ပါ* — ထပ်ကြိုးစားပါ သို့မဟုတ် ⬅️ Back နှိပ်ပါ -",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
        )
        return NM_GIFT_PIN

    # PIN correct — set gift data and show review
    d.pop("nm_gift_pending", None)
    d["nm_amt"]     = 0
    d["nm_kpay"]    = 0
    d["nm_cash"]    = 0
    d["nm_mins"]    = d.get("nm_default_mins", 0)
    d["nm_is_gift"] = True
    name = d.get("nm_name", "")
    m_id = d.get("nm_id", "")
    await update.message.reply_text(
        f"📋 *Review — Gift / Free Membership*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name: *{name}*\n"
        f"🪪 Member ID: *{m_id}*\n"
        f"📞 Phone: *{d.get('nm_phone', '-')}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎁 Type: *Gift / Free Card*\n"
        f"💵 Amount: *0 Ks*\n"
        f"⏱️ Mins Added: *{d['nm_mins']:,} mins*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"မှန်ကန်ပါသလား? ✅ Confirm & Save နှိပ်ပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BTN_CONFIRM_SAVE], NAV_ROW], resize_keyboard=True),
    )
    return NM_CONFIRM


async def prompt_nm_kpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d   = context.user_data
    amt = d.get("nm_amt", 0)
    await update.message.reply_text(
        step_hdr(6, 6, "Payment — Kpay") +
        f"👤 *{d.get('nm_name','')}*  |  🪪 *{d.get('nm_id','')}*\n"
        f"💵 Card Amount: *{amt:,} Ks*\n\n"
        "💳 Kpay ပမာဏ ရိုက်ပါ (မရှိလျှင် 0) -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return NM_KPAY


async def step_nm_kpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        context.user_data.pop("nm_amt", None)
        return await prompt_nm_amt(update, context)

    try:
        kpay = int(text.replace(",", "").strip())
    except ValueError:
        await update.message.reply_text("⚠️ ဂဏန်းသက်သက် ရိုက်ပေးပါ -")
        return NM_KPAY

    d = context.user_data
    if kpay > d["nm_amt"]:
        await update.message.reply_text(
            f"⚠️ Kpay (*{kpay:,} Ks*) သည် စုစုပေါင်း (*{d['nm_amt']:,} Ks*) ထက် မကျော်ရပါ -",
            parse_mode="Markdown",
        )
        return NM_KPAY

    cash = d["nm_amt"] - kpay
    d["nm_kpay"] = kpay
    d["nm_cash"] = cash

    # Show full Review Your Entry summary before saving
    phone_display = d.get("nm_phone", "-")
    email_display = d.get("nm_email", "") or "—"
    kb = [[BTN_CONFIRM_SAVE], NAV_ROW]
    await update.message.reply_text(
        f"📋 *Review Your Entry — First Purchase*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name: *{d['nm_name']}*\n"
        f"🪪 Member ID: *{d['nm_id']}*\n"
        f"📞 Phone: *{phone_display}*\n"
        f"📧 Email: *{email_display}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💵 Total Amount: *{d['nm_amt']:,} Ks*\n"
        f"⏳ Base Mins (Card): *{d['nm_mins']:,} mins*\n"
        f"🎁 Bonus Mins: *0 mins*\n"
        f"🔥 Total Added Mins: *{d['nm_mins']:,} mins*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💳 Kpay: *{kpay:,} Ks*  |  💵 Cash: *{cash:,} Ks*\n\n"
        f"မှန်ကန်ပါသလား? ✅ Confirm & Save နှိပ်ပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return NM_CONFIRM


async def step_nm_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        # Gift cards skip Kpay step — go back to amount selection
        if context.user_data.get("nm_is_gift"):
            context.user_data.pop("nm_is_gift", None)
            return await prompt_nm_amt(update, context)
        return await prompt_nm_kpay(update, context)

    if text != BTN_CONFIRM_SAVE:
        return NM_CONFIRM

    d        = context.user_data
    phone    = d.get("nm_phone", "-")
    is_gift  = d.get("nm_is_gift", False)

    # ── Pre-compute (lightweight sync — reserve rows before background) ──
    row_no   = next_member_row_no()
    cw_row   = next_write_row(member_sh)
    tl_row   = next_write_row(topup_sh)
    nm_staff = d.get("nm_staff", "")

    # Balance = mins just added (new member has no prior balance — Phase B)
    bal_mins = d["nm_mins"]

    # Initial effective rate (gift cards have no rate)
    initial_rate = (round(d["nm_amt"] / d["nm_mins"], 4)
                    if d["nm_mins"] > 0 and not is_gift else 0)

    # Snapshot all fields before clearing user_data
    nm_id    = d["nm_id"];  nm_name = d["nm_name"]
    nm_amt   = d["nm_amt"]; nm_mins = d["nm_mins"]
    nm_kpay  = d["nm_kpay"]; nm_cash = d["nm_cash"]
    nm_email = d.get("nm_email", "")
    today    = today_str()
    nm_vid   = f"NM-{nm_id}-{now_mmt().strftime('%Y%m%d%H%M%S')}"
    tl_type  = "Gift" if is_gift else "First Purchase"

    nm_staff_line = f"\n👤 Registered by: *{nm_staff}*" if nm_staff else ""
    if is_gift:
        msg = (
            f"🎁 *Gift Member Created!*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🪪 ID: *{nm_id}*  |  👤 *{nm_name}*\n"
            f"📞 Phone: *{phone}*{nm_staff_line}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎁 Type: *Gift / Free Card*\n"
            f"⏱️ Added: *{nm_mins:,} mins*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 *Wallet Balance: {bal_mins:,} mins*"
        )
    else:
        msg = (
            f"✅ *Member Created!*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🪪 ID: *{nm_id}*  |  👤 *{nm_name}*\n"
            f"📞 Phone: *{phone}*{nm_staff_line}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 Amount: *{nm_amt:,} Ks*  |  ⏱️ Added: *{nm_mins:,} mins*\n"
            f"💳 Kpay: *{nm_kpay:,} Ks*  |  💵 Cash: *{nm_cash:,} Ks*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 *Wallet Balance: {bal_mins:,} mins*"
        )

    # Save receipt JSON (local disk — instant)
    save_receipt_json(nm_vid, {
        "type": "new_member", "voucher_id": nm_vid, "date": today,
        "name": nm_name, "member_id": nm_id, "phone": phone, "email": nm_email,
        "amount": nm_amt, "mins": nm_mins, "kpay": nm_kpay, "cash": nm_cash,
        "balance_mins": bal_mins, "rank": "New Member",
        "prev_balance": 0, "balance_change": nm_mins, "balance_after": bal_mins,
        "is_gift": is_gift,
    })
    receipt_kb = get_receipt_kb(nm_vid)
    context.user_data.clear()

    # ── RECEIPT — sent BEFORE sheet writes ────────────────────────
    await update.message.reply_text(msg, parse_mode="Markdown",
                                    reply_markup=ReplyKeyboardRemove())
    if receipt_kb:
        await update.message.reply_text("🖨️ Receipt ပုံနှိပ်ရန် -", reply_markup=receipt_kb)

    # ── SHEET WRITES — background ─────────────────────────────────
    async def _nm_bg():
        def _do():
            # 1. Card_Wallet (cols A–D + K + M)
            # Col E = Lifetime Spend (formula in sheet — do NOT overwrite)
            # Col M = Email
            batch = [
                {"range": f"A{cw_row}:D{cw_row}",
                 "values": [[row_no, nm_id, nm_name, phone]]},
                {"range": f"K{cw_row}", "values": [[nm_staff]]},
            ]
            if nm_email:
                batch.append({"range": f"M{cw_row}", "values": [[nm_email]]})
            member_sh.batch_update(batch, value_input_option="USER_ENTERED")
            # 2. TopUp_Log (cols A–C, E–I, J)
            topup_sh.batch_update(
                [{"range": f"A{tl_row}:C{tl_row}",
                  "values": [[today, nm_id, "New Member"]]},
                 {"range": f"E{tl_row}:I{tl_row}",
                  "values": [[nm_amt, nm_kpay, nm_cash, nm_mins, tl_type]]},
                 {"range": f"J{tl_row}", "values": [[nm_staff]]}],
                value_input_option="USER_ENTERED",
            )
            # 3. Effective rate (skipped for gift cards)
            if initial_rate > 0:
                update_member_effective_rate(nm_id, initial_rate)
        try:
            await asyncio.to_thread(_do)
        except Exception as _e:
            logging.error("nm_bg_write: %s", _e)
    asyncio.create_task(_nm_bg())

    return await show_main_menu(update, context)


# ═════════════════════════════════════════
#  TOP UP (EXISTING MEMBER) FLOW
# ═════════════════════════════════════════

async def prompt_tu_member(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           search_results: list | None = None, query: str = ""):
    members = fetch_members()
    if search_results is not None:
        display = search_results
        hint    = f"🔍 *\"{query}\"* — {len(display)} ရလဒ် တွေ့သည်\n" if display else f"❌ *\"{query}\"* — မတွေ့ပါ — ထပ်ရှာပါ\n"
    else:
        display = members
        hint    = "🔍 _ID/Name ရိုက်ပြီး ရှာနိုင်သည်_\n" if len(members) > 5 else ""
    kb = [[BTN_BACK, BTN_CANCEL]] + [[m] for m in display]
    await update.message.reply_text(
        step_hdr(1, 3, "Select Member") +
        f"{hint}"
        "👤 Member ID ရွေးပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return TU_MEMBER


async def step_tu_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_mm_menu(update, context)

    member_id = text.strip()
    members   = fetch_members()

    # If not an exact ID, treat as search query
    if member_id not in members:
        q       = member_id.lower()
        results = [m for m in members if q in m.lower()]
        if len(results) == 1:
            member_id = results[0]
        else:
            return await prompt_tu_member(update, context, search_results=results, query=member_id)

    context.user_data["tu_id"] = member_id

    # Single consolidated Card_Wallet read
    data                           = fetch_member_data(member_id)
    master_thresh, immortal_thresh = fetch_rank_thresholds()
    bonus_table                    = fetch_bonus_table()
    context.user_data["tu_rank"]            = data["rank_raw"]
    context.user_data["tu_total_spend"]     = data["net_spend"]
    context.user_data["tu_phone"]           = data["phone"]
    context.user_data["tu_name"]            = data["name"]
    context.user_data["tu_wallet_mins"]     = data["wallet_mins"]
    context.user_data["tu_master_thresh"]   = master_thresh
    context.user_data["tu_immortal_thresh"] = immortal_thresh
    context.user_data["tu_bonus_table"]     = bonus_table

    return await prompt_tu_amt(update, context)


async def prompt_tu_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_rank        = context.user_data.get("tu_rank", "Warrior")
    bonus_table     = context.user_data.get("tu_bonus_table", [])
    net_spend       = context.user_data.get("tu_total_spend", 0)
    master_thresh   = context.user_data.get("tu_master_thresh", 0)
    immortal_thresh = context.user_data.get("tu_immortal_thresh", 0)
    m_id            = context.user_data.get("tu_id", "")
    tu_name         = context.user_data.get("tu_name", "")
    tu_wallet       = context.user_data.get("tu_wallet_mins")
    wallet_line     = f"\n💰 Current Wallet: *{tu_wallet:,} mins*" if tu_wallet is not None else ""

    r     = display_rank(raw_rank)
    r_em  = rank_emoji(r)

    # Next-tier progress line
    if r == "Warrior" and master_thresh > 0:
        remaining = max(master_thresh - net_spend, 0)
        progress  = (
            f"📈 Ranking Progress: *{net_spend:,} Ks*\n"
            f"🏅 Master ရရန် *{remaining:,} Ks* လိုသေးသည်"
        )
    elif r == "Master" and immortal_thresh > 0:
        remaining = max(immortal_thresh - net_spend, 0)
        progress  = (
            f"📈 Ranking Progress: *{net_spend:,} Ks*\n"
            f"💎 Immortal ရရန် *{remaining:,} Ks* လိုသေးသည်"
        )
    else:
        progress = f"📈 Ranking Progress: *{net_spend:,} Ks*  _(🏆 Top Rank!)_"

    # Rank-specific bonus table
    bonus_lines = build_rank_bonus_lines(r, bonus_table)
    if bonus_lines:
        table_text  = "\n".join(bonus_lines)
        bonus_block = f"\n🎁 *{r_em} {r} Rank Bonus Table:*\n{table_text}\n"
    else:
        bonus_block = ""

    await update.message.reply_text(
        step_hdr(2, 3, "Top-Up Amount") +
        f"🪪 *{m_id}* — {tu_name}{wallet_line}\n"
        f"🎖 Rank: *{r_em} {r}*\n"
        f"{progress}\n"
        f"{bonus_block}\n"
        f"💵 Top Up Amount (Ks) ရိုက်ပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return TU_AMT


async def step_tu_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        context.user_data.pop("tu_id", None)
        return await prompt_tu_member(update, context)

    try:
        amt = int(text.replace(",", "").strip())
    except ValueError:
        await update.message.reply_text("⚠️ ဂဏန်းသက်သက် ရိုက်ပေးပါ -")
        return TU_AMT

    if amt <= 0:
        await update.message.reply_text("⚠️ ပမာဏ 0 ထက်ကြီးရမည် -")
        return TU_AMT

    hourly_rate = fetch_base_rate()
    base_mins   = round((amt * 60) / hourly_rate) if hourly_rate else 0
    rank        = context.user_data.get("tu_rank", "Warrior")
    bonus_table = context.user_data.get("tu_bonus_table") or fetch_bonus_table()
    bonus_mins  = get_bonus_mins(rank, amt, bonus_table)
    total_mins  = base_mins + bonus_mins

    context.user_data["tu_amt"]        = amt
    context.user_data["tu_base_mins"]  = base_mins
    context.user_data["tu_bonus_mins"] = bonus_mins
    context.user_data["tu_mins"]       = total_mins   # saved to sheet col H
    return await prompt_tu_kpay(update, context)


async def prompt_tu_kpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d   = context.user_data
    amt = d.get("tu_amt", 0)
    r   = display_rank(d.get("tu_rank", "Warrior"))
    r_em = rank_emoji(r)
    await update.message.reply_text(
        step_hdr(3, 3, "Payment — Kpay") +
        f"🪪 *{d.get('tu_id','')}* — {d.get('tu_name','')}\n"
        f"🎖 Rank: *{r_em} {r}*  |  💰 Top Up: *{amt:,} Ks*\n"
        f"⏱ {d.get('tu_base_mins',0):,} base + 🎁 {d.get('tu_bonus_mins',0)} bonus mins\n\n"
        "💳 Kpay ပမာဏ ရိုက်ပါ (မရှိလျှင် 0) -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return TU_KPAY


async def step_tu_kpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        context.user_data.pop("tu_amt", None)
        return await prompt_tu_amt(update, context)

    try:
        kpay = int(text.replace(",", "").strip())
    except ValueError:
        await update.message.reply_text("⚠️ ဂဏန်းသက်သက် ရိုက်ပေးပါ -")
        return TU_KPAY

    d = context.user_data
    if kpay > d["tu_amt"]:
        await update.message.reply_text(
            f"⚠️ Kpay (*{kpay:,} Ks*) သည် စုစုပေါင်း (*{d['tu_amt']:,} Ks*) ထက် မကျော်ရပါ -",
            parse_mode="Markdown",
        )
        return TU_KPAY

    cash = d["tu_amt"] - kpay
    d["tu_kpay"] = kpay
    d["tu_cash"] = cash

    r          = display_rank(d.get("tu_rank", "Warrior"))
    r_em       = rank_emoji(r)
    base_mins  = d.get("tu_base_mins", 0)
    bonus_mins = d.get("tu_bonus_mins", 0)
    total_mins = d.get("tu_mins", 0)

    # Corrected remaining-to-next-tier: Threshold − (Current Spend + This Top-Up)
    net_spend       = d.get("tu_total_spend", 0)
    master_thresh   = d.get("tu_master_thresh", 0)
    immortal_thresh = d.get("tu_immortal_thresh", 0)
    tu_amt          = d.get("tu_amt", 0)
    if r == "Warrior" and master_thresh > 0:
        remaining = master_thresh - (net_spend + tu_amt)
        next_tier_ln = (
            f"\n📊 After Top-Up Spend: *{net_spend + tu_amt:,} Ks*\n"
            f"🏅 Remaining to Master: *{max(remaining,0):,} Ks*"
            + ("\n🎉 ဤသွင်းမှုပြီးပါက 🏅 *Master* ဖြစ်သွားမည်!" if remaining <= 0 else "")
        )
    elif r == "Master" and immortal_thresh > 0:
        remaining = immortal_thresh - (net_spend + tu_amt)
        next_tier_ln = (
            f"\n📊 After Top-Up Spend: *{net_spend + tu_amt:,} Ks*\n"
            f"💎 Remaining to Immortal: *{max(remaining,0):,} Ks*"
            + ("\n🎉 ဤသွင်းမှုပြီးပါက 💎 *Immortal* ဖြစ်သွားမည်!" if remaining <= 0 else "")
        )
    else:
        next_tier_ln = "\n🏆 _Top Rank — Immortal!_"

    kb = [[BTN_CONFIRM_SAVE], NAV_ROW]
    await update.message.reply_text(
        f"📋 *Review Your Entry — Top Up*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🪪 *{d['tu_id']}* — {d.get('tu_name','')}\n"
        f"🎖 Rank: *{r_em} {r}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Top Up Amount: *{tu_amt:,} Ks*\n"
        f"⏳ Base Mins: *{base_mins:,} mins*\n"
        f"🎁 Rank Bonus: *+{bonus_mins} mins*\n"
        f"🔥 Total to be Added: *{total_mins:,} mins*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💳 Kpay: *{kpay:,} Ks*  |  💵 Cash: *{cash:,} Ks*"
        f"{next_tier_ln}\n\n"
        f"မှန်ကန်ပါသလား? ✅ Confirm & Save နှိပ်ပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return TU_CONFIRM


async def step_tu_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await prompt_tu_kpay(update, context)

    if text != BTN_CONFIRM_SAVE:
        return TU_CONFIRM

    d = context.user_data
    # ── Pre-compute (lightweight sync — reserve row before background) ──
    # 0. Capture tier + balance BEFORE write (needed for col C and rate calc)
    current_tier = fetch_member_tier(d["tu_id"])
    prev_bal     = fetch_balance_mins(d["tu_id"])
    tl_row       = next_write_row(topup_sh)

    # 1. Balance = previous balance + mins just added (Phase B — no sheet re-read)
    bal_mins = prev_bal + d["tu_mins"]

    # 2. Pre-compute new effective rate (weighted average)
    try:
        old_rate = fetch_member_effective_rate(d["tu_id"])
        if old_rate <= 0:
            old_rate = fetch_alltime_effective_rate()
        denom    = prev_bal + d["tu_mins"]
        new_rate = round((prev_bal * old_rate + d["tu_amt"]) / denom, 4) if denom > 0 else 0
    except Exception as _e:
        logging.warning("tu_confirm rate pre-calc: %s", _e)
        new_rate = 0

    # Snapshot all fields before clearing user_data
    tu_id       = d["tu_id"];       tu_amt  = d["tu_amt"]
    tu_kpay     = d["tu_kpay"];     tu_cash = d["tu_cash"]
    tu_mins     = d["tu_mins"];     tu_name = d.get("tu_name", "")
    tu_base     = d.get("tu_base_mins", 0)
    tu_bonus    = d.get("tu_bonus_mins", 0)
    tu_phone    = d.get("tu_phone", "-")
    tu_rank_raw = d.get("tu_rank", "Warrior")
    today       = today_str()
    session_snap = d.get("_session_snap")
    after_topup  = d.get("after_topup")
    added_mins   = tu_mins

    r_saved  = display_rank(tu_rank_raw)
    r_em     = rank_emoji(r_saved)
    bal_line = f"\n💰 *Current Balance: {bal_mins:,} mins*" if bal_mins > 0 else ""
    msg = (
        f"✅ *Top Up သိမ်းဆည်းပြီးပါပြီ!*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🪪 *{tu_id}* — {tu_name}\n"
        f"🎖 Rank: *{r_em} {r_saved}*\n"
        f"💰 Amount: *{tu_amt:,} Ks*\n"
        f"⏳ Base: *{tu_base:,} mins*  "
        f"🎁 Bonus: *+{tu_bonus:,} mins*\n"
        f"🔥 Total Added: *{tu_mins:,} mins*\n"
        f"💳 Kpay: *{tu_kpay:,} Ks*  |  💵 Cash: *{tu_cash:,} Ks*"
        f"{bal_line}"
    )

    tu_vid = f"TU-{tu_id}-{now_mmt().strftime('%Y%m%d%H%M%S')}"
    save_receipt_json(tu_vid, {
        "type": "topup", "voucher_id": tu_vid, "date": today,
        "member_id": tu_id, "rank": r_saved, "amount": tu_amt,
        "base_mins": tu_base, "bonus_mins": tu_bonus, "total_mins": tu_mins,
        "kpay": tu_kpay, "cash": tu_cash, "phone": tu_phone,
        "balance_mins": bal_mins, "prev_balance": prev_bal,
        "balance_change": tu_mins, "balance_after": bal_mins,
    })
    receipt_kb = get_receipt_kb(tu_vid)
    context.user_data.clear()

    # ── RECEIPT — sent BEFORE sheet writes ────────────────────────
    await update.message.reply_text(msg, parse_mode="Markdown",
                                    reply_markup=ReplyKeyboardRemove())
    if receipt_kb:
        await update.message.reply_text("🖨️ Receipt ပုံနှိပ်ရန် -", reply_markup=receipt_kb)

    # ── SHEET WRITES — background ─────────────────────────────────
    async def _tu_bg():
        def _do():
            topup_sh.batch_update(
                [{"range": f"A{tl_row}:C{tl_row}",
                  "values": [[today, tu_id, current_tier]]},
                 {"range": f"E{tl_row}:I{tl_row}",
                  "values": [[tu_amt, tu_kpay, tu_cash, tu_mins, "Top Up"]]}],
                value_input_option="USER_ENTERED",
            )
            if new_rate > 0:
                update_member_effective_rate(tu_id, new_rate)
        try:
            await asyncio.to_thread(_do)
        except Exception as _e:
            logging.error("tu_bg_write: %s", _e)
    asyncio.create_task(_tu_bg())

    if after_topup == "console_sale" and session_snap:
        # Restore session data and update wallet balance
        context.user_data.update(session_snap)
        new_wallet  = (session_snap.get("wallet_mins") or 0) + added_mins
        context.user_data["wallet_mins"] = new_wallet
        eff_cost    = session_snap.get("effective_cost_mins", session_snap.get("mins", 0))
        total_mins  = session_snap.get("actual_play_mins", session_snap.get("mins", 0))
        multiplier  = session_snap.get("multiplier", 1.0)
        base_rate   = session_snap.get("base_rate", fetch_base_rate())

        if new_wallet >= eff_cost:
            # Now sufficient after top-up — normal wallet flow
            context.user_data["mins"]      = total_mins
            context.user_data["game_amt"]  = 0
            context.user_data.pop("cash_down_ks", None)
            context.user_data.pop("shortfall_mins", None)
            context.user_data.pop("shortfall_ks", None)
            await update.message.reply_text(
                f"✅ Top Up ပြီးပြီ — balance ပြည့်သည်\n📝 Sales Voucher ဆက်ဖွင့်သည်...",
                reply_markup=ReplyKeyboardRemove(),
            )
        else:
            # Still insufficient — Cash Down for remaining shortfall
            new_shortfall_mins = eff_cost - new_wallet
            new_shortfall_ks   = round(new_shortfall_mins * base_rate / 60)
            wallet_play_mins   = int(new_wallet / multiplier) if multiplier > 0 else new_wallet
            context.user_data["shortfall_mins"] = new_shortfall_mins
            context.user_data["shortfall_ks"]   = new_shortfall_ks
            context.user_data["cash_down_ks"]   = new_shortfall_ks
            context.user_data["game_amt"]        = new_shortfall_ks
            context.user_data["mins"]            = wallet_play_mins
            await update.message.reply_text(
                f"⚠️ Balance ဆက်မလောက်သေးပါ ({new_shortfall_mins} mins ≈ {new_shortfall_ks:,} Ks)\n"
                f"Cash Down အဖြစ် ဆက်သွားပါမည်...",
                reply_markup=ReplyKeyboardRemove(),
            )
        return await prompt_food_menu(update, context)

    return await show_main_menu(update, context)


# ═════════════════════════════════════════
#  DAILY SALES — PROMPT FUNCTIONS
# ═════════════════════════════════════════

async def prompt_member(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        search_results: list | None = None, query: str = ""):
    v_no    = context.user_data["v_no"]
    members = fetch_members()

    if search_results is not None:
        # Filtered view after a search query
        display  = search_results
        hint     = f"🔍 *\"{query}\"* — {len(display)} ရလဒ် တွေ့သည်\n"
    else:
        display  = members
        hint     = "🔍 _ID ရိုက်ပြီး ရှာနိုင်သည်_ (e.g. `PSV_A`)\n" if len(members) > 5 else ""

    # Guest always pinned at top; members below
    kb = [["0 (Guest)"]] + [[m] for m in display] + [[BTN_BACK_MAIN, BTN_CANCEL]]
    await update.message.reply_text(
        step_hdr(1, 6, "Select Member") +
        f"📋 Voucher: *{v_no}*\n\n"
        f"{hint}"
        f"👤 Member ID ရွေးပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
    )
    return MEMBER


async def prompt_console(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m_id        = context.user_data.get("m_id", "")
    is_guest    = m_id.strip() == "0 (Guest)"
    label       = "Guest" if is_guest else m_id
    wallet_mins = context.user_data.get("wallet_mins")

    if not is_guest and wallet_mins is not None:
        balance_line = f"\n💰 *Wallet Balance: {wallet_mins:,} mins*"
        if wallet_mins <= 0:
            balance_line += "  ⚠️ _Wallet ကုန်ဆုံးနေပြီ!_"
    else:
        balance_line = ""

    try:
        _cons = [c["id"] for c in fetch_console_status()]
    except Exception:
        _cons = sorted(VALID_CONSOLES)
    kb  = [_cons[i:i+3] for i in range(0, len(_cons), 3)]
    kb += [NAV_ROW]
    await update.message.reply_text(
        step_hdr(2, 6, "Select Console") +
        f"👤 *{label}*{balance_line}\n\n"
        "🕹️ Console ID ရွေးပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
    )
    return CONSOLE


async def prompt_mins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallet_mins = context.user_data.get("wallet_mins")
    m_id        = context.user_data.get("m_id", "")
    c_id        = context.user_data.get("c_id", "")
    is_guest    = m_id.strip() == "0 (Guest)"
    label       = "Guest" if is_guest else m_id

    if not is_guest and wallet_mins is not None:
        wallet_line = f"\n💰 *Wallet Balance: {wallet_mins:,} mins*"
        if wallet_mins <= 0:
            wallet_line += "  ⚠️ _Wallet ပိုင်ဆိုင်မှုကုန်ဆုံးနေပြီ!_"
    else:
        wallet_line = ""

    kb_mins = [
        ["30", "60", "90"],
        ["120", "150", "180"],
        ["240", "300", "360"],
        NAV_ROW,
    ]
    await update.message.reply_text(
        step_hdr(3, 6, "Play Time (Mins)") +
        f"👤 *{label}*  |  🕹️ *{c_id}*{wallet_line}\n\n"
        f"🕒 Play Mins ကို ရွေးပါ — သို့မဟုတ် ရိုက်ထည့်ပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb_mins, one_time_keyboard=True, resize_keyboard=True),
    )
    return MINS


async def prompt_food_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prices     = context.user_data.get("food_prices", {})
    cart_items = context.user_data.get("food_items", [])
    names      = list(prices.keys())
    rows       = [names[i: i + 2] for i in range(0, len(names), 2)]
    clear_row  = [[BTN_CLEAR_CART]] if cart_items else []
    kb         = rows + [[BTN_DONE]] + clear_row + [NAV_ROW]

    # Price list
    price_lines = [f"  • {n}  —  {p:,} Ks" for n, p in prices.items()]
    price_block = "\n".join(price_lines) if price_lines else "  (menu မရှိပါ)"

    # Running cart (already fetched above)
    if cart_items:
        cart_lines    = [f"  ✓ {i['name']} x{i['qty']} = {i['subtotal']:,} Ks" for i in cart_items]
        cart_subtotal = sum(i["subtotal"] for i in cart_items)
        cart_block = (
            f"\n🛒 *ရွေးပြီးသားပစ္စည်း:*\n"
            + "\n".join(cart_lines)
            + f"\n  ─ Subtotal: *{cart_subtotal:,} Ks*\n"
        )
    else:
        cart_block = ""

    await update.message.reply_text(
        step_hdr(4, 6, "Food & Drinks") +
        f"📋 *Menu & Prices:*\n{price_block}\n"
        f"{cart_block}\n"
        f"🍔 Food & Drink ရွေးပါ (မရှိလျှင် Done ✅) -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
    )
    return FOOD_MENU


async def prompt_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d         = context.user_data
    mins      = d["mins"]
    base_rate = fetch_base_rate()
    d["base_rate"] = base_rate
    is_guest  = d["m_id"].strip() == "0 (Guest)"

    food_lines, food_total = [], 0
    for item in d["food_items"]:
        food_lines.append(f"  • {item['name']} x{item['qty']} = {item['subtotal']:,} Ks")
        food_total += item["subtotal"]
    food_sec = "\n".join(food_lines) if food_lines else "  • မရှိပါ"

    if is_guest:
        multiplier   = fetch_console_multiplier(d.get("c_id", ""))
        game_amt     = round((mins * base_rate * multiplier) / 60)
        net_total    = game_amt + food_total
        mult_display = f"{multiplier:g}"
        d.update(game_amt=game_amt, food_total=food_total,
                 net_total=net_total, remaining_mins=None, multiplier=multiplier)

        body = (
            f"🕹️ Console: *{d.get('c_id', '-')}*\n"
            f"📊 Rate Multiplier: *{mult_display}x*\n"
            f"🎮 Game: {mins} min ({base_rate:,} Ks/hr × {mult_display}) = *{game_amt:,} Ks*\n"
            f"🍔 Food & Drink:\n{food_sec}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 Food Total: *{food_total:,} Ks*\n"
            f"✅ *Net Payable: {net_total:,} Ks*"
        )
        title = "📋 *စာရင်းအချုပ် — Guest*"

    else:
        cash_down_ks = d.get("cash_down_ks", 0)
        actual_mins  = d.get("actual_play_mins", mins)

        if cash_down_ks > 0:
            # Member + Cash Down: shortfall paid as cash
            game_amt  = cash_down_ks
            net_total = game_amt + food_total
            wallet_bal = d.get("wallet_mins", 0)
            d["remaining_mins"] = 0
            d.update(game_amt=game_amt, food_total=food_total, net_total=net_total)
            wallet_block = (
                f"⏳ Wallet: {wallet_bal} mins → 0 (fully used)\n"
                f"🎮 Actual Play: {actual_mins} mins\n"
                f"💵 Cash Down: *{cash_down_ks:,} Ks* (shortfall)\n"
            )
            title = "📋 *စာရင်းအချုပ် — Member + Cash Down*"
        else:
            game_amt  = 0
            net_total = food_total
            wallet_mins       = d.get("wallet_mins")
            effective_cost    = d.get("effective_cost_mins", mins)
            multiplier_val    = d.get("multiplier", 1.0)
            if wallet_mins is not None:
                remaining = wallet_mins - effective_cost
                d["remaining_mins"] = remaining

                # Safety guard — if wallet still insufficient (e.g. stale balance from
                # previous cash-down session), redirect to shortfall screen instead of
                # allowing a negative-balance save.
                if remaining < 0:
                    base_rate_val = d.get("base_rate", fetch_base_rate())
                    shortfall_mins = -remaining
                    shortfall_ks   = round(shortfall_mins * base_rate_val / 60)
                    d["shortfall_mins"] = shortfall_mins
                    d["shortfall_ks"]   = shortfall_ks
                    await update.message.reply_text(
                        f"⚠️ <b>Wallet မလောက်ပါ!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"💳 Balance  : <b>{wallet_mins} mins</b>\n"
                        f"🎮 Cost     : <b>{effective_cost} mins</b>\n"
                        f"❗ Shortfall: <b>{shortfall_mins} mins ≈ {shortfall_ks:,} Ks</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Top Up (သို့) Cash Down ရွေးပါ",
                        parse_mode="HTML",
                    )
                    return await prompt_session_shortfall(update, context)

                mult_tag = f" ×{multiplier_val:g}" if multiplier_val != 1.0 else ""
                wallet_block = (
                    f"⏳ Wallet Balance: {wallet_mins} mins\n"
                    f"🎮 Play: {mins} mins{mult_tag} → Cost: {effective_cost} wallet mins\n"
                    f"📉 Remaining: {remaining} mins\n"
                )
            else:
                d["remaining_mins"] = None
                wallet_block = f"🎮 Playing: {mins} mins\n"
            d.update(game_amt=game_amt, food_total=food_total, net_total=net_total)
            title = "📋 *စာရင်းအချုပ် — Member*"

        body = (
            f"💳 Member: *{d['m_id']}*\n"
            f"{wallet_block}"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🍔 Food & Drink:\n{food_sec}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"✅ *Net Payable: {net_total:,} Ks*"
        )

    text = (
        step_hdr(5, 6, "Review Summary") +
        f"{title}\n━━━━━━━━━━━━━━━━━━\n{body}\n\n"
        f"မှန်ကန်ပါသလား? Yes နှိပ်ပြီး Payment ဆက်သွားပါ -"
    )
    kb   = [[BTN_YES], NAV_ROW]
    await update.message.reply_text(
        text, parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
    )
    return CONFIRM_SUMMARY


async def prompt_kpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d   = context.user_data
    net = d.get("net_total", 0)
    m_id = d.get("m_id", "-")
    c_id = d.get("c_id", "-")
    label = "Guest" if m_id.strip() == "0 (Guest)" else m_id
    await update.message.reply_text(
        step_hdr(6, 6, "Payment — Kpay") +
        f"👤 *{label}*  |  🕹️ *{c_id}*\n"
        f"💰 Grand Total: *{net:,} Ks*\n\n"
        f"💳 Kpay ပမာဏ ရိုက်ပါ (မရှိလျှင် 0) -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], one_time_keyboard=True, resize_keyboard=True),
    )
    return KPAY_AMT


# ═════════════════════════════════════════
#  DAILY SALES — STEP HANDLERS
# ═════════════════════════════════════════

async def step_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK_MAIN:
        return await show_main_menu(update, context)
    if text == BTN_BACK:
        return await prompt_member(update, context)

    members = fetch_members()

    # ── Exact match (keyboard tap or exact type) ──────────────────────────
    if text == "0 (Guest)" or text in members:
        context.user_data["m_id"] = text
        if text != "0 (Guest)":
            context.user_data["wallet_mins"] = fetch_wallet_mins(text)
        else:
            context.user_data["wallet_mins"] = None
        if text != "0 (Guest)":
            return await _check_member_in_session(update, context, text)
        return await prompt_console(update, context)

    # ── Search mode: partial match on ID (case-insensitive) ──────────────
    query   = text
    matches = [m for m in members if query.upper() in m.upper()]

    if len(matches) == 1:
        # Auto-select the single result
        context.user_data["m_id"] = matches[0]
        context.user_data["wallet_mins"] = fetch_wallet_mins(matches[0])
        await update.message.reply_text(
            f"✅ *{matches[0]}* ကို ရွေးချယ်လိုက်သည်",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return await _check_member_in_session(update, context, matches[0])

    if matches:
        # Show filtered keyboard
        return await prompt_member(update, context, search_results=matches, query=query)

    # No match at all
    await update.message.reply_text(
        f"⚠️ *\"{query}\"* နှင့် ကိုက်ညီသော Member မတွေ့ပါ\n"
        f"_ID တစ်စိတ်တစ်ဒေသ ရိုက်ထည့်ပြီး ထပ်ကြိုးစားပါ -_",
        parse_mode="Markdown",
    )
    return await prompt_member(update, context)


async def step_console(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        context.user_data.pop("c_id", None)
        return await prompt_member(update, context)

    if text not in VALID_CONSOLES:
        await update.message.reply_text("⚠️ ကျေးဇူးပြု၍ keyboard မှ Console ID ရွေးပါ -")
        return await prompt_console(update, context)

    context.user_data["c_id"] = text
    return await _check_console_in_session(update, context, text)


async def _check_member_in_session(update, context, member_id: str):
    """Check if member has active session(s). Shows all with per-console + combined options."""
    try:
        consoles = fetch_console_status()
    except Exception:
        return await prompt_console(update, context)

    actives = [
        c for c in consoles
        if c.get("member") == member_id and c.get("status") in ("Active", "Scheduled")
    ]
    if not actives:
        return await prompt_console(update, context)

    # Store all active sessions
    context.user_data["_in_session_consoles"] = actives

    if len(actives) == 1:
        active  = actives[0]
        start_t = active.get("start", "?")
        _, dfmt = calc_duration(start_t) if start_t and start_t != "?" else (0, "?")
        await update.message.reply_text(
            f"⚠️ <b>{member_id}</b> သည် ဆက်ရှိနေဆဲ Session ရှိသည်!\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕹️ Console : <b>{active['id']}</b>\n"
            f"🕐 Start   : <b>{start_t}</b>  ({dfmt})\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Session ကို End ပြီးမှ Sales Voucher ဆက်မလား?",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(
                [[BTN_YES_END_SESSION], [BTN_NO_RESELECT]], resize_keyboard=True),
        )
    else:
        lines = []
        kb    = []
        for c in actives:
            s = c.get("start", "?")
            _, dfmt = calc_duration(s) if s and s != "?" else (0, "?")
            lines.append(f"🕹️ <b>{c['id']}</b>  |  🕐 {s} ({dfmt})")
            kb.append([f"⏹ {c['id']} ကိုပဲ End"])
        kb.append(["⏹ ပေါင်းပြီး End (Combined Bill)"])
        kb.append([BTN_NO_RESELECT])
        await update.message.reply_text(
            f"⚠️ <b>{member_id}</b> — Active Session <b>{len(actives)} ခု</b> ရှိသည်!\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(lines) +
            f"\n━━━━━━━━━━━━━━━━━━\n"
            f"ဘယ် Session ကို End မည်နည်း?",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
    return DS_MEMBER_IN_SESSION


async def _end_single_session_and_launch(update, context, active: dict, m_id: str):
    """End one session and launch the sales voucher for it."""
    bk_id         = active.get("booking_id", "")
    session_cid   = active.get("id", "")
    session_staff = active.get("staff", "")
    start_t       = active.get("start", "")
    total_mins, dur_fmt = calc_duration(start_t) if start_t else (0, "?")
    end_t = now_mmt().strftime("%H:%M")

    # Cancel any pending reminder loop for this console
    _cancel_remind(session_cid, update.effective_chat.id)

    ok = end_booking(bk_id) if bk_id else False
    if ok:
        await update.message.reply_text(
            f"✅ <b>Session ဆုံးပြီ!</b>\n"
            f"🕹️ {session_cid}  👤 {m_id}  ⏱ {dur_fmt} ({total_mins} mins)\n"
            f"🕐 {start_t} → {end_t}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📝 Sales Voucher ဖွင့်နေသည်...",
            parse_mode="HTML", reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await update.message.reply_text(
            "⚠️ Session end မရပါ — data ယူပြီး ဆက်သွားပါမည်", parse_mode="HTML")
    context.user_data.clear()
    return await launch_session_sale(update, context,
                                     session_cid, m_id, total_mins, session_staff)


async def step_ds_member_in_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle session-end choice after member-in-session warning in Daily Sales."""
    text    = update.message.text.strip()
    actives = context.user_data.get("_in_session_consoles", [])
    m_id    = context.user_data.get("m_id", "Guest")

    if text in (BTN_NO_RESELECT, BTN_CANCEL):
        context.user_data.pop("_in_session_consoles", None)
        context.user_data.pop("m_id", None)
        context.user_data.pop("wallet_mins", None)
        return await prompt_member(update, context)

    # Single-session shorthand button
    if text == BTN_YES_END_SESSION and actives:
        context.user_data.pop("_in_session_consoles", None)
        return await _end_single_session_and_launch(update, context, actives[0], m_id)

    # Per-console end: "⏹ C-09 ကိုပဲ End"
    for ac in actives:
        if text == f"⏹ {ac['id']} ကိုပဲ End":
            context.user_data.pop("_in_session_consoles", None)
            return await _end_single_session_and_launch(update, context, ac, m_id)

    # Combined bill: end ALL sessions, sum durations + pre-compute effective cost
    if text == "⏹ ပေါင်းပြီး End (Combined Bill)":
        context.user_data.pop("_in_session_consoles", None)
        total_mins          = 0
        total_effective_mins = 0
        session_staff       = ""
        cid_list            = []
        end_t               = now_mmt().strftime("%H:%M")
        summary_lines       = []
        for ac in actives:
            bk_id   = ac.get("booking_id", "")
            start_t = ac.get("start", "")
            mins, dfmt = calc_duration(start_t) if start_t else (0, "?")
            mult_i  = fetch_console_multiplier(ac["id"])
            eff_i   = round(mins * mult_i)
            total_mins           += mins
            total_effective_mins += eff_i
            end_booking(bk_id)
            cid_list.append(ac["id"])
            if not session_staff:
                session_staff = ac.get("staff", "")
            mult_tag = f" ×{mult_i:g}" if mult_i != 1.0 else ""
            summary_lines.append(
                f"🕹️ {ac['id']}{mult_tag}  ⏱ {dfmt} ({mins} mins → {eff_i} wallet mins)  🕐 {start_t}→{end_t}"
            )
        combined_cid = "+".join(cid_list)
        await update.message.reply_text(
            f"✅ <b>Sessions ဆုံးပြီ! (Combined)</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(summary_lines) +
            f"\n━━━━━━━━━━━━━━━━━━\n"
            f"👤 {m_id}  |  ⏱ Play: <b>{total_mins} mins</b>  |  💳 Cost: <b>{total_effective_mins} wallet mins</b>\n"
            f"📝 Combined Sales Voucher ဖွင့်နေသည်...",
            parse_mode="HTML", reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data.clear()
        return await launch_session_sale(update, context,
                                         combined_cid, m_id, total_mins, session_staff,
                                         pre_effective_mins=total_effective_mins)

    # Unrecognised — re-show (restore actives first)
    context.user_data["_in_session_consoles"] = actives
    return await _check_member_in_session(update, context, m_id)


async def _check_console_in_session(update, context, console_id: str):
    """Check if the chosen console has an active session. If yes → prompt."""
    try:
        consoles = fetch_console_status()
    except Exception:
        return await prompt_mins(update, context)

    active = next(
        (c for c in consoles if c.get("id") == console_id
         and c.get("status") in ("Active", "Scheduled")),
        None,
    )
    if not active:
        return await prompt_mins(update, context)

    context.user_data["_in_session_console"] = active
    start_t  = active.get("start", "?")
    mbr      = active.get("member", "Guest")
    _, dur_fmt = calc_duration(start_t) if start_t and start_t != "?" else (0, "?")
    await update.message.reply_text(
        f"⚠️ <b>{console_id}</b> သည် Active Session ရှိနေသည်!\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 Member : <b>{mbr}</b>\n"
        f"🕐 Start  : <b>{start_t}</b>  ({dur_fmt})\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"ဒီ Session ကို End ပြီးမှ ဆက်မလား?",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([[BTN_YES_END_SESSION], [BTN_NO_RESELECT]],
                                         resize_keyboard=True),
    )
    return DS_CONSOLE_IN_SESSION


async def step_ds_console_in_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Yes/No after console-in-session warning in Daily Sales."""
    text = update.message.text.strip()

    if text == BTN_NO_RESELECT or text == BTN_CANCEL:
        context.user_data.pop("_in_session_console", None)
        context.user_data.pop("c_id", None)
        return await prompt_console(update, context)

    if text == BTN_YES_END_SESSION:
        active        = context.user_data.pop("_in_session_console", {})
        bk_id         = active.get("booking_id", "")
        session_cid   = context.user_data.get("c_id") or active.get("id", "")
        session_mbr   = active.get("member", "Guest")
        session_staff = active.get("staff", "")
        start_t       = active.get("start", "")
        total_mins, dur_fmt = calc_duration(start_t) if start_t else (0, "?")

        ok = end_booking(bk_id) if bk_id else False
        end_t = now_mmt().strftime("%H:%M")
        status_msg = (
            f"✅ <b>Session ဆုံးပြီ!</b>\n"
            f"🕹️ {session_cid}  👤 {session_mbr}  ⏱ {dur_fmt} ({total_mins} mins)\n"
            f"🕐 {start_t} → {end_t}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📝 Sales Voucher ဖွင့်နေသည်..."
        ) if ok else (
            f"⚠️ Session end မရပါ — data ယူပြီး ဆက်သွားပါမည်"
        )
        await update.message.reply_text(status_msg, parse_mode="HTML",
                                        reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        return await launch_session_sale(update, context,
                                         session_cid, session_mbr, total_mins, session_staff)

    # Unrecognised — re-show prompt
    return await _check_console_in_session(update, context,
                                            context.user_data.get("c_id", ""))


async def step_mins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        context.user_data.pop("mins", None)
        return await prompt_console(update, context)

    try:
        mins = int(text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ ဂဏန်းသက်သက် ရိုက်ပေးပါ -")
        return MINS

    if mins <= 0:
        await update.message.reply_text("⚠️ မိနစ် 1 နှင့်အထက် ထည့်ပေးပါ -")
        return MINS

    context.user_data["mins"]       = mins
    context.user_data["food_items"] = []
    context.user_data["base_rate"]  = fetch_base_rate()

    # Fetch food prices and filter out 0-stock items
    food_prices = fetch_food_prices()
    stock_map: dict = {}
    inv_data = _replit_get("sheets/inventory")
    if inv_data:
        stock_map = {i["name"]: max(0, i.get("current_stock", 0)) for i in inv_data.get("items", [])}
        food_prices = {k: v for k, v in food_prices.items() if stock_map.get(k, 1) > 0}
    context.user_data["food_prices"]    = food_prices
    context.user_data["food_stock_map"] = stock_map
    return await prompt_food_menu(update, context)


async def step_food_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text

    if choice == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if choice == BTN_BACK:
        context.user_data.pop("mins", None)
        context.user_data.pop("food_items", None)
        return await prompt_mins(update, context)

    if choice == BTN_DONE:
        return await prompt_confirm(update, context)

    if choice == BTN_CLEAR_CART:
        context.user_data["food_items"] = []
        return await prompt_food_menu(update, context)

    prices = context.user_data.get("food_prices", {})
    if choice not in prices:
        await update.message.reply_text("⚠️ Menu မှ ရွေးချယ်ပါ သို့မဟုတ် Done ✅ နှိပ်ပါ -")
        return await prompt_food_menu(update, context)

    unit_price = prices.get(choice, 0)
    context.user_data["last_food"] = choice

    # Calculate remaining available stock (minus already in cart)
    stock_map   = context.user_data.get("food_stock_map", {})
    total_stock = stock_map.get(choice, 999)
    carted_qty  = sum(i["qty"] for i in context.user_data.get("food_items", []) if i["name"] == choice)
    max_qty     = max(0, total_stock - carted_qty)
    context.user_data["last_food_max"] = max_qty

    qty_btns = [str(q) for q in range(1, min(max_qty + 1, 6))]
    qty_row  = [qty_btns] if qty_btns else []
    stock_note = f"\n📦 Stock ကျန်: *{max_qty} pcs*" if total_stock < 999 else ""
    await update.message.reply_text(
        step_hdr(4, 6, "Food Qty") +
        f"🔢 *{choice}* ({unit_price:,} Ks/ခု){stock_note}\n\nအရေအတွက် ရွေးပါ သို့မဟုတ် ရိုက်ပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            qty_row + [NAV_ROW],
            one_time_keyboard=True, resize_keyboard=True,
        ),
    )
    return FOOD_QTY


async def step_food_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        context.user_data.pop("last_food", None)
        return await prompt_food_menu(update, context)

    try:
        qty = int(text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ ဂဏန်းသက်သက် ရိုက်ပေးပါ -")
        return FOOD_QTY

    if qty <= 0:
        await update.message.reply_text("⚠️ အရေအတွက် 1 နှင့်အထက် ဖြစ်ရမည် -")
        return FOOD_QTY

    name    = context.user_data["last_food"]
    max_qty = context.user_data.get("last_food_max", 999)
    if qty > max_qty:
        await update.message.reply_text(
            f"❌ *{name}* — stock *{max_qty} pcs* သာ ကျန်တော့သည်!\n\n"
            f"{max_qty} နှင့်အောက် ထည့်ပေးပါ -",
            parse_mode="Markdown",
        )
        return FOOD_QTY

    unit_price = context.user_data["food_prices"].get(name, 0)
    context.user_data["food_items"].append({
        "name":       name,
        "qty":        qty,
        "unit_price": unit_price,
        "subtotal":   qty * unit_price,
    })
    return await prompt_food_menu(update, context)


async def step_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text

    if choice == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if choice == BTN_BACK:
        return await prompt_food_menu(update, context)
    if choice == BTN_YES:
        return await prompt_discount(update, context)

    return await prompt_confirm(update, context)


async def step_kpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        context.user_data.pop("kpay", None)
        return await prompt_confirm(update, context)

    try:
        kpay = int(text.replace(",", "").strip())
    except ValueError:
        await update.message.reply_text("⚠️ ဂဏန်းသက်သက် ရိုက်ပေးပါ -")
        return KPAY_AMT

    d = context.user_data
    if kpay > d["net_total"]:
        await update.message.reply_text(
            f"⚠️ Kpay (*{kpay:,} Ks*) သည် စုစုပေါင်း (*{d['net_total']:,} Ks*) ထက် မကျော်ရပါ -",
            parse_mode="Markdown",
        )
        return KPAY_AMT

    cash = d["net_total"] - kpay
    d["kpay"] = kpay
    d["cash"]  = cash

    # Build full Review Your Entry before final save
    m_id       = d.get("m_id", "-")
    c_id       = d.get("c_id", "-")
    play_mins  = d.get("mins", 0)
    game_amt   = d.get("game_amt", 0)
    food_total = d.get("food_total", 0)
    net_total  = d.get("net_total", 0)
    discount   = d.get("discount", 0)
    gross      = d.get("gross_total", net_total)
    mult       = d.get("multiplier", 1.0)
    is_guest   = m_id.strip() == "0 (Guest)"

    food_lines_review = [
        f"  • {i['name']} x{i['qty']} = {i['subtotal']:,} Ks"
        for i in d.get("food_items", [])
    ]
    food_sec_review = "\n".join(food_lines_review) if food_lines_review else "  • မရှိ"

    member_ln = "👤 Guest" if is_guest else f"💳 Member: *{m_id}*"
    game_ln   = (
        f"🎮 {play_mins} mins × {mult:g}x = *{game_amt:,} Ks*"
        if is_guest else f"🎮 Play: *{play_mins} mins* (Wallet deducted)"
    )
    disc_ln = f"💸 Discount: *-{discount:,} Ks*\n" if discount > 0 else ""

    kb = [[BTN_CONFIRM_SAVE], NAV_ROW]
    await update.message.reply_text(
        f"📋 *Review Your Entry — Daily Sales*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{member_ln}\n"
        f"🕹️ Console: *{c_id}*  |  {game_ln}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🍔 Food & Drink:\n{food_sec_review}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🧾 Game: *{game_amt:,} Ks*  |  Food: *{food_total:,} Ks*\n"
        f"{'💰 Gross: *' + f'{gross:,}' + ' Ks*  →  ' if discount > 0 else ''}"
        f"{disc_ln}"
        f"💰 Net Payable: *{net_total:,} Ks*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💳 Kpay: *{kpay:,} Ks*  |  💵 Cash: *{cash:,} Ks*\n\n"
        f"မှန်ကန်ပါသလား? ✅ Confirm & Save နှိပ်ပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SALE_CONFIRM


async def step_sale_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await prompt_kpay(update, context)

    if text != BTN_CONFIRM_SAVE:
        return SALE_CONFIRM

    d     = context.user_data
    kpay  = d.get("kpay", 0)
    cash  = d.get("cash", 0)
    today = today_str()

    v_no       = d["v_no"]
    m_id       = d.get("m_id", "-")
    c_id       = d.get("c_id", "-")
    play_mins  = d.get("mins", 0)
    game_amt   = d.get("game_amt", 0)
    food_total = d.get("food_total", 0)
    net_total  = d.get("net_total", 0)
    mult       = d.get("multiplier", 1.0)
    is_guest   = m_id.strip() == "0 (Guest)"
    wallet_before = d.get("wallet_mins")   # balance before this session (None for guests)

    discount = d.get("discount", 0)

    # ── Pre-compute (lightweight sync) ────────────────────────────
    s_row      = next_write_row(sales_sh)   # reserve row before background write
    staff_name = d.get("staff", "")
    food_costs = fetch_food_costs()
    food_sold  = list(d.get("food_items", []))

    # Wallet deduct: play_mins × multiplier (Phase B — no sheet read needed)
    wallet_deduct  = round(play_mins * mult)
    remaining_mins = (wallet_before - wallet_deduct) if not is_guest and wallet_before is not None else None

    # Build receipt strings before clearing user_data
    food_lines_receipt = [
        f"  • {i['name']} x{i['qty']} = {i['subtotal']:,} Ks" for i in food_sold
    ]
    food_sec_receipt = "\n".join(food_lines_receipt) if food_lines_receipt else "  • မရှိ"
    member_ln = "👤 Guest" if is_guest else f"💳 Member: *{m_id}*"
    game_ln   = (f"🎮 Game: {play_mins} mins × {mult:g}x = *{game_amt:,} Ks*" if is_guest
                 else f"🎮 Play: *{play_mins} mins*  |  Wallet: *-{wallet_deduct} mins*")
    wallet_bal_line = (f"\n💰 *Remaining Balance: {remaining_mins:,} mins*"
                       if not is_guest and remaining_mins is not None else "")
    receipt_kb  = get_receipt_kb(v_no)
    staff_line  = f"\n👤 Staff: *{staff_name}*" if staff_name else ""

    # Save receipt JSON (local disk — instant)
    save_receipt_json(v_no, {
        "type":           "sale",
        "voucher_id":     v_no,
        "date":           today,
        "member_id":      m_id,
        "console_id":     c_id,
        "play_mins":      play_mins,
        "game_amt":       game_amt,
        "food_items":     food_sold,
        "food_total":     food_total,
        "net_total":      net_total,
        "kpay":           kpay,
        "cash":           cash,
        "multiplier":     mult,
        "is_guest":       is_guest,
        "prev_balance":   wallet_before,
        "balance_change": -wallet_deduct if not is_guest else None,
        "balance_after":  remaining_mins,
    })
    context.user_data.clear()

    # ── RECEIPT — sent BEFORE sheet writes ────────────────────────
    await update.message.reply_text(
        f"✅ *{v_no} သိမ်းဆည်းပြီးပါပြီ!*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{member_ln}{staff_line}\n"
        f"🕹️ Console: *{c_id}*  |  {game_ln}\n"
        f"🍔 Food & Drink:\n{food_sec_receipt}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🧾 Game: *{game_amt:,} Ks*  |  Food: *{food_total:,} Ks*\n"
        f"💰 Grand Total: *{net_total:,} Ks*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💳 Kpay: *{kpay:,} Ks*  |  💵 Cash: *{cash:,} Ks*"
        f"{wallet_bal_line}",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    if receipt_kb:
        await update.message.reply_text("🖨️ Receipt ပုံနှိပ်ရန် -", reply_markup=receipt_kb)

    # ── SHEET WRITES — background (user already has receipt) ──────
    _disc = discount if discount else ""
    async def _sale_bg():
        def _do():
            sales_sh.batch_update(
                [{"range": f"A{s_row}:K{s_row}",
                  "values": [[today, v_no, m_id, c_id, play_mins,
                              game_amt, food_total, _disc, net_total, kpay, cash]]},
                 {"range": f"O{s_row}", "values": [[staff_name]]}],
                value_input_option="USER_ENTERED",
            )
            for item in food_sold:
                cp = food_costs.get(item["name"], 0)
                stock_sh.append_row(
                    [today, v_no, item["name"], item["qty"],
                     item.get("unit_price", 0), item.get("subtotal", 0), cp, cp * item["qty"]],
                    value_input_option="USER_ENTERED",
                )
            if food_sold:
                _update_inv_total_k1()
                _replit_get("sheets/inventory?nocache=1")
        try:
            await asyncio.to_thread(_do)
        except Exception as _e:
            logging.error("sale_bg_write: %s", _e)
    asyncio.create_task(_sale_bg())

    # ── Waitlist notify (non-blocking) ───────────────────────────────────────
    _wl_cid = d.get("c_id", "")
    if _wl_cid and _wl_cid not in ("-", ""):
        async def _wl_notify():
            try:
                resp = await asyncio.to_thread(
                    _replit_post, "waitlist/notify", {"console_id": _wl_cid}
                )
                if resp and resp.get("notified"):
                    logging.info("Waitlist notified: %s for console %s",
                                 resp.get("entry", {}).get("customer_name", "?"), _wl_cid)
            except Exception as _e:
                logging.warning("waitlist notify error: %s", _e)
        asyncio.create_task(_wl_notify())

    # ── Low balance alert (non-blocking, member only) ────────────────────────
    if not is_guest:
        asyncio.create_task(_check_low_balance_alert(m_id, c_id))

    return await show_main_menu(update, context)


# ═════════════════════════════════════════
#  PAYROLL CALCULATION
# ═════════════════════════════════════════

def calc_monthly_payroll(month_str: str | None = None) -> list[dict]:
    """
    Calculate monthly payroll for all staff.
    month_str format: 'YYYY-MM' (default = current month).
    Rules:
      - New Member card: 1,500 Ks per card registered
      - Game play bonus (BUSINESS-WIDE total mins):
          ≥ 90,000 mins (1,500 hrs) → 50,000 Ks each
          ≥ 120,000 mins (2,000 hrs) → 100,000 Ks each
      - Food & Drink: daily TOTAL ≥ 50,000 Ks
          → 5% of amount EXCEEDING 50,000 each day
    """
    if month_str is None:
        month_str = now_mmt().strftime("%Y-%m")
    year_i, mon_i = int(month_str[:4]), int(month_str[5:7])

    staff_list = fetch_staff()
    if not staff_list:
        return []

    # daily_food_total: date_key → total F&D sales for that day (ALL staff combined)
    daily_food_total: dict[str, int] = {}
    total_play_mins = 0   # BUSINESS-WIDE sum — col O may be empty for older sessions

    def _parse_date(val: str):
        for fmt in ("%m/%d/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(val.strip(), fmt)
            except ValueError:
                pass
        # fallback: try splitting manually for M/D/YYYY
        try:
            parts = val.strip().split("/")
            if len(parts) == 3:
                return datetime(int(parts[2]), int(parts[0]), int(parts[1]))
        except Exception:
            pass
        return None

    # ── Sales_Daily: col E=PlayMins (idx4), G=FoodTotal (idx6) ──
    try:
        sales_rows = sales_sh.get_all_values()
        for row in sales_rows[1:]:
            if len(row) < 7:
                continue
            d = _parse_date(row[0])
            if not d or d.year != year_i or d.month != mon_i:
                continue
            day_key = d.strftime("%Y-%m-%d")

            # Sum ALL play_mins for the month (regardless of staff field)
            total_play_mins += _int(row[4])

            # Food — accumulate daily TOTAL regardless of staff
            daily_food_total[day_key] = daily_food_total.get(day_key, 0) + _int(row[6])
    except Exception as e:
        logging.warning("calc_monthly_payroll sales read: %s", e)

    # ── TopUp_Log: count total new member registrations for the month ──
    total_nm_count = 0
    try:
        topup_rows = topup_sh.get_all_values()
        for row in topup_rows[1:]:
            if len(row) < 9:
                continue
            if row[8].strip() != "First Purchase":
                continue
            d = _parse_date(row[0].strip())
            if not d or d.year != year_i or d.month != mon_i:
                continue
            total_nm_count += 1
    except Exception as e:
        logging.warning("calc_monthly_payroll topup read: %s", e)

    # ── Shared commissions (same for ALL staff) ──
    # New Member: total cards × 1,500 each
    shared_nm_comm = total_nm_count * 1500

    # Food & Drink: days where cafe total ≥ 50,000 → 5% on amount ABOVE 50,000
    # e.g. 60,000 → (60,000-50,000)*5% = 500; 120,000 → 70,000*5% = 3,500
    food_days_qualified = 0
    shared_food_comm    = 0
    for daily_total in daily_food_total.values():
        if daily_total >= 50000:
            shared_food_comm += int((daily_total - 50000) * 0.05)
            food_days_qualified += 1

    base_salaries = fetch_base_salaries()
    attendance    = fetch_attendance(month_str)

    # Business-wide play bonus (same for all staff — total mins not per-staff)
    play_hrs_total = round(total_play_mins / 60, 1)
    game_bonus_shared = (
        100000 if total_play_mins >= 120000 else
        (50000 if total_play_mins >= 90000 else 0)
    )

    payroll = []
    for s in staff_list:
        commission = game_bonus_shared + shared_nm_comm + shared_food_comm
        base_sal   = base_salaries.get(s, 0)

        att = attendance.get(s, {})
        leave_days      = att.get("leave_days", 0)
        late_count      = att.get("late_count", 0)
        deduct_per_late = att.get("deduct_per_late", 500)
        leave_deduct    = int((base_sal / 26) * leave_days) if base_sal > 0 and leave_days > 0 else 0
        late_deduct     = late_count * deduct_per_late
        total_deduct    = leave_deduct + late_deduct
        net_total       = base_sal + commission - total_deduct

        payroll.append({
            "staff":               s,
            "base_salary":         base_sal,
            "play_hrs":            play_hrs_total,
            "play_mins":           total_play_mins,
            "game_bonus":          game_bonus_shared,
            "nm_count":            total_nm_count,
            "nm_commission":       shared_nm_comm,
            "food_commission":     shared_food_comm,
            "food_days_qualified": food_days_qualified,
            "total_commission":    commission,
            "leave_days":          leave_days,
            "late_count":          late_count,
            "deduct_per_late":     deduct_per_late,
            "leave_deduct":        leave_deduct,
            "late_deduct":         late_deduct,
            "total_deduct":        total_deduct,
            "grand_total":         net_total,
            "advance":             0,          # filled below
            "net_total":           net_total,  # remaining = grand_total - advance
        })

    # Attach salary advances for this month
    try:
        advances = fetch_salary_advances(month_str)
        for p in payroll:
            adv_data       = advances.get(p["staff"], {"total": 0, "cash": 0, "kpay": 0})
            p["advance"]      = adv_data["total"]
            p["advance_cash"] = adv_data["cash"]
            p["advance_kpay"] = adv_data["kpay"]
            p["net_total"]    = max(0, p["grand_total"] - adv_data["total"])
    except Exception as e:
        logging.warning("calc_monthly_payroll advances: %s", e)

    return payroll


async def cmd_payroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show monthly payroll breakdown for all staff."""
    month_str   = now_mmt().strftime("%Y-%m")
    month_label = now_mmt().strftime("%B %Y")
    await update.message.reply_text("⏳ Payroll တွက်နေသည်...", reply_markup=ReplyKeyboardRemove())
    try:
        payroll = calc_monthly_payroll(month_str)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return await show_main_menu(update, context)

    if not payroll:
        await update.message.reply_text(
            "⚠️ Staff ဒေတာ မရှိပါ\n\n"
            "Google Sheet → *Setting* tab:\n"
            "• S2:S3 — Staff Name ထည့်ပါ\n"
            "• T2:T3 — Base Salary ထည့်ပါ",
            parse_mode="Markdown",
        )
        return await show_main_menu(update, context)

    lines = [f"💼 *Salary & Payroll — {month_label}*\n━━━━━━━━━━━━━━━━━━"]
    for p in payroll:
        if p["play_mins"] >= 120000:
            bonus_note = "🏆 ≥2,000 hrs"
        elif p["play_mins"] >= 90000:
            bonus_note = "🎯 ≥1,500 hrs"
        else:
            hrs_left = round((90000 - p["play_mins"]) / 60, 1)
            bonus_note = f"_{hrs_left:,} hrs လိုသေး_"
        base_line = f"💵 Base Salary    : *{p['base_salary']:,} Ks*\n" if p["base_salary"] > 0 else ""

        # Deduction lines
        deduct_lines = ""
        if p["leave_days"] > 0 or p["late_count"] > 0:
            deduct_lines += f"─────────────────\n"
        if p["leave_days"] > 0:
            deduct_lines += f"📅 ခွင့်ယူ        : *{p['leave_days']} ရက်* → *-{p['leave_deduct']:,} Ks*\n"
        if p["late_count"] > 0:
            deduct_lines += f"⏰ နောက်ကျ       : *{p['late_count']} ကြိမ်* × {p['deduct_per_late']:,} → *-{p['late_deduct']:,} Ks*\n"
        if p["total_deduct"] > 0:
            deduct_lines += f"📉 ဖြတ်တောက်     : *-{p['total_deduct']:,} Ks*\n"

        lines.append(
            f"\n👤 *{p['staff']}*\n"
            f"─────────────────\n"
            f"{base_line}"
            f"🎮 Game Play     : *{p['play_hrs']:,.1f} hrs*  {bonus_note}\n"
            f"   Play Bonus   : *{p['game_bonus']:,} Ks*\n"
            f"🆕 New Members   : *{p['nm_count']} cards* → *{p['nm_commission']:,} Ks*\n"
            f"🍔 Food Comm     : *{p['food_days_qualified']} day(s)* → *{p['food_commission']:,} Ks*\n"
            f"📊 Commission    : *{p['total_commission']:,} Ks*\n"
            f"{deduct_lines}"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 *Gross Payable  : {p['grand_total']:,} Ks*"
            + (
                f"\n💸 Advance Paid    : *-{p['advance']:,} Ks*\n"
                f"💵 *Remaining Pay  : {p['net_total']:,} Ks*"
                if p.get("advance", 0) > 0 else ""
            )
        )
    lines.append("\n\n_/setattend နဲ့ ခွင့်/နောက်ကျ ထည့်နိုင်သည်_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return await show_main_menu(update, context)


# ─────────────────────────────────────────
#  CANCEL
# ─────────────────────────────────────────
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ ဖျက်သိမ်းလိုက်ပါပြီ။", reply_markup=ReplyKeyboardRemove()
    )
    return await show_main_menu(update, context)


# ─────────────────────────────────────────
#  DISCOUNT STEP (Daily Sales)
# ─────────────────────────────────────────

async def prompt_discount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d     = context.user_data
    gross = d.get("net_total", 0)          # net_total before discount = gross
    d["gross_total"] = gross               # store for review screen
    d.setdefault("discount", 0)
    kb = [[BTN_SKIP_DISC], NAV_ROW]
    await update.message.reply_text(
        f"💸 *Discount ထည့်မလား?*\n\n"
        f"💰 Gross Total: *{gross:,} Ks*\n\n"
        f"Discount ပမာဏ ရိုက်ပါ (ဥပမာ 500)\n"
        f"သို့မဟုတ် *Skip* နှိပ်ပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return DISCOUNT


async def step_discount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await prompt_confirm(update, context)
    if text == BTN_SKIP_DISC:
        context.user_data["discount"] = 0
        return await prompt_kpay(update, context)

    try:
        disc = int(text.replace(",", "").strip())
    except ValueError:
        await update.message.reply_text("⚠️ ဂဏန်းသက်သက် ရိုက်ပေးပါ -")
        return DISCOUNT

    d     = context.user_data
    gross = d.get("gross_total", d.get("net_total", 0))
    if disc < 0 or disc >= gross:
        await update.message.reply_text(
            f"⚠️ Discount (*{disc:,} Ks*) သည် Gross (*{gross:,} Ks*) ထက် မကျော်ရပါ -",
            parse_mode="Markdown",
        )
        return DISCOUNT

    d["discount"]  = disc
    d["net_total"] = gross - disc
    await update.message.reply_text(
        f"✅ Discount *{disc:,} Ks* ထည့်ပြီး\n"
        f"💰 Net Payable: *{d['net_total']:,} Ks*",
        parse_mode="Markdown",
    )
    return await prompt_kpay(update, context)


# ─────────────────────────────────────────
#  COMMAND SHORTCUTS
# ─────────────────────────────────────────
async def cmd_sales_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: /sales — new sale entry."""
    context.user_data.clear()
    context.user_data["v_no"] = next_voucher()
    context.user_data["staff"] = ""
    return await prompt_member(update, context)


async def cmd_topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: /topup — jump to Top Up member selection."""
    context.user_data.clear()
    return await prompt_tu_member(update, context)


async def cmd_member_mgmt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: /member — Member Management menu."""
    context.user_data.clear()
    return await show_mm_menu(update, context)


async def cmd_check_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: /check — jump straight to member lookup."""
    context.user_data.clear()
    return await prompt_mm_lookup(update, context)


async def cmd_newmember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: /newmember — jump straight to new member registration."""
    context.user_data.clear()
    context.user_data["nm_staff"] = ""
    return await prompt_nm_name(update, context)


async def cmd_ranks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: /ranks — show rank tier info."""
    context.user_data.clear()
    return await show_rank_info(update, context)


async def cmd_stockin_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: /stockin — PIN verify then Stock In."""
    context.user_data.clear()
    context.user_data["stock_dest"] = "stockin"
    await update.message.reply_text(
        "🔐 *Stock In — PIN လိုအပ်သည်*\n\nPIN နံပါတ် ထည့်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STOCK_PIN


async def cmd_stockout_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: /stockout — PIN verify then Stock Out."""
    context.user_data.clear()
    context.user_data["stock_dest"] = "stockout"
    await update.message.reply_text(
        "🔐 *Stock Out — PIN လိုအပ်သည်*\n\nPIN နံပါတ် ထည့်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STOCK_PIN


async def cmd_stock_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: /stock — PIN verify then Stock menu."""
    context.user_data.clear()
    context.user_data["stock_dest"] = "menu"
    await update.message.reply_text(
        "🔐 *Stock Update — PIN လိုအပ်သည်*\n\nPIN နံပါတ် ထည့်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STOCK_PIN


# ═════════════════════════════════════════
#  ATTENDANCE WIZARD (/setattend)
# ═════════════════════════════════════════

BTN_ATTEND_DONE  = "✅ ပြီးပါပြီ"
BTN_ATTEND_NEXT  = "➡️ နောက် Staff"
BTN_ATTEND_SKIP  = "⏩ Skip (0)"


# ═════════════════════════════════════════
#  FINANCE MODULE
# ═════════════════════════════════════════

OPEX_CATEGORIES    = [
    "Rent", "Electricity & Water", "Internet", "Salary Payroll",
    "Marketing", "Charges", "Maintenance", "Petty Cash", "Other",
]
ASSET_CATEGORIES   = ["Electronics", "Furniture", "Equipment", "Vehicles", "Other"]
PREPAID_CATEGORIES = ["Rent", "Insurance", "Subscription", "License", "Other"]
FINANCE_ACCOUNTS   = ["Cash Box", "KBZ Bank", "MMQR", "AYA Bank"]
PAY_METHODS        = ["Cash", "KPay", "Bank Transfer"]


def get_opex_sh():
    return wb.worksheet("OPEX_Log")

def get_assets_sh():
    return wb.worksheet("Assets_Register")

def get_prepaid_fin_sh():
    return wb.worksheet("Prepaid_Expenses")

def get_acct_trf_sh():
    return wb.worksheet("Account_Transfers")

def get_payables_sh():
    return wb.worksheet("Payables")

def get_receivables_sh():
    return wb.worksheet("Receivables")

def get_advpay_sh():
    return wb.worksheet("Advance_Payments")


# ─────────────────────────────────────────
#  Finance Main Menu
# ─────────────────────────────────────────

async def show_finance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Finance Management sub-menu."""
    kb = [
        # ── Capital & Equity ──
        [BTN_FIN_CAPITAL, BTN_FIN_SHAREHOLDER, BTN_FIN_TRANSFER],
        # ── Record Expenses / Assets ──
        [BTN_FIN_OPEX,    BTN_FIN_ASSET,       BTN_FIN_ASSET_DISPOSE],
        [BTN_FIN_PREPAID],
        # ── Payables / Receivables / Advances ──
        [BTN_FIN_PAYABLE, BTN_FIN_SETTLE_PAY],
        [BTN_FIN_RECEIVABLE, BTN_FIN_SETTLE_REC],
        [BTN_FIN_ADVPAY,  BTN_FIN_SETTLE_ADVPAY],
        # ── Reports ──
        [BTN_FIN_ACCTS,   BTN_FIN_REPORT],
        # ── Admin ──
        [BTN_FIN_SETUP,   BTN_BACK_MAIN],
    ]
    await update.message.reply_text(
        "💼 *Finance Management*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Action ရွေးပါ ↓",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return FINANCE_MENU


async def step_finance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route Finance menu choices."""
    choice = update.message.text.strip()
    if choice == BTN_BACK_MAIN:
        return await show_main_menu(update, context)
    if choice == BTN_FIN_CAPITAL:
        return await prompt_cap_acct(update, context)
    if choice == BTN_FIN_SHAREHOLDER:
        return await show_shareholder_menu(update, context)
    if choice == BTN_FIN_OPEX:
        return await prompt_opex_cat(update, context)
    if choice == BTN_FIN_ASSET:
        return await prompt_asset_name(update, context)
    if choice == BTN_FIN_ASSET_DISPOSE:
        return await prompt_asset_dispose_sel(update, context)
    if choice == BTN_FIN_PREPAID:
        return await prompt_prepaid_desc(update, context)
    if choice == BTN_FIN_TRANSFER:
        return await prompt_acct_trf_from(update, context)
    if choice == BTN_FIN_PAYABLE:
        return await prompt_pay_vendor(update, context)
    if choice == BTN_FIN_RECEIVABLE:
        return await prompt_rec_cust(update, context)
    if choice == BTN_FIN_SETTLE_PAY:
        return await show_settle_list(update, context, "payable")
    if choice == BTN_FIN_SETTLE_REC:
        return await show_settle_list(update, context, "receivable")
    if choice == BTN_FIN_ADVPAY:
        return await prompt_advpay_party(update, context)
    if choice == BTN_FIN_SETTLE_ADVPAY:
        return await show_advpay_settle(update, context)
    if choice == BTN_FIN_ACCTS:
        return await cmd_fin_accts(update, context)
    if choice == BTN_FIN_REPORT:
        return await show_fin_report_menu(update, context)
    if choice == BTN_FIN_SETUP:
        return await cmd_finance_setup(update, context)
    return await show_finance_menu(update, context)


# ─────────────────────────────────────────
#  OPEX Entry Flow
# ─────────────────────────────────────────

async def prompt_opex_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("fin", {})["flow"] = "opex"
    kb = [[c] for c in OPEX_CATEGORIES] + [NAV_ROW]
    await update.message.reply_text(
        "📝 *OPEX ထည့် — အမျိုးအစား*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "လည်ပတ်ကုန်ကျစရိတ် အမျိုးအစား ရွေးပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return OPEX_CAT


async def step_opex_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_finance_menu(update, context)
    if text not in OPEX_CATEGORIES:
        await update.message.reply_text("⚠️ အောက်မှ ရွေးပါ")
        return OPEX_CAT
    context.user_data["fin"]["opex_cat"] = text
    kb = [["⏩ Skip"], NAV_ROW]
    await update.message.reply_text(
        f"📝 *{text}* — အသေးစိတ်ဖော်ပြချက်\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ဖော်ပြချက် ရိုက်ပါ (သို့) Skip:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return OPEX_DESC


async def step_opex_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await prompt_opex_cat(update, context)
    context.user_data["fin"]["opex_desc"] = "" if text == "⏩ Skip" else text
    await update.message.reply_text(
        "💰 *ကုန်ကျစရိတ် ပမာဏ (Ks)*\n\nAmount ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return OPEX_AMT


async def step_opex_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await prompt_opex_cat(update, context)
    try:
        amt = int(text.replace(",", ""))
        if amt <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ မှန်ကန်သော ပမာဏ ရိုက်ပါ")
        return OPEX_AMT
    context.user_data["fin"]["opex_amt"] = amt
    kb = [[a] for a in FINANCE_ACCOUNTS] + [NAV_ROW]
    await update.message.reply_text(
        f"💰 *{amt:,} Ks* — ငွေ Account ရွေးပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return OPEX_ACCT


async def step_opex_acct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "ပမာဏ ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
        )
        return OPEX_AMT
    if text not in FINANCE_ACCOUNTS:
        await update.message.reply_text("⚠️ Account ရွေးပါ")
        return OPEX_ACCT
    d = context.user_data["fin"]
    d["opex_acct"] = text
    # Auto-derive pay method from account
    _pay_map = {"Cash Box": "Cash", "MMQR": "KPay", "KBZ Bank": "Bank Transfer", "AYA Bank": "Bank Transfer"}
    d["opex_pay"] = _pay_map.get(text, "Cash")
    kb = [[BTN_CONFIRM_SAVE], NAV_ROW]
    await update.message.reply_text(
        "📝 <b>OPEX မှတ်တမ်း — အတည်ပြုချက်</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📌 Category : <b>{d['opex_cat']}</b>\n"
        f"📋 Note     : <b>{d.get('opex_desc','') or '—'}</b>\n"
        f"💰 Amount   : <b>{d['opex_amt']:,} Ks</b>\n"
        f"🏦 Account  : <b>{d['opex_acct']}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "မှန်ကန်ပါသလား? ✅ Confirm &amp; Save နှိပ်ပါ",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return OPEX_CONFIRM


async def step_opex_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [[a] for a in FINANCE_ACCOUNTS] + [NAV_ROW]
        await update.message.reply_text(
            "Account ပြန်ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return OPEX_ACCT
    if text not in PAY_METHODS:
        await update.message.reply_text("⚠️ ငွေပေးပုံ ရွေးပါ")
        return OPEX_PAY
    d = context.user_data["fin"]
    d["opex_pay"] = text
    kb = [[BTN_CONFIRM_SAVE], NAV_ROW]
    await update.message.reply_text(
        "📝 *OPEX မှတ်တမ်း — အတည်ပြုချက်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📌 Category : *{d['opex_cat']}*\n"
        f"📋 Note     : *{d.get('opex_desc','') or '—'}*\n"
        f"💰 Amount   : *{d['opex_amt']:,} Ks*\n"
        f"🏦 Account  : *{d['opex_acct']}*\n"
        f"💳 Payment  : *{d['opex_pay']}*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "မှန်ကန်ပါသလား? ✅ Confirm & Save နှိပ်ပါ",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return OPEX_CONFIRM


async def step_opex_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [[a] for a in FINANCE_ACCOUNTS] + [NAV_ROW]
        await update.message.reply_text(
            "Account ပြန်ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return OPEX_ACCT
    if text != BTN_CONFIRM_SAVE:
        await update.message.reply_text("✅ Confirm & Save နှိပ်ပါ")
        return OPEX_CONFIRM
    d = context.user_data["fin"]
    await update.message.reply_text("⏳ သိမ်းဆည်းနေသည်...")
    try:
        def _do():
            sh = get_opex_sh()
            sh.append_row(
                [today_str(), d["opex_cat"], d.get("opex_desc", ""),
                 d["opex_amt"], d["opex_acct"], d["opex_pay"]],
                value_input_option="USER_ENTERED",
            )
        await asyncio.to_thread(_do)
        await update.message.reply_text(
            f"✅ *OPEX မှတ်တမ်း သိမ်းဆည်းပြီး!*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 {d['opex_cat']}  |  💰 {d['opex_amt']:,} Ks\n"
            f"🏦 {d['opex_acct']}  |  💳 {d['opex_pay']}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return await show_finance_menu(update, context)


# ─────────────────────────────────────────
#  Asset Entry Flow
# ─────────────────────────────────────────

async def prompt_asset_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("fin", {})["flow"] = "asset"
    await update.message.reply_text(
        "🏢 *Asset မှတ်တမ်း — Asset အမည်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Asset အမည် ရိုက်ပါ\n(ဥပမာ: PS5 Console #3):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return ASSET_NAME


async def step_asset_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_finance_menu(update, context)
    if not text:
        await update.message.reply_text("⚠️ Asset အမည် ထည့်ပါ")
        return ASSET_NAME
    context.user_data["fin"]["asset_name"] = text
    kb = [[c] for c in ASSET_CATEGORIES] + [NAV_ROW]
    await update.message.reply_text(
        f"🏢 *{text}* — အမျိုးအစား ရွေးပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ASSET_CAT


async def step_asset_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await prompt_asset_name(update, context)
    if text not in ASSET_CATEGORIES:
        await update.message.reply_text("⚠️ Category ရွေးပါ")
        return ASSET_CAT
    context.user_data["fin"]["asset_cat"] = text
    today = now_mmt().strftime("%-m/%-d/%Y")
    kb = [[today], NAV_ROW]
    await update.message.reply_text(
        "📅 *ဝယ်ယူသည့် ရက်စွဲ (M/D/YYYY)*\n"
        f"ဥပမာ: {today}",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ASSET_DATE


async def step_asset_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [[c] for c in ASSET_CATEGORIES] + [NAV_ROW]
        await update.message.reply_text(
            "Category ပြန်ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return ASSET_CAT
    context.user_data["fin"]["asset_date"] = text
    await update.message.reply_text(
        "💰 *Asset ဝယ်ယူစျေး (Ks)*\n\nAmount ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return ASSET_COST


async def step_asset_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        today = now_mmt().strftime("%-m/%-d/%Y")
        await update.message.reply_text(
            "ရက်စွဲ ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([[today], NAV_ROW], resize_keyboard=True),
        )
        return ASSET_DATE
    try:
        cost = int(text.replace(",", ""))
        if cost <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ ပမာဏ မှန်ကန်စွာ ရိုက်ပါ")
        return ASSET_COST
    context.user_data["fin"]["asset_unit_cost"] = cost
    kb = [["1"], ["2"], ["3"], ["5"], ["10"], NAV_ROW]
    await update.message.reply_text(
        "🔢 *Qty (အရေအတွက်)*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Unit Cost: *{cost:,} Ks*\n\n"
        "မည်သည့် အရေအတွက် ဝယ်ယူသည်?\n(ဥပမာ — PS5 ၃ လုံး → 3):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ASSET_QTY


async def step_asset_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "Unit Cost ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
        )
        return ASSET_COST
    try:
        qty = int(text.replace(",", ""))
        if qty <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ အရေအတွက် (ကိန်းဂဏန်း) ရိုက်ပါ")
        return ASSET_QTY
    context.user_data["fin"]["asset_qty"] = qty
    kb = [["3"], ["5"], ["10"], NAV_ROW]
    await update.message.reply_text(
        "📅 *Useful Life (နှစ်)*\n"
        "သုံးစွဲမည့် သက်တမ်း နှစ်အရေအတွက် ရိုက်ပါ\n(ဥပမာ: 3, 5, 10):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ASSET_LIFE


async def step_asset_life(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        unit_cost = context.user_data["fin"].get("asset_unit_cost", 0)
        kb = [["1"], ["2"], ["3"], ["5"], ["10"], NAV_ROW]
        await update.message.reply_text(
            f"Qty ပြန်ရိုက်ပါ (Unit Cost: {unit_cost:,} Ks):",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return ASSET_QTY
    try:
        life = int(text)
        if life <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ နှစ် (ကိန်းဂဏန်း) ရိုက်ပါ")
        return ASSET_LIFE
    context.user_data["fin"]["asset_life"] = life
    cost = context.user_data["fin"].get("asset_unit_cost", 0)
    salvage_hint = int(cost * 0.1)
    kb = [[str(salvage_hint)], ["0"], NAV_ROW]
    await update.message.reply_text(
        "♻️ *Salvage Value (Ks)*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ပျက်စီးပြီးနောက် ရောင်းချနိုင်မည့် တန်ဖိုး\n"
        f"မသိပါက *0* ရိုက်ပါ\n"
        f"(အကြံ 10%: {salvage_hint:,} Ks)",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ASSET_SALVAGE


async def step_asset_salvage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [["3"], ["5"], ["10"], NAV_ROW]
        await update.message.reply_text(
            "Useful Life ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return ASSET_LIFE
    try:
        salvage = int(text.replace(",", ""))
    except ValueError:
        await update.message.reply_text("⚠️ ဂဏန်း ရိုက်ပါ")
        return ASSET_SALVAGE
    d = context.user_data["fin"]
    d["asset_salvage"] = salvage
    kb = [[a] for a in FINANCE_ACCOUNTS] + [NAV_ROW]
    await update.message.reply_text(
        "🏦 ငွေပေးသော Account ရွေးပါ:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ASSET_PAY


async def step_asset_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        unit_cost = context.user_data["fin"].get("asset_unit_cost", 0)
        salvage_hint = int(unit_cost * 0.1)
        kb = [[str(salvage_hint)], ["0"], NAV_ROW]
        await update.message.reply_text(
            "Salvage Value/Unit ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return ASSET_SALVAGE
    if text not in FINANCE_ACCOUNTS:
        kb = [[a] for a in FINANCE_ACCOUNTS] + [NAV_ROW]
        await update.message.reply_text(
            "⚠️ Account ရွေးပါ",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return ASSET_PAY
    d = context.user_data["fin"]
    d["asset_pay"] = text  # account name e.g. "KBZ Bank", "MMQR", "Cash Box"
    life      = d["asset_life"]
    unit_cost = d["asset_unit_cost"]
    qty       = d.get("asset_qty", 1)
    salvage   = d["asset_salvage"]
    total_cost = unit_cost * qty
    total_salvage = salvage * qty
    annual_total = int((total_cost - total_salvage) / life) if life > 0 else 0
    annual_per_unit = int((unit_cost - salvage) / life) if life > 0 else 0
    kb = [[BTN_CONFIRM_SAVE], NAV_ROW]
    qty_line = f"🔢 Qty           : <b>{qty} ခု</b>\n" if qty > 1 else ""
    qty_note = f" ({annual_per_unit:,} × {qty})" if qty > 1 else ""
    await update.message.reply_text(
        "🏢 <b>Asset မှတ်တမ်း — အတည်ပြုချက်</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🏷️ Name         : <b>{d['asset_name']}</b>\n"
        f"📁 Category     : <b>{d['asset_cat']}</b>\n"
        f"📅 Purchase Date: <b>{d['asset_date']}</b>\n"
        f"💰 Unit Cost    : <b>{unit_cost:,} Ks</b>\n"
        f"{qty_line}"
        f"💵 Total Cost   : <b>{total_cost:,} Ks</b>\n"
        f"📅 Useful Life  : <b>{life} နှစ်</b>\n"
        f"♻️ Salvage/Unit : <b>{salvage:,} Ks</b>\n"
        f"📉 Annual Depr  : <b>{annual_total:,} Ks/year</b>{qty_note}\n"
        f"🏦 Account      : <b>{d['asset_pay']}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "မှန်ကန်ပါသလား? ✅ Confirm &amp; Save နှိပ်ပါ",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ASSET_CONFIRM


async def step_asset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [[a] for a in FINANCE_ACCOUNTS] + [NAV_ROW]
        await update.message.reply_text(
            "🏦 ငွေပေးသော Account ပြန်ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return ASSET_PAY
    if text != BTN_CONFIRM_SAVE:
        await update.message.reply_text("✅ Confirm & Save နှိပ်ပါ")
        return ASSET_CONFIRM
    d = context.user_data["fin"]
    unit_cost = d["asset_unit_cost"]
    qty       = d.get("asset_qty", 1)
    pay       = d.get("asset_pay", "")
    await update.message.reply_text("⏳ သိမ်းဆည်းနေသည်...")
    try:
        def _do():
            sh = get_assets_sh()
            sh.append_row(
                [d["asset_name"], d["asset_cat"], d["asset_date"],
                 unit_cost, qty, d["asset_life"], d["asset_salvage"],
                 "Active", "", "", "", "", pay],
                value_input_option="USER_ENTERED",
            )
        await asyncio.to_thread(_do)
        total_cost = unit_cost * qty
        await update.message.reply_text(
            f"✅ <b>Asset မှတ်တမ်း သိမ်းပြီး!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ {d['asset_name']}  |  {d['asset_cat']}\n"
            f"💰 {unit_cost:,} Ks × {qty} = {total_cost:,} Ks  |  {d['asset_life']} နှစ်\n"
            f"🏦 {pay}",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return await show_finance_menu(update, context)


# ─────────────────────────────────────────
#  Asset Dispose Flow
# ─────────────────────────────────────────

_BIZ_START = datetime(2026, 6, 1)


def _calc_nbv_per_unit(asset: dict, as_of: datetime) -> int:
    """Net Book Value per unit at as_of date (straight-line from BUSINESS_START)."""
    unit_cost = asset["unit_cost"]
    salvage   = asset["salvage"]
    life      = asset["life"]
    date_str  = asset["date"]
    if not life or life <= 0:
        return unit_cost
    try:
        parts = date_str.split("/")
        purchase = datetime(int(parts[2]), int(parts[0]), int(parts[1])) if len(parts) == 3 else datetime.fromisoformat(date_str)
    except Exception:
        return unit_cost
    start = max(purchase, _BIZ_START)
    elapsed = (as_of.year - start.year) * 12 + (as_of.month - start.month) + 1
    total_months = int(life * 12)
    months = max(0, min(elapsed, total_months))
    acc_dep = round(((unit_cost - salvage) / total_months) * months) if total_months > 0 else 0
    return max(salvage, unit_cost - acc_dep)


async def prompt_asset_dispose_sel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("fin", {})["flow"] = "asset_dispose"
    await update.message.reply_text("⏳ Asset list ဖတ်နေသည်...")
    try:
        def _read():
            sh = get_assets_sh()
            rows = sh.get_all_values()
            assets = []
            for i, r in enumerate(rows[1:], start=2):
                name = (r[0] if len(r) > 0 else "").strip()
                if not name:
                    continue
                status = (r[7] if len(r) > 7 else "Active").strip()
                if status == "Disposed":
                    continue
                try:
                    qty = int((r[4] if len(r) > 4 else "1").replace(",", "") or "1")
                except Exception:
                    qty = 1
                try:
                    disp_qty = int((r[9] if len(r) > 9 else "0").replace(",", "") or "0")
                except Exception:
                    disp_qty = 0
                remaining = qty - disp_qty
                if remaining <= 0:
                    continue
                try:
                    unit_cost = int((r[3] if len(r) > 3 else "0").replace(",", "") or "0")
                except Exception:
                    unit_cost = 0
                try:
                    life = float((r[5] if len(r) > 5 else "0").replace(",", "") or "0")
                except Exception:
                    life = 0
                try:
                    salvage = int((r[6] if len(r) > 6 else "0").replace(",", "") or "0")
                except Exception:
                    salvage = 0
                assets.append({
                    "row": i,
                    "name": name,
                    "cat": (r[1] if len(r) > 1 else "").strip(),
                    "date": (r[2] if len(r) > 2 else "").strip(),
                    "unit_cost": unit_cost,
                    "qty": qty,
                    "life": life,
                    "salvage": salvage,
                    "status": status,
                    "disp_qty": disp_qty,
                    "remaining": remaining,
                })
            return assets
        assets = await asyncio.to_thread(_read)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return await show_finance_menu(update, context)

    if not assets:
        await update.message.reply_text("⚠️ Active asset မရှိပါ")
        return await show_finance_menu(update, context)

    context.user_data["fin"]["dispose_assets"] = assets
    kb = [[f"{a['name']} (x{a['remaining']})"] for a in assets] + [NAV_ROW]
    await update.message.reply_text(
        "🔄 *Asset ထုတ်ရောင်းမည်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ထုတ်ရောင်းမည့် Asset ရွေးပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ASSET_DISPOSE_SEL


async def step_asset_dispose_sel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_finance_menu(update, context)

    assets = context.user_data.get("fin", {}).get("dispose_assets", [])
    selected = next((a for a in assets if text == f"{a['name']} (x{a['remaining']})"), None)
    if not selected:
        await update.message.reply_text("⚠️ List မှ ရွေးပါ")
        return ASSET_DISPOSE_SEL

    context.user_data["fin"]["dispose_asset"] = selected
    nbv_pu = _calc_nbv_per_unit(selected, now_mmt())
    total_nbv = nbv_pu * selected["remaining"]
    today = now_mmt().strftime("%-m/%-d/%Y")
    await update.message.reply_text(
        f"🔄 *{selected['name']}* ထုတ်ရောင်းမည်\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📦 Qty ကျန်     : *{selected['remaining']}* ခု\n"
        f"📉 NBV/unit     : *{nbv_pu:,} Ks*\n"
        f"💰 Total NBV    : *{total_nbv:,} Ks*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        "📅 ထုတ်ရောင်းသည့် ရက်စွဲ (M/D/YYYY):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[today], NAV_ROW], resize_keyboard=True),
    )
    return ASSET_DISPOSE_DATE


async def step_asset_dispose_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        assets = context.user_data.get("fin", {}).get("dispose_assets", [])
        kb = [[f"{a['name']} (x{a['remaining']})"] for a in assets] + [NAV_ROW]
        await update.message.reply_text("Asset ပြန်ရွေးပါ:",
                                         reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return ASSET_DISPOSE_SEL

    context.user_data["fin"]["dispose_date"] = text
    asset = context.user_data["fin"]["dispose_asset"]
    remaining = asset["remaining"]
    quick = [str(i) for i in range(1, min(remaining + 1, 7))]
    kb = [quick] + [NAV_ROW] if remaining <= 6 else [[str(i)] for i in range(1, min(remaining + 1, 7))] + [NAV_ROW]
    await update.message.reply_text(
        f"📦 *ထုတ်ရောင်းမည့် Qty*\n\n"
        f"ကျန်ရှိ: *{remaining}* ခု\n"
        "ထုတ်ရောင်းမည့် အရေအတွက် ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[str(i)] for i in range(1, min(remaining + 1, 7))] + [NAV_ROW],
                                          resize_keyboard=True),
    )
    return ASSET_DISPOSE_QTY


async def step_asset_dispose_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        today = now_mmt().strftime("%-m/%-d/%Y")
        await update.message.reply_text("ရက်စွဲ ပြန်ရိုက်ပါ:",
                                         reply_markup=ReplyKeyboardMarkup([[today], NAV_ROW], resize_keyboard=True))
        return ASSET_DISPOSE_DATE

    asset = context.user_data["fin"]["dispose_asset"]
    try:
        qty = int(text.replace(",", ""))
        if qty <= 0 or qty > asset["remaining"]:
            raise ValueError
    except ValueError:
        await update.message.reply_text(f"⚠️ 1 မှ {asset['remaining']} ကြားဖြင့် ရိုက်ပါ")
        return ASSET_DISPOSE_QTY

    context.user_data["fin"]["dispose_qty"] = qty
    await update.message.reply_text(
        f"💰 *ရောင်းရငွေ (Ks)*\n\n"
        f"Qty: {qty} ခု ထုတ်ရောင်း — ရောင်းရငွေ စုစုပေါင်း ရိုက်ပါ\n"
        "(ရောင်းမဲ့မဟုတ်/ပစ်ပယ်ရင် 0 ရိုက်ပါ):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["0"], NAV_ROW], resize_keyboard=True),
    )
    return ASSET_DISPOSE_PROCEEDS


async def step_asset_dispose_proceeds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        asset = context.user_data["fin"]["dispose_asset"]
        remaining = asset["remaining"]
        await update.message.reply_text("Qty ပြန်ရိုက်ပါ:",
                                         reply_markup=ReplyKeyboardMarkup([[str(i)] for i in range(1, min(remaining + 1, 7))] + [NAV_ROW], resize_keyboard=True))
        return ASSET_DISPOSE_QTY

    try:
        proceeds = int(text.replace(",", ""))
        if proceeds < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ ပမာဏ ရိုက်ပါ (0 ရိုက်ပါ ရောင်းငွေ မရှိရင်)")
        return ASSET_DISPOSE_PROCEEDS

    d   = context.user_data["fin"]
    asset = d["dispose_asset"]
    qty   = d["dispose_qty"]
    dispose_date_str = d["dispose_date"]

    try:
        parts = dispose_date_str.split("/")
        as_of = datetime(int(parts[2]), int(parts[0]), int(parts[1])) if len(parts) == 3 else now_mmt()
    except Exception:
        as_of = now_mmt()

    nbv_pu      = _calc_nbv_per_unit(asset, as_of)
    nbv_disposed = nbv_pu * qty
    gain_loss    = proceeds - nbv_disposed
    gl_icon = "💚" if gain_loss >= 0 else "🔴"
    gl_word = "Gain" if gain_loss >= 0 else "Loss"

    d["dispose_proceeds"]  = proceeds
    d["dispose_nbv"]       = nbv_disposed
    d["dispose_gain_loss"] = gain_loss

    kb = [[BTN_CONFIRM_SAVE], NAV_ROW]
    await update.message.reply_text(
        f"🔄 *Dispose အတည်ပြုချက်*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏷️ Asset        : *{asset['name']}*\n"
        f"📅 Dispose Date : *{dispose_date_str}*\n"
        f"📦 Qty          : *{qty}* ခု (ကျန် {asset['remaining'] - qty})\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📉 NBV @ dispose : *{nbv_disposed:,} Ks*\n"
        f"💵 ရောင်းရငွေ    : *{proceeds:,} Ks*\n"
        f"{gl_icon} {gl_word}         : *{abs(gain_loss):,} Ks*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        "✅ Confirm & Save နှိပ်ပါ",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ASSET_DISPOSE_CONFIRM


async def step_asset_dispose_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text("ရောင်းငွေ ပြန်ရိုက်ပါ:",
                                         reply_markup=ReplyKeyboardMarkup([["0"], NAV_ROW], resize_keyboard=True))
        return ASSET_DISPOSE_PROCEEDS
    if text != BTN_CONFIRM_SAVE:
        await update.message.reply_text("✅ Confirm & Save နှိပ်ပါ")
        return ASSET_DISPOSE_CONFIRM

    d     = context.user_data["fin"]
    asset = d["dispose_asset"]
    qty   = d["dispose_qty"]
    proceeds = d["dispose_proceeds"]
    dispose_date = d["dispose_date"]
    await update.message.reply_text("⏳ သိမ်းဆည်းနေသည်...")

    try:
        def _write():
            sh = get_assets_sh()
            row_idx = asset["row"]
            prev_disp_qty = asset["disp_qty"]
            new_disp_qty  = prev_disp_qty + qty
            new_remaining = asset["qty"] - new_disp_qty
            new_status    = "Disposed" if new_remaining <= 0 else "Partially Disposed"
            try:
                prev_proc = int(str(sh.cell(row_idx, 11).value or "0").replace(",", ""))
            except Exception:
                prev_proc = 0
            sh.update_cell(row_idx, 8,  new_status)
            sh.update_cell(row_idx, 9,  dispose_date)
            sh.update_cell(row_idx, 10, new_disp_qty)
            sh.update_cell(row_idx, 11, prev_proc + proceeds)
        await asyncio.to_thread(_write)

        gain_loss = d["dispose_gain_loss"]
        gl_icon = "💚" if gain_loss >= 0 else "🔴"
        gl_word = "Gain" if gain_loss >= 0 else "Loss"
        await update.message.reply_text(
            f"✅ *Dispose မှတ်တမ်း သိမ်းပြီး!*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ {asset['name']}  |  Qty: {qty} ခု\n"
            f"{gl_icon} {gl_word}: {abs(gain_loss):,} Ks",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return await show_finance_menu(update, context)


# ─────────────────────────────────────────
#  Prepaid Entry Flow
# ─────────────────────────────────────────

async def prompt_prepaid_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("fin", {})["flow"] = "prepaid"
    await update.message.reply_text(
        "📅 *Prepaid ထည့် — ဖော်ပြချက်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ကြိုပေးငွေ ဖော်ပြချက် ရိုက်ပါ\n"
        "(ဥပမာ: မြေငှားရမ်းခ 6 လ):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return PREPAID_DESC


async def step_prepaid_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_finance_menu(update, context)
    context.user_data["fin"]["prepaid_desc"] = text
    kb = [[c] for c in PREPAID_CATEGORIES] + [NAV_ROW]
    await update.message.reply_text(
        "📁 *Prepaid Category ရွေးပါ:*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return PREPAID_CAT


async def step_prepaid_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await prompt_prepaid_desc(update, context)
    if text not in PREPAID_CATEGORIES:
        await update.message.reply_text("⚠️ Category ရွေးပါ")
        return PREPAID_CAT
    context.user_data["fin"]["prepaid_cat"] = text
    await update.message.reply_text(
        "💰 *ကြိုပေးငွေ စုစုပေါင်း (Ks)*\n\nAmount ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return PREPAID_AMT


async def step_prepaid_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [[c] for c in PREPAID_CATEGORIES] + [NAV_ROW]
        await update.message.reply_text(
            "Category ပြန်ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return PREPAID_CAT
    try:
        amt = int(text.replace(",", ""))
        if amt <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ ပမာဏ မှန်ကန်စွာ ရိုက်ပါ")
        return PREPAID_AMT
    context.user_data["fin"]["prepaid_amt"] = amt
    kb = [[a] for a in FINANCE_ACCOUNTS] + [NAV_ROW]
    await update.message.reply_text(
        f"💰 *{amt:,} Ks* — ငွေ ထုတ်မည့် Account ရွေးပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return PREPAID_ACCT


async def step_prepaid_acct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "Amount ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
        )
        return PREPAID_AMT
    if text not in FINANCE_ACCOUNTS:
        kb = [[a] for a in FINANCE_ACCOUNTS] + [NAV_ROW]
        await update.message.reply_text(
            "⚠️ Account ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return PREPAID_ACCT
    context.user_data["fin"]["prepaid_acct"] = text
    today = now_mmt().strftime("%-m/%-d/%Y")
    kb = [[today], NAV_ROW]
    await update.message.reply_text(
        "📅 *Start Date (M/D/YYYY)*\n\nစတင်ရက်စွဲ ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return PREPAID_START


def _prepaid_add_months(date_str: str, months: int) -> str:
    """Parse M/D/YYYY or YYYY-MM-DD, add months, return M/D/YYYY."""
    import calendar as _cal
    from datetime import date as _date
    s = date_str.strip()
    try:
        if "-" in s and len(s) == 10:
            d = _date.fromisoformat(s)
        else:
            parts = s.split("/")
            d = _date(int(parts[2]), int(parts[0]), int(parts[1]))
    except Exception:
        return s  # fallback
    m = d.month - 1 + months
    yr = d.year + m // 12
    mo = m % 12 + 1
    day = min(d.day, _cal.monthrange(yr, mo)[1])
    return f"{mo}/{day}/{yr}"


async def step_prepaid_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [[a] for a in FINANCE_ACCOUNTS] + [NAV_ROW]
        await update.message.reply_text(
            "Account ပြန်ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return PREPAID_ACCT
    context.user_data["fin"]["prepaid_start"] = text
    kb = [["1"], ["3"], ["6"], ["12"], NAV_ROW]
    await update.message.reply_text(
        "📅 *Period — လပေါင်း ဘယ်လောက်?*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ကြိုပေးငွေ သက်တမ်း (လပေါင်း) ရိုက်ပါ\n"
        "(ဥပမာ — 6 လ Rent ဆိုရင် 6):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return PREPAID_END


async def step_prepaid_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle period months input; auto-calculate end date."""
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        today = now_mmt().strftime("%-m/%-d/%Y")
        await update.message.reply_text(
            "Start Date ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([[today], NAV_ROW], resize_keyboard=True),
        )
        return PREPAID_START
    try:
        period = int(text)
        if period <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ လပေါင်း ဂဏန်း ရိုက်ပါ (ဥပမာ: 6)")
        return PREPAID_END
    d = context.user_data["fin"]
    d["prepaid_period"] = period
    end_date = _prepaid_add_months(d["prepaid_start"], period)
    d["prepaid_end"] = end_date
    kb = [[BTN_CONFIRM_SAVE], NAV_ROW]
    monthly = d["prepaid_amt"] // period if period else 0
    await update.message.reply_text(
        "📅 *Prepaid မှတ်တမ်း — အတည်ပြုချက်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📋 ဖော်ပြ     : *{d['prepaid_desc']}*\n"
        f"📁 Category  : *{d['prepaid_cat']}*\n"
        f"💰 Total Paid: *{d['prepaid_amt']:,} Ks*\n"
        f"🏦 Account    : *{d.get('prepaid_acct','—')}*\n"
        f"📅 Start     : *{d['prepaid_start']}*\n"
        f"⏱ Period    : *{period} လ*\n"
        f"📅 End       : *{end_date}* (auto)\n"
        f"📊 Monthly   : *{monthly:,} Ks/လ*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ Confirm & Save နှိပ်ပါ",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return PREPAID_CONFIRM


async def step_prepaid_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [["1"], ["3"], ["6"], ["12"], NAV_ROW]
        await update.message.reply_text(
            "Period (လပေါင်း) ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return PREPAID_END
    if text != BTN_CONFIRM_SAVE:
        await update.message.reply_text("✅ Confirm & Save နှိပ်ပါ")
        return PREPAID_CONFIRM
    d = context.user_data["fin"]
    await update.message.reply_text("⏳ သိမ်းဆည်းနေသည်...")
    try:
        def _do():
            sh = get_prepaid_fin_sh()
            sh.append_row(
                [d["prepaid_desc"], d["prepaid_cat"], d["prepaid_amt"],
                 d["prepaid_start"], d["prepaid_end"], d.get("prepaid_acct", "")],
                value_input_option="USER_ENTERED",
            )
        await asyncio.to_thread(_do)
        await update.message.reply_text(
            f"✅ *Prepaid မှတ်တမ်း သိမ်းပြီး!*\n"
            f"📋 {d['prepaid_desc']}  |  {d['prepaid_cat']}\n"
            f"💰 {d['prepaid_amt']:,} Ks  |  {d['prepaid_start']} → {d['prepaid_end']}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return await show_finance_menu(update, context)


# ─────────────────────────────────────────
#  Account Transfer Flow
# ─────────────────────────────────────────

async def prompt_acct_trf_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("fin", {})["flow"] = "transfer"
    kb = [[a] for a in FINANCE_ACCOUNTS] + [NAV_ROW]
    await update.message.reply_text(
        "💸 *Account Transfer — ငွေလွှဲမည်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ငွေထုတ်မည့် Account (From) ရွေးပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ACCT_TRF_FROM


async def step_acct_trf_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_finance_menu(update, context)
    if text not in FINANCE_ACCOUNTS:
        await update.message.reply_text("⚠️ Account ရွေးပါ")
        return ACCT_TRF_FROM
    context.user_data["fin"]["trf_from"] = text
    to_accts = [a for a in FINANCE_ACCOUNTS if a != text]
    kb = [[a] for a in to_accts] + [NAV_ROW]
    await update.message.reply_text(
        f"💸 *From: {text}*\nTo Account ရွေးပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ACCT_TRF_TO


async def step_acct_trf_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await prompt_acct_trf_from(update, context)
    if text not in FINANCE_ACCOUNTS:
        await update.message.reply_text("⚠️ Account ရွေးပါ")
        return ACCT_TRF_TO
    context.user_data["fin"]["trf_to"] = text
    await update.message.reply_text(
        "💰 *ငွေပမာဏ (Ks)*\n\nAmount ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return ACCT_TRF_AMT


async def step_acct_trf_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        to_accts = [a for a in FINANCE_ACCOUNTS if a != context.user_data["fin"].get("trf_from", "")]
        kb = [[a] for a in to_accts] + [NAV_ROW]
        await update.message.reply_text(
            "To Account ပြန်ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return ACCT_TRF_TO
    try:
        amt = int(text.replace(",", ""))
        if amt <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ ပမာဏ မှန်ကန်စွာ ရိုက်ပါ")
        return ACCT_TRF_AMT
    context.user_data["fin"]["trf_amt"] = amt
    kb = [["⏩ Skip"], NAV_ROW]
    await update.message.reply_text(
        "📝 *မှတ်ချက် (Notes)*\n\nမှတ်ချက် ရိုက်ပါ သို့ Skip:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ACCT_TRF_NOTE


async def step_acct_trf_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "Amount ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
        )
        return ACCT_TRF_AMT
    d = context.user_data["fin"]
    d["trf_note"] = "" if text == "⏩ Skip" else text
    kb = [[BTN_CONFIRM_SAVE], NAV_ROW]
    await update.message.reply_text(
        "💸 *Account Transfer — အတည်ပြုချက်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🏦 From   : *{d['trf_from']}*\n"
        f"🏦 To     : *{d['trf_to']}*\n"
        f"💰 Amount : *{d['trf_amt']:,} Ks*\n"
        f"📝 Note   : *{d.get('trf_note','') or '—'}*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ Confirm & Save နှိပ်ပါ",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ACCT_TRF_CONFIRM


async def step_acct_trf_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [["⏩ Skip"], NAV_ROW]
        await update.message.reply_text(
            "မှတ်ချက် ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return ACCT_TRF_NOTE
    if text != BTN_CONFIRM_SAVE:
        await update.message.reply_text("✅ Confirm & Save နှိပ်ပါ")
        return ACCT_TRF_CONFIRM
    d = context.user_data["fin"]
    await update.message.reply_text("⏳ သိမ်းဆည်းနေသည်...")
    try:
        def _do():
            sh = get_acct_trf_sh()
            sh.append_row(
                [today_str(), d["trf_from"], d["trf_to"],
                 d["trf_amt"], d.get("trf_note", "")],
                value_input_option="USER_ENTERED",
            )
        await asyncio.to_thread(_do)
        await update.message.reply_text(
            f"✅ *Transfer မှတ်တမ်း သိမ်းပြီး!*\n"
            f"💸 {d['trf_from']} → {d['trf_to']}\n"
            f"💰 {d['trf_amt']:,} Ks",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return await show_finance_menu(update, context)


# ─────────────────────────────────────────
#  Payable Entry Flow
# ─────────────────────────────────────────

async def prompt_pay_vendor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("fin", {})["flow"] = "payable"
    await update.message.reply_text(
        "📤 *Payable ထည့် — Vendor*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ပေးရမည့် ဆရာ/ကုမ္ပဏီ အမည် ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return PAY_VENDOR


async def step_pay_vendor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_finance_menu(update, context)
    context.user_data["fin"]["pay_vendor"] = text
    await update.message.reply_text(
        "📝 *ဖော်ပြချက်*\n\nဘာအတွက် ပေးရသည် ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return PAY_DESC


async def step_pay_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await prompt_pay_vendor(update, context)
    context.user_data["fin"]["pay_desc"] = text
    await update.message.reply_text(
        "💰 *ပေးရမည့် ပမာဏ (Ks)*\n\nAmount ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return PAY_AMT


async def step_pay_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "ဖော်ပြချက် ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
        )
        return PAY_DESC
    try:
        amt = int(text.replace(",", ""))
        if amt <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ ပမာဏ မှန်ကန်စွာ ရိုက်ပါ")
        return PAY_AMT
    context.user_data["fin"]["pay_amt"] = amt
    today = now_mmt().strftime("%-m/%-d/%Y")
    kb = [[today], NAV_ROW]
    await update.message.reply_text(
        "📅 *Due Date (M/D/YYYY)*\n\nပေးဆပ်ရမည့် ရက်စွဲ ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return PAY_DUE


async def step_pay_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "Amount ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
        )
        return PAY_AMT
    context.user_data["fin"]["pay_due"] = text
    kb = [[a] for a in FINANCE_ACCOUNTS] + [["⏩ Skip"], NAV_ROW]
    await update.message.reply_text(
        "🏦 *ပေးမည့် Account ရွေးပါ:*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return PAY_ACCT


async def step_pay_acct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        today = now_mmt().strftime("%-m/%-d/%Y")
        await update.message.reply_text(
            "Due Date ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([[today], NAV_ROW], resize_keyboard=True),
        )
        return PAY_DUE
    d = context.user_data["fin"]
    d["pay_acct"] = "" if text == "⏩ Skip" else text
    kb = [[BTN_CONFIRM_SAVE], NAV_ROW]
    await update.message.reply_text(
        "📤 *Payable — အတည်ပြုချက်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🏪 Vendor  : *{d['pay_vendor']}*\n"
        f"📝 Desc    : *{d['pay_desc']}*\n"
        f"💰 Amount  : *{d['pay_amt']:,} Ks*\n"
        f"📅 Due     : *{d['pay_due']}*\n"
        f"🏦 Account : *{d.get('pay_acct','') or '—'}*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ Confirm & Save နှိပ်ပါ",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return PAY_CONFIRM


async def step_pay_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [[a] for a in FINANCE_ACCOUNTS] + [["⏩ Skip"], NAV_ROW]
        await update.message.reply_text(
            "Account ပြန်ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return PAY_ACCT
    if text != BTN_CONFIRM_SAVE:
        await update.message.reply_text("✅ Confirm & Save နှိပ်ပါ")
        return PAY_CONFIRM
    d = context.user_data["fin"]
    await update.message.reply_text("⏳ သိမ်းဆည်းနေသည်...")
    try:
        def _do():
            sh = get_payables_sh()
            sh.append_row(
                [today_str(), d["pay_vendor"], d["pay_desc"],
                 d["pay_amt"], d["pay_due"], "Pending", "", d.get("pay_acct", ""), ""],
                value_input_option="USER_ENTERED",
            )
        await asyncio.to_thread(_do)
        await update.message.reply_text(
            f"✅ *Payable မှတ်တမ်း သိမ်းပြီး!*\n"
            f"📤 {d['pay_vendor']} — {d['pay_desc']}\n"
            f"💰 {d['pay_amt']:,} Ks  |  Due: {d['pay_due']}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return await show_finance_menu(update, context)


# ─────────────────────────────────────────
#  Receivable Entry Flow
# ─────────────────────────────────────────

async def prompt_rec_cust(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("fin", {})["flow"] = "receivable"
    await update.message.reply_text(
        "📥 *Receivable ထည့် — Customer*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ရမည့် ဆရာ/ကုမ္ပဏီ အမည် ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return REC_CUST


async def step_rec_cust(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_finance_menu(update, context)
    context.user_data["fin"]["rec_cust"] = text
    await update.message.reply_text(
        "📝 *ဖော်ပြချက်*\n\nဘာအတွက် ရမည် ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return REC_DESC


async def step_rec_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await prompt_rec_cust(update, context)
    context.user_data["fin"]["rec_desc"] = text
    await update.message.reply_text(
        "💰 *ရမည့် ပမာဏ (Ks)*\n\nAmount ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return REC_AMT


async def step_rec_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "ဖော်ပြချက် ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
        )
        return REC_DESC
    try:
        amt = int(text.replace(",", ""))
        if amt <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ ပမာဏ မှန်ကန်စွာ ရိုက်ပါ")
        return REC_AMT
    context.user_data["fin"]["rec_amt"] = amt
    today = now_mmt().strftime("%-m/%-d/%Y")
    kb = [[today], NAV_ROW]
    await update.message.reply_text(
        "📅 *Expected Date (M/D/YYYY)*\n\nရမည့် ရက်စွဲ ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return REC_DUE


async def step_rec_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "Amount ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
        )
        return REC_AMT
    context.user_data["fin"]["rec_due"] = text
    kb = [[a] for a in FINANCE_ACCOUNTS] + [["⏩ Skip"], NAV_ROW]
    await update.message.reply_text(
        "🏦 *ရမည့် Account ရွေးပါ:*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return REC_ACCT


async def step_rec_acct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        today = now_mmt().strftime("%-m/%-d/%Y")
        await update.message.reply_text(
            "Expected Date ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([[today], NAV_ROW], resize_keyboard=True),
        )
        return REC_DUE
    d = context.user_data["fin"]
    d["rec_acct"] = "" if text == "⏩ Skip" else text
    kb = [[BTN_CONFIRM_SAVE], NAV_ROW]
    await update.message.reply_text(
        "📥 *Receivable — အတည်ပြုချက်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 Customer : *{d['rec_cust']}*\n"
        f"📝 Desc     : *{d['rec_desc']}*\n"
        f"💰 Amount   : *{d['rec_amt']:,} Ks*\n"
        f"📅 Expected : *{d['rec_due']}*\n"
        f"🏦 Account  : *{d.get('rec_acct','') or '—'}*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ Confirm & Save နှိပ်ပါ",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return REC_CONFIRM


async def step_rec_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [[a] for a in FINANCE_ACCOUNTS] + [["⏩ Skip"], NAV_ROW]
        await update.message.reply_text(
            "Account ပြန်ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return REC_ACCT
    if text != BTN_CONFIRM_SAVE:
        await update.message.reply_text("✅ Confirm & Save နှိပ်ပါ")
        return REC_CONFIRM
    d = context.user_data["fin"]
    await update.message.reply_text("⏳ သိမ်းဆည်းနေသည်...")
    try:
        def _do():
            sh = get_receivables_sh()
            sh.append_row(
                [today_str(), d["rec_cust"], d["rec_desc"],
                 d["rec_amt"], d["rec_due"], "Pending", "", d.get("rec_acct", ""), ""],
                value_input_option="USER_ENTERED",
            )
        await asyncio.to_thread(_do)
        await update.message.reply_text(
            f"✅ *Receivable မှတ်တမ်း သိမ်းပြီး!*\n"
            f"📥 {d['rec_cust']} — {d['rec_desc']}\n"
            f"💰 {d['rec_amt']:,} Ks  |  Expected: {d['rec_due']}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return await show_finance_menu(update, context)


# ─────────────────────────────────────────
#  Settle Payable / Receivable Flows
# ─────────────────────────────────────────

async def show_settle_list(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str):
    """Show list of Pending payables or receivables for settling."""
    is_pay = kind == "payable"
    label  = "Payable" if is_pay else "Receivable"
    icon   = "📤" if is_pay else "📥"
    await update.message.reply_text(f"⏳ Pending {label} ဆွဲယူနေသည်...")
    try:
        def _read():
            sh = get_payables_sh() if is_pay else get_receivables_sh()
            return sh.get_all_values()
        rows = await asyncio.to_thread(_read)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return await show_finance_menu(update, context)

    # Payables/Receivables cols: A=Date B=Vendor/Cust C=Desc D=Amt E=Due F=Status G=PaidDate H=Acct
    pending = [(i + 2, r) for i, r in enumerate(rows[1:])
               if r and (r[5] if len(r) > 5 else "").strip().lower() in ("pending", "")]
    context.user_data.setdefault("fin", {})["settle_kind"]    = kind
    context.user_data["fin"]["settle_pending"] = pending   # list of (sheet_row, row_data)

    if not pending:
        await update.message.reply_text(
            f"✅ Pending {label} မရှိပါ — အားလုံး Settle ပြီးပြီ!"
        )
        return await show_finance_menu(update, context)

    lines = [f"{icon} *Pending {label} စာရင်း*", "━━━━━━━━━━━━━━━━━━"]
    for idx, (_, r) in enumerate(pending, 1):
        party = (r[1] if len(r) > 1 else "?").strip()
        desc  = (r[2] if len(r) > 2 else "").strip()
        amt   = (r[3] if len(r) > 3 else "0").strip()
        due   = (r[4] if len(r) > 4 else "").strip()
        try:
            amt_fmt = f"{int(str(amt).replace(',','').replace('.','').split('.')[0]):,}"
        except Exception:
            amt_fmt = amt
        lines.append(f"{idx}. *{party}*\n   {desc}\n   💰 {amt_fmt} Ks  |  📅 Due: {due}")
    lines += ["━━━━━━━━━━━━━━━━━━", "ဘယ် ဂဏန်းကို Settle မည်? (ဥပမာ: 1)"]

    num_kb = [[str(i)] for i in range(1, len(pending) + 1)]
    num_kb.append([BTN_FIN_BACK, BTN_CANCEL])
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(num_kb, resize_keyboard=True),
    )
    return PAY_SETTLE_LIST if is_pay else REC_SETTLE_LIST


async def _handle_settle_list(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_FIN_BACK:
        return await show_finance_menu(update, context)
    d = context.user_data.get("fin", {})
    pending = d.get("settle_pending", [])
    try:
        idx = int(text) - 1
        assert 0 <= idx < len(pending)
    except (ValueError, AssertionError):
        await update.message.reply_text("⚠️ မှန်ကန်သော ဂဏန်း ရိုက်ပါ")
        return PAY_SETTLE_LIST if kind == "payable" else REC_SETTLE_LIST

    sheet_row, r = pending[idx]
    d["settle_row"]  = sheet_row
    d["settle_data"] = r
    acct = (r[7] if len(r) > 7 else "").strip()
    # If account not set, ask user which account to pay from/into
    if not acct:
        is_pay = kind == "payable"
        party  = (r[1] if len(r) > 1 else "?").strip()
        amt    = (r[3] if len(r) > 3 else "0").strip()
        try:
            amt_fmt = f"{int(str(amt).replace(',','').split('.')[0]):,}"
        except Exception:
            amt_fmt = amt
        verb = "ပေးမည့်" if is_pay else "လက်ခံမည့်"
        kb = [[a] for a in FINANCE_ACCOUNTS] + [[BTN_BACK, BTN_CANCEL]]
        await update.message.reply_text(
            f"🏦 *{party} — {amt_fmt} Ks*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"ငွေ {verb} Account ရွေးပါ:",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return PAY_SETTLE_ACCT if is_pay else REC_SETTLE_ACCT
    # Account already set — go straight to confirm
    return await _show_settle_confirm(update, context, kind)


async def _show_settle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str):
    """Render the settle confirmation screen (account already in d['settle_acct'] or settle_data)."""
    d = context.user_data.get("fin", {})
    r      = d.get("settle_data", [])
    is_pay = kind == "payable"
    party  = (r[1] if len(r) > 1 else "?").strip()
    desc   = (r[2] if len(r) > 2 else "").strip()
    amt    = (r[3] if len(r) > 3 else "0").strip()
    due    = (r[4] if len(r) > 4 else "").strip()
    acct   = d.get("settle_acct") or (r[7] if len(r) > 7 else "").strip()
    try:
        amt_fmt = f"{int(str(amt).replace(',','').split('.')[0]):,}"
    except Exception:
        amt_fmt = amt
    status_new = "Paid" if is_pay else "Received"
    label = "Payable" if is_pay else "Receivable"
    icon  = "📤" if is_pay else "📥"
    await update.message.reply_text(
        f"{icon} *Settle {label} — အတည်ပြုချက်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 {'Vendor' if is_pay else 'Customer'} : *{party}*\n"
        f"📋 Description      : {desc}\n"
        f"💰 Amount           : *{amt_fmt} Ks*\n"
        f"📅 Due Date         : {due}\n"
        f"🏦 Account          : *{acct or '—'}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ Confirm နှိပ်ရင် Status → *{status_new}*, Date → *{today_str()}*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BTN_CONFIRM_SAVE], [BTN_BACK, BTN_CANCEL]], resize_keyboard=True),
    )
    return PAY_SETTLE_CONFIRM if is_pay else REC_SETTLE_CONFIRM


async def _handle_settle_acct(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str):
    """Handle account selection step during settle."""
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_settle_list(update, context, kind)
    if text not in FINANCE_ACCOUNTS:
        kb = [[a] for a in FINANCE_ACCOUNTS] + [[BTN_BACK, BTN_CANCEL]]
        await update.message.reply_text(
            "⚠️ Account ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return PAY_SETTLE_ACCT if kind == "payable" else REC_SETTLE_ACCT
    context.user_data["fin"]["settle_acct"] = text
    return await _show_settle_confirm(update, context, kind)


# ─────────────────────────────────────────
#  ADVANCE PAYMENT FLOW
#  Sheet: Advance_Payments!A:H
#  A=Date, B=Party, C=Description, D=Amount, E=Account, F=Expected Date, G=Status, H=Notes
# ─────────────────────────────────────────

async def prompt_advpay_party(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("fin", {})["flow"] = "advpay"
    kb = [["⬅️ Finance Menu", BTN_CANCEL]]
    await update.message.reply_text(
        "💵 *Advance Payment ထည့်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Vendor/Party အမည် ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ADVPAY_PARTY


async def step_advpay_party(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_FIN_BACK or text == "⬅️ Finance Menu":
        return await show_finance_menu(update, context)
    context.user_data["fin"]["advpay_party"] = text
    await update.message.reply_text(
        "📝 ဘာအတွက် ကြိုပေးသလဲ? (Description):",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return ADVPAY_DESC


async def step_advpay_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "Party/Vendor ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([["⬅️ Finance Menu", BTN_CANCEL]], resize_keyboard=True),
        )
        return ADVPAY_PARTY
    context.user_data["fin"]["advpay_desc"] = text
    await update.message.reply_text(
        "💰 Amount (Ks) ရိုက်ပါ:",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return ADVPAY_AMT


async def step_advpay_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "Description ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
        )
        return ADVPAY_DESC
    try:
        amt = int(text.replace(",", "").replace(".", ""))
        if amt <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ ပမာဏ မှန်ကန်စွာ ရိုက်ပါ")
        return ADVPAY_AMT
    context.user_data["fin"]["advpay_amt"] = amt
    kb = [[a] for a in FINANCE_ACCOUNTS] + [NAV_ROW]
    await update.message.reply_text(
        f"💰 *{amt:,} Ks* — ထုတ်မည့် Account ရွေးပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ADVPAY_ACCT


async def step_advpay_acct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "Amount ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
        )
        return ADVPAY_AMT
    if text not in FINANCE_ACCOUNTS:
        kb = [[a] for a in FINANCE_ACCOUNTS] + [NAV_ROW]
        await update.message.reply_text("⚠️ Account ရွေးပါ:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return ADVPAY_ACCT
    context.user_data["fin"]["advpay_acct"] = text
    today = now_mmt().strftime("%-m/%-d/%Y")
    kb = [[today], NAV_ROW]
    await update.message.reply_text(
        "📅 Expected Return/Settle Date (M/D/YYYY):",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ADVPAY_DUE


async def step_advpay_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [[a] for a in FINANCE_ACCOUNTS] + [NAV_ROW]
        await update.message.reply_text("Account ပြန်ရွေးပါ:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return ADVPAY_ACCT
    context.user_data["fin"]["advpay_due"] = text
    kb = [["⏩ Skip"], NAV_ROW]
    await update.message.reply_text(
        "📝 Notes (မလိုလျှင် Skip နှိပ်ပါ):",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ADVPAY_NOTE


async def step_advpay_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        today = now_mmt().strftime("%-m/%-d/%Y")
        await update.message.reply_text(
            "Expected Date ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([[today], NAV_ROW], resize_keyboard=True),
        )
        return ADVPAY_DUE
    d = context.user_data["fin"]
    d["advpay_note"] = "" if text == "⏩ Skip" else text
    kb = [[BTN_CONFIRM_SAVE], NAV_ROW]
    await update.message.reply_text(
        "💵 *Advance Payment — အတည်ပြုချက်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 Party      : *{d['advpay_party']}*\n"
        f"📝 Desc       : *{d['advpay_desc']}*\n"
        f"💰 Amount     : *{d['advpay_amt']:,} Ks*\n"
        f"🏦 Account    : *{d['advpay_acct']}*\n"
        f"📅 Expect Date: *{d['advpay_due']}*\n"
        f"📋 Notes      : {d.get('advpay_note') or '—'}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ Confirm & Save နှိပ်ပါ",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ADVPAY_CONFIRM


async def step_advpay_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [["⏩ Skip"], NAV_ROW]
        await update.message.reply_text("Notes ပြန်ရိုက်ပါ:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return ADVPAY_NOTE
    if text != BTN_CONFIRM_SAVE:
        await update.message.reply_text("✅ Confirm & Save နှိပ်ပါ")
        return ADVPAY_CONFIRM
    d = context.user_data["fin"]
    await update.message.reply_text("⏳ သိမ်းဆည်းနေသည်...")
    try:
        def _do():
            sh = get_advpay_sh()
            sh.append_row(
                [today_str(), d["advpay_party"], d["advpay_desc"],
                 d["advpay_amt"], d["advpay_acct"], d["advpay_due"],
                 "Pending", d.get("advpay_note", "")],
                value_input_option="USER_ENTERED",
            )
        await asyncio.to_thread(_do)
        await update.message.reply_text(
            f"✅ *Advance Payment မှတ်ပြီး!*\n"
            f"👤 {d['advpay_party']}  |  💰 {d['advpay_amt']:,} Ks\n"
            f"🏦 {d['advpay_acct']}  |  📅 {d['advpay_due']}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return await show_finance_menu(update, context)


async def show_advpay_settle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all Pending advance payments for settlement."""
    context.user_data.setdefault("fin", {})
    try:
        def _fetch():
            sh = get_advpay_sh()
            return sh.get_all_values()
        rows = await asyncio.to_thread(_fetch)
    except Exception as e:
        await update.message.reply_text(f"❌ Sheet error: {e}")
        return await show_finance_menu(update, context)

    pending = [(i + 2, r) for i, r in enumerate(rows[1:]) if len(r) >= 7 and (r[6] or "").strip().lower() == "pending"]
    if not pending:
        await update.message.reply_text("ℹ️ Pending Advance Payment မရှိပါ")
        return await show_finance_menu(update, context)

    context.user_data["fin"]["advpay_pending"] = pending
    lines = ["💵 *Pending Advance Payments*\n━━━━━━━━━━━━━━━━━━"]
    num_kb = []
    for idx, (_, r) in enumerate(pending):
        party = (r[1] if len(r) > 1 else "?").strip()
        amt   = (r[3] if len(r) > 3 else "0").strip()
        due   = (r[5] if len(r) > 5 else "").strip()
        try:
            amt_fmt = f"{int(str(amt).replace(',','').split('.')[0]):,}"
        except Exception:
            amt_fmt = amt
        lines.append(f"{idx+1}. {party}  |  {amt_fmt} Ks  |  {due}")
        num_kb.append([str(idx + 1)])
    num_kb.append([BTN_FIN_BACK, BTN_CANCEL])
    await update.message.reply_text(
        "\n".join(lines) + "\n\nဂဏန်း ရွေးပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(num_kb, resize_keyboard=True),
    )
    return ADVPAY_LIST


async def step_advpay_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_FIN_BACK:
        return await show_finance_menu(update, context)
    d = context.user_data.get("fin", {})
    pending = d.get("advpay_pending", [])
    try:
        idx = int(text) - 1
        assert 0 <= idx < len(pending)
    except (ValueError, AssertionError):
        await update.message.reply_text("⚠️ မှန်ကန်သော ဂဏန်း ရိုက်ပါ")
        return ADVPAY_LIST
    sheet_row, r = pending[idx]
    d["advpay_settle_row"]  = sheet_row
    d["advpay_settle_data"] = r
    party = (r[1] if len(r) > 1 else "?").strip()
    desc  = (r[2] if len(r) > 2 else "").strip()
    amt   = (r[3] if len(r) > 3 else "0").strip()
    acct  = (r[4] if len(r) > 4 else "").strip()
    due   = (r[5] if len(r) > 5 else "").strip()
    try:
        amt_fmt = f"{int(str(amt).replace(',','').split('.')[0]):,}"
    except Exception:
        amt_fmt = amt
    await update.message.reply_text(
        "💵 *Advance Settle — အတည်ပြုချက်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 Party     : *{party}*\n"
        f"📝 Desc      : {desc}\n"
        f"💰 Amount    : *{amt_fmt} Ks*\n"
        f"🏦 Account   : *{acct}*\n"
        f"📅 Due       : {due}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"✅ Confirm → Status *Settled*, Date → *{today_str()}*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BTN_CONFIRM_SAVE], [BTN_BACK, BTN_CANCEL]], resize_keyboard=True),
    )
    return ADVPAY_SETTLE_CONFIRM


async def step_advpay_settle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_advpay_settle(update, context)
    if text != BTN_CONFIRM_SAVE:
        await update.message.reply_text("✅ Confirm & Save နှိပ်ပါ")
        return ADVPAY_SETTLE_CONFIRM
    d = context.user_data.get("fin", {})
    sheet_row = d.get("advpay_settle_row")
    r = d.get("advpay_settle_data", [])
    await update.message.reply_text("⏳ သိမ်းဆည်းနေသည်...")
    try:
        def _do():
            sh = get_advpay_sh()
            sh.update_cell(sheet_row, 7, "Settled")       # col G = Status
            sh.update_cell(sheet_row, 8, today_str())     # col H = Settle Date (overwrites Notes)
        await asyncio.to_thread(_do)
        party = (r[1] if len(r) > 1 else "?").strip()
        amt   = (r[3] if len(r) > 3 else "0").strip()
        try:
            amt_fmt = f"{int(str(amt).replace(',','').split('.')[0]):,}"
        except Exception:
            amt_fmt = amt
        await update.message.reply_text(
            f"✅ *Advance Settled!*\n"
            f"👤 {party}  |  💰 {amt_fmt} Ks\n"
            f"📅 {today_str()}  |  Status: *Settled*",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return await show_finance_menu(update, context)


async def step_pay_settle_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_settle_list(update, context, "payable")


async def step_rec_settle_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_settle_list(update, context, "receivable")


async def step_pay_settle_acct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_settle_acct(update, context, "payable")


async def step_rec_settle_acct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_settle_acct(update, context, "receivable")


async def _handle_settle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_settle_list(update, context, kind)
    if text != BTN_CONFIRM_SAVE:
        await update.message.reply_text("✅ Confirm & Save နှိပ်ပါ")
        return PAY_SETTLE_CONFIRM if kind == "payable" else REC_SETTLE_CONFIRM
    d = context.user_data.get("fin", {})
    sheet_row = d.get("settle_row")
    is_pay    = kind == "payable"
    status_new = "Paid" if is_pay else "Received"
    await update.message.reply_text("⏳ သိမ်းဆည်းနေသည်...")
    settle_acct = d.get("settle_acct", "")
    try:
        def _do():
            sh = get_payables_sh() if is_pay else get_receivables_sh()
            sh.update_cell(sheet_row, 6, status_new)   # col F = Status
            sh.update_cell(sheet_row, 7, today_str())  # col G = Paid/Received Date
            if settle_acct:
                sh.update_cell(sheet_row, 8, settle_acct)  # col H = Account
        await asyncio.to_thread(_do)
        r     = d.get("settle_data", [])
        party = (r[1] if len(r) > 1 else "?").strip()
        amt   = (r[3] if len(r) > 3 else "0").strip()
        acct_used = settle_acct or (r[7] if len(r) > 7 else "").strip()
        try:
            amt_fmt = f"{int(str(amt).replace(',','').split('.')[0]):,}"
        except Exception:
            amt_fmt = amt
        label = "Payable" if is_pay else "Receivable"
        await update.message.reply_text(
            f"✅ *{label} Settled!*\n"
            f"👤 {party}\n"
            f"💰 {amt_fmt} Ks  |  🏦 {acct_used or '—'}\n"
            f"📅 {today_str()}  |  Status: *{status_new}*",
            parse_mode="Markdown",
        )
        d.pop("settle_acct", None)   # clear for next settle
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return await show_finance_menu(update, context)


async def step_pay_settle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_settle_confirm(update, context, "payable")


async def step_rec_settle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_settle_confirm(update, context, "receivable")


# ─────────────────────────────────────────
#  Shareholders Flow
# ─────────────────────────────────────────

def get_capital_sh():
    return wb.worksheet("Capital_Setup")


_SHARE_ROLES = ["Operation Partner", "Silent Partner"]


async def show_shareholder_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show shareholders list (A=Name, B=Role, C=Capital, D=Ownership%) + Add button."""
    await update.message.reply_text("⏳ Shareholders ဆွဲယူနေသည်...")
    try:
        def _read():
            return get_capital_sh().get_all_values()
        rows = await asyncio.to_thread(_read)
        # Capital_Setup: A=Shareholder, B=Role, C=Capital(Ks), D=Ownership%
        shareholders = [r for r in rows[1:] if r and (r[0] if r else "").strip()]
        lines = ["👥 *Shareholders & Capital*", "━━━━━━━━━━━━━━━━━━"]
        total = 0
        if shareholders:
            for i, r in enumerate(shareholders, 1):
                name  = (r[0] if len(r) > 0 else "?").strip()
                role  = (r[1] if len(r) > 1 else "").strip() or "Partner"
                try:
                    cap = int(str(r[2] if len(r) > 2 else "0").replace(",", ""))
                except ValueError:
                    cap = 0
                try:
                    own = float(str(r[3] if len(r) > 3 else "0").replace(",", "").replace("%", ""))
                except ValueError:
                    own = 0.0
                total += cap
                role_icon = "🔑" if "operation" in role.lower() else "🤝"
                lines.append(
                    f"{i}. 👤 *{name}*\n"
                    f"   {role_icon} {role}  |  📊 {own:.0f}%\n"
                    f"   💰 {cap:,} Ks"
                )
            lines += ["━━━━━━━━━━━━━━━━━━", f"💼 *Total Capital : {total:,} Ks*"]
        else:
            lines.append("_(Shareholders မရှိသေးပါ)_")
    except Exception as e:
        lines = [f"❌ Error: {e}\n💡 ⚙️ Sheet Setup ဖြင့် Finance Sheets ဆောက်ပါ"]

    kb = [["➕ Shareholder ထည့်"], [BTN_FIN_BACK], [BTN_BACK_MAIN]]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SHARE_NAME


async def step_shareholder_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_BACK_MAIN:
        return await show_main_menu(update, context)
    if text == BTN_FIN_BACK:
        return await show_finance_menu(update, context)
    if text == "➕ Shareholder ထည့်":
        return await prompt_share_name(update, context)
    return await show_shareholder_menu(update, context)


async def prompt_share_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("fin", {})["flow"] = "shareholder"
    await update.message.reply_text(
        "👤 *Shareholder အသစ် — (1/4) နာမည်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Shareholder နာမည် ရိုက်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BTN_BACK, BTN_CANCEL]], resize_keyboard=True),
    )
    return SHARE_NAME


async def step_share_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK_MAIN:
        return await show_main_menu(update, context)
    if text == BTN_FIN_BACK:
        return await show_finance_menu(update, context)
    if text in (BTN_BACK, "➕ Shareholder ထည့်"):
        return await show_shareholder_menu(update, context)
    if not text:
        await update.message.reply_text("⚠️ နာမည် ရိုက်ပါ")
        return SHARE_NAME
    context.user_data["fin"]["share_name"] = text
    kb = [[r] for r in _SHARE_ROLES] + [[BTN_BACK, BTN_CANCEL]]
    await update.message.reply_text(
        f"👤 *{text}* — (2/4) Role ရွေးပါ",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SHARE_ROLE


async def step_share_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await prompt_share_name(update, context)
    if text not in _SHARE_ROLES:
        await update.message.reply_text("⚠️ Role ရွေးပါ")
        return SHARE_ROLE
    context.user_data["fin"]["share_role"] = text
    await update.message.reply_text(
        f"💼 *{text}* — (3/4) Capital ပမာဏ\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ထည့်ဝင်သော ငွေပမာဏ (Ks) ရိုက်ပါ\n(ဥပမာ: 150000000):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BTN_BACK, BTN_CANCEL]], resize_keyboard=True),
    )
    return SHARE_CAP


async def step_share_cap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [[r] for r in _SHARE_ROLES] + [[BTN_BACK, BTN_CANCEL]]
        await update.message.reply_text(
            "Role ပြန်ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return SHARE_ROLE
    try:
        cap = int(text.replace(",", ""))
        if cap <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ မှန်ကန်သော ပမာဏ ရိုက်ပါ")
        return SHARE_CAP
    context.user_data["fin"]["share_cap"] = cap
    kb = [["33"], ["34"], ["50"], ["100"], [BTN_BACK, BTN_CANCEL]]
    await update.message.reply_text(
        f"💰 *{cap:,} Ks* — (4/4) Ownership %\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ပိုင်ဆိုင်မှု ရာခိုင်နှုန်း ရိုက်ပါ (ဥပမာ: 33):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SHARE_OWN


async def step_share_own(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "Capital ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([[BTN_BACK, BTN_CANCEL]], resize_keyboard=True),
        )
        return SHARE_CAP
    try:
        own = float(text.replace("%", ""))
        if own < 0 or own > 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ 0–100 ကြား ဂဏန်း ရိုက်ပါ")
        return SHARE_OWN
    d = context.user_data["fin"]
    d["share_own"] = own
    kb = [[BTN_CONFIRM_SAVE], [BTN_BACK, BTN_CANCEL]]
    await update.message.reply_text(
        "👥 *Shareholder — အတည်ပြုချက်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 နာမည်    : *{d['share_name']}*\n"
        f"💼 Role     : *{d['share_role']}*\n"
        f"💰 Capital  : *{d['share_cap']:,} Ks*\n"
        f"📊 Ownership: *{own:.0f}%*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ Confirm & Save နှိပ်ပါ",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SHARE_CONFIRM


async def step_share_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [["33"], ["34"], ["50"], ["100"], [BTN_BACK, BTN_CANCEL]]
        await update.message.reply_text(
            "Ownership % ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return SHARE_OWN
    if text != BTN_CONFIRM_SAVE:
        await update.message.reply_text("✅ Confirm & Save နှိပ်ပါ")
        return SHARE_CONFIRM
    d = context.user_data["fin"]
    name = d["share_name"]
    role = d["share_role"]
    cap  = d["share_cap"]
    own  = d["share_own"]
    await update.message.reply_text("⏳ သိမ်းဆည်းနေသည်...")
    try:
        def _do():
            # Capital_Setup: A=Shareholder, B=Role, C=Capital(Ks), D=Ownership%
            sh = get_capital_sh()
            sh.append_row([name, role, cap, own], value_input_option="USER_ENTERED")
        await asyncio.to_thread(_do)
        await update.message.reply_text(
            f"✅ *Shareholder မှတ်တမ်း သိမ်းပြီး!*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 {name}  |  💼 {role}\n"
            f"💰 {cap:,} Ks  |  📊 {own:.0f}%",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Error: {e}\n💡 ⚙️ Sheet Setup ဖြင့် Capital_Setup sheet ဆောက်ပါ"
        )
    return await show_shareholder_menu(update, context)


# ─────────────────────────────────────────
#  Initial Capital Flow
# ─────────────────────────────────────────

CAPITAL_ACCOUNTS = ["Cash Box", "KBZ Bank", "MMQR", "AYA Bank"]

async def prompt_cap_acct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("fin", {})["flow"] = "capital"
    kb = [[a] for a in CAPITAL_ACCOUNTS] + [NAV_ROW]
    await update.message.reply_text(
        "🏦 *Initial Capital — Account ရွေးပါ*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ငွေ ထည့်မည့် Account ရွေးပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return CAP_ACCT


async def step_cap_acct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await show_finance_menu(update, context)
    if text not in CAPITAL_ACCOUNTS:
        await update.message.reply_text("⚠️ Account ရွေးပါ")
        return CAP_ACCT
    context.user_data["fin"]["cap_acct"] = text
    await update.message.reply_text(
        f"🏦 *{text}* — Initial Capital ပမာဏ\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ငွေပမာဏ ရိုက်ပါ (ဥပမာ: 300000000):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
    )
    return CAP_AMT


async def step_cap_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        kb = [[a] for a in CAPITAL_ACCOUNTS] + [NAV_ROW]
        await update.message.reply_text(
            "Account ပြန်ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return CAP_ACCT
    try:
        amt = int(text.replace(",", ""))
        if amt <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ မှန်ကန်သော ပမာဏ ရိုက်ပါ")
        return CAP_AMT
    context.user_data["fin"]["cap_amt"] = amt
    d = context.user_data["fin"]
    kb = [[BTN_CONFIRM_SAVE], NAV_ROW]
    await update.message.reply_text(
        "🏦 *Initial Capital — အတည်ပြုချက်*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🏦 Account  : *{d['cap_acct']}*\n"
        f"💰 Amount   : *{amt:,} Ks*\n"
        f"📅 Date     : *{today_str()}*\n"
        f"📋 Type     : *Opening Balance*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ Confirm & Save နှိပ်ပါ",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return CAP_CONFIRM


async def step_cap_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "Amount ပြန်ရိုက်ပါ:",
            reply_markup=ReplyKeyboardMarkup([NAV_ROW], resize_keyboard=True),
        )
        return CAP_AMT
    if text != BTN_CONFIRM_SAVE:
        await update.message.reply_text("✅ Confirm & Save နှိပ်ပါ")
        return CAP_CONFIRM
    d = context.user_data["fin"]
    acct = d["cap_acct"]
    amt  = d["cap_amt"]
    await update.message.reply_text("⏳ သိမ်းဆည်းနေသည်...")
    try:
        def _do():
            sh = wb.worksheet("Accounts")
            # Check if account row already exists; update if so, append if not
            rows = sh.get_all_values()
            for i, row in enumerate(rows[1:], start=2):
                if (row[0] if row else "").strip() == acct:
                    # Update opening balance column C (index 2)
                    sh.update_cell(i, 3, amt)
                    return "updated"
            # Append new row: Name, Type, Opening, Notes
            acct_type = "Bank" if ("bank" in acct.lower() or "kbz" in acct.lower() or "aya" in acct.lower()) else ("Digital" if "mmqr" in acct.lower() else "Cash")
            sh.append_row([acct, acct_type, amt, "Initial Capital"], value_input_option="USER_ENTERED")
            return "added"
        result = await asyncio.to_thread(_do)
        action = "အပ်ဒိတ်" if result == "updated" else "ထည့်သွင်း"
        await update.message.reply_text(
            f"✅ *Initial Capital {action}ပြီး!*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🏦 {acct}\n"
            f"💰 {amt:,} Ks\n\n"
            f"💡 Account Balances တွင် ယခု ပြသမည်",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Error: {e}\n\n"
            f"💡 ⚙️ Sheet Setup ကို ဦးစွာ လုပ်ဆောင်ပြီး Accounts sheet ဆောက်ပါ"
        )
    return await show_finance_menu(update, context)


# ─────────────────────────────────────────
#  Finance Reports
# ─────────────────────────────────────────

async def show_fin_report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [BTN_FIN_PNL,          BTN_FIN_BS],
        [BTN_FIN_DEPR,         BTN_FIN_PROFIT_SHARE],
        [BTN_FIN_BACK],
        [BTN_BACK_MAIN],
    ]
    await update.message.reply_text(
        "📊 *Finance Reports*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ကြည့်လိုသော Report ရွေးပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return FIN_REPORT_MENU


async def step_fin_report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == BTN_FIN_BACK:
        return await show_finance_menu(update, context)
    if choice == BTN_BACK_MAIN:
        return await show_main_menu(update, context)
    if choice == BTN_FIN_PNL:
        return await cmd_fin_pnl(update, context)
    if choice == BTN_FIN_BS:
        return await cmd_fin_bs(update, context)
    if choice == BTN_FIN_DEPR:
        return await cmd_fin_depr(update, context)
    if choice == BTN_FIN_PROFIT_SHARE:
        return await cmd_fin_profit_share(update, context)
    return await show_fin_report_menu(update, context)


async def cmd_fin_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch P&L from VPS API."""
    await update.message.reply_text("⏳ P&L ဆွဲယူနေသည်...")
    now = now_mmt()
    m   = now.strftime("%Y-%m")
    data = await asyncio.to_thread(_replit_get, f"finance/pnl?m={m}")
    if not data:
        await update.message.reply_text("❌ P&L API ချိတ်မရပါ — VPS စစ်ပါ")
        return await show_fin_report_menu(update, context)
    rev          = data.get("revenue", {})
    topup        = data.get("topup", {})
    opex_cats    = data.get("opex", {})
    total_opex   = data.get("total_opex", 0)
    depr         = data.get("depreciation", 0)
    disp_gl      = data.get("disposal_gain_loss", 0)
    ebit         = data.get("ebit", 0)
    om_bonus     = data.get("om_bonus", 0)
    net          = data.get("net_profit", 0)
    payroll      = opex_cats.get("Payroll", 0)
    other_opex   = total_opex - payroll
    total_costs  = total_opex + depr
    game_rev     = rev.get("game", 0)
    food_rev     = rev.get("food", 0)
    discounts    = rev.get("discounts", 0)
    topup_rev    = topup.get("total", 0)
    total_rev    = rev.get("total", 0)        # excludes topup — topup is liability, not revenue
    month_label  = now.strftime("%B %Y")
    lines = [
        f"📊 *P&L Report — {month_label}*",
        "━━━━━━━━━━━━━━━━━━",
        "💰 *Revenue*",
        f"  Game Play    : {game_rev:>12,} Ks",
        f"  Food & Drink : {food_rev:>12,} Ks",
    ]
    if discounts:
        lines.append(f"  Discount     :({discounts:>11,} Ks)")
    lines += [
        f"  ─────────────────────────",
        f"  Total Revenue: {total_rev:>12,} Ks",
        f"  TopUp (Liab) : {topup_rev:>12,} Ks",
        "━━━━━━━━━━━━━━━━━━",
        "📤 *Operating Costs*",
        f"  Payroll      : {payroll:>12,} Ks",
        f"  OPEX         : {other_opex:>12,} Ks",
        f"  Depreciation : {depr:>12,} Ks",
        f"  ─────────────────────────",
        f"  Total Costs  : {total_costs:>12,} Ks",
    ]
    if disp_gl != 0:
        label = "Disposal Gain" if disp_gl > 0 else "Disposal Loss"
        sign  = "+" if disp_gl > 0 else ""
        lines += [
            "━━━━━━━━━━━━━━━━━━",
            f"🔄 *Other Items*",
            f"  {label:<13}: {sign}{disp_gl:>10,} Ks",
        ]
    lines += [
        "━━━━━━━━━━━━━━━━━━",
        f"  EBIT         : {ebit:>12,} Ks",
    ]
    if om_bonus:
        lines.append(f"  OM Bonus(10%):({om_bonus:>11,} Ks)")
    lines += [
        f"{'✅' if net >= 0 else '🔴'} *Net Profit : {net:,} Ks*",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return await show_fin_report_menu(update, context)


async def cmd_fin_bs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch Balance Sheet from VPS API."""
    await update.message.reply_text("⏳ Balance Sheet ဆွဲယူနေသည်...")
    data = await asyncio.to_thread(_replit_get, "finance/balance-sheet")
    if not data:
        await update.message.reply_text("❌ Balance Sheet API ချိတ်မရပါ")
        return await show_fin_report_menu(update, context)
    assets     = data.get("assets", {})
    liab       = data.get("liabilities", {})
    equity     = data.get("equity", {})
    assets_tot = assets.get("total", 0)
    liab_tot   = liab.get("total", 0)
    equity_tot = equity.get("total", 0)
    cash_net   = assets.get("current_total", 0)
    fixed_tot  = assets.get("fixed_total", 0)
    receivables = assets.get("receivables", 0)
    advances_pending = assets.get("advances_pending", 0)
    member_liab = liab.get("member_liability", 0)
    payables    = liab.get("payables", 0)
    lines = [
        "🏦 *Balance Sheet*",
        "━━━━━━━━━━━━━━━━━━",
        "📦 *Assets*",
        f"  Cash (Net)    : {cash_net:>12,} Ks",
        f"  Fixed Assets  : {fixed_tot:>12,} Ks",
        f"  Receivables   : {receivables:>12,} Ks",
    ]
    if advances_pending:
        lines.append(f"  Adv. Pending  : {advances_pending:>12,} Ks")
    lines += [
        f"  ─────────────────────────",
        f"  Total Assets  : {assets_tot:>12,} Ks",
        "━━━━━━━━━━━━━━━━━━",
        "📤 *Liabilities*",
        f"  Member Liab   : {member_liab:>12,} Ks",
        f"  Payables      : {payables:>12,} Ks",
        f"  ─────────────────────────",
        f"  Total Liab    : {liab_tot:>12,} Ks",
        "━━━━━━━━━━━━━━━━━━",
        f"💼 *Equity : {equity_tot:,} Ks*",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return await show_fin_report_menu(update, context)


async def cmd_fin_accts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch account balances from VPS API. Always returns to Finance main menu."""
    await update.message.reply_text("⏳ Account Balances ဆွဲယူနေသည်...")
    data = await asyncio.to_thread(_replit_get, "finance/accounts")
    if not data:
        await update.message.reply_text(
            "❌ Accounts API ချိတ်မရပါ\n"
            "💡 ⚙️ Sheet Setup ဖြင့် Finance Sheets ဆောက်ပါ"
        )
        return await show_finance_menu(update, context)
    accounts  = data.get("accounts", [])
    total_bal = data.get("total_balance", 0)
    if not accounts:
        await update.message.reply_text(
            "⚠️ Account မှတ်တမ်း မရှိသေးပါ\n"
            "💡 🏦 Initial Capital ဖြင့် Account ငွေ စတင်ထည့်ပါ"
        )
        return await show_finance_menu(update, context)
    lines = ["💰 *Account Balances*", "━━━━━━━━━━━━━━━━━━"]
    for a in accounts:
        name = a.get("name", "?")
        bal  = a.get("balance", a.get("opening", 0))
        low  = name.lower()
        icon = "🏦" if ("bank" in low or "kbz" in low or "aya" in low) else ("📱" if "mmqr" in low else "💵")
        lines.append(f"{icon} {name:<16}: {int(bal):>10,} Ks")
    lines += ["━━━━━━━━━━━━━━━━━━", f"  *Total : {int(total_bal):,} Ks*"]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return await show_finance_menu(update, context)


async def cmd_fin_depr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show depreciation summary from VPS API."""
    await update.message.reply_text("⏳ Depreciation ဆွဲယူနေသည်...")
    now_yr = now_mmt().year
    data = await asyncio.to_thread(_replit_get, f"finance/depreciation?year={now_yr}")
    if not data:
        await update.message.reply_text("❌ Depreciation API ချိတ်မရပါ")
        return await show_fin_report_menu(update, context)
    schedule   = data.get("schedule", [])
    year_total = data.get("year_total", 0)
    if not schedule:
        await update.message.reply_text("📉 Assets_Register တွင် မှတ်တမ်းမရှိပါ")
        return await show_fin_report_menu(update, context)
    lines = [f"📉 *Depreciation — {now_yr}*", "━━━━━━━━━━━━━━━━━━"]
    for a in schedule[:10]:
        name    = a.get("name", "?")[:20]
        annual  = a.get("year_total", 0)
        bv_end  = a.get("book_value_end", 0)
        dep_mo  = round(annual / 12) if annual else 0
        lines.append(f"🏷️ *{name}*\n   📉 {dep_mo:,}/mo  |  Annual: {annual:,} Ks\n   📘 Book Value (yr-end): {bv_end:,} Ks")
    lines += ["━━━━━━━━━━━━━━━━━━", f"📊 *Total Dep {now_yr}: {year_total:,} Ks*"]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return await show_fin_report_menu(update, context)


async def cmd_fin_profit_share(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show profit sharing distribution."""
    await update.message.reply_text("⏳ Profit Sharing ဆွဲယူနေသည်...")
    now  = now_mmt()
    m    = now.strftime("%Y-%m")
    data = await asyncio.to_thread(_replit_get, f"finance/profit-sharing?m={m}")
    if not data:
        await update.message.reply_text("❌ Profit Sharing API ချိတ်မရပါ")
        return await show_fin_report_menu(update, context)
    ebit         = data.get("ebit", 0)
    om_bonus     = data.get("om_bonus", 0)
    distributable = data.get("distributable_profit", 0)
    shareholders = data.get("shareholders", [])
    month_label  = now.strftime("%B %Y")
    lines = [
        f"💸 *Profit Sharing — {month_label}*",
        "━━━━━━━━━━━━━━━━━━",
        f"💰 Net Profit   : *{ebit:,} Ks*",
        f"🎯 OM Bonus     : *{om_bonus:,} Ks*",
        f"📊 Distributable: *{distributable:,} Ks*",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for s in shareholders:
        name      = s.get("name", "?")
        pct       = s.get("ownership", 0)
        dividend  = s.get("dividend", 0)
        s_bonus   = s.get("om_bonus", 0)
        total_inc = s.get("total_income", 0)
        if s_bonus:
            lines.append(f"👤 {name} ({pct}%) : {dividend:,} + {s_bonus:,} bonus = *{total_inc:,} Ks*")
        else:
            lines.append(f"👤 {name} ({pct}%) : {total_inc:,} Ks")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return await show_fin_report_menu(update, context)


# ─────────────────────────────────────────
#  Finance Sheet Setup
# ─────────────────────────────────────────

async def cmd_finance_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create all Finance sheets via VPS API."""
    await update.message.reply_text(
        "⚙️ Finance Sheets ဆောက်နေသည်...\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Capital Setup | Assets Register | OPEX Log\n"
        "Accounts | Account Transfers\n"
        "Payables | Receivables | Advance Staff\n"
        "Prepaid Expenses | Advance Payments",
    )
    result = await asyncio.to_thread(_replit_post, "finance/setup-sheets", {}, 90)
    if result and result.get("ok"):
        created = result.get("created", [])
        skipped = result.get("skipped", [])
        created_str = ", ".join(created) if created else "None"
        skipped_str = ", ".join(skipped) if skipped else "None"
        await update.message.reply_text(
            f"✅ Finance Sheets ပြင်ဆင်ပြီး!\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"✅ Created : {len(created)} sheets\n"
            f"⏩ Skipped : {len(skipped)} (ရှိပြီးသား)\n\n"
            f"Created: {created_str}\n"
            f"Skipped: {skipped_str}",
        )
    else:
        err = result.get("error", "unknown") if result else "API ချိတ်မရ"
        await update.message.reply_text(f"❌ Setup မအောင်မြင်ပါ: {err}")
    return await show_finance_menu(update, context)


# ─────────────────────────────────────────
#  /finance shortcut command
# ─────────────────────────────────────────

async def cmd_finance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: /finance — PIN then Finance menu."""
    return await _pin_then("finance", "Finance", update, context)


# ═════════════════════════════════════════
#  ADMIN PANEL
# ═════════════════════════════════════════

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: /admin — Admin Panel PIN prompt."""
    return await _pin_then("admin", "Admin Panel", update, context)


async def cmd_payroll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: /payroll — PIN then payroll."""
    return await _pin_then("payroll", "Payroll", update, context)


async def cmd_kpi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: /kpi — PIN then staff KPI."""
    return await _pin_then("kpi", "Staff KPI", update, context)


async def cmd_setattend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: /setattend — PIN then attendance."""
    return await _pin_then("setattend", "Attendance", update, context)


async def _pin_then(cmd_key: str, label: str, update, context):
    """Prompt for admin PIN then route to cmd_key after success."""
    context.user_data.clear()
    context.user_data["_after_pin"] = cmd_key
    await update.message.reply_text(
        f"🔐 *{label} — PIN လိုအပ်သည်*\n\nPIN နံပါတ် ထည့်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ADMIN_PIN


async def step_admin_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify Admin PIN — delete message then route."""
    entered = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if entered != STOCK_ACCESS_PIN:
        await update.message.reply_text(
            "❌ PIN မမှန်ကန်ပါ။\n\nMain Menu သို့ ပြန်သွားမည်။",
            reply_markup=ReplyKeyboardRemove(),
        )
        return await show_main_menu(update, context)

    # Route to specific command if called via direct /cmd shortcut
    after = context.user_data.pop("_after_pin", None)
    if after == "payroll":
        return await cmd_payroll(update, context)
    if after == "kpi":
        return await cmd_staff_kpi(update, context)
    if after == "setattend":
        return await cmd_setattend(update, context)
    if after == "finance":
        return await show_finance_menu(update, context)
    return await show_admin_menu(update, context)


async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Admin sub-menu."""
    kb = [
        [BTN_STOCK_UPDATE,   BTN_ADMIN_ATTEND],
        [BTN_ADMIN_SAL_ADV,  BTN_PAYROLL],
        [BTN_STAFF_KPI,      BTN_ADMIN_LIB],
        [BTN_ADMIN_PNL,      BTN_ADMIN_CF],
        [BTN_FINANCE,        BTN_CON_MANAGE],
        [BTN_BACK_MAIN],
    ]
    await update.message.reply_text(
        "🔧 *Admin Panel*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Action ရွေးပါ ↓",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ADMIN_MENU


async def step_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route Admin menu choices."""
    choice = update.message.text.strip()

    if choice == BTN_BACK_MAIN:
        return await show_main_menu(update, context)
    if choice == BTN_STOCK_UPDATE:
        return await show_stock_menu(update, context)
    if choice == BTN_ADMIN_ATTEND:
        return await cmd_setattend(update, context)
    if choice == BTN_ADMIN_SAL_ADV:
        return await cmd_admin_sal_adv(update, context)
    if choice == BTN_PAYROLL:
        return await cmd_payroll(update, context)
    if choice == BTN_STAFF_KPI:
        return await cmd_staff_kpi(update, context)
    if choice == BTN_ADMIN_PNL:
        return await cmd_admin_pnl(update, context)
    if choice == BTN_ADMIN_CF:
        return await cmd_admin_cashflow(update, context)
    if choice == BTN_ADMIN_LIB:
        return await cmd_admin_liability(update, context)
    if choice == BTN_ADMIN_BOOK:
        return await cmd_admin_bookings(update, context)
    if choice == BTN_STAFF_BOOK:
        return await cmd_staff_booking(update, context)
    if choice == BTN_SBK_CONFIRMED:
        return await cmd_confirmed_bookings(update, context)
    if choice == BTN_CONSOLES:
        return await show_console_menu(update, context)
    if choice == BTN_CON_MANAGE:
        return await show_con_mgmt_menu(update, context)
    if choice == BTN_FINANCE:
        return await show_finance_menu(update, context)

    return await show_admin_menu(update, context)


# ─────────────────────────────────────────
#  SALARY ADVANCE — helper + flow
# ─────────────────────────────────────────

def fetch_salary_advances(month_str: str) -> dict[str, dict]:
    """Return {staff: {total, cash, kpay}} for the given month (YYYY-MM).
    Sheet cols: A=Date, B=Staff, C=Amount, D=Payment(Cash/KPay), E=Note
    """
    year_i, mon_i = int(month_str[:4]), int(month_str[5:7])
    result: dict[str, dict] = {}
    try:
        sh = get_salary_adv_sh()
        for row in sh.get_all_values()[1:]:
            if len(row) < 3 or not row[0].strip():
                continue
            d = _parse_date_mmt(row[0].strip())
            if not d or d.year != year_i or d.month != mon_i:
                continue
            staff   = row[1].strip()
            amount  = _int(row[2])
            payment = row[3].strip().lower() if len(row) > 3 else "cash"
            if staff and amount > 0:
                if staff not in result:
                    result[staff] = {"total": 0, "cash": 0, "kpay": 0}
                result[staff]["total"] += amount
                if "kpay" in payment:
                    result[staff]["kpay"] += amount
                else:
                    result[staff]["cash"] += amount
    except Exception as e:
        logging.warning("fetch_salary_advances: %s", e)
    return result


async def cmd_admin_sal_adv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: start Salary Advance recording."""
    staff_list = fetch_staff()
    if not staff_list:
        await update.message.reply_text("❌ Staff list ရှာမတွေ့ပါ။")
        return await show_admin_menu(update, context)

    kb = [[s] for s in staff_list] + [[BTN_BACK_MAIN]]
    await update.message.reply_text(
        "💸 *Salary Advance မှတ်တမ်း*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Advance ပေးမည့် Staff ကို ရွေးပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SAL_ADV_STAFF


async def step_sal_adv_staff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == BTN_BACK_MAIN:
        return await show_admin_menu(update, context)

    staff_list = fetch_staff()
    if choice not in staff_list:
        await update.message.reply_text("❌ Staff မတွေ့ပါ။ ထပ်မံ ရွေးပါ:")
        return SAL_ADV_STAFF

    context.user_data["sal_adv_staff"] = choice
    await update.message.reply_text(
        f"💸 *{choice}* — Salary Advance\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"ပေးမည့် ပမာဏ (Ks) ရိုက်ထည့်ပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return SAL_ADV_AMT


async def step_sal_adv_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = int(update.message.text.strip().replace(",", "").replace(".", ""))
        if amt <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ ငွေပမာဏ မှန်ကန်စွာ ထည့်ပါ (ဂဏန်းများသာ):")
        return SAL_ADV_AMT

    context.user_data["sal_adv_amt"] = amt
    kb = [["💵 Cash", "💙 KPay"], [BTN_BACK_MAIN]]
    await update.message.reply_text(
        f"💸 ပေးချေနည်း ရွေးပါ:\n"
        f"Amount: *{amt:,} Ks*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SAL_ADV_PAY


async def step_sal_adv_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == BTN_BACK_MAIN:
        return await show_admin_menu(update, context)

    if "kpay" in choice.lower() or "KPay" in choice:
        payment = "KPay"
    elif "cash" in choice.lower() or "Cash" in choice:
        payment = "Cash"
    else:
        await update.message.reply_text("❌ Cash သို့မဟုတ် KPay ရွေးပါ:")
        return SAL_ADV_PAY

    context.user_data["sal_adv_pay"] = payment
    staff     = context.user_data["sal_adv_staff"]
    amt       = context.user_data["sal_adv_amt"]
    today_str = now_mmt().strftime("%m/%d/%Y")
    pay_icon  = "💵" if payment == "Cash" else "💙"

    kb = [["✅ အတည်ပြုမည်"], [BTN_BACK_MAIN]]
    await update.message.reply_text(
        f"💸 *Salary Advance အတည်ပြုချက်*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👔 Staff    : *{staff}*\n"
        f"💰 Amount   : *{amt:,} Ks*\n"
        f"{pay_icon} Payment  : *{payment}*\n"
        f"📅 Date     : *{today_str}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"မှန်ပါသလား?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SAL_ADV_CONFIRM


async def step_sal_adv_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == BTN_BACK_MAIN or choice != "✅ အတည်ပြုမည်":
        await update.message.reply_text("❌ ပယ်ဖျက်လိုက်သည်။")
        return await show_admin_menu(update, context)

    staff     = context.user_data.get("sal_adv_staff", "")
    amt       = context.user_data.get("sal_adv_amt", 0)
    payment   = context.user_data.get("sal_adv_pay", "Cash")
    today_str = now_mmt().strftime("%m/%d/%Y")
    month_str = now_mmt().strftime("%Y-%m")
    pay_icon  = "💵" if payment == "Cash" else "💙"

    try:
        sh = get_salary_adv_sh()
        sh.append_row([today_str, staff, amt, payment, ""])
        advances  = fetch_salary_advances(month_str)
        staff_adv = advances.get(staff, {"total": 0, "cash": 0, "kpay": 0})
        cum       = staff_adv["total"]
        await update.message.reply_text(
            f"✅ *Salary Advance မှတ်တမ်းသွင်းပြီး*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👔 {staff}\n"
            f"💰 Amount   : *{amt:,} Ks*\n"
            f"{pay_icon} Payment  : *{payment}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 ဒီလ စုစုပေါင်း Advance: *{cum:,} Ks*\n"
            f"  💵 Cash: {staff_adv['cash']:,} + 💙 KPay: {staff_adv['kpay']:,}\n"
            f"_(လစာချိန်မှာ ဒီပမာဏ နုတ်မည်)_",
            parse_mode="Markdown",
        )
    except Exception as e:
        logging.error("step_sal_adv_confirm: %s", e)
        await update.message.reply_text(f"❌ Error: {e}")

    context.user_data.pop("sal_adv_staff", None)
    context.user_data.pop("sal_adv_amt", None)
    context.user_data.pop("sal_adv_pay", None)
    return await show_admin_menu(update, context)


# ─────────────────────────────────────────
#  ADMIN FINANCIAL REPORTS — helper
# ─────────────────────────────────────────

def _parse_date_mmt(val: str):
    for fmt in ("%m/%d/%Y", "%-m/%-d/%Y"):
        try:
            return datetime.strptime(val.strip(), fmt)
        except ValueError:
            continue
    return None


def fetch_alltime_effective_rate() -> float:
    """All-time average Ks/min across every TopUp_Log row (incl. bonus mins).

    This is the only correct rate to use for both:
      - Member earned revenue  (mins used × this rate)
      - Card Liability         (wallet balance mins × this rate)

    Example: member paid 90,000 for 600 base + 100 bonus = 700 total mins
             → rate = 90,000 / 700 = 128.57 Ks/min  (NOT 166.67 Ks/min)
    """
    total_paid = 0
    total_mins = 0
    try:
        for row in topup_sh.get_all_values()[1:]:
            if len(row) < 8:
                continue
            amt  = _int(row[4])
            mins = _int(row[7])   # col H = AddedMins (including bonus)
            if amt > 0 and mins > 0:
                total_paid += amt
                total_mins += mins
    except Exception as e:
        logging.warning("fetch_alltime_effective_rate: %s", e)
    if total_mins > 0:
        return round(total_paid / total_mins, 2)
    # Hard fallback — avoids using naive base_rate
    return fetch_base_rate() or 150.0


def calc_monthly_pnl(month_str: str) -> dict:
    """Aggregate monthly P&L, cash-flow and liability data from all sheets."""
    year_i, mon_i = int(month_str[:4]), int(month_str[5:7])

    # Per-member rates (col L) — fall back to all-time avg if not yet stored
    alltime_rate = fetch_alltime_effective_rate()
    rate_dict    = build_member_rate_dict()   # {member_id: stored_rate}

    res = dict(
        guest_game_rev=0,
        food_rev=0,
        discount_total=0,
        wallet_deduct_mins=0,
        topup_amount=0, topup_kpay=0, topup_cash=0, topup_mins=0,
        sales_kpay=0,           # KPay received from all sales rows (ops cash in)
        sales_cash=0,           # Cash received from all sales rows (ops cash in)
        stock_in_total=0,
        stock_in_cash=0,
        stock_in_kpay=0,
        stock_out_cogs=0,
        payroll_total=0,        # gross salary expense (P&L)
        payroll_advance=0,      # advances already paid mid-month (total)
        payroll_advance_cash=0, # advance portion paid by Cash
        payroll_advance_kpay=0, # advance portion paid by KPay
        payroll_net_pay=0,      # remaining payout at month end
        effective_rate=alltime_rate,
        alltime_rate=alltime_rate,
        member_game_rev=0,
    )

    # 1. Sales_Daily ─ col layout: A=date B=v_no C=member D=console E=play_mins
    #                               F=game_amt G=food_total H=discount I=net_total
    #                               J=kpay K=cash ... N=wallet_deduct(idx13)
    member_deduct: dict[str, int] = {}
    try:
        for row in sales_sh.get_all_values()[1:]:
            if len(row) < 7:
                continue
            d = _parse_date_mmt(row[0])
            if not d or d.year != year_i or d.month != mon_i:
                continue
            member_id  = row[2].strip() if len(row) > 2 else ""
            food_total = _int(row[6]) if len(row) > 6 else 0
            discount   = _int(row[7]) if len(row) > 7 else 0
            kpay       = _int(row[9]) if len(row) > 9 else 0
            cash       = _int(row[10]) if len(row) > 10 else 0
            res["food_rev"]       += food_total
            res["discount_total"] += discount
            # ALL kpay/cash from sales = operating cash received (guest game + food)
            res["sales_kpay"] += kpay
            res["sales_cash"] += cash
            is_guest = member_id in ("", "0 (Guest)")
            if is_guest:
                game_amt = _int(row[5]) if len(row) > 5 else 0
                res["guest_game_rev"] += game_amt
            else:
                w_deduct = _int(row[13]) if len(row) > 13 else 0
                res["wallet_deduct_mins"] += w_deduct
                member_deduct[member_id] = member_deduct.get(member_id, 0) + w_deduct
    except Exception as e:
        logging.warning("calc_pnl sales: %s", e)

    # 2. TopUp_Log
    try:
        for row in topup_sh.get_all_values()[1:]:
            if len(row) < 8:
                continue
            d = _parse_date_mmt(row[0])
            if not d or d.year != year_i or d.month != mon_i:
                continue
            res["topup_amount"] += _int(row[4])
            res["topup_kpay"]   += _int(row[5]) if len(row) > 5 else 0
            res["topup_cash"]   += _int(row[6]) if len(row) > 6 else 0
            res["topup_mins"]   += _int(row[7]) if len(row) > 7 else 0
    except Exception as e:
        logging.warning("calc_pnl topup: %s", e)

    # Per-member revenue: deducted_mins × each member's own stored rate
    # Falls back to alltime_rate for members without a stored rate (legacy / missing)
    member_game_rev = 0
    for m_id, mins in member_deduct.items():
        rate = rate_dict.get(m_id, alltime_rate)
        member_game_rev += int(mins * rate)
    res["member_game_rev"] = member_game_rev

    # 3. Stock_In (purchases)
    try:
        for row in stock_in_sh.get_all_values()[1:]:
            if len(row) < 5:
                continue
            d = _parse_date_mmt(row[0])
            if not d or d.year != year_i or d.month != mon_i:
                continue
            total   = _int(row[4])
            payment = row[5].strip() if len(row) > 5 else ""
            res["stock_in_total"] += total
            # Parse "Cash X / KPay Y" or plain "Cash" / "KPay"
            if "/" in payment:
                parts = payment.split("/")
                for p in parts:
                    p = p.strip()
                    if p.lower().startswith("cash"):
                        res["stock_in_cash"] += _int("".join(filter(lambda c: c.isdigit(), p)))
                    elif p.lower().startswith("kpay"):
                        res["stock_in_kpay"] += _int("".join(filter(lambda c: c.isdigit(), p)))
            elif payment.lower() == "cash":
                res["stock_in_cash"] += total
            elif payment.lower() == "kpay":
                res["stock_in_kpay"] += total
    except Exception as e:
        logging.warning("calc_pnl stock_in: %s", e)

    # 4. Stock_Out COGS (col H = idx 7)
    try:
        for row in stock_sh.get_all_values()[1:]:
            if len(row) < 8:
                continue
            d = _parse_date_mmt(row[0])
            if not d or d.year != year_i or d.month != mon_i:
                continue
            res["stock_out_cogs"] += _int(row[7])
    except Exception as e:
        logging.warning("calc_pnl stock_out: %s", e)

    # 5. Payroll — gross salary expense + advance breakdown
    try:
        payroll = calc_monthly_payroll(month_str)
        res["payroll_total"]         = sum(p["grand_total"]    for p in payroll)
        res["payroll_advance"]       = sum(p.get("advance",      0) for p in payroll)
        res["payroll_advance_cash"]  = sum(p.get("advance_cash", 0) for p in payroll)
        res["payroll_advance_kpay"]  = sum(p.get("advance_kpay", 0) for p in payroll)
        res["payroll_net_pay"]       = sum(p.get("net_total",    0) for p in payroll)
    except Exception as e:
        logging.warning("calc_pnl payroll: %s", e)

    return res


async def cmd_admin_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: Monthly P&L — revenue recognition basis (via API cache)."""
    month_label = now_mmt().strftime("%B %Y")
    await update.message.reply_text(
        f"⏳ *{month_label}* P&L တွက်နေသည်...",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    try:
        r = _replit_get("sheets/pnl")
        if "error" in r:
            raise RuntimeError(r["error"])
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return await show_admin_menu(update, context)

    game_rev    = r["guest_game_rev"] + r["member_game_rev"]
    total_rev   = game_rev + r["food_rev"]
    net_rev     = total_rev - r["discount_total"]
    total_cost  = r["stock_out_cogs"] + r["payroll_total"]
    net_profit  = net_rev - total_cost

    adv_received = r["topup_amount"]
    adv_earned   = r["member_game_rev"]
    adv_delta    = adv_received - adv_earned   # +ve = liability grew, -ve = liability shrank

    def _ks(v):  return f"{v:,} Ks"
    def _neg(v): return f"({abs(v):,} Ks)"
    profit_icon = "🟢" if net_profit >= 0 else "🔴"

    rate_line = (
        f"   ↳ {r['wallet_deduct_mins']:,} mins × {r['effective_rate']:,.1f} Ks/min\n"
        if r["wallet_deduct_mins"] > 0 else ""
    )
    adv_line = (
        f"   ↳ Advance: {r['payroll_advance']:,}  |  Remaining: {r['payroll_net_pay']:,}\n"
        if r.get("payroll_advance", 0) > 0 else ""
    )
    adv_delta_tag = f"+{adv_delta:,}" if adv_delta >= 0 else f"({abs(adv_delta):,})"
    adv_dir  = "⬆️ Liability ↑" if adv_delta >= 0 else "⬇️ Liability ↓"

    msg = (
        f"📊 *P&L — {month_label}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💹 *REVENUE (Earned)*\n"
        f"  🎮 Guest Game   : *{_ks(r['guest_game_rev'])}*\n"
        f"  💳 Member Game  : *{_ks(r['member_game_rev'])}*\n"
        f"{rate_line}"
        f"  🍔 Food & Drink : *{_ks(r['food_rev'])}*\n"
        f"  🏷️ Discount     : *{_neg(r['discount_total'])}*\n"
        f"  ─────────────────\n"
        f"  💰 Net Revenue  : *{_ks(net_rev)}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💸 *COSTS*\n"
        f"  📦 Food COGS    : *{_neg(r['stock_out_cogs'])}*\n"
        f"  👔 Payroll      : *{_neg(r['payroll_total'])}*\n"
        f"{adv_line}"
        f"  ─────────────────\n"
        f"  📤 Total Costs  : *{_neg(total_cost)}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{profit_icon} *Net Profit : "
        f"{'(' + str(abs(net_profit)) + ')' if net_profit < 0 else str(net_profit)} Ks*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💳 *Member Card Advance*\n"
        f"  📨 Topup ဝင်    : *+{_ks(adv_received)}*\n"
        f"  ✅ Earned (used) : *{_neg(adv_earned)}*\n"
        f"  ─────────────────\n"
        f"  {adv_dir} : *{adv_delta_tag} Ks*\n"
        f"  _({r['topup_mins']:,} mins ထည့်  |  {r['wallet_deduct_mins']:,} mins သုံး)_\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"_Rate: {r['effective_rate']:,.1f} Ks/min_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")
    return await show_admin_menu(update, context)


async def cmd_admin_cashflow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: Monthly Cash Flow — all actual money movements (via API cache)."""
    month_label = now_mmt().strftime("%B %Y")
    await update.message.reply_text(
        f"⏳ *{month_label}* Cash Flow တွက်နေသည်...",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    try:
        r = _replit_get("sheets/pnl")
        if "error" in r:
            raise RuntimeError(r["error"])
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return await show_admin_menu(update, context)

    sal_advance     = r.get("payroll_advance", 0)
    sal_net_pay     = r.get("payroll_net_pay", 0)
    sal_gross       = r["payroll_total"]
    sal_adv_cash    = r.get("payroll_advance_cash", 0)
    sal_adv_kpay    = r.get("payroll_advance_kpay", 0)

    # Cash IN
    sales_cash  = r["sales_cash"]
    sales_kpay  = r["sales_kpay"]
    topup_cash  = r["topup_cash"]
    topup_kpay  = r["topup_kpay"]
    total_in    = sales_cash + sales_kpay + topup_cash + topup_kpay

    # Cash OUT
    stock_out   = r["stock_in_total"]
    total_out   = stock_out + sal_gross
    net_cash    = total_in - total_out

    def _ks(v):   return f"{v:,} Ks"
    def _neg(v):  return f"({abs(v):,} Ks)"
    def _icon(v): return "🟢" if v >= 0 else "🔴"

    # Payroll breakdown
    if sal_advance > 0:
        pay_line = (
            f"  👔 Payroll       : *{_neg(sal_gross)}*\n"
            f"     Advance ထုတ်   : ({sal_advance:,})  Cash {sal_adv_cash:,} / KPay {sal_adv_kpay:,}\n"
            f"     Month-end ကျန် : ({sal_net_pay:,})\n"
        )
    else:
        pay_line = f"  👔 Payroll       : *{_neg(sal_gross)}*\n"

    msg = (
        f"💵 *Cash Flow — {month_label}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🟢 *CASH ဝင် (IN)*\n"
        f"  Sales Cash        : *{_ks(sales_cash)}*\n"
        f"  Sales KPay        : *{_ks(sales_kpay)}*\n"
        f"  Member Topup Cash : *{_ks(topup_cash)}*\n"
        f"  Member Topup KPay : *{_ks(topup_kpay)}*\n"
        f"  ─────────────────\n"
        f"  💰 Total IN       : *{_ks(total_in)}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔴 *CASH ထွက် (OUT)*\n"
        f"  📦 Stock ဝယ်       : *{_neg(stock_out)}*\n"
        f"     Cash {r['stock_in_cash']:,} / KPay {r['stock_in_kpay']:,}\n"
        f"{pay_line}"
        f"  ─────────────────\n"
        f"  📤 Total OUT      : *{_neg(total_out)}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{_icon(net_cash)} *Net Cash : "
        f"{'(' + str(abs(net_cash)) + ')' if net_cash < 0 else str(net_cash)} Ks*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 *မှတ်ချက်*\n"
        f"  Member Topup ➜ Wallet liability (မိနစ်ကြွေးကျန်)\n"
        f"  {r['topup_mins']:,} mins ထည့်  |  {r['wallet_deduct_mins']:,} mins သုံး"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")
    return await show_admin_menu(update, context)


async def cmd_admin_liability(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: Advance Payment Liability — via API cache (no direct sheet reads)."""
    await update.message.reply_text(
        "⏳ Advance Payment Liability တွက်နေသည်...",
        reply_markup=ReplyKeyboardRemove(),
    )
    try:
        liab = _replit_get("sheets/liability")
        pnl  = _replit_get("sheets/pnl")
        if "error" in liab:
            raise RuntimeError(liab["error"])

        active_count    = liab["active_count"]
        total_mins      = liab["total_mins"]
        total_liability = liab["total_liability"]
        alltime_rate    = liab["alltime_rate"]
        stored_count    = liab["stored_rate_count"]
        top_members     = liab["top_members"]   # [{id,name,mins,liability,rate}]

        # This month movement from pnl cache
        month_label = now_mmt().strftime("%B %Y")
        if "error" not in pnl:
            adv_received = pnl["topup_amount"]
            adv_earned   = pnl["member_game_rev"]
            adv_net      = adv_received - adv_earned
            move_sec = (
                f"📅 *This Month ({month_label})*\n"
                f"  📨 Topups     : +{adv_received:,} Ks\n"
                f"  ✅ Earned     : ({adv_earned:,} Ks)\n"
                f"  {'⬆️' if adv_net>=0 else '⬇️'} Net Change : "
                f"{'+'if adv_net>=0 else ''}{adv_net:,} Ks\n"
                f"━━━━━━━━━━━━━━━━━━\n"
            )
        else:
            move_sec = ""

        top_lines = "\n".join(
            f"  {i+1}. *{m['id']}* {m['name'][:9]} — {m['mins']:,} min × {m['rate']:.1f} = *{m['liability']:,} Ks*"
            for i, m in enumerate(top_members)
        )
        top_sec = f"🔝 *Top 5 (Highest Liability)*\n{top_lines}\n━━━━━━━━━━━━━━━━━━\n" if top_lines else ""

        rate_note = (
            f"   _(Per-member rate — {stored_count} stored, "
            f"{max(0, active_count - stored_count)} using avg fallback)_\n"
        )

        msg = (
            f"💳 *Advance Payment Liability*\n"
            f"_(Member Card Unearned Balances)_\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👥 Active Members   : *{active_count}*\n"
            f"⏱️ Total Balance    : *{total_mins:,} mins*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 *Total Liability : {total_liability:,} Ks*\n"
            f"{rate_note}"
            f"   _(ကျွန်တော်တို့ Member များသို့ ရှင်ပေးရဦးမည့် ငွေ)_\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{move_sec}"
            f"{top_sec}"
        )
    except Exception as e:
        logging.error("cmd_admin_liability: %s", e)
        msg = f"❌ Error: {e}"

    await update.message.reply_text(msg, parse_mode="Markdown")
    return await show_admin_menu(update, context)


# ═════════════════════════════════════════
#  STAFF ADVANCE BOOKING FLOW
# ═════════════════════════════════════════

def _sbk_console_kb() -> list:
    """Return keyboard of all consoles with live+reserved status via API."""
    try:
        data = _replit_get("sheets/consoles")
        consoles = data.get("consoles", []) if isinstance(data, dict) else []
    except Exception:
        consoles = []
    if not consoles:
        # fallback to local fetch
        try:
            consoles = [{"id": c["id"], "type": c.get("type",""), "liveStatus": c.get("status","Free")}
                        for c in fetch_console_status()]
        except Exception:
            return [[c] for c in sorted(VALID_CONSOLES)] + [[BTN_BACK, BTN_CANCEL]]
    rows = []
    row  = []
    for c in sorted(consoles, key=lambda x: x["id"]):
        live = c.get("liveStatus", "Free")
        if live == "Free":
            icon = "✅"
        elif live == "Reserved":
            icon = "🟡"
        else:
            icon = "🔴"
        label = f"{c['id']} ({c.get('type','?')}) {icon}"
        row.append(label)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([BTN_BACK, BTN_CANCEL])
    return rows


def _sbk_parse_console_label(text: str) -> tuple[str, str]:
    """Extract (console_id, console_type) from keyboard label like 'C - 01 (PS5 Pro) ✅'."""
    # Format: "C - 01 (PS5 Pro) ✅" or "C - 01 (PS5) 🔴"
    import re
    m = re.match(r"^(C\s*-\s*\d+)\s*\(([^)]+)\)", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    cid = text.split("(")[0].strip().split("✅")[0].strip().split("🔴")[0].strip().split("🟡")[0].strip()
    return cid, "PS Console"


async def cmd_staff_book_hub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Booking hub: show pending bookings + navigation to confirmed bookings."""
    context.user_data["sbk_from_hub"] = True

    # Fetch counts for both statuses in parallel (sync thread)
    pending_bks   = _replit_get("bookings?status=pending") or []
    confirmed_bks = _replit_get("bookings?status=confirmed") or []

    n_pending   = len(pending_bks)   if isinstance(pending_bks,   list) else 0
    n_confirmed = len(confirmed_bks) if isinstance(confirmed_bks, list) else 0

    await update.message.reply_text(
        f"📅 *Customer Booking*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 Pending: *{n_pending}* ခု  |  ✅ Confirmed: *{n_confirmed}* ခု",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [[BTN_SBK_NEW], [BTN_SBK_CONFIRMED], [BTN_BACK_MAIN]],
            resize_keyboard=True,
        ),
    )

    if not pending_bks:
        await update.message.reply_text(
            "📋 *Pending Bookings မရှိပါ*",
            parse_mode="Markdown",
        )
        return MAIN_MENU

    await update.message.reply_text(
        f"📋 *Pending Bookings — {n_pending} ခု*\n"
        f"━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
    )
    for b in pending_bks:
        card = (
            f"🎫 *Booking #{b['id']}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 {b['customerName']}  📞 {b.get('phone') or '—'}\n"
            f"📅 {b['date']}  🕐 {b['timeSlot']}\n"
            f"🎮 {b['consoleType']}  ⏱️ {b['durationMins']} mins\n"
            f"🕹️ {b.get('gameName') or '-'}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"bkm:approve:{b['id']}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"bkm:reject:{b['id']}"),
        ]])
        await update.message.reply_text(card, parse_mode="Markdown", reply_markup=kb)
    return MAIN_MENU


async def cmd_confirmed_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show upcoming confirmed bookings with Cancel button each."""
    await update.message.reply_text("⏳ Confirmed bookings စစ်နေသည်...")
    bookings = _replit_get("bookings?status=confirmed") or []
    if not isinstance(bookings, list):
        bookings = []

    now_str  = now_mmt().strftime("%H:%M")
    today_s  = now_mmt().strftime("%-m/%-d/%Y")

    # Sort: today first (upcoming slots), then future dates
    def sort_key(b):
        return (b.get("date", ""), b.get("timeSlot", ""))
    bookings = sorted(bookings, key=sort_key)

    if not bookings:
        await update.message.reply_text(
            "✅ *Confirmed Bookings မရှိပါ*",
            parse_mode="Markdown",
        )
        return MAIN_MENU

    await update.message.reply_text(
        f"✅ *Confirmed Bookings — {len(bookings)} ခု*\n"
        f"━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
    )

    for b in bookings[:15]:
        console_hint = b.get("consoleId") or b.get("consoleType", "?")
        is_today = b.get("date", "") == today_s
        today_tag = "  🔵 Today" if is_today else ""
        card = (
            f"✅ *Booking #{b['id']}*{today_tag}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 {b['customerName']}  📞 {b.get('phone') or '—'}\n"
            f"📅 {b['date']}  🕐 {b['timeSlot']}\n"
            f"🎮 {console_hint}  ⏱️ {b.get('durationMins', '?')} mins\n"
            f"🕹️ {b.get('gameName') or '-'}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚫 Cancel", callback_data=f"bkc:{b['id']}"),
        ]])
        await update.message.reply_text(card, parse_mode="Markdown", reply_markup=kb)

    return MAIN_MENU


async def cmd_staff_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: show all consoles (free ✅ / busy 🔴) for staff advance booking."""
    from_hub = context.user_data.get("sbk_from_hub", False)
    context.user_data.clear()
    if from_hub:
        context.user_data["sbk_from_hub"] = True
    rows = _sbk_console_kb()
    await update.message.reply_text(
        "📅 *Customer Advance Booking*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ = Free   🔴 = Busy\n\n"
        "🕹️ Console ရွေးပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True),
    )
    return SBK_CONSOLE


async def step_sbk_console(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle console selection."""
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text in (BTN_BACK, BTN_BACK_MAIN):
        if context.user_data.get("sbk_from_hub"):
            return await cmd_staff_book_hub(update, context)
        return await show_admin_menu(update, context)

    cid, ctype = _sbk_parse_console_label(text)
    # validate against known consoles
    try:
        all_c = fetch_console_status()
        valid = {c["id"] for c in all_c}
    except Exception:
        valid = VALID_CONSOLES
    if cid not in valid:
        await update.message.reply_text("⚠️ Keyboard မှ Console ရွေးပေးပါ")
        return await cmd_staff_booking(update, context)

    context.user_data["sbk_console_id"]   = cid
    context.user_data["sbk_console_type"] = ctype

    # Offer member list for quick selection
    try:
        members = fetch_members()
    except Exception:
        members = []
    kb = [["👤 Guest (Walk-in)"]] + [[m] for m in members[:20]] + [[BTN_BACK, BTN_CANCEL]]
    await update.message.reply_text(
        f"🕹️ Console: <b>{cid}</b>  ({ctype})\n\n"
        "👤 Customer name ထည့်ပါ\n"
        "( Member list မှ ရွေး သို့ Manual ရိုက် )",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SBK_CUST_NAME


async def step_sbk_cust_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle customer name / member selection."""
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await cmd_staff_booking(update, context)

    name = "Guest" if text == "👤 Guest (Walk-in)" else text
    context.user_data["sbk_cust_name"] = name

    # Ask phone
    kb = [[BTN_SBK_SKIP_PHONE], [BTN_BACK, BTN_CANCEL]]
    await update.message.reply_text(
        f"👤 Customer: <b>{name}</b>\n\n"
        "📞 Phone number ထည့်ပါ (optional — Skip နှိပ်နိုင်)",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SBK_DATE


async def step_sbk_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle phone → then ask booking date."""
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await cmd_staff_booking(update, context)

    phone = "—" if text == BTN_SBK_SKIP_PHONE else text
    context.user_data["sbk_phone"] = phone

    # Ask date
    today    = now_mmt().date()
    tomorrow = today + timedelta(days=1)
    d2       = today + timedelta(days=2)
    def dfmt(d): return d.strftime("%-m/%-d/%Y")
    kb = [
        [dfmt(today) + " (ယနေ့)", dfmt(tomorrow) + " (မနက်ဖြန်)"],
        [dfmt(d2)],
        [BTN_SBK_CUSTOM],
        [BTN_BACK, BTN_CANCEL],
    ]
    await update.message.reply_text(
        "📅 Booking Date ရွေးပါ:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SBK_TIME


async def step_sbk_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle date → then ask time slot."""
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        # re-ask phone
        name = context.user_data.get("sbk_cust_name", "")
        kb = [[BTN_SBK_SKIP_PHONE], [BTN_BACK, BTN_CANCEL]]
        await update.message.reply_text(
            f"👤 Customer: <b>{name}</b>\n\n📞 Phone number ထည့်ပါ:",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return SBK_DATE

    if text == BTN_SBK_CUSTOM:
        await update.message.reply_text(
            "📅 ရက် ရိုက်ထည့်ပါ (format: M/D/YYYY)\nဥပမာ: 5/10/2026",
            reply_markup=ReplyKeyboardMarkup([[BTN_BACK, BTN_CANCEL]], resize_keyboard=True),
        )
        return SBK_TIME

    # Parse date from label like "5/4/2026 (ယနေ့)"
    import re as _re
    m = _re.match(r"(\d{1,2}/\d{1,2}/\d{4})", text)
    if m:
        date_str = m.group(1)
    else:
        # try direct parse e.g. "5/4/2026"
        parts = text.split("/")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            date_str = text
        else:
            await update.message.reply_text("⚠️ ရက် format မမှန်ပါ (M/D/YYYY)")
            return SBK_TIME

    context.user_data["sbk_date"] = date_str

    # Build time slot keyboard
    slots = [
        ["10:00", "11:00", "12:00"],
        ["13:00", "14:00", "15:00"],
        ["16:00", "17:00", "18:00"],
        ["19:00", "20:00", "21:00"],
        ["22:00"],
    ]
    kb = slots + [[BTN_BACK, BTN_CANCEL]]
    await update.message.reply_text(
        f"📅 {date_str}\n\n⏰ Time Slot ရွေးပါ (HH:MM):",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SBK_DUR


async def step_sbk_dur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle time slot → ask duration."""
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        # re-ask date
        today    = now_mmt().date()
        tomorrow = today + timedelta(days=1)
        d2       = today + timedelta(days=2)
        def dfmt(d): return d.strftime("%-m/%-d/%Y")
        kb = [
            [dfmt(today) + " (ယနေ့)", dfmt(tomorrow) + " (မနက်ဖြန်)"],
            [dfmt(d2)],
            [BTN_SBK_CUSTOM],
            [BTN_BACK, BTN_CANCEL],
        ]
        await update.message.reply_text(
            "📅 Booking Date ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return SBK_TIME

    # Validate HH:MM
    import re as _re2
    if not _re2.match(r"^\d{1,2}:\d{2}$", text):
        await update.message.reply_text("⚠️ Time format: HH:MM  (ဥပမာ: 14:30)")
        return SBK_DUR

    context.user_data["sbk_time"] = text

    kb = [
        ["30", "60", "90"],
        ["120", "150", "180"],
        ["240", "300", "360"],
        [BTN_BACK, BTN_CANCEL],
    ]
    await update.message.reply_text(
        f"⏰ {text}\n\n⏱️ Duration (မိနစ်) ရွေးပါ:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SBK_GAME


async def step_sbk_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle duration → ask game."""
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        # re-ask time
        slots = [
            ["10:00", "11:00", "12:00"],
            ["13:00", "14:00", "15:00"],
            ["16:00", "17:00", "18:00"],
            ["19:00", "20:00", "21:00"],
            ["22:00"],
        ]
        await update.message.reply_text(
            "⏰ Time Slot ရွေးပါ:",
            reply_markup=ReplyKeyboardMarkup(slots + [[BTN_BACK, BTN_CANCEL]], resize_keyboard=True),
        )
        return SBK_DUR

    try:
        dur = int(text)
    except ValueError:
        await update.message.reply_text("⚠️ ဂဏန်းသာ ထည့်ပါ သို့ keyboard မှ ရွေးပါ")
        return SBK_GAME
    context.user_data["sbk_dur"] = dur

    # Build game keyboard
    try:
        games = fetch_games()
        game_names = [g["title"] for g in games if g.get("title")][:30]
    except Exception:
        game_names = []

    kb = [[BTN_SBK_SKIP_GAME]]
    row = []
    for g in game_names:
        row.append(g)
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([BTN_BACK, BTN_CANCEL])

    await update.message.reply_text(
        f"⏱️ Duration: <b>{dur} mins</b>\n\n"
        "🎮 Game ရွေးပါ (optional):",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SBK_CONFIRM


async def step_sbk_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """SBK_CONFIRM state — phase 1: receive game name and show summary.
       Phase 2: receive BTN_SBK_CONFIRM_BOOK and create the booking.
    """
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)

    # ── Phase 2: create booking (user pressed confirm) ─────────────────────
    if text == BTN_SBK_CONFIRM_BOOK:
        cid      = context.user_data.get("sbk_console_id", "")
        ctype    = context.user_data.get("sbk_console_type", "")
        name     = context.user_data.get("sbk_cust_name", "Guest")
        phone    = context.user_data.get("sbk_phone", "—")
        date     = context.user_data.get("sbk_date", "")
        slot     = context.user_data.get("sbk_time", "")
        dur      = context.user_data.get("sbk_dur", 60)
        game     = context.user_data.get("sbk_game", "")
        staff    = update.effective_user.full_name or "Staff"

        await update.message.reply_text("⏳ Booking ဖန်တီးနေသည်...", reply_markup=ReplyKeyboardRemove())

        payload = {
            "customerName": name,
            "phone":        phone,
            "date":         date,
            "timeSlot":     slot,
            "consoleType":  ctype,
            "consoleId":    cid,
            "durationMins": int(dur),
            "gameName":     game or None,
            "status":       "confirmed",
            "source":       "staff",
            "staffNote":    f"Console: {cid} | Booked by {staff}",
        }

        result = await asyncio.to_thread(_replit_post, "bookings", payload)
        if not result or "id" not in result:
            await update.message.reply_text(
                "❌ Booking create မအောင်မြင်ပါ\nAPI စစ်ပြီး ထပ်ကြိုးစားပါ",
            )
            return await show_main_menu(update, context)

        bk_id = result["id"]

        # Notify staff group
        if STAFF_NOTIFY_CHAT:
            notif = (
                f"📅 <b>New Staff Booking #{bk_id}</b>\n"
                f"🕹️ {cid} ({ctype})\n"
                f"👤 {name}  📞 {phone}\n"
                f"📅 {date}  ⏰ {slot}\n"
                f"⏱️ {dur} mins  🎮 {game or '—'}\n"
                f"Created by {staff}"
            )
            _notify_customer(STAFF_NOTIFY_CHAT, notif)

        # Fire n8n reminder webhook (non-blocking)
        asyncio.create_task(_post_n8n_booking_reminder(
            bk_id=bk_id, customer_name=name, phone=phone,
            console_id=cid, console_type=ctype,
            date_str=date, time_slot=slot, duration_mins=int(dur),
            tg_chat="",
        ))

        await update.message.reply_text(
            f"✅ <b>Booking #{bk_id} Created!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕹️ Console  : <b>{cid}</b>  ({ctype})\n"
            f"👤 Customer : <b>{name}</b>  📞 {phone}\n"
            f"📅 {date}  ⏰ {slot}\n"
            f"⏱️ {dur} mins  🎮 {game or '—'}\n"
            f"Status: <b>Confirmed ✅</b>\n"
            f"{'📲 Reminder scheduled via n8n' if N8N_BOOKING_WEBHOOK else ''}",
            parse_mode="HTML",
        )
        context.user_data.clear()
        return await show_main_menu(update, context)

    # ── BTN_BACK: re-show game selection ──────────────────────────────────
    if text == BTN_BACK:
        dur = context.user_data.get("sbk_dur", 60)
        context.user_data.pop("sbk_game", None)
        try:
            games = fetch_games()
            game_names = [g["title"] for g in games if g.get("title")][:30]
        except Exception:
            game_names = []
        kb = [[BTN_SBK_SKIP_GAME]]
        row = []
        for g in game_names:
            row.append(g)
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        kb.append([BTN_BACK, BTN_CANCEL])
        await update.message.reply_text(
            f"⏱️ Duration: <b>{dur} mins</b>\n\n🎮 Game ရွေးပါ:",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return SBK_CONFIRM

    # ── Phase 1: receive game name, show summary ───────────────────────────
    game = "" if text == BTN_SBK_SKIP_GAME else text
    context.user_data["sbk_game"] = game

    cid   = context.user_data.get("sbk_console_id", "")
    ctype = context.user_data.get("sbk_console_type", "")
    name  = context.user_data.get("sbk_cust_name", "")
    phone = context.user_data.get("sbk_phone", "—")
    date  = context.user_data.get("sbk_date", "")
    slot  = context.user_data.get("sbk_time", "")
    dur   = context.user_data.get("sbk_dur", 0)

    # ── SSD transfer check ─────────────────────────────────────────────────
    ssd_warning = ""
    context.user_data["sbk_needs_ssd"] = False
    if game:
        installed = await asyncio.to_thread(get_games_on_console, cid)
        installed_lower = [g.lower() for g in installed]
        if game.lower() not in installed_lower:
            # check if game exists on any console (installed anywhere)
            consoles_with_game = await asyncio.to_thread(get_consoles_with_game, game)
            ssd_consoles = [
                r["console_id"] for r in fetch_console_games()
                if r["game_title"].lower() == game.lower()
                and r["install_type"] == "Portable SSD"
            ]
            if consoles_with_game:
                context.user_data["sbk_needs_ssd"] = True
                ssd_warning = (
                    f"\n⚠️ <b>SSD Transfer လိုသည်!</b>\n"
                    f"「{game}」 ကို {cid} မှာ Install မရှိပါ\n"
                    f"{'🔌 SSD (' + ', '.join(ssd_consoles) + ') မှ transfer လိုမည်' if ssd_consoles else '📋 Install မှတ်တမ်း စစ်ဆေးပါ'}\n"
                )
            else:
                ssd_warning = f"\n📝 <i>「{game}」 Install မှတ်တမ်း မရှိသေးပါ</i>\n"

    summary = (
        f"📋 <b>Booking Summary — စစ်ဆေးပါ</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕹️ Console  : <b>{cid}</b>  ({ctype})\n"
        f"👤 Customer : <b>{name}</b>\n"
        f"📞 Phone    : {phone}\n"
        f"📅 Date     : <b>{date}</b>\n"
        f"⏰ Time     : <b>{slot}</b>\n"
        f"⏱️ Duration : <b>{dur} mins</b>\n"
        f"🎮 Game     : {game or '—'}"
        f"{ssd_warning}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"ဤ booking ကို create မည်လား?"
    )
    kb = [[BTN_SBK_CONFIRM_BOOK], [BTN_BACK, BTN_CANCEL]]
    await update.message.reply_text(
        summary, parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SBK_CONFIRM


# ═════════════════════════════════════════
#  BOOKING MANAGEMENT (staff side)
# ═════════════════════════════════════════

def _notify_customer(chat_id_or_phone: str, text: str):
    """Send Telegram message via customer bot token to notify customer."""
    if not CUSTOMER_BOT_TOKEN or not chat_id_or_phone:
        return
    try:
        import urllib.request as _req
        payload = json.dumps({
            "chat_id": chat_id_or_phone,
            "text": text,
            "parse_mode": "HTML",
        }).encode()
        r = _req.Request(
            f"https://api.telegram.org/bot{CUSTOMER_BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        _req.urlopen(r, timeout=10)
    except Exception as e:
        logging.warning("customer notify failed: %s", e)


def get_customer_chat_id(member_id: str) -> str | None:
    """Look up most-recent Telegram chat_id for a member from bookings store."""
    try:
        bks = _replit_get(f"bookings?memberId={member_id}")
        if bks:
            for b in bks:
                cid = (b.get("telegramChatId") or b.get("telegram_chat_id") or "").strip()
                if cid:
                    return cid
    except Exception as e:
        logging.warning("get_customer_chat_id %s: %s", member_id, e)
    return None


async def _check_low_balance_alert(member_id: str, console_id: str) -> None:
    """Wait for Sheet formula to settle, then send low-balance alert to customer."""
    try:
        await asyncio.sleep(7)
        balance = await asyncio.to_thread(fetch_balance_mins, member_id)
        threshold = int(os.environ.get("LOW_BALANCE_THRESHOLD", "120"))
        if balance >= threshold:
            return
        chat_id = await asyncio.to_thread(get_customer_chat_id, member_id)
        if not chat_id:
            return
        msg = (
            f"⚠️ <b>PS VIBE — Balance နည်းလာပြီ!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💳 Member: <code>{member_id}</code>\n"
            f"🎮 လက်ကျန် Balance: <b>{balance} မိနစ်</b>\n"
            f"⏱️ PS5 ဆိုပါက {balance} မိနစ် ကစားနိုင်သေးသည်\n"
            f"\n"
            f"💰 ဆက်ကစားနိုင်ရန် Top-up လုပ်ပါ 👇\n"
            f"/topup"
        )
        await asyncio.to_thread(_notify_customer, chat_id, msg)
        logging.info("low_balance_alert sent: member=%s balance=%d", member_id, balance)
    except Exception as e:
        logging.warning("_check_low_balance_alert %s: %s", member_id, e)


async def cmd_admin_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending bookings — each as a separate card with ✅/❌ inline buttons."""
    await update.message.reply_text("⏳ Pending bookings စစ်နေသည်...", reply_markup=ReplyKeyboardRemove())
    bookings = _replit_get("bookings?status=pending")
    if not bookings:
        await update.message.reply_text(
            "✅ *Pending bookings မရှိပါ*",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return await show_admin_menu(update, context)

    await update.message.reply_text(
        f"📋 *Pending Bookings — {len(bookings)} ခု*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )

    for b in bookings:
        card = (
            f"🎫 *Booking #{b['id']}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 {b['customerName']}  📞 {b['phone']}\n"
            f"📅 {b['date']}  🕐 {b['timeSlot']}\n"
            f"🎮 {b['consoleType']}  ⏱️ {b['durationMins']} mins\n"
            f"🕹️ {b.get('gameName') or '-'}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"bkm:approve:{b['id']}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"bkm:reject:{b['id']}"),
        ]])
        await update.message.reply_text(card, parse_mode="Markdown", reply_markup=kb)

    return await show_admin_menu(update, context)


async def cmd_approve_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /approve_<id> text command (fallback)."""
    cmd = update.message.text.strip()
    try:
        bk_id = int(cmd.split("_", 1)[1])
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Invalid command. Use /approve_<id>")
        return
    await _do_booking_action(bk_id, "approve", update.effective_user.full_name or "Staff",
                             reply_fn=update.message.reply_text)


async def cmd_reject_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /reject_<id> text command (fallback)."""
    cmd = update.message.text.strip()
    try:
        bk_id = int(cmd.split("_", 1)[1])
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Invalid command. Use /reject_<id>")
        return
    await _do_booking_action(bk_id, "reject", update.effective_user.full_name or "Staff",
                             reply_fn=update.message.reply_text)


async def cb_booking_mgmt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline button handler for ✅/❌ on pending booking cards."""
    query = update.callback_query
    await query.answer()
    try:
        _, action, bk_id_str = query.data.split(":")
        bk_id = int(bk_id_str)
    except Exception:
        return
    staff_name = query.from_user.full_name or "Staff"

    async def edit_fn(text: str, **kw):
        # Replace the card text to show result inline
        try:
            await query.edit_message_text(text, **kw)
        except Exception:
            pass

    await _do_booking_action(bk_id, action, staff_name, reply_fn=edit_fn)


async def _do_booking_action(bk_id: int, action: str, staff_name: str, reply_fn):
    """Shared approve/reject logic — updates DB, replies, notifies customer."""
    new_status = "confirmed" if action == "approve" else "rejected"

    patch_body: dict = {
        "status":    new_status,
        "staffNote": f"{'Approved' if action == 'approve' else 'Rejected'} by {staff_name}",
    }

    # Auto-assign a free console of the matching type on approval
    # If booking has a gameName, prefer consoles that have the game installed
    assigned_console = ""
    install_warn     = ""
    if action == "approve":
        bk_data      = await asyncio.to_thread(_replit_get, f"bookings/{bk_id}")
        bk_info      = bk_data or {}
        console_type = bk_info.get("consoleType", "")
        game_name    = (bk_info.get("gameName") or "").strip()

        if console_type:
            consoles_data = await asyncio.to_thread(_replit_get, "sheets/consoles")
            consoles      = (consoles_data or {}).get("consoles", []) if consoles_data else []
            free = [c for c in consoles
                    if c.get("type", "").strip() == console_type
                    and c.get("liveStatus", "").lower() == "free"]

            chosen = None
            if free and game_name:
                # Prefer a free console that already has the game installed
                consoles_with_game = await asyncio.to_thread(get_consoles_with_game, game_name)
                cw_upper  = {c.upper() for c in consoles_with_game}
                game_free = [c for c in free if c["id"].upper() in cw_upper]

                if game_free:
                    chosen = game_free[0]
                else:
                    # Fall back to first free console, but warn staff
                    chosen = free[0]
                    if consoles_with_game:
                        install_warn = (
                            f"\n⚠️ <b>「{game_name}」 Install စစ်ဆေးပါ!</b>\n"
                            f"Free console ({chosen['id']}) မှာ install မရှိပါ\n"
                            f"Install ရှိသော console: <b>{', '.join(consoles_with_game)}</b>\n"
                            f"ကြိုတင် Install / SSD transfer ပြင်ဆင်ပါ"
                        )
                    else:
                        install_warn = (
                            f"\n⚠️ <b>「{game_name}」 မည်သည့် Console မှ Install မရှိ!</b>\n"
                            f"Session မတိုင်မီ Install ပြင်ဆင်ပါ"
                        )
            elif free:
                chosen = free[0]

            if chosen:
                patch_body["consoleId"] = chosen["id"]
                assigned_console        = chosen["id"]
                global _BK_TS
                _BK_TS = 0.0  # invalidate booking cache so status reflects new reservation

    result = await asyncio.to_thread(
        _replit_patch,
        f"bookings/{bk_id}/status",
        patch_body,
    )
    if not result:
        await reply_fn(f"❌ Booking #{bk_id} ကို update မရပါ")
        return

    # Console conflict — 409 response
    if isinstance(result, dict) and result.get("error") == "console_conflict":
        conflict_msg = result.get("message", "")
        await reply_fn(
            f"⚠️ *Console Conflict!*\n\n"
            f"🖥️ {assigned_console} သည် ထပ်နေပြီ ဖြစ်သည်\n"
            f"_{conflict_msg}_\n\n"
            f"📌 Booking #{bk_id} ကို manually console ပြောင်းပြီး ထပ်ကြိုးစားပါ",
            parse_mode="Markdown",
        )
        return

    b = result
    if action == "approve":
        console_line = f"\n🖥️ Console: <b>{assigned_console}</b>" if assigned_console else ""
        game_line    = f"\n🕹️ Game: <b>{b.get('gameName') or '—'}</b>" if b.get("gameName") else ""
        msg = (
            f"✅ <b>Booking #{bk_id} Confirmed!</b>\n"
            f"👤 {b['customerName']}  📞 {b['phone']}\n"
            f"📅 {b['date']}  🕐 {b['timeSlot']}\n"
            f"🎮 {b['consoleType']}  ⏱️ {b['durationMins']} mins"
            f"{game_line}{console_line}\n"
            f"<i>Approved by {staff_name}</i>"
            f"{install_warn}"
        )
    else:
        msg = (
            f"❌ <b>Booking #{bk_id} Rejected</b>\n"
            f"👤 {b['customerName']}  📅 {b['date']}  🕐 {b['timeSlot']}\n"
            f"<i>Rejected by {staff_name}</i>"
        )
    await reply_fn(msg, parse_mode="HTML")

    # Notify customer via customer bot if we have their chat_id
    tg_chat = b.get("telegramChatId") or ""
    if tg_chat and CUSTOMER_BOT_TOKEN:
        if action == "approve":
            console_line = f"\n🖥️ Console: <b>{assigned_console}</b>" if assigned_console else ""
            cust_msg = (
                f"🎉 <b>Booking Confirmed!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🎫 Booking #{bk_id}\n"
                f"📅 {b['date']}  🕐 {b['timeSlot']}\n"
                f"🎮 {b['consoleType']}  ⏱️ {b['durationMins']} mins{console_line}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"PS Vibe မှ ကြိုဆိုပါသည်! ✨\n"
                f"<i>10 မိနစ်အလိုတွင် reminder ပို့ပါမည်</i>"
            )
        else:
            cust_msg = (
                f"😔 <b>Booking #{bk_id} Rejected</b>\n\n"
                f"📅 {b['date']}  🕐 {b['timeSlot']}\n\n"
                f"အဆင်မပြေသဖြင့် တောင်းပန်ပါသည်။ နောက်ထပ် booking ထပ်မံလုပ်နိုင်ပါသည်။\n"
                f"📞 ဆက်သွယ်ရန် @psvibeofficial"
            )
        _notify_customer(tg_chat, cust_msg)

    # Fire n8n reminder when customer booking approved
    if action == "approve":
        asyncio.create_task(_post_n8n_booking_reminder(
            bk_id=bk_id,
            customer_name=b.get("customerName", ""),
            phone=b.get("phone", ""),
            console_id=b.get("consoleId") or "",
            console_type=b.get("consoleType", ""),
            date_str=b.get("date", ""),
            time_slot=b.get("timeSlot", ""),
            duration_mins=int(b.get("durationMins") or 60),
            tg_chat=tg_chat,
        ))


async def cmd_setattend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start attendance wizard — pick staff to record leave/late for."""
    context.user_data.clear()
    staff_list = fetch_staff()
    context.user_data["attend_staff_list"] = staff_list
    context.user_data["attend_idx"]        = 0
    context.user_data["attend_records"]    = {}
    month_str   = now_mmt().strftime("%Y-%m")
    month_label = now_mmt().strftime("%B %Y")
    context.user_data["attend_month"] = month_str
    kb = [[s] for s in staff_list] + [[BTN_CANCEL]]
    await update.message.reply_text(
        f"📅 *Attendance — {month_label}*\n\n"
        f"ခွင့်ယူ / နောက်ကျ မှတ်တမ်းကို Staff တစ်ယောက်ချင်း ထည့်ပေးပါ\n\n"
        f"မှတ်မည့် Staff ကို ရွေးပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ATTEND_STAFF


async def step_attend_staff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_ATTEND_DONE:
        return await _attend_finish(update, context)
    staff_list = context.user_data.get("attend_staff_list", [])
    if text not in staff_list:
        await update.message.reply_text("⚠️ Keyboard မှ Staff ရွေးပေးပါ -")
        return ATTEND_STAFF
    context.user_data["attend_current"] = text
    kb = [[BTN_ATTEND_SKIP], [BTN_CANCEL]]
    await update.message.reply_text(
        f"👤 *{text}*\n\n"
        f"📅 *ခွင့်ယူ ရက်* ဘယ်နှစ်ရက် ထည့်မည်နည်း?\n"
        f"_(0 ဆိုရင် Skip နှိပ်ပါ)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ATTEND_LEAVE


async def step_attend_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_ATTEND_SKIP:
        context.user_data["_att_leave"] = 0
    else:
        try:
            days = int(text)
            if days < 0:
                raise ValueError
            context.user_data["_att_leave"] = days
        except ValueError:
            await update.message.reply_text("⚠️ ဂဏန်းသက်သက် ရိုက်ပေးပါ (0 ↑) -")
            return ATTEND_LEAVE
    kb = [[BTN_ATTEND_SKIP], [BTN_CANCEL]]
    await update.message.reply_text(
        f"⏰ *နောက်ကျ ကြိမ်* ဘယ်နှစ်ကြိမ်?\n"
        f"_(0 ဆိုရင် Skip နှိပ်ပါ)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ATTEND_LATE


async def step_attend_late(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_ATTEND_SKIP:
        context.user_data["_att_late"] = 0
        context.user_data["_att_deduct"] = 500
        return await _attend_save_and_next(update, context)
    try:
        late = int(text)
        if late < 0:
            raise ValueError
        context.user_data["_att_late"] = late
    except ValueError:
        await update.message.reply_text("⚠️ ဂဏန်းသက်သက် ရိုက်ပေးပါ (0 ↑) -")
        return ATTEND_LATE
    if context.user_data["_att_late"] == 0:
        context.user_data["_att_deduct"] = 500
        return await _attend_save_and_next(update, context)
    kb = [[BTN_ATTEND_SKIP], [BTN_CANCEL]]
    await update.message.reply_text(
        f"💸 *တစ်ကြိမ် နောက်ကျ ဖြတ်တောက်ကြေး*\n"
        f"_(default 500 Ks — Skip နှိပ်ရင် 500 Ks သုံးမည်)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ATTEND_DEDUCT


async def step_attend_deduct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_ATTEND_SKIP:
        context.user_data["_att_deduct"] = 500
    else:
        try:
            amt = int(text.replace(",", ""))
            if amt < 0:
                raise ValueError
            context.user_data["_att_deduct"] = amt
        except ValueError:
            await update.message.reply_text("⚠️ ဂဏန်းသက်သက် ရိုက်ပေးပါ -")
            return ATTEND_DEDUCT
    return await _attend_save_and_next(update, context)


async def _attend_save_and_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save current staff attendance then ask for next staff or finish."""
    d              = context.user_data
    staff          = d.get("attend_current", "")
    leave_days     = d.pop("_att_leave", 0)
    late_count     = d.pop("_att_late", 0)
    deduct_per_late= d.pop("_att_deduct", 500)
    month_str      = d.get("attend_month", now_mmt().strftime("%Y-%m"))

    save_attendance(month_str, staff, leave_days, late_count, deduct_per_late)
    d.setdefault("attend_records", {})[staff] = {
        "leave": leave_days, "late": late_count, "deduct": deduct_per_late,
    }

    remaining = [s for s in d.get("attend_staff_list", []) if s not in d["attend_records"]]
    if remaining:
        kb = [[s] for s in remaining] + [[BTN_ATTEND_DONE], [BTN_CANCEL]]
        await update.message.reply_text(
            f"✅ *{staff}* — မှတ်တမ်းသိမ်းပြီး\n\n"
            f"📅 ခွင့်: *{leave_days} ရက်*  ⏰ နောက်ကျ: *{late_count} ကြိမ်*\n\n"
            f"နောက် Staff ရွေးပါ (သို့မဟုတ် ✅ ပြီးပါပြီ) -",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return ATTEND_STAFF
    return await _attend_finish(update, context)


async def _attend_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show summary and exit."""
    records    = context.user_data.get("attend_records", {})
    month_str  = context.user_data.get("attend_month", "")
    month_label= now_mmt().strftime("%B %Y")
    if not records:
        await update.message.reply_text("ℹ️ မှတ်တမ်း မထည့်ရသေး — OK ပါ။")
        return await show_main_menu(update, context)
    lines = [f"✅ *Attendance Saved — {month_label}*\n━━━━━━━━━━━━━━━━━━"]
    for s, rec in records.items():
        lines.append(
            f"👤 *{s}*\n"
            f"   📅 ခွင့်: {rec['leave']} ရက်  ⏰ နောက်ကျ: {rec['late']} ကြိမ် ({rec['deduct']:,}/ကြိမ်)"
        )
    lines.append("\n_/payroll နဲ့ စစ်ကြည့်နိုင်ပါပြီ_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return await show_main_menu(update, context)


def _replit_get(path: str, timeout: int = 30):
    """GET JSON from API server. Returns parsed dict or None on error."""
    base = _api_base()
    if not base:
        return None
    try:
        import urllib.request as _req
        with _req.urlopen(f"{base}/api/{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logging.warning("API GET /%s failed: %s", path, e)
        return None


def _replit_patch(path: str, body: dict):
    """PATCH JSON to API server. Returns parsed response or None on error.
    On HTTP 409 conflict, returns dict with 'error' key instead of None."""
    base = _api_base()
    if not base:
        return None
    try:
        import urllib.request as _req
        import urllib.error as _err
        data = json.dumps(body).encode()
        req  = _req.Request(
            f"{base}/api/{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        with _req.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except _err.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = {"error": f"http_{e.code}"}
        err_body["__status__"] = e.code
        logging.warning("API PATCH /%s HTTP %s: %s", path, e.code, err_body)
        return err_body
    except Exception as e:
        logging.warning("API PATCH /%s failed: %s", path, e)
        return None


def _replit_post(path: str, body: dict, timeout: int = 30):
    """POST JSON to API server. Returns parsed response or None on error."""
    base = _api_base()
    if not base:
        return None
    try:
        import urllib.request as _req
        data = json.dumps(body).encode()
        req  = _req.Request(
            f"{base}/api/{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _req.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logging.warning("Replit API POST /%s failed: %s", path, e)
        return None


def _update_inv_total_k1() -> int:
    """Calculate total inventory value from Inventory!G col and write to K1. Returns total."""
    try:
        vals = inv_sh.col_values(7)[1:]          # col G = Inventory Value, skip header
        total = 0
        for v in vals:
            try:
                s = str(v).replace(",", "").strip()
                if s:
                    total += int(float(s))
            except (ValueError, TypeError):
                pass
        updated_at = now_mmt().strftime("%-m/%-d/%Y %H:%M")
        inv_sh.update("K1", [[total]], value_input_option="USER_ENTERED")
        inv_sh.update("L1", [[updated_at]], value_input_option="USER_ENTERED")
        logging.info("Inv K1 updated: %d at %s", total, updated_at)
        return total
    except Exception as e:
        logging.warning("K1 update failed: %s", e)
        return 0


async def cmd_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current inventory levels from Replit API."""
    await update.message.reply_text("⏳ Inventory စစ်နေသည်...", reply_markup=ReplyKeyboardRemove())
    data = _replit_get("sheets/inventory")
    if not data:
        await update.message.reply_text(
            "❌ Inventory data ရယူ၍ မရပါ။",
            reply_markup=ReplyKeyboardMarkup([[BTN_BACK_MAIN]], resize_keyboard=True),
        )
        return
    items = data.get("items", [])
    STATUS_EMOJI = {
        "In Stock":     "🟢",
        "Low Stock":    "🟡",
        "Out of Stock": "🔴",
        "No Stock":     "⚫",
    }
    lines = ["📦 *Inventory Status*\n━━━━━━━━━━━━━━━━━━"]
    for item in items:
        em    = STATUS_EMOJI.get(item["status"], "⚫")
        stock = max(0, item.get("current_stock", 0))
        val   = item.get("inv_value", 0)
        val_str = f"  _{val:,} Ks_" if val > 0 else ""
        lines.append(f"{em} *{item['name']}*: {stock} pcs{val_str}")
    total_val = sum(i.get("inv_value", 0) for i in items)
    if total_val:
        lines.append(f"\n━━━━━━━━━━━━━━━━━━\n💰 Total Inv Value (FIFO): *{total_val:,} Ks*")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BTN_BACK_MAIN]], resize_keyboard=True),
    )


async def cmd_stocktoday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show today's items sold from Replit API."""
    await update.message.reply_text("⏳ Today's stock data ရယူနေသည်...", reply_markup=ReplyKeyboardRemove())
    data = _replit_get("sheets/stock-today")
    if not data:
        await update.message.reply_text("❌ Stock data ရယူ၍ မရပါ။")
        return
    items = data.get("items", [])
    if not items:
        await update.message.reply_text("ℹ️ ဒီနေ့ ပစ္စည်းများ မရောင်းရသေးပါ။")
        return
    total_val = sum(i["value"] for i in items)
    total_qty = sum(i["qty"] for i in items)
    lines = [f"🛒 *Items Sold Today — {data.get('date','')}*\n━━━━━━━━━━━━━━━━━━"]
    for item in items:
        lines.append(f"• *{item['name']}*: {item['qty']} pcs — {item['value']:,} Ks")
    lines.append(f"━━━━━━━━━━━━━━━━━━\n📦 Total: *{total_qty} items*  💰 *{total_val:,} Ks*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_today_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Today's combined sales + stock report with per-staff breakdown."""
    await update.message.reply_text("⏳ Today's report ရယူနေသည်...", reply_markup=ReplyKeyboardRemove())
    rd    = _replit_get("sheets/report-data")   # single batch call (was 3 calls)
    sales = rd.get("summary")   if rd else None
    stock = rd.get("stock_today") if rd else None
    inv   = rd.get("inventory") if rd else None
    date  = today_str()
    kb    = ReplyKeyboardMarkup([[BTN_BACK_MAIN]], resize_keyboard=True)

    if not sales and not stock:
        await update.message.reply_text("❌ Data ရယူ၍ မရပါ။", reply_markup=kb)
        return MAIN_MENU

    lines = [f"📊 *Today's Report — {date}*\n━━━━━━━━━━━━━━━━━━"]

    # Sales summary
    if sales:
        cnt      = sales.get("today_count", 0)
        net      = sales.get("today_net", 0)
        kpay     = sales.get("today_kpay", 0)
        cash_    = sales.get("today_cash", 0)
        kpay_pct = round(kpay / net * 100) if net > 0 else 0
        cash_pct = 100 - kpay_pct if net > 0 else 0
        avg_rev  = round(net / cnt) if cnt > 0 else 0
        lines.append(f"🎮 *Sessions:* {cnt}    📊 Avg: *{avg_rev:,} Ks*")
        lines.append(f"💰 *Revenue:* *{net:,} Ks*")
        lines.append(f"  💳 KPay: {kpay:,} Ks  ({kpay_pct}%)")
        lines.append(f"  💵 Cash:  {cash_:,} Ks  ({cash_pct}%)")
    else:
        lines.append("🎮 Sales data မရပါ")

    # Per-staff breakdown from Sales_Daily
    sb = _replit_get("sheets/staff-breakdown")   # API cache (was direct gspread call)
    staff_stats = sb.get("staff", {}) if sb else {}

    if staff_stats:
        lines.append(f"━━━━━━━━━━━━━━━━━━")
        lines.append(f"👥 *Per-Staff:*")
        for s, sd in staff_stats.items():
            lines.append(f"  👤 *{s}* — {sd['sessions']} sessions | *{sd['revenue']:,} Ks*")

    # Food/Drinks sold today
    if stock and stock.get("items"):
        items        = stock["items"]
        total_qty    = sum(i["qty"] for i in items)
        total_rev    = sum(i["value"] for i in items)
        total_cog    = sum(i.get("cogs", 0) for i in items)
        gross_margin = round((total_rev - total_cog) / total_rev * 100) if total_rev > 0 else 0
        lines.append(f"━━━━━━━━━━━━━━━━━━")
        lines.append(f"🍔 *Food & Drinks:* {total_qty} pcs  |  *{total_rev:,} Ks*")
        for item in items:
            lines.append(f"  • {item['name']}: {item['qty']} pcs — {item['value']:,} Ks")
        if total_cog > 0:
            lines.append(f"  _{total_cog:,} Ks COGS_  |  GP: *{gross_margin}%*")

    # Low stock alert
    if inv:
        low = [i for i in inv.get("items", []) if i["status"] in ("Low Stock", "Out of Stock")]
        if low:
            lines.append(f"━━━━━━━━━━━━━━━━━━")
            lines.append("⚠️ *Low/Out Stock Alert:*")
            for i in low:
                em = "🔴" if i["status"] == "Out of Stock" else "🟡"
                lines.append(f"  {em} *{i['name']}* — {i['current_stock']} pcs")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb)
    return MAIN_MENU


# ═════════════════════════════════════════
#  FINANCIAL REPORT  (Sales_Daily + TopUp_Log)
# ═════════════════════════════════════════

async def cmd_financial_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick Financial Report: this week's summary + current month P&L."""
    await update.message.reply_text("⏳ Financial report ရယူနေသည်...", reply_markup=ReplyKeyboardRemove())
    kb = ReplyKeyboardMarkup([[BTN_BACK_MAIN]], resize_keyboard=True)

    # Fetch weekly report and monthly P&L in parallel
    now   = now_mmt()
    m_str = now.strftime("%Y-%m")
    weekly, pnl = await asyncio.gather(
        asyncio.to_thread(_replit_get, "sheets/weekly-report"),
        asyncio.to_thread(_replit_get, f"sheets/pnl?m={m_str}"),
    )

    lines: list[str] = [f"💹 <b>Financial Report</b>"]

    # ── Weekly summary ──
    if weekly:
        wm = weekly.get("telegram_message", "")
        if wm:
            lines.append(wm)
        else:
            def _fmt(n): return f"{round(n or 0):,}"
            ws  = weekly.get("week_start", "?")
            we  = weekly.get("week_end",   "?")
            net = weekly.get("net_total",  0)
            lines.append(
                f"📅 <b>This Week</b>  {ws} – {we}\n"
                f"🎮 Sessions : <b>{weekly.get('sessions', 0)}</b>\n"
                f"💰 Net      : <b>{_fmt(net)} Ks</b>\n"
                f"📲 KPay: {_fmt(weekly.get('kpay',0))} Ks  |  💵 Cash: {_fmt(weekly.get('cash',0))} Ks\n"
                f"🏦 Top-Ups  : <b>{_fmt(weekly.get('topup_amt',0))} Ks</b>  ({weekly.get('topup_count',0)} txns)\n"
                f"🆕 New Members: <b>{weekly.get('new_members',0)}</b>"
            )
    else:
        lines.append("📊 Weekly data မရပါ")

    # ── Monthly P&L ──
    if pnl:
        def _f(n): return f"{round(n or 0):,}"
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(f"📆 <b>{m_str} — Monthly P&amp;L</b>")
        game_rev   = pnl.get("game_rev",      pnl.get("salesNet",      0))
        food_rev   = pnl.get("food_rev",      pnl.get("foodRev",       0))
        topup_amt  = pnl.get("topup_amount",  pnl.get("topupAmount",   0))
        net_total  = pnl.get("net_total",     pnl.get("salesNet",      0))
        kpay       = pnl.get("kpay",          pnl.get("salesKpay",     0))
        cash       = pnl.get("cash",          pnl.get("salesCash",     0))
        lines.append(
            f"🎮 Game Rev  : <b>{_f(game_rev)} Ks</b>\n"
            f"🍔 Food Rev  : <b>{_f(food_rev)} Ks</b>\n"
            f"🏦 Top-Up    : <b>{_f(topup_amt)} Ks</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 Net Total : <b>{_f(net_total)} Ks</b>\n"
            f"📲 KPay: {_f(kpay)} Ks  |  💵 Cash: {_f(cash)} Ks"
        )
    else:
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(f"📆 {m_str} monthly data မရပါ")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb,
    )
    return MAIN_MENU


# ═════════════════════════════════════════
#  BROADCAST  (admin-only /broadcast command)
# ═════════════════════════════════════════

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /broadcast <message> — sends a message to all known customer Telegram IDs.

    Access control: set ADMIN_USER_IDS env var to a comma-separated list of
    allowed Telegram user IDs.  If unset, any staff-bot user may broadcast.
    """
    uid = str(update.effective_user.id)
    if _BROADCAST_ADMIN_IDS and uid not in _BROADCAST_ADMIN_IDS:
        await update.message.reply_text("❌ Permission denied. Admin access only.")
        return

    parts = (update.message.text or "").split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "📢 <b>Broadcast Usage</b>\n\n"
            "/broadcast &lt;message text&gt;\n\n"
            "<i>Example:</i>\n"
            "/broadcast PS Vibe မှ မင်္ဂလာပါ! 🎮 Weekend special offer ရှိသည်!",
            parse_mode="HTML",
        )
        return

    msg_text = parts[1].strip()

    # Fetch target IDs from API (bookings.json source)
    data = await asyncio.to_thread(_replit_get, "bookings/broadcast-targets")
    if not data:
        await update.message.reply_text("❌ Broadcast target list ကို server မှ ရယူ၍ မရပါ။")
        return

    telegram_ids: list[str] = data.get("telegram_ids", [])
    if not telegram_ids:
        await update.message.reply_text(
            "⚠️ Registered customer Telegram ID များ မတွေ့ပါ။\n"
            "Customer bot မှတဆင့် booking ပြုလုပ်သော customers များ ရှိမှသာ broadcast ပြုလုပ်နိုင်သည်။"
        )
        return

    status_msg = await update.message.reply_text(
        f"📡 {len(telegram_ids)} ဦးထံ sending..."
    )

    sent = 0
    failed = 0
    for tg_id in telegram_ids:
        try:
            await context.bot.send_message(
                chat_id=int(tg_id),
                text=f"📢 <b>PS Vibe</b>\n\n{msg_text}",
                parse_mode="HTML",
            )
            sent += 1
            await asyncio.sleep(0.05)   # ~20 msg/sec — within Telegram rate limits
        except Exception as e:
            logging.warning("Broadcast failed for %s: %s", tg_id, e)
            failed += 1

    await status_msg.edit_text(
        f"✅ <b>Broadcast complete</b>\n\n"
        f"📤 Sent    : <b>{sent}</b>\n"
        f"❌ Failed  : <b>{failed}</b>",
        parse_mode="HTML",
    )


async def cmd_staff_kpi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Staff KPI — today's per-staff breakdown from Sales_Daily + overall summary."""
    await update.message.reply_text("⏳ KPI data ရယူနေသည်...", reply_markup=ReplyKeyboardRemove())
    rd    = _replit_get("sheets/report-data")   # single batch call (was 2 calls)
    sales = rd.get("summary")     if rd else None
    stock = rd.get("stock_today") if rd else None
    date  = today_str()
    kb    = ReplyKeyboardMarkup([[BTN_BACK_MAIN]], resize_keyboard=True)

    if not sales:
        await update.message.reply_text("❌ Sales data ရယူ၍ မရပါ။", reply_markup=kb)
        return MAIN_MENU

    cnt      = sales.get("today_count", 0)
    net      = sales.get("today_net", 0)
    kpay     = sales.get("today_kpay", 0)
    cash_    = sales.get("today_cash", 0)
    total_tx = sales.get("total_count", 0)
    avg_rev  = round(net / cnt) if cnt > 0 else 0

    food_qty        = sum(i["qty"] for i in stock.get("items", [])) if stock else 0
    food_item_count = len(stock.get("items", [])) if stock else 0

    # Per-staff breakdown — read Sales_Daily directly
    sb = _replit_get("sheets/staff-breakdown")   # API cache (was direct gspread call)
    staff_stats = sb.get("staff", {}) if sb else {}

    # Performance rating
    if cnt >= 10:
        perf, star = "Excellent", "⭐⭐⭐"
    elif cnt >= 5:
        perf, star = "Good", "⭐⭐"
    elif cnt >= 1:
        perf, star = "Fair", "⭐"
    else:
        perf, star = "No Sessions Yet", "—"

    lines = [
        f"📈 *Staff KPI — {date}*\n━━━━━━━━━━━━━━━━━━",
        f"🎮 Sessions : *{cnt}*    💰 Revenue : *{net:,} Ks*",
        f"📊 Avg/Session : *{avg_rev:,} Ks*",
        f"━━━━━━━━━━━━━━━━━━",
        f"💳 KPay : *{kpay:,} Ks*   |   💵 Cash : *{cash_:,} Ks*",
    ]

    if staff_stats:
        lines.append(f"━━━━━━━━━━━━━━━━━━")
        lines.append(f"👥 *Per-Staff Breakdown:*")
        for s, sd in staff_stats.items():
            s_avg  = round(sd["revenue"] / sd["sessions"]) if sd["sessions"] > 0 else 0
            s_hrs  = round(sd["mins"] / 60, 1)
            lines.append(
                f"\n  👤 *{s}*\n"
                f"     Sessions : *{sd['sessions']}*  |  Play : *{s_hrs} hrs*\n"
                f"     Revenue  : *{sd['revenue']:,} Ks*  (avg {s_avg:,} Ks)"
            )

    lines.extend([
        f"\n━━━━━━━━━━━━━━━━━━",
        f"🛒 Food Sold : *{food_qty} pcs* ({food_item_count} types)",
        f"━━━━━━━━━━━━━━━━━━",
        f"🏆 Performance : *{star} {perf}*",
        f"📋 All-Time Records : *{total_tx}*",
    ])
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb)
    return MAIN_MENU


async def cmd_console_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show live console status — uses API (Sheet + PostgreSQL reservations)."""
    await update.message.reply_text("⏳ Console status ဆွဲနေသည်…", parse_mode="Markdown")

    data = await asyncio.to_thread(_replit_get, "sheets/consoles")
    api_consoles = (data.get("consoles", []) if isinstance(data, dict) else [])

    if not api_consoles:
        # Fallback: Google Sheet only (no reservations)
        try:
            raw = fetch_console_status()
            api_consoles = [{"id": c["id"], "type": c.get("type", ""),
                             "liveStatus": c["status"],
                             "member": c.get("member"), "startTime": c.get("start"),
                             "reservedFor": None, "reservedAt": None, "reservedDuration": None}
                            for c in raw]
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
            return

    free_list  = [c for c in api_consoles if c.get("liveStatus", "Free") == "Free"]
    busy_list  = [c for c in api_consoles if c.get("liveStatus", "Free") in ("Active", "Scheduled")]
    rsv_list   = [c for c in api_consoles if c.get("liveStatus", "Free") == "Reserved"]

    now_str = now_mmt().strftime("%H:%M")
    lines = [
        f"🕹️ *Console Status Board*  |  {now_str} MMT",
        "━━━━━━━━━━━━━━━━━━",
        f"✅ Free: {len(free_list)}  |  🔴 Active: {len(busy_list)}  |  🟡 Reserved: {len(rsv_list)}",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for c in sorted(api_consoles, key=lambda x: x.get("id", "")):
        cid    = c.get("id", "?")
        ctype  = c.get("type", "")
        live   = c.get("liveStatus", "Free")
        ctype_str = f" ({ctype})" if ctype else ""

        if live == "Free":
            icon   = "🟢"
            detail = "Free"
        elif live == "Reserved":
            icon      = "🟡"
            rsv_who   = c.get("reservedFor") or c.get("member") or "Guest"
            rsv_at    = c.get("reservedAt") or c.get("startTime") or "—"
            # Calculate end time
            dur = c.get("reservedDuration") or c.get("durationMins") or 60
            try:
                sh, sm = map(int, rsv_at.split(":"))
                total_m = sh * 60 + sm + int(dur)
                end_str = f"{total_m // 60:02d}:{total_m % 60:02d}"
                time_range = f"{rsv_at}–{end_str}"
            except Exception:
                time_range = rsv_at
            detail = f"Reserved {time_range} — {rsv_who}"
        else:
            icon   = "🔴"
            mbr    = c.get("member") or "Guest"
            since  = f" since {c['startTime']}" if c.get("startTime") else ""
            detail = f"Active — {mbr}{since}"

        # Installed games on this console (excluding SSD-transferred ones shown separately)
        installed = [
            r["game_title"] for r in fetch_console_games()
            if r["console_id"].upper() == cid.upper() and r["game_title"]
        ]
        game_str = ""
        if installed:
            game_str = f"\n    🎮 {' · '.join(installed)}"

        lines.append(f"{icon} *{cid}*{ctype_str}: {detail}{game_str}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


async def prompt_book_console(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              origin: str = "console"):
    """Show available consoles for booking. origin='console'|'admin'."""
    context.user_data["bk_origin"] = origin
    try:
        consoles = fetch_console_status()
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return CONSOLE_MENU

    free = [c for c in consoles if c["status"] == "Free"]
    if not free:
        await update.message.reply_text(
            "⚠️ လက်ရှိ Free ဖြစ်သော Console မရှိပါ\n"
            "Active session များ ဦးစွာ ဆုံးအောင်လုပ်ပါ",
            reply_markup=ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True),
        )
        return CONSOLE_MENU

    kb = [[c["id"] + (f" ({c['type']})" if c.get("type") else "")] for c in free]
    kb += [[BTN_BACK, BTN_CANCEL]]
    await update.message.reply_text(
        "▶️ *New Console Session*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🕹️ Console ရွေးပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
    )
    return BOOK_CONSOLE


async def step_book_console(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text in (BTN_BACK, BTN_BACK_MAIN):
        return await show_console_menu(update, context)

    # Extract console ID (text may include " (PS5)" suffix)
    cid = text.split("(")[0].strip()
    valid = {c["id"] for c in fetch_console_status()} or VALID_CONSOLES
    if cid not in valid:
        await update.message.reply_text("⚠️ Keyboard မှ Console ရွေးပေးပါ")
        return await prompt_book_console(update, context)

    context.user_data["bk_console"] = cid
    members = fetch_members()
    kb = [["0 (Guest)"]] + [[m] for m in members] + [[BTN_BACK, BTN_CANCEL]]
    await update.message.reply_text(
        f"🕹️ *{cid}* — session\n\n"
        "👤 Member ID ရွေးပါ (သို့) ရိုက်ရှာပါ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
    )
    return BOOK_MEMBER


async def step_book_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        return await prompt_book_console(update, context)
    if text == BTN_BACK_MAIN:
        return await show_main_menu(update, context)

    cid = context.user_data.get("bk_console", "")
    try:
        members = fetch_members()
    except Exception as e:
        await update.message.reply_text(f"❌ Member list ဖတ်မရပါ: {e}\nထပ်ကြိုးစားပါ")
        return BOOK_MEMBER

    member_id = "Guest"

    if text == "0 (Guest)":
        member_id = "Guest"
    elif text in members:
        member_id = text
    else:
        # partial search
        matches = [m for m in members if text.upper() in m.upper()]
        if len(matches) == 1:
            member_id = matches[0]
        elif matches:
            kb = [["0 (Guest)"]] + [[m] for m in matches] + [[BTN_BACK, BTN_CANCEL]]
            await update.message.reply_text(
                f"🔍 <b>{len(matches)}</b> ကိုက်ညီသည် — ရွေးပါ:",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
            )
            return BOOK_MEMBER
        else:
            member_id = text  # allow free-text (walk-in not in sheet)

    try:
        staff_list = fetch_staff_names()
        staff = staff_list[0] if len(staff_list) == 1 else context.user_data.get("staff_name", "")
    except Exception:
        staff = context.user_data.get("staff_name", "")

    # ── Duplicate session guard (non-guest only) ─────────────────────────
    if member_id not in ("Guest", "0 (Guest)"):
        try:
            all_consoles = fetch_console_status()
        except Exception:
            all_consoles = []
        existing = [
            c for c in all_consoles
            if c.get("member") == member_id and c.get("status") in ("Active", "Scheduled")
        ]
        if existing:
            # Build list of all active sessions for this member
            session_lines = []
            for ex in existing:
                s = ex.get("start", "?")
                _, dfmt = calc_duration(s) if s and s != "?" else (0, "?")
                session_lines.append(f"🕹️ <b>{ex['id']}</b>  |  🕐 {s} ({dfmt})")
            sessions_text = "\n".join(session_lines)
            # Store pending booking params for the confirm handler
            context.user_data["bk_pending_member"] = member_id
            context.user_data["bk_pending_staff"]  = staff
            await update.message.reply_text(
                f"⚠️ <b>ထပ်နေသော Session {len(existing)} ခုရှိသည်!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👤 Member  : <b>{member_id}</b>\n"
                f"{sessions_text}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"ဒီ member ကို <b>{cid}</b> မှာ ထပ် session ဖွင့်မည်လား?",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardMarkup(
                    [[BTN_BOOK_PROCEED], [BTN_NO_RESELECT]],
                    resize_keyboard=True,
                ),
            )
            return BOOK_DUP_WARN
    # ────────────────────────────────────────────────────────────────────

    # Save resolved member+staff and ask which game first
    context.user_data["bk_member"] = member_id
    context.user_data["bk_staff"]  = staff
    return await prompt_book_game(update, context)


async def prompt_book_game(update, context):
    """Ask which game the customer will play this session.
    Only shows games installed on the selected console.
    Offers SSD Transfer button if game is not yet installed.
    """
    cid       = context.user_data.get("bk_console", "")
    member_id = context.user_data.get("bk_member", "Guest")
    installed = await asyncio.to_thread(get_games_on_console, cid)
    kb_rows: list = []
    if installed:
        row: list = []
        for t in installed:
            row.append(t)
            if len(row) == 2:
                kb_rows.append(row)
                row = []
        if row:
            kb_rows.append(row)
        note = f"📋 <b>{cid}</b> တွင် ထည့်ထားသော ဂိမ်း {len(installed)} ခု"
    else:
        note = f"⚠️ <b>{cid}</b> တွင် ဂိမ်း မထည့်ရသေးပါ — SSD မှ Transfer ဦးလုပ်ပါ"
    kb_rows.append([BTN_SSD_TRANSFER])
    kb_rows.append([BTN_SKIP_GAME])
    kb_rows.append([BTN_BACK, BTN_CANCEL])
    await update.message.reply_text(
        f"🎮 <b>ဘယ် Game ကစားမည်?</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕹️ Console : <b>{cid}</b>  👤 <b>{member_id}</b>\n"
        f"{note}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"မပါသော ဂိမ်း ဆော့မည်ဆို <b>🔄 SSD Transfer</b> နှိပ်ပါ",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb_rows, resize_keyboard=True),
    )
    return BOOK_GAME


async def step_book_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle game selection for the session."""
    text = update.message.text.strip()
    cid       = context.user_data.get("bk_console", "")
    member_id = context.user_data.get("bk_member", "Guest")
    staff     = context.user_data.get("bk_staff", "")
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        context.user_data["bk_member"] = member_id
        context.user_data["bk_staff"]  = staff
        return BOOK_MEMBER
    if text == BTN_SSD_TRANSFER:
        # Redirect to SSD transfer, auto-fill target console = bk_console
        context.user_data["ssd_return_to_session"] = True
        context.user_data["ssd_xfer_target_cons"]  = cid
        await update.message.reply_text(
            f"🔄 <b>SSD → {cid} Transfer</b>\n\nSSD ကို ရွေးပါ:",
            parse_mode="HTML",
            reply_markup=_ssd_kb(),
        )
        return SSD_XFER_SSD
    game = "" if text == BTN_SKIP_GAME else text
    context.user_data["bk_game"] = game
    return await prompt_book_mins(update, context)


async def prompt_book_mins(update, context):
    """Ask for planned play duration so a 5-min reminder can be scheduled."""
    cid       = context.user_data.get("bk_console", "")
    member_id = context.user_data.get("bk_member", "Guest")
    kb = [
        ["30", "60", "90"],
        ["120", "150", "180"],
        ["240", "300", "360"],
        [BTN_SKIP_TIMER],
        [BTN_BACK, BTN_CANCEL],
    ]
    await update.message.reply_text(
        f"⏱️ <b>Play Duration (Timer)</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕹️ Console : <b>{cid}</b>  👤 <b>{member_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"ကစားမည့် မိနစ် ရွေးပါ (5min မတိုင်ခင် auto-remind ပေးမည်)\n"
        f"မလိုပါက Skip နှိပ်ပါ",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return BOOK_MINS


async def step_book_mins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle planned-mins input and finalize the booking."""
    text      = update.message.text.strip()
    cid       = context.user_data.get("bk_console", "")
    member_id = context.user_data.pop("bk_member", "Guest")
    staff     = context.user_data.pop("bk_staff", "")

    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)
    if text == BTN_BACK:
        # Go back to game selection
        context.user_data["bk_member"] = member_id
        context.user_data["bk_staff"]  = staff
        return await prompt_book_game(update, context)

    planned_mins = 0
    if text != BTN_SKIP_TIMER:
        try:
            planned_mins = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ ဂဏန်းသာ ထည့်ပါ သို့ keyboard မှ ရွေးပါ")
            context.user_data["bk_member"] = member_id
            context.user_data["bk_staff"]  = staff
            return await prompt_book_mins(update, context)

    game = context.user_data.pop("bk_game", "")
    return await _do_create_booking(update, context, cid, member_id, staff, planned_mins, game)


def _extend_timer_kb(cid: str, member_id: str, chat_id: int) -> InlineKeyboardMarkup:
    """Inline keyboard attached to reminder messages for extending the session."""
    tag = f"{cid}|{member_id}|{chat_id}"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ +30 min", callback_data=f"ext:30:{tag}"),
            InlineKeyboardButton("➕ +60 min", callback_data=f"ext:60:{tag}"),
            InlineKeyboardButton("➕ +90 min", callback_data=f"ext:90:{tag}"),
        ],
        [InlineKeyboardButton("✏️ Custom (မိနစ် ကိုယ်တိုင်ထည့်)", callback_data=f"ext:custom:{tag}")],
        [InlineKeyboardButton("✅ ပြီးပြီ (End မည်)", callback_data=f"ext:0:{tag}")],
    ])


# ── Reminder task tracker (keyed by "cid|chat_id") ──────────────────────────
_REMIND_TASKS: dict[str, "asyncio.Task[None]"] = {}

def _remind_key(cid: str, chat_id: int) -> str:
    return f"{cid}|{chat_id}"

def _cancel_remind(cid: str, chat_id: int) -> None:
    key  = _remind_key(cid, chat_id)
    task = _REMIND_TASKS.pop(key, None)
    if task and not task.done():
        task.cancel()

def _is_session_active(cid: str) -> bool:
    """Quick sync check: is this console still Active in Console_Booking today?"""
    try:
        sh   = get_booking_sh()
        rows = sh.get_all_values()
        td   = today_str()
        for row in rows[1:]:
            if len(row) < 7:
                continue
            if row[1].strip() == td and row[2].strip() == cid and row[6].strip() == "Active":
                return True
    except Exception:
        return True   # assume active if can't read sheet
    return False

async def _remind_loop(
    bot, chat_id: int, cid: str, member_id: str,
    planned_mins: int, end_t: str, initial_delay: int,
):
    """Fires reminder at initial_delay, then every 5 mins while session is still Active.

    IMPORTANT: The FIRST fire always sends (no active-check) so that edge cases
    like an interrupted session-end flow (status briefly "Ended") still deliver
    the inline-keyboard Extend/Done prompt.  Subsequent fires check the sheet.
    """
    key = _remind_key(cid, chat_id)
    _REMIND_TASKS[key] = asyncio.current_task()   # type: ignore[assignment]
    try:
        await asyncio.sleep(initial_delay)
        fire_count = 0
        while True:
            # Skip active-check on first fire — always deliver the first reminder
            if fire_count > 0:
                still_active = await asyncio.to_thread(_is_session_active, cid)
                if not still_active:
                    break
            fire_count += 1
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⏰ <b>Session Reminder!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"🕹️ Console : <b>{cid}</b>\n"
                        f"👤 Member  : <b>{member_id}</b>\n"
                        f"⏱️ Planned : <b>{planned_mins} mins</b>\n"
                        f"🕑 End ~   : <b>{end_t}</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"⚠️ <b>Session ဆုံးချိန်ရောက်ပြီ!</b>\n"
                        f"ဆက်ကစားမည်ဆိုက ➕ Extend ကိုနှိပ်ပါ\n"
                        f"ပြီးပြီဆိုက ✅ ပြီးပြီ ကိုနှိပ်ပြီး ⏹️ Session ဆုံး နှိပ်ပါ"
                    ),
                    parse_mode="HTML",
                    reply_markup=_extend_timer_kb(cid, member_id, chat_id),
                )
            except Exception:
                pass
            # ── customer session warning (if member has a known chat_id) ───
            if member_id not in ("Guest", "0 (Guest)", ""):
                try:
                    cust_chat = await asyncio.to_thread(get_customer_chat_id, member_id)
                    if cust_chat:
                        cust_msg = (
                            f"⏰ <b>PS VIBE — Session သတိပေးချက်!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"🕹️ Console: <b>{cid}</b>\n"
                            f"⏱️ <b>5 မိနစ် ကျန်တော့သည်</b>\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"ဆက်ကစားလိုပါက Staff ကို ပြောပြပါ"
                        )
                        await asyncio.to_thread(_notify_customer, cust_chat, cust_msg)
                except Exception:
                    pass
            # ── next reminder in 5 mins ────────────────────────────────────
            await asyncio.sleep(5 * 60)
    except asyncio.CancelledError:
        pass
    finally:
        _REMIND_TASKS.pop(key, None)

async def _send_session_reminder(
    bot, chat_id: int, cid: str, member_id: str,
    planned_mins: int, end_t: str, delay_secs: int,
):
    """Legacy single-fire wrapper — kept for n8n fallback path.
    Real repeat logic lives in _remind_loop."""
    await asyncio.sleep(delay_secs)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⏰ <b>Session Reminder!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🕹️ Console : <b>{cid}</b>\n"
                f"👤 Member  : <b>{member_id}</b>\n"
                f"⏱️ Planned : <b>{planned_mins} mins</b>\n"
                f"🕑 End ~   : <b>{end_t}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ <b>Session ဆုံးချိန်ရောက်ပြီ!</b>\n"
                f"ဆက်ကစားမည်ဆိုက ➕ Extend ကိုနှိပ်ပါ\n"
                f"ပြီးပြီဆိုက ✅ ပြီးပြီ ကိုနှိပ်ပြီး ⏹️ Session ဆုံး နှိပ်ပါ"
            ),
            parse_mode="HTML",
            reply_markup=_extend_timer_kb(cid, member_id, chat_id),
        )
    except Exception:
        pass


async def _post_n8n_session_reminder(
    chat_id: int, cid: str, member_id: str,
    planned_mins: int, end_t: str, delay_secs: int,
) -> bool:
    """POST session reminder payload to n8n webhook (restart-proof timer).
    Uses stdlib urllib so no extra package needed on VPS."""
    if not N8N_SESSION_WEBHOOK:
        return False
    import json as _json
    import urllib.request as _req
    remind_at_dt  = now_mmt() + timedelta(seconds=delay_secs)
    remind_at_iso = remind_at_dt.isoformat()
    message = (
        f"⏰ <b>Session Reminder!</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕹️ Console : <b>{cid}</b>\n"
        f"👤 Member  : <b>{member_id}</b>\n"
        f"⏱️ Planned : <b>{planned_mins} mins</b>\n"
        f"🕑 End ~   : <b>{end_t}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <b>5 မိနစ်အတွင်း Session ဆုံးမည်!</b>\n"
        f"ဆက်ကစားမည်ဆိုက ➕ Extend ကိုနှိပ်ပါ\n"
        f"ပြီးပြီဆိုက ✅ ပြီးပြီ ကိုနှိပ်ပြီး ⏹️ Session ဆုံး နှိပ်ပါ"
    )
    payload = _json.dumps({
        "chat_id":     chat_id,
        "cid":         cid,
        "member_id":   member_id,
        "planned_mins": planned_mins,
        "end_t":       end_t,
        "remind_at":   remind_at_iso,
        "message":     message,
    }).encode()
    try:
        request = _req.Request(
            N8N_SESSION_WEBHOOK,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        await asyncio.to_thread(lambda: _req.urlopen(request, timeout=10))
        return True
    except Exception as e:
        logging.warning(f"n8n session reminder POST failed: {e}")
        return False


async def _post_n8n_booking_reminder(
    bk_id: int, customer_name: str, phone: str,
    console_id: str, console_type: str,
    date_str: str, time_slot: str, duration_mins: int,
    tg_chat: str = "",
) -> bool:
    """POST booking confirmation to n8n for restart-proof follow-up reminders.
    n8n workflow schedules:
      • 10-min-before  → customer + staff reminder
      • At booking time → staff check-in prompt (Arrived / No-Show buttons)
      • +15 min         → auto-cancel if still confirmed
    """
    if not N8N_BOOKING_WEBHOOK:
        return False
    import json as _json, urllib.request as _req2, re as _re
    m = _re.match(r"(\d+)/(\d+)/(\d+)", date_str or "")
    if not m:
        logging.warning("_post_n8n_booking_reminder: bad date_str=%s", date_str)
        return False
    try:
        mon, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        h, mi = map(int, time_slot.split(":"))
        booking_dt  = datetime(year, mon, day, h, mi, tzinfo=MMT)
        booking_iso = booking_dt.isoformat()
    except Exception as e:
        logging.warning("_post_n8n_booking_reminder: parse error %s", e)
        return False
    api_url  = (_api_base() + "/api") if _api_base() else ""
    payload  = _json.dumps({
        "bk_id":            bk_id,
        "customer_name":    customer_name,
        "phone":            phone,
        "console_id":       console_id,
        "console_type":     console_type,
        "date":             date_str,
        "time_slot":        time_slot,
        "booking_iso":      booking_iso,
        "duration_mins":    duration_mins,
        "staff_notify_chat": STAFF_NOTIFY_CHAT,
        "telegram_chat_id": tg_chat,
        "replit_api_url":   api_url,
    }).encode()
    try:
        req = _req2.Request(
            N8N_BOOKING_WEBHOOK, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        await asyncio.to_thread(lambda: _req2.urlopen(req, timeout=10))
        logging.info("n8n booking reminder queued — bk#%s at %s", bk_id, booking_iso)
        return True
    except Exception as e:
        logging.warning("n8n booking webhook POST failed: %s", e)
        return False


async def cmd_cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of upcoming confirmed bookings — staff can cancel any of them."""
    await update.message.reply_text("⏳ Booking list ရယူနေသည်...")
    data = await asyncio.to_thread(_replit_get, "bookings?status=confirmed")
    bks  = data if isinstance(data, list) else []
    if not bks:
        await update.message.reply_text(
            "📅 ဖျက်ရန် Confirmed Booking မရှိပါ",
            reply_markup=ReplyKeyboardMarkup([[BTN_BACK_MAIN]], resize_keyboard=True),
        )
        return MAIN_MENU
    now_str = now_mmt().strftime("%H:%M")
    upcoming = [b for b in bks if (b.get("date","") > now_mmt().strftime("%-m/%-d/%Y")
                                   or (b.get("date","") == now_mmt().strftime("%-m/%-d/%Y")
                                       and (b.get("timeSlot","") or "99:99") >= now_str))]
    if not upcoming:
        upcoming = bks  # show all if none are upcoming
    for b in upcoming[:10]:
        console_hint = b.get("consoleId") or b.get("consoleType","?")
        card = (
            f"🎫 <b>#{b['id']} {b['customerName']}</b>\n"
            f"📅 {b['date']}  ⏰ {b['timeSlot']}\n"
            f"🕹️ {console_hint}  ⏱️ {b.get('durationMins','?')} min\n"
            f"📞 {b.get('phone','-')}  "
            f"{'🔵 Today' if b.get('date') == now_mmt().strftime('%-m/%-d/%Y') else ''}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚫 Cancel Booking", callback_data=f"bkc:{b['id']}"),
        ]])
        await update.message.reply_text(card, parse_mode="HTML", reply_markup=kb)
    await update.message.reply_text(
        f"↑ Cancel လုပ်ချင်သည့် Booking ကိုရွေးပါ ({len(upcoming)} bookings).",
        reply_markup=ReplyKeyboardMarkup([[BTN_BACK_MAIN]], resize_keyboard=True),
    )
    return MAIN_MENU


# Module-level store for pending cancel-with-custom-note requests
# Key: user_id (int) → {"bk_id": int, "staff": str, "chat_id": int}
_pending_cancel_note: dict[int, dict] = {}


async def cb_cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline 🚫 Cancel Booking — show reason selection first."""
    query = update.callback_query
    await query.answer()
    try:
        bk_id = int(query.data.split(":")[1])
    except Exception:
        return

    # Fetch current booking info for confirmation display
    bk_info = await asyncio.to_thread(_replit_get, f"bookings/{bk_id}")
    if not bk_info or isinstance(bk_info, list):
        bk_info = {}

    cur_status = bk_info.get("status", "")
    if cur_status in ("cancelled", "rejected", "completed"):
        try:
            await query.edit_message_text(
                f"⚠️ Booking #{bk_id} မှာ ဆောင်ရွက်မရနိုင်ပါ (status: {cur_status})",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    cust_name = bk_info.get("customerName", "?")
    date_str  = bk_info.get("date", "?")
    slot_str  = bk_info.get("timeSlot", "?")
    cons_str  = bk_info.get("consoleId") or bk_info.get("consoleType", "?")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Customer ရပ်တောင်းသောကြောင့်", callback_data=f"bkcr:{bk_id}:cust")],
        [InlineKeyboardButton("🖥️ Console / Technical ပြဿနာ",  callback_data=f"bkcr:{bk_id}:cons")],
        [InlineKeyboardButton("📅 Schedule ပြောင်းလဲသောကြောင့်", callback_data=f"bkcr:{bk_id}:sche")],
        [InlineKeyboardButton("✏️ Note ကိုယ်တိုင်ရိုက်မည်",       callback_data=f"bkcr:{bk_id}:custom")],
        [InlineKeyboardButton("↩️ မပယ်ဖျက်တော့ပါ",              callback_data=f"bkcr:{bk_id}:abort")],
    ])
    try:
        await query.edit_message_text(
            f"🚫 <b>Cancel Booking #{bk_id}?</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 {cust_name}  📅 {date_str}\n"
            f"⏰ {slot_str}  🎮 {cons_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"ပယ်ဖျက်ရသည့် အကြောင်းပြချက်ရွေးပါ ↓",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception:
        pass


async def cb_cancel_with_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle reason selection for cancel booking flow."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":", 2)
    if len(parts) < 3:
        return
    try:
        bk_id = int(parts[1])
    except Exception:
        return
    reason_key = parts[2]
    staff_name = query.from_user.full_name or "Staff"

    if reason_key == "abort":
        try:
            await query.edit_message_text("↩️ Cancel ပယ်ဖျက်မည့် လုပ်ငန်းကို ရပ်လိုက်သည်။")
        except Exception:
            pass
        return

    if reason_key == "custom":
        # Store pending and ask for typed note
        _pending_cancel_note[query.from_user.id] = {
            "bk_id":   bk_id,
            "staff":   staff_name,
            "chat_id": query.message.chat_id,
        }
        try:
            await query.edit_message_text(
                f"✏️ <b>Booking #{bk_id} — Custom Note</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"ပယ်ဖျက်ရသည့် အကြောင်းပြချက် ရိုက်ပို့ပါ:\n"
                f"<i>(e.g. Double booking, မလာနိုင်ဘူး...)</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    reason_labels = {
        "cust": "Customer ရပ်တောင်းသောကြောင့်",
        "cons": "Console / Technical ပြဿနာကြောင့်",
        "sche": "Schedule ပြောင်းလဲသောကြောင့်",
    }
    reason = reason_labels.get(reason_key, "Staff Cancelled")
    await _do_cancel_booking(query, bk_id, staff_name, reason)


async def _do_cancel_booking(query_or_msg, bk_id: int, staff_name: str, reason: str):
    """Execute the cancel PATCH and notify customer. Works for both callback query and message."""
    staff_note = f"Cancelled by {staff_name}: {reason}"
    result = await asyncio.to_thread(
        _replit_patch,
        f"bookings/{bk_id}/status",
        {"status": "cancelled", "staffNote": staff_note},
    )
    is_query = hasattr(query_or_msg, "edit_message_text")
    if not result:
        txt = f"❌ Booking #{bk_id} cancel မရပါ — API စစ်ပါ"
        try:
            if is_query:
                await query_or_msg.edit_message_text(txt)
            else:
                await query_or_msg.reply_text(txt)
        except Exception:
            pass
        return

    done_txt = (
        f"🚫 <b>Booking #{bk_id} Cancelled</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 {result.get('customerName','?')}  📅 {result.get('date','?')}\n"
        f"⏰ {result.get('timeSlot','?')}  🎮 {result.get('consoleType','?')}\n"
        f"📝 {reason}\n"
        f"👮 {staff_name}"
    )
    try:
        if is_query:
            await query_or_msg.edit_message_text(done_txt, parse_mode="HTML")
        else:
            await query_or_msg.reply_text(done_txt, parse_mode="HTML")
    except Exception:
        pass

    # Notify customer if they have Telegram
    tg_chat = result.get("telegramChatId") or ""
    if tg_chat and CUSTOMER_BOT_TOKEN:
        cust_msg = (
            f"❌ <b>Booking #{bk_id} ကို ပယ်ဖျက်ပြီ</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📅 {result.get('date','?')}  ⏰ {result.get('timeSlot','?')}\n"
            f"🎮 {result.get('consoleType','?')}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📝 အကြောင်းပြချက်: {reason}\n"
            f"ကျေးဇူးပြု၍ ဆက်သွယ်ရန် @psvibeofficial"
        )
        _notify_customer(tg_chat, cust_msg)


async def handle_cancel_note_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle typed cancel reason from staff (pending custom note)."""
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id or user_id not in _pending_cancel_note:
        return
    pending   = _pending_cancel_note.pop(user_id)
    bk_id     = pending["bk_id"]
    staff     = pending["staff"]
    reason    = (update.message.text or "").strip() or "Note မပေး"
    await _do_cancel_booking(update.message, bk_id, staff, reason)


async def _do_extend(bot, query, cid: str, member_id: str,
                     chat_id: int, extra_mins: int):
    """Shared logic: acknowledge extension and schedule next reminder."""
    now        = now_mmt()
    new_end_dt = now + timedelta(minutes=extra_mins)
    new_end_t  = new_end_dt.strftime("%H:%M")
    has_remind = extra_mins > 5

    text = (
        f"⏰ <b>Session Extended!</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕹️ Console : <b>{cid}</b>\n"
        f"👤 Member  : <b>{member_id}</b>\n"
        f"➕ Extended: <b>+{extra_mins} mins</b>\n"
        f"🕑 New End : <b>{new_end_t}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{'⏰ Next reminder ပေးမည်' if has_remind else '⚠️ 5min မတိုင်တော့ Reminder မပေးနိုင်'}"
    )
    if query is not None:
        await query.edit_message_text(text, parse_mode="HTML")
    else:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

    _cancel_remind(cid, chat_id)   # stop old loop before starting new one
    if has_remind:
        ext_delay = (extra_mins - 5) * 60   # seconds until 5-min-before-end
        if N8N_SESSION_WEBHOOK:
            # n8n fires restart-proof text reminder at ext_delay;
            # bot loop fires at the SAME time so inline-keyboard buttons are always shown.
            asyncio.create_task(
                _post_n8n_session_reminder(
                    chat_id, cid, member_id, extra_mins, new_end_t, ext_delay,
                )
            )
        # Bot loop: fire at "5 min before end", then every 5 min (same timing with or without n8n)
        task = asyncio.create_task(
            _remind_loop(bot, chat_id, cid, member_id,
                         extra_mins, new_end_t, ext_delay)
        )
        _REMIND_TASKS[_remind_key(cid, chat_id)] = task


async def cb_extend_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """CallbackQuery handler for ➕ Extend / ✏️ Custom / ✅ Done buttons."""
    query = update.callback_query
    await query.answer()

    data = query.data  # "ext:{extra_str}:{cid}|{member_id}|{chat_id}"
    try:
        _, extra_str, tag = data.split(":", 2)
        cid, member_id, chat_id_str = tag.split("|", 2)
        chat_id = int(chat_id_str)
    except Exception:
        await query.edit_message_text("⚠️ Data error — ထပ်မံကြိုးစားပါ")
        return

    # ── ✅ End ───────────────────────────────────────────────────────────────
    if extra_str == "0":
        await query.edit_message_text(
            f"✅ <b>Session ပြီးပြီ!</b>\n"
            f"🕹️ Console : <b>{cid}</b>  👤 <b>{member_id}</b>\n"
            f"⏹️ Session ဆုံး နှိပ်ပြီး Voucher ဖန်တီးပါ",
            parse_mode="HTML",
        )
        return

    # ── ✏️ Custom ────────────────────────────────────────────────────────────
    if extra_str == "custom":
        # Store pending extend context so the next text reply is captured
        context.user_data["_extend_pending"] = {
            "cid": cid, "member_id": member_id, "chat_id": chat_id,
        }
        # Edit reminder to signal we're waiting
        await query.edit_message_text(
            f"✏️ <b>Custom Extend</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕹️ Console : <b>{cid}</b>  👤 <b>{member_id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"ဆက်ကစားမည့် မိနစ် ရိုက်ထည့်ပြီး Send လုပ်ပါ\n"
            f"(ဥပမာ: <code>45</code>)",
            parse_mode="HTML",
        )
        # ForceReply so the keyboard pops up on mobile automatically
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏱️ ဆက်ကစားမည့် မိနစ် ထည့်ပါ:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="မိနစ် (ဥပမာ 45)"),
        )
        return

    # ── Preset +N ────────────────────────────────────────────────────────────
    try:
        extra_mins = int(extra_str)
    except ValueError:
        await query.edit_message_text("⚠️ Data error")
        return

    await _do_extend(context.bot, query, cid, member_id, chat_id, extra_mins)


async def handle_custom_extend_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group -1 handler: captures the free-text reply for custom extend minutes.
    Raises ApplicationHandlerStop to prevent ConversationHandler from also firing."""
    pending = context.user_data.get("_extend_pending")
    if pending is None:
        return  # not our message — let ConversationHandler handle it normally

    text = update.message.text.strip()
    try:
        extra_mins = int(text)
        if extra_mins <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ မှန်ကန်သော ဂဏန်း ရိုက်ထည့်ပါ (ဥပမာ: 45)",
            reply_markup=ForceReply(selective=True, input_field_placeholder="မိနစ် (ဥပမာ 45)"),
        )
        raise ApplicationHandlerStop  # keep _extend_pending; wait for correct input

    cid       = pending["cid"]
    member_id = pending["member_id"]
    chat_id   = pending["chat_id"]
    context.user_data.pop("_extend_pending", None)

    await _do_extend(context.bot, None, cid, member_id, chat_id, extra_mins)
    raise ApplicationHandlerStop  # done — don't let conv handler see this message


async def _do_create_booking(update, context, cid: str, member_id: str,
                              staff: str, planned_mins: int = 0, game: str = ""):
    """Actually create the booking, show confirmation, and schedule timer if set."""
    try:
        bk_id = create_booking(cid, member_id, staff, notes=game)
    except Exception as e:
        await update.message.reply_text(f"❌ Session save မအောင်မြင်ပါ: {e}")
        return await show_console_menu(update, context)

    # Track current session game in Console_Games (type = "Session")
    if game:
        try:
            # Remove any previous Session entry for this console first
            _delete_session_game(cid)
            write_console_game(cid, game, "Session", f"BK:{bk_id}")
        except Exception:
            pass

    now      = now_mmt()
    start_t  = now.strftime("%H:%M")
    timer_line = ""
    game_line  = f"\n🎮 Game    : <b>{game}</b>" if game else ""
    if planned_mins > 5:
        end_dt     = now + timedelta(minutes=planned_mins)
        end_t      = end_dt.strftime("%H:%M")
        delay_secs = (planned_mins - 5) * 60
        chat_id    = update.effective_chat.id

        if N8N_SESSION_WEBHOOK:
            # n8n fires restart-proof text reminder at delay_secs (5 min before end)
            asyncio.create_task(
                _post_n8n_session_reminder(
                    chat_id, cid, member_id, planned_mins, end_t, delay_secs,
                )
            )
        # Bot loop fires at the SAME time (5 min before end), with or without n8n,
        # so inline-keyboard Extend/Done buttons are always shown on time.
        _cancel_remind(cid, chat_id)   # clear any stale task for this console
        task = asyncio.create_task(
            _remind_loop(context.bot, chat_id, cid, member_id,
                         planned_mins, end_t, delay_secs)
        )
        _REMIND_TASKS[_remind_key(cid, chat_id)] = task
        timer_line = f"\n⏰ Timer    : <b>{planned_mins} mins</b> (remind @ {end_t} — ဆုံးတဲ့အချိန် 5min ကြားတိုင်း repeat)"

    await update.message.reply_text(
        f"✅ <b>Session စတင်ပြီ!</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🪪 ID      : <code>{bk_id}</code>\n"
        f"🕹️ Console : <b>{cid}</b>\n"
        f"👤 Member  : <b>{member_id}</b>"
        f"{game_line}\n"
        f"🕐 Start   : <b>{start_t}</b>"
        f"{timer_line}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Session ဆုံးသောအခါ ⏹️ Session ဆုံး နှိပ်ပါ",
        parse_mode="HTML",
    )
    return await show_console_menu(update, context)


async def step_book_dup_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Proceed / Back after duplicate-session warning during booking."""
    text = update.message.text.strip()
    cid         = context.user_data.get("bk_console", "")
    member_id   = context.user_data.pop("bk_pending_member", "Guest")
    staff       = context.user_data.pop("bk_pending_staff", "")

    if text == BTN_BOOK_PROCEED:
        # Proceed with game selection step
        context.user_data["bk_member"] = member_id
        context.user_data["bk_staff"]  = staff
        return await prompt_book_game(update, context)

    # BTN_NO_RESELECT / BTN_CANCEL / anything else → back to member selection
    context.user_data["bk_console"] = cid
    return BOOK_MEMBER


# ═════════════════════════════════════════
#  CONSOLE MANAGEMENT — full submenu
# ═════════════════════════════════════════

async def show_console_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Console Management submenu — accessible from Main Menu and Admin Panel."""
    kb = [
        [BTN_START_SESSION,  BTN_END_SESSION],
        [BTN_STATUS_BOARD,   BTN_GAME_LIB_MENU],
        [BTN_CHANGE_GAME,    BTN_SSD_MANAGE],
        [BTN_BACK_MAIN],
    ]
    await update.message.reply_text(
        "🕹️ *Console Management*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Action ရွေးပါ ↓",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return CONSOLE_MENU


async def step_console_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == BTN_BACK_MAIN:
        return await show_main_menu(update, context)
    if choice == BTN_START_SESSION:
        return await prompt_book_console(update, context)
    if choice == BTN_END_SESSION:
        return await prompt_end_session(update, context)
    if choice == BTN_STATUS_BOARD:
        await cmd_console_status(update, context)
        return await show_console_menu(update, context)
    if choice == BTN_GAME_LIB_MENU:
        return await show_game_menu(update, context)
    if choice == BTN_SSD_MANAGE:
        return await show_ssd_menu(update, context)
    if choice == BTN_CHANGE_GAME:
        return await prompt_game_change_cons(update, context)
    return await show_console_menu(update, context)


# ─── Game Change for Active Session ───────────────────────────────────────────

async def prompt_game_change_cons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show active consoles so staff can pick which one to change game for."""
    try:
        consoles = fetch_console_status()
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return await show_console_menu(update, context)
    active = [c for c in consoles if c["status"] == "Active"]
    if not active:
        await update.message.reply_text(
            "ℹ️ လက်ရှိ Active session မရှိပါ",
            reply_markup=ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True),
        )
        return CONSOLE_MENU
    kb = [[f"{c['id']} ({c.get('member') or 'Guest'})"] for c in active] + [[BTN_BACK]]
    await update.message.reply_text(
        "🔄 <b>Game ပြောင်း</b>\n\nGame ပြောင်းမည့် Active Console ရွေးပါ:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return GAME_CHANGE_CONS


async def step_game_change_cons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_BACK:
        return await show_console_menu(update, context)
    cid = text.split("(")[0].strip()
    context.user_data["gc_console"] = cid
    # Show current game
    cur_games = [
        r["game_title"] for r in fetch_console_games()
        if r["console_id"].upper() == cid.upper() and r["install_type"] == "Session"
    ]
    cur_str = f"ဘာသိသလဲ: <b>{cur_games[0]}</b>" if cur_games else "Current Game: —"
    # Only show games installed on this console
    installed = await asyncio.to_thread(get_games_on_console, cid)
    kb_rows: list = []
    if installed:
        row: list = []
        for t in installed:
            row.append(t)
            if len(row) == 2:
                kb_rows.append(row)
                row = []
        if row:
            kb_rows.append(row)
    else:
        kb_rows.append(["(ဂိမ်း မရှိသေးပါ)"])
    kb_rows.append([BTN_SSD_TRANSFER])
    kb_rows.append([BTN_SKIP_GAME])
    kb_rows.append([BTN_BACK])
    await update.message.reply_text(
        f"🕹️ <b>{cid}</b>\n{cur_str}\n\n"
        f"🎮 အသစ် ကစားမည့် ဂိမ်း ရွေးပါ\n"
        f"မပါသော ဂိမ်းဆို <b>🔄 SSD Transfer</b> နှိပ်ပါ:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb_rows, resize_keyboard=True),
    )
    return GAME_CHANGE_GAME


async def step_game_change_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    cid  = context.user_data.pop("gc_console", "")
    if text == BTN_BACK:
        return await prompt_game_change_cons(update, context)
    if text == BTN_SKIP_GAME:
        await update.message.reply_text("ℹ️ ပြောင်းမပြောင်းဘဲ ထားခဲ့သည်")
        return await show_console_menu(update, context)
    if text == BTN_SSD_TRANSFER:
        # Redirect to SSD transfer; after transfer return to game-change console picker
        context.user_data["gc_console"] = cid  # restore popped cid
        context.user_data["ssd_return_to_session"] = True
        context.user_data["ssd_xfer_target_cons"]  = cid
        await update.message.reply_text(
            f"🔄 <b>SSD → {cid} Transfer</b>\n\nSSD ကို ရွေးပါ:",
            parse_mode="HTML",
            reply_markup=_ssd_kb(),
        )
        return SSD_XFER_SSD
    new_game = text
    # Delete old Session entry, write new one
    _delete_session_game(cid)
    ok = add_console_game(cid, new_game, "Session", "Changed")
    if ok:
        await update.message.reply_text(
            f"✅ <b>{cid}</b> → Game ပြောင်းပြီ\n🎮 <b>{new_game}</b>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("❌ Game ပြောင်းမရပါ — ထပ်ကြိုးစားပါ")
    return await show_console_menu(update, context)


# ─── End Session ──────────────────────────────────────────────────────────────

async def prompt_end_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of active sessions for the user to pick and end."""
    try:
        consoles = fetch_console_status()
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return await show_console_menu(update, context)

    active = [c for c in consoles if c["status"] == "Active"]
    if not active:
        await update.message.reply_text(
            "ℹ️ လက်ရှိ Active session မရှိပါ",
            reply_markup=ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True),
        )
        return CONSOLE_MENU

    lines = ["⏹️ <b>Session ဆုံးမည် — Console ရွေးပါ</b>\n━━━━━━━━━━━━━━━━━━"]
    kb    = []
    for c in active:
        _, dur_fmt = calc_duration(c["start"]) if c.get("start") else (0, "?")
        mbr  = c.get("member") or "Guest"
        ctype = f" ({c['type']})" if c.get("type") else ""
        lines.append(f"🔴 <b>{c['id']}</b>{ctype}  |  👤 {mbr}  |  ⏱ {dur_fmt}")
        kb.append([c["id"] + (f" ({c['type']})" if c.get("type") else "")])
    kb.append([BTN_BACK])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
    )
    return END_SESSION_SELECT


async def step_end_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked a console to end — find its booking and end it."""
    text = update.message.text.strip()
    if text in (BTN_BACK, BTN_BACK_MAIN):
        return await show_console_menu(update, context)
    if text == BTN_CANCEL:
        return await cmd_cancel(update, context)

    cid = text.split("(")[0].strip()
    try:
        consoles = fetch_console_status()
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return await show_console_menu(update, context)

    target = next((c for c in consoles if c["id"] == cid and c["status"] == "Active"), None)
    if not target:
        await update.message.reply_text(
            f"⚠️ <b>{cid}</b> မှာ Active session မတွေ့ပါ\nStatus စစ်ကြည့်ပါ",
            parse_mode="HTML",
        )
        return await prompt_end_session(update, context)

    bk_id   = target.get("booking_id", "")
    start_t = target.get("start", "")
    mbr     = target.get("member") or "Guest"
    session_staff = target.get("staff", "")
    total_mins, dur_fmt = calc_duration(start_t) if start_t else (0, "?")

    ok = end_booking(bk_id) if bk_id else False
    if not ok:
        await update.message.reply_text(f"❌ Booking ID ရှာမတွေ့ပါ ({bk_id})")
        return await show_console_menu(update, context)

    end_t = now_mmt().strftime("%H:%M")

    # ── SSD Transfer Warning ────────────────────────────────────────────────
    ssd_warn = ""
    ssd_transfers = [
        r for r in fetch_console_games()
        if r["console_id"].upper() == cid.upper()
        and "SSD Transfer" in r.get("install_type", "")
    ]
    if ssd_transfers:
        game_names = [r["game_title"] for r in ssd_transfers]
        ssd_warn = (
            f"\n\n⚠️ <b>SSD ပြန်ရွေ့ပါ!</b>\n"
            f"ဤ console မှ SSD ထဲ ပြန်ရွေ့ရမည့် ဂိမ်းများ:\n"
            + "\n".join(f"  📀 {g}" for g in game_names)
        )

    # Show current session game if any
    session_games = [
        r["game_title"] for r in fetch_console_games()
        if r["console_id"].upper() == cid.upper() and r["install_type"] == "Session"
    ]
    game_line = f"\n🎮 Game     : <b>{session_games[0]}</b>" if session_games else ""

    await update.message.reply_text(
        f"✅ <b>Session ဆုံးပြီ!</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕹️ Console  : <b>{cid}</b>\n"
        f"👤 Member   : <b>{mbr}</b>"
        f"{game_line}\n"
        f"🕐 Start    : <b>{start_t}</b>\n"
        f"🕑 End      : <b>{end_t}</b>\n"
        f"⏱ Duration : <b>{dur_fmt}</b> ({total_mins} mins)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 Sales Voucher ဖွင့်နေသည်..."
        f"{ssd_warn}",
        parse_mode="HTML",
    )
    # Clean up Session game entry for this console
    _delete_session_game(cid)
    return await launch_session_sale(update, context, cid, mbr, total_mins, session_staff)


# ─── Session → Daily Sales Bridge ─────────────────────────────────────────────

async def launch_session_sale(
    update, context,
    cid: str, member_id: str, total_mins: int, session_staff: str,
    pre_effective_mins: int = 0,
):
    """Pre-fill user_data from session data and route to the Daily Sales food-menu.
    pre_effective_mins: if > 0, use as effective wallet cost directly (for combined
    sessions where each console may have a different multiplier).
    """
    is_guest = member_id in ("Guest", "0 (Guest)", "")

    base_rate  = fetch_base_rate()
    # For combined cids (e.g. "C-09+C-10") multiplier lookup returns 1.0 — that's fine
    # because pre_effective_mins already encodes the per-console multipliers.
    multiplier = fetch_console_multiplier(cid) if "+" not in cid else 1.0

    # Fetch food prices filtered by stock
    food_prices = fetch_food_prices()
    stock_map: dict = {}
    inv_data = _replit_get("sheets/inventory")
    if inv_data:
        stock_map = {i["name"]: max(0, i.get("current_stock", 0))
                     for i in inv_data.get("items", [])}
        food_prices = {k: v for k, v in food_prices.items()
                       if stock_map.get(k, 1) > 0}

    m_id = "0 (Guest)" if is_guest else member_id

    context.user_data.update({
        "m_id":             m_id,
        "c_id":             cid,
        "mins":             total_mins,
        "actual_play_mins": total_mins,
        "base_rate":        base_rate,
        "multiplier":       multiplier,
        "v_no":             next_voucher(),
        "food_items":       [],
        "food_prices":      food_prices,
        "food_stock_map":   stock_map,
        "staff":            session_staff,
        "from_session":     True,
    })

    if is_guest:
        game_amt = round((total_mins * base_rate * multiplier) / 60)
        context.user_data["wallet_mins"] = None
        context.user_data["game_amt"]    = game_amt
        return await prompt_food_menu(update, context)

    # Member — check wallet balance
    try:
        wallet_balance = fetch_wallet_mins(member_id) or 0
    except Exception:
        wallet_balance = 0

    context.user_data["wallet_mins"] = wallet_balance

    # Effective cost in wallet-mins:
    #   single console → play_mins × multiplier
    #   combined       → pre-computed sum of (mins_i × mult_i) per session
    effective_cost_mins = pre_effective_mins if pre_effective_mins > 0 \
                          else round(total_mins * multiplier)
    context.user_data["effective_cost_mins"] = effective_cost_mins

    if wallet_balance >= effective_cost_mins:
        # Sufficient — wallet covers it fully
        context.user_data["game_amt"] = 0
        return await prompt_food_menu(update, context)

    # Insufficient — compute shortfall and show choice screen
    shortfall_wallet_mins = effective_cost_mins - wallet_balance
    shortfall_ks          = round(shortfall_wallet_mins * base_rate / 60)
    context.user_data["shortfall_mins"] = shortfall_wallet_mins
    context.user_data["shortfall_ks"]   = shortfall_ks
    return await prompt_session_shortfall(update, context)


async def prompt_session_shortfall(update, context):
    """Show the insufficient-balance screen with Top Up / Cash Down / Skip options."""
    d              = context.user_data
    m_id           = d.get("m_id", "-")
    wallet_balance = d.get("wallet_mins", 0)
    eff_cost       = d.get("effective_cost_mins", 0)
    shortfall_mins = d.get("shortfall_mins", 0)
    shortfall_ks   = d.get("shortfall_ks", 0)
    actual_mins    = d.get("actual_play_mins", d.get("mins", 0))
    base_rate      = d.get("base_rate", 0)
    multiplier     = d.get("multiplier", 1.0)

    kb = [
        [BTN_TOPUP_SESSION],
        [BTN_CASH_DOWN],
        [BTN_SKIP_SALES],
    ]
    await update.message.reply_text(
        f"⚠️ <b>Wallet မလောက်ပါ!</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💳 Member      : <b>{m_id}</b>\n"
        f"🎮 Play Time   : <b>{actual_mins} mins</b>\n"
        f"⚙️ Multiplier  : <b>×{multiplier:g}</b>\n"
        f"🔢 Cost (wallet): <b>{eff_cost} mins</b>\n"
        f"⏳ Balance     : <b>{wallet_balance} mins</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"❗ Shortfall   : <b>{shortfall_mins} mins ≈ {shortfall_ks:,} Ks</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💳 Top Up — mins ဖြည့်ပြီး ဆက်\n"
        f"💵 Cash Down — shortfall ကို cash ပေး\n"
        f"⏭ Skip — Sales မမှတ်တမ်း",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SESSION_SHORTFALL


async def step_session_shortfall(update, context):
    """Handle the Top Up / Cash Down / Skip choice after insufficient balance."""
    text = update.message.text.strip()

    if text == BTN_SKIP_SALES or text == BTN_CANCEL:
        context.user_data.clear()
        return await show_console_menu(update, context)

    if text == BTN_CASH_DOWN:
        d          = context.user_data
        wallet_bal = d.get("wallet_mins", 0)
        multiplier = d.get("multiplier", 1.0)
        base_rate  = d.get("base_rate", fetch_base_rate())
        shortfall_ks = d.get("shortfall_ks", 0)

        # Record only the wallet-covered portion in Sales_Daily col E
        wallet_play_mins = int(wallet_bal / multiplier) if multiplier > 0 else wallet_bal
        d["mins"]          = wallet_play_mins   # col E — wallet fully depleted
        d["game_amt"]      = shortfall_ks       # cash for extra time
        d["cash_down_ks"]  = shortfall_ks
        d["remaining_mins"] = 0
        return await prompt_food_menu(update, context)

    if text == BTN_TOPUP_SESSION:
        d = context.user_data
        # Snapshot session data so it survives user_data operations during TU flow
        snap_keys = [
            "m_id", "c_id", "mins", "actual_play_mins", "base_rate", "multiplier",
            "v_no", "food_items", "food_prices", "food_stock_map", "staff",
            "from_session", "wallet_mins", "effective_cost_mins",
            "shortfall_mins", "shortfall_ks",
        ]
        d["_session_snap"] = {k: d[k] for k in snap_keys if k in d}
        d["after_topup"]   = "console_sale"
        # Pre-fill tu_id so Top Up member step is skipped
        tu_id = d["m_id"]
        d["tu_id"] = tu_id
        # Load member data for Top Up flow
        try:
            tu_data = fetch_member_data(tu_id)
            master_thresh, immortal_thresh = fetch_rank_thresholds()
            bonus_table = fetch_bonus_table()
            d["tu_rank"]            = tu_data["rank_raw"]
            d["tu_total_spend"]     = tu_data["net_spend"]
            d["tu_phone"]           = tu_data["phone"]
            d["tu_name"]            = tu_data["name"]
            d["tu_wallet_mins"]     = tu_data["wallet_mins"]
            d["tu_master_thresh"]   = master_thresh
            d["tu_immortal_thresh"] = immortal_thresh
            d["tu_bonus_table"]     = bonus_table
        except Exception as e:
            await update.message.reply_text(f"❌ Member data ဖတ်မရပါ: {e}")
            return await prompt_session_shortfall(update, context)
        return await prompt_tu_amt(update, context)

    # Unrecognised input — re-show screen
    return await prompt_session_shortfall(update, context)


# ─── Game Library ──────────────────────────────────────────────────────────────

async def show_game_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [BTN_VIEW_GAMES,      BTN_ADD_GAME],
        [BTN_CONSOLE_INSTALL, BTN_DEL_GAME],
        [BTN_SSD_MANAGE,      BTN_DISC_RECORD],
        [BTN_BACK_MAIN],
    ]
    games = fetch_games()
    count = len(games)
    await update.message.reply_text(
        f"🎮 *Game Library* ({count} ဂိမ်း)\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Action ရွေးပါ ↓",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return GAME_MENU


async def step_game_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice in (BTN_BACK, BTN_BACK_MAIN):
        return await show_main_menu(update, context)
    if choice == BTN_VIEW_GAMES:
        games       = fetch_games()
        cgames      = fetch_console_games()  # Console_Games sheet records
        if not games:
            await update.message.reply_text("ℹ️ Game Library ဗလာ ဖြစ်နေသည်\nဂိမ်းထည့်ပါ")
        else:
            # Build a map: game_title_lower → [console_ids]
            install_map: dict[str, list[str]] = {}
            for r in cgames:
                gt = r.get("game_title", "").strip()
                cid = r.get("console_id", "").strip()
                if gt and cid:
                    install_map.setdefault(gt.lower(), []).append(cid)

            lines = [f"🎮 <b>Game Library</b> ({len(games)} ဂိမ်း)\n━━━━━━━━━━━━━━━━━━"]
            for i, g in enumerate(games, 1):
                name  = (g.get("platform", "") or g.get("title", "")).strip()
                discs = g.get("players", "").strip()   # col D = disc count
                if not name:
                    continue
                # Installed consoles
                cons_list = install_map.get(name.lower(), [])
                discs = g.get("discs", "").strip()
                discs_str   = f"  💿 <b>{discs}pc</b>" if discs and discs not in ("", "0") else ""
                install_str = f"  🖥️ {', '.join(cons_list)}" if cons_list else "  🖥️ <i>Not installed</i>"
                lines.append(f"{i}. <b>{name}</b>{discs_str}\n   {install_str}")
            chunk = ""
            for ln in lines:
                if len(chunk) + len(ln) + 2 > 3800:
                    await update.message.reply_text(chunk, parse_mode="HTML")
                    chunk = ln
                else:
                    chunk = chunk + "\n" + ln if chunk else ln
            if chunk:
                await update.message.reply_text(chunk, parse_mode="HTML")
        return await show_game_menu(update, context)
    if choice == BTN_ADD_GAME:
        context.user_data.pop("new_game", None)
        context.user_data["new_game"] = {}
        await update.message.reply_text(
            "➕ *ဂိမ်းအသစ် ထည့်*\n━━━━━━━━━━━━━━━━━━\n"
            "🎮 ဂိမ်းနာမည် ရိုက်ပါ:",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True),
        )
        return GAME_ADD_TITLE
    if choice == BTN_CONSOLE_INSTALL:
        return await show_ginst_menu(update, context)
    if choice == BTN_SSD_MANAGE:
        return await show_ssd_menu(update, context)
    if choice == BTN_DISC_RECORD:
        games = fetch_games()
        if not games:
            await update.message.reply_text("ℹ️ Game Library ဗလာ ဖြစ်နေသည်")
            return await show_game_menu(update, context)
        context.user_data["disc_games"] = games
        # Build label→game mapping for step_disc_select lookup
        disc_map = {}
        kb_rows  = []
        for g in games:
            d   = g.get("discs", "").strip()
            lbl = f"{g['title']}  💿{d}pc" if d and d != "0" else f"{g['title']}  💿--"
            disc_map[lbl] = g
            kb_rows.append([lbl])
        kb_rows.append([BTN_BACK])
        context.user_data["disc_map"] = disc_map
        await update.message.reply_text(
            "💿 <b>Game Discs Record</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "ပြင်မည့် ဂိမ်း ရွေးပါ:",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(kb_rows, resize_keyboard=True),
        )
        return DISC_SELECT
    if choice == BTN_DEL_GAME:
        games = fetch_games()
        if not games:
            await update.message.reply_text("ℹ️ ဖျက်ရန် ဂိမ်းမရှိပါ")
            return await show_game_menu(update, context)
        kb = [[f"{i}. {g['platform']}" ] for i, g in enumerate(games, 1)]
        kb.append([BTN_BACK])
        context.user_data["del_games"] = games
        await update.message.reply_text(
            "🗑️ *ဂိမ်းဖျက်မည်*\n━━━━━━━━━━━━━━━━━━\n"
            "ဖျက်မည့် ဂိမ်းကို ရွေးပါ:",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
        )
        return GAME_DEL_SELECT
    return await show_game_menu(update, context)


async def step_game_add_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await show_game_menu(update, context)
    context.user_data["new_game"]["title"] = text
    kb = [["PS4", "PS5"], ["VR", "PC"], [BTN_CANCEL]]
    await update.message.reply_text(
        f"🎮 <b>{text}</b>\n\n"
        "📱 Platform ရွေးပါ:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
    )
    return GAME_ADD_PLATFORM


async def step_game_add_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await show_game_menu(update, context)
    context.user_data["new_game"]["platform"] = text
    kb = [["Action", "Sports"], ["Racing", "Fighting"],
          ["Adventure", "RPG"], ["Other", BTN_CANCEL]]
    await update.message.reply_text(
        "🎯 Genre ရွေးပါ (သို့) ရိုက်ထည့်ပါ:",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
    )
    return GAME_ADD_GENRE


async def step_game_add_genre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await show_game_menu(update, context)
    context.user_data["new_game"]["genre"] = text
    kb = [["Available", "New"], ["Popular", "Unavailable"], [BTN_CANCEL]]
    await update.message.reply_text(
        "📊 Status ရွေးပါ:",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
    )
    return GAME_ADD_STATUS


async def step_game_add_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await show_game_menu(update, context)
    g = context.user_data.get("new_game", {})
    g["status"] = text
    try:
        sh = get_game_lib_sh()
        sh.append_row(
            [g.get("title",""), g.get("platform",""), g.get("genre",""),
             "1-2", g.get("status",""), ""],
            value_input_option="USER_ENTERED",
        )
        await update.message.reply_text(
            f"✅ <b>ဂိမ်းထည့်ပြီ!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎮 Title    : <b>{g.get('title','')}</b>\n"
            f"📱 Platform : <b>{g.get('platform','')}</b>\n"
            f"🎯 Genre    : <b>{g.get('genre','')}</b>\n"
            f"📊 Status   : <b>{text}</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Save မအောင်မြင်ပါ: {e}")
    return await show_game_menu(update, context)


async def step_game_del_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_BACK:
        return await show_game_menu(update, context)
    games = context.user_data.get("del_games", [])
    target = None
    for i, g in enumerate(games, 1):
        if text.startswith(f"{i}."):
            target = g
            break
    if not target:
        await update.message.reply_text("⚠️ Keyboard မှ ရွေးပေးပါ")
        return GAME_DEL_SELECT
    game_name = target.get("platform", target.get("title", ""))
    try:
        sh = get_game_lib_sh()
        sh.delete_rows(target["row"])
        await update.message.reply_text(
            f"🗑️ <b>\"{game_name}\"</b> ဂိမ်း ဖျက်ပြီ",
            parse_mode="HTML",
        )
    except Exception as e:
        err_str = str(e)
        if "protected" in err_str.lower() or "400" in err_str:
            await update.message.reply_text(
                f"⚠️ <b>Game Library sheet protected</b> ဖြစ်သောကြောင့်\n"
                f"bot မှ row ဖျက်ခြင်း မပြုနိုင်ပါ\n\n"
                f"📋 Google Sheet ကို directly ဝင်ပြီး\n"
                f"<b>\"{game_name}\"</b> row ကို ဖျက်ပေးပါ",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(f"❌ ဖျက်မရပါ: {err_str}")
    return await show_game_menu(update, context)


# ─── Console-Game Install (GINST) flows ─────────────────────────────────────

async def show_ginst_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    records = fetch_console_games()
    count   = len(records)
    kb = [
        [BTN_GINST_VIEW],
        [BTN_GINST_ADD, BTN_GINST_REMOVE],
        [BTN_BACK],
    ]
    await update.message.reply_text(
        f"🖥️ *Console Install* ({count} မှတ်တမ်း)\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📋 ကြည့် — ဘယ် console မှာ ဘာ ရှိသလဲ\n"
        "➕ ထည့် — Game install မှတ်သား\n"
        "❌ ဖျက် — Install မှတ်တမ်း ဖျက်",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return GINST_MENU


async def step_ginst_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in (BTN_BACK, BTN_CANCEL):
        return await show_game_menu(update, context)
    if text == BTN_GINST_VIEW:
        return await _ginst_pick_console(update, context, next_state=GINST_VIEW_CONS, prompt="👁 ကြည့်မည့် Console ရွေးပါ:")
    if text == BTN_GINST_ADD:
        return await _ginst_pick_console(update, context, next_state=GINST_ADD_CONS, prompt="➕ Game ထည့်မည့် Console ရွေးပါ:")
    if text == BTN_GINST_REMOVE:
        return await _ginst_pick_console(update, context, next_state=GINST_DEL_CONS, prompt="❌ ဖျက်မည့် Console ရွေးပါ:")
    return await show_ginst_menu(update, context)


async def _ginst_pick_console(update, context, next_state, prompt):
    """Show console selection keyboard for a GINST operation."""
    cons = get_consoles_from_setting()
    if not cons:
        await update.message.reply_text("⚠️ Console မရှိသေးပါ\nConsole CRUD မှ ထည့်ပါ")
        return await show_ginst_menu(update, context)
    kb = [[c["id"]] for c in cons]
    kb.append([BTN_BACK])
    await update.message.reply_text(
        prompt,
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return next_state


# ─── GINST VIEW ──

async def step_ginst_view_cons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in (BTN_BACK, BTN_CANCEL):
        return await show_ginst_menu(update, context)
    console_id = text
    games = get_games_on_console(console_id)
    records = [r for r in fetch_console_games()
               if r["console_id"].upper() == console_id.upper()]
    if not records:
        await update.message.reply_text(
            f"ℹ️ <b>{console_id}</b> မှာ Install မှတ်တမ်း မရှိသေးပါ",
            parse_mode="HTML",
        )
    else:
        lines = [f"🖥️ <b>{console_id}</b> — Install ({len(records)} ဂိမ်း)\n━━━━━━━━━━━━━━━━━━"]
        for i, r in enumerate(records, 1):
            icon = "💾" if "HDD" in r["install_type"] else ("💿" if "Disc" in r["install_type"] else "🔌")
            lines.append(f"{i}. {icon} <b>{r['game_title']}</b>  <i>{r['install_type']}</i>  <code>{r['date']}</code>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    return await show_ginst_menu(update, context)


# ─── GINST ADD ──

async def step_ginst_add_cons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in (BTN_BACK, BTN_CANCEL):
        return await show_ginst_menu(update, context)
    context.user_data["ginst_console_id"] = text
    games = fetch_games()
    if not games:
        await update.message.reply_text(
            "⚠️ Game Library ဗလာ ဖြစ်နေသည်\nGame Library မှ ဂိမ်းထည့်ပါ"
        )
        return await show_ginst_menu(update, context)
    kb = []
    row = []
    for g in games:
        row.append(g["title"])
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([BTN_BACK])
    await update.message.reply_text(
        f"🖥️ <b>{text}</b>\n\n🎮 Install မည့် ဂိမ်းကို ရွေးပါ:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return GINST_ADD_GAME


async def step_ginst_add_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in (BTN_BACK, BTN_CANCEL):
        return await show_ginst_menu(update, context)
    cid   = context.user_data.get("ginst_console_id", "")
    title = text
    install_type = "HDD"

    # ── Duplicate check ───────────────────────────────────────────────────────
    existing = await asyncio.to_thread(fetch_console_games)
    already  = any(
        r["console_id"].strip().upper() == cid.upper()
        and r["game_title"].strip().lower() == title.strip().lower()
        for r in existing
    )
    if already:
        await update.message.reply_text(
            f"⚠️ <b>\"{title}\"</b> သည် <b>{cid}</b> မှာ ရှိပြီးသားပါ",
            parse_mode="HTML",
        )
        return await show_ginst_menu(update, context)

    ok, gl_ok = await asyncio.gather(
        asyncio.to_thread(add_console_game, cid, title, install_type),
        asyncio.to_thread(update_game_library_install, title, cid, True),
    )
    if ok:
        gl_note = "  📊 Game Library ✅" if gl_ok else "  📊 Game Library ⚠️ (manual update လို)"
        await update.message.reply_text(
            f"✅ <b>Install မှတ်သားပြီ!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🖥️ Console : <b>{cid}</b>\n"
            f"🎮 Game    : <b>{title}</b>\n"
            f"💾 Type    : <b>{install_type}</b>\n"
            f"{gl_note}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"❌ မှတ်သားမရပါ — ထပ်ကြိုးစားပါ\n"
            f"(Console: {cid} | Game: {title})",
        )
    return await show_ginst_menu(update, context)


async def step_ginst_add_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in (BTN_BACK, BTN_CANCEL):
        return await show_ginst_menu(update, context)
    type_map = {BTN_GINST_HDD: "HDD", BTN_GINST_DISC: "Disc", BTN_GINST_SSD: "Portable SSD"}
    if text not in type_map:
        await update.message.reply_text("⚠️ Keyboard မှ ရွေးပေးပါ")
        return GINST_ADD_TYPE
    install_type = type_map[text]
    cid   = context.user_data.get("ginst_console_id", "")
    title = context.user_data.get("ginst_game_title", "")
    # Save to Console_Games sheet + sync Game_Library checkbox
    ok, gl_ok = await asyncio.gather(
        asyncio.to_thread(add_console_game, cid, title, install_type),
        asyncio.to_thread(update_game_library_install, title, cid, True),
    )
    if ok:
        icon = "💾" if install_type == "HDD" else ("💿" if install_type == "Disc" else "🔌")
        gl_note = "  📊 Game Library ✅" if gl_ok else "  📊 Game Library ⚠️ (manual update လို)"
        await update.message.reply_text(
            f"✅ <b>Install မှတ်သားပြီ!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🖥️ Console : <b>{cid}</b>\n"
            f"🎮 Game    : <b>{title}</b>\n"
            f"{icon} Type   : <b>{install_type}</b>\n"
            f"{gl_note}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("❌ Save မအောင်မြင်ပါ — ထပ်ကြိုးစားပါ")
    return await show_ginst_menu(update, context)


# ─── GINST DELETE ──

async def step_ginst_del_cons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in (BTN_BACK, BTN_CANCEL):
        return await show_ginst_menu(update, context)
    console_id = text
    records = [r for r in fetch_console_games()
               if r["console_id"].upper() == console_id.upper()]
    if not records:
        await update.message.reply_text(
            f"ℹ️ <b>{console_id}</b> မှာ Install မှတ်တမ်း မရှိသေးပါ",
            parse_mode="HTML",
        )
        return await show_ginst_menu(update, context)
    context.user_data["ginst_console_id"]  = console_id
    context.user_data["ginst_del_records"] = records
    kb = [[f"{i}. {r['game_title']}"] for i, r in enumerate(records, 1)]
    kb.append([BTN_BACK])
    await update.message.reply_text(
        f"❌ <b>{console_id}</b> — ဖျက်မည့် ဂိမ်းရွေးပါ:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return GINST_DEL_GAME


async def step_ginst_del_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in (BTN_BACK, BTN_CANCEL):
        return await show_ginst_menu(update, context)
    records = context.user_data.get("ginst_del_records", [])
    cid     = context.user_data.get("ginst_console_id", "")
    target  = None
    for i, r in enumerate(records, 1):
        if text.startswith(f"{i}."):
            target = r
            break
    if not target:
        await update.message.reply_text("⚠️ Keyboard မှ ရွေးပေးပါ")
        return GINST_DEL_GAME
    # Remove from Console_Games + clear Game_Library checkbox
    ok, gl_ok = await asyncio.gather(
        asyncio.to_thread(remove_console_game, cid, target["game_title"]),
        asyncio.to_thread(update_game_library_install, target["game_title"], cid, False),
    )
    if ok:
        gl_note = "  📊 Game Library ✅" if gl_ok else "  📊 Game Library ⚠️ (manual update လို)"
        await update.message.reply_text(
            f"🗑️ <b>{cid}</b> မှ <b>\"{target['game_title']}\"</b> ဖျက်ပြီ\n{gl_note}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("❌ ဖျက်မရပါ — ထပ်ကြိုးစားပါ")
    return await show_ginst_menu(update, context)


# ═════════════════════════════════════════
#  EXTERNAL SSD MANAGEMENT
# ═════════════════════════════════════════

def _ssd_kb():
    """Keyboard with all 3 SSD names + Back."""
    return ReplyKeyboardMarkup(
        [[BTN_SSD_T1], [BTN_SSD_BLUE], [BTN_SSD_GREY], [BTN_BACK]],
        resize_keyboard=True,
    )


# ═════════════════════════════════════════
#  GAME DISCS RECORD
# ═════════════════════════════════════════

async def step_disc_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked a game from the disc record list (button label includes disc count)."""
    text  = update.message.text.strip()
    if text == BTN_BACK:
        return await show_game_menu(update, context)
    # Buttons are labelled "Title  💿Npc" or "Title  💿--"
    disc_map = context.user_data.get("disc_map", {})
    target   = disc_map.get(text)
    if not target:
        await update.message.reply_text("⚠️ ဂိမ်း မတွေ့ပါ — ထပ်ရွေးပါ")
        return DISC_SELECT
    context.user_data["disc_target"] = target
    current = target.get("discs", "").strip()
    cur_str = f"{current}pc" if current and current != "0" else "မမှတ်ထားရသေး"
    await update.message.reply_text(
        f"💿 <b>{target['title']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"လက်ရှိ disc: <b>{cur_str}</b>\n\n"
        f"ခွေဘယ်နှ့ ရှိသည် ရိုက်ထည့်ပါ (ဂဏန်းသာ):",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True),
    )
    return DISC_SET_QTY


async def step_disc_set_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User entered the new disc count — save to sheet."""
    text = update.message.text.strip()
    if text == BTN_BACK:
        return await show_game_menu(update, context)
    if not text.isdigit():
        await update.message.reply_text("⚠️ ဂဏန်းသာ ရိုက်ပါ (ဥပမာ: 2)")
        return DISC_SET_QTY
    count  = int(text)
    target = context.user_data.get("disc_target", {})
    row    = target.get("row", 0)
    title  = target.get("title", "?")
    if not row:
        await update.message.reply_text("❌ Error — ထပ်ကြိုးစားပါ")
        return await show_game_menu(update, context)
    ok = await asyncio.to_thread(set_game_disc_count, row, count)
    if ok:
        await update.message.reply_text(
            f"✅ <b>{title}</b>\n"
            f"💿 Disc count: <b>{count}pc</b> မှတ်သားပြီ",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("❌ Sheet ထဲ save မရပါ — ထပ်ကြိုးစားပါ")
    return await show_game_menu(update, context)


async def show_ssd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """SSD Management hub."""
    # Count games per SSD
    cgames = fetch_console_games()
    counts = {sid: sum(1 for r in cgames if r["console_id"] == sid) for sid in SSD_NAMES}
    count_str = "  |  ".join(f"{SSD_NAMES[s]}: {counts[s]}ဂိမ်း" for s in SSD_NAMES)
    kb = [
        [BTN_SSD_VIEW],
        [BTN_SSD_ADD,     BTN_SSD_REMOVE],
        [BTN_SSD_TRANSFER, BTN_SSD_RETURN],
        [BTN_BACK],
    ]
    await update.message.reply_text(
        f"📀 *External SSD Management*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{count_str}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 ကြည့် — SSD ထဲ ဘာ ရှိသလဲ\n"
        f"➕ ထည့် — SSD ထဲ ဂိမ်း မှတ်သား\n"
        f"❌ ဖျက် — SSD မှ ဂိမ်း ဖျက်\n"
        f"🔄 Transfer — SSD → Console (session အတွက်)\n"
        f"↩️ Return   — Console → SSD (session ပြီးပြီ)",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SSD_MENU


async def step_ssd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in (BTN_BACK, BTN_BACK_MAIN):
        return await show_console_menu(update, context)
    if text == BTN_SSD_VIEW:
        await update.message.reply_text("📀 ကြည့်မည့် SSD ရွေးပါ:", reply_markup=_ssd_kb())
        return SSD_VIEW_SSD
    if text == BTN_SSD_ADD:
        await update.message.reply_text("➕ ဂိမ်း ထည့်မည့် SSD ရွေးပါ:", reply_markup=_ssd_kb())
        return SSD_ADD_SSD
    if text == BTN_SSD_REMOVE:
        await update.message.reply_text("❌ ဂိမ်း ဖျက်မည့် SSD ရွေးပါ:", reply_markup=_ssd_kb())
        return SSD_DEL_SSD
    if text == BTN_SSD_TRANSFER:
        await update.message.reply_text(
            "🔄 *SSD → Console Transfer*\n\nSSD ကို ရွေးပါ:",
            parse_mode="Markdown",
            reply_markup=_ssd_kb(),
        )
        return SSD_XFER_SSD
    if text == BTN_SSD_RETURN:
        await update.message.reply_text(
            "↩️ *Console → SSD Return*\n\nConsole ရွေးပါ:",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(
                [[c["id"]] for c in get_consoles_from_setting()] + [[BTN_BACK]],
                resize_keyboard=True,
            ),
        )
        return SSD_RET_CONS
    return await show_ssd_menu(update, context)


# ── SSD View ──────────────────────────────────────────────────────────────────

async def step_ssd_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all games stored on the chosen SSD."""
    text = update.message.text.strip()
    if text == BTN_BACK:
        return await show_ssd_menu(update, context)
    ssd_id = SSD_BTN_TO_ID.get(text)
    if not ssd_id:
        await update.message.reply_text("⚠️ မှန်သော SSD ရွေးပါ:", reply_markup=_ssd_kb())
        return SSD_VIEW_SSD
    rows = [r for r in fetch_console_games() if r["console_id"] == ssd_id]
    if not rows:
        await update.message.reply_text(
            f"📀 <b>{SSD_NAMES[ssd_id]}</b> — ဂိမ်း မရှိသေးပါ",
            parse_mode="HTML",
        )
    else:
        lines = [f"📀 <b>{SSD_NAMES[ssd_id]}</b> ({len(rows)} ဂိမ်း)\n━━━━━━━━━━━━━━━━━━"]
        for i, r in enumerate(rows, 1):
            install_t = r.get("install_type", "")
            note_str  = f"  [{r['notes']}]" if r.get("notes") else ""
            lines.append(f"{i}. 🎮 {r['game_title']}  ({install_t}){note_str}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    return await show_ssd_menu(update, context)


# ── SSD Add Game ───────────────────────────────────────────────────────────────

async def step_ssd_add_ssd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User chose which SSD to add game to."""
    text = update.message.text.strip()
    if text == BTN_BACK:
        return await show_ssd_menu(update, context)
    ssd_id = SSD_BTN_TO_ID.get(text)
    if not ssd_id:
        await update.message.reply_text("⚠️ မှန်သော SSD ရွေးပါ:", reply_markup=_ssd_kb())
        return SSD_ADD_SSD
    context.user_data["ssd_target"] = ssd_id
    # Show Game Library as options
    games = fetch_game_library()
    titles = [g["title"] for g in games if g.get("title")]
    kb_rows = [[t] for t in titles] + [[BTN_BACK]]
    await update.message.reply_text(
        f"📀 <b>{SSD_NAMES[ssd_id]}</b> ထဲ ထည့်မည့် ဂိမ်း ရွေးပါ:\n"
        f"(Library မှ ရွေးပါ သို့ ဂိမ်းနာမည် ရိုက်ထည့်)",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb_rows, resize_keyboard=True),
    )
    return SSD_ADD_GAME


async def step_ssd_add_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User typed/chose the game name to add to SSD — save directly as SSD Copy."""
    text = update.message.text.strip()
    if text == BTN_BACK:
        return await show_ssd_menu(update, context)
    ssd_id    = context.user_data.get("ssd_target", "")
    game      = text
    inst_type = "SSD Copy"

    # ── Duplicate check ───────────────────────────────────────────────────────
    existing = await asyncio.to_thread(fetch_console_games)
    already  = any(
        r["console_id"].strip().upper() == ssd_id.upper()
        and r["game_title"].strip().lower() == game.strip().lower()
        for r in existing
    )
    if already:
        await update.message.reply_text(
            f"⚠️ <b>\"{game}\"</b> သည် <b>{SSD_NAMES.get(ssd_id, ssd_id)}</b> မှာ ရှိပြီးသားပါ",
            parse_mode="HTML",
        )
        return await show_ssd_menu(update, context)

    ok = await asyncio.to_thread(write_console_game, ssd_id, game, inst_type)
    if ok:
        await update.message.reply_text(
            f"✅ <b>{SSD_NAMES.get(ssd_id, ssd_id)}</b> ထဲ <b>\"{game}\"</b> ထည့်ပြီ",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("❌ မှတ်သားမရပါ — ထပ်ကြိုးစားပါ")
    return await show_ssd_menu(update, context)


async def step_ssd_add_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store selected install type for SSD game."""
    text = update.message.text.strip()
    if text == BTN_BACK:
        return await show_ssd_menu(update, context)
    valid = {BTN_GINST_HDD: "HDD", BTN_GINST_DISC: "Disc", BTN_GINST_SSD: "SSD Copy"}
    if text not in valid:
        await update.message.reply_text("⚠️ မှန်သော type ရွေးပါ")
        return SSD_ADD_TYPE
    ssd_id   = context.user_data.get("ssd_target", "")
    game     = context.user_data.get("ssd_game", "")
    inst_type = valid[text]
    ok = write_console_game(ssd_id, game, inst_type, "")
    if ok:
        await update.message.reply_text(
            f"✅ <b>{SSD_NAMES.get(ssd_id, ssd_id)}</b> ထဲ <b>\"{game}\"</b> ထည့်ပြီ",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("❌ မှတ်သားမရပါ — ထပ်ကြိုးစားပါ")
    return await show_ssd_menu(update, context)


# ── SSD Remove Game ────────────────────────────────────────────────────────────

async def step_ssd_del_ssd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User chose which SSD to remove a game from."""
    text = update.message.text.strip()
    if text == BTN_BACK:
        return await show_ssd_menu(update, context)
    ssd_id = SSD_BTN_TO_ID.get(text)
    if not ssd_id:
        await update.message.reply_text("⚠️ မှန်သော SSD ရွေးပါ:", reply_markup=_ssd_kb())
        return SSD_DEL_SSD
    rows = [r for r in fetch_console_games() if r["console_id"] == ssd_id]
    if not rows:
        await update.message.reply_text(
            f"📀 <b>{SSD_NAMES[ssd_id]}</b> — ဂိမ်း မရှိသေးပါ",
            parse_mode="HTML",
        )
        return await show_ssd_menu(update, context)
    context.user_data["ssd_target"] = ssd_id
    titles = [r["game_title"] for r in rows]
    kb_rows = [[t] for t in titles] + [[BTN_BACK]]
    await update.message.reply_text(
        f"📀 <b>{SSD_NAMES[ssd_id]}</b> မှ ဖျက်မည့် ဂိမ်း ရွေးပါ:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb_rows, resize_keyboard=True),
    )
    return SSD_DEL_GAME


async def step_ssd_del_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete chosen game from SSD."""
    text = update.message.text.strip()
    ssd_id = context.user_data.get("ssd_target", "")
    if text == BTN_BACK:
        return await show_ssd_menu(update, context)
    rows = [r for r in fetch_console_games() if r["console_id"] == ssd_id]
    target = next((r for r in rows if r["game_title"] == text), None)
    if not target:
        await update.message.reply_text("⚠️ ဂိမ်း မတွေ့ပါ — ထပ်ရွေးပါ")
        return SSD_DEL_GAME
    ok = delete_console_game(ssd_id, text)
    if ok:
        await update.message.reply_text(
            f"🗑️ <b>{SSD_NAMES.get(ssd_id, ssd_id)}</b> မှ <b>\"{text}\"</b> ဖျက်ပြီ",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("❌ ဖျက်မရပါ — ထပ်ကြိုးစားပါ")
    return await show_ssd_menu(update, context)


# ── SSD → Console Transfer ─────────────────────────────────────────────────────

async def step_ssd_xfer_ssd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User chose source SSD for transfer."""
    text = update.message.text.strip()
    if text == BTN_BACK:
        return await show_ssd_menu(update, context)
    ssd_id = SSD_BTN_TO_ID.get(text)
    if not ssd_id:
        await update.message.reply_text("⚠️ မှန်သော SSD ရွေးပါ:", reply_markup=_ssd_kb())
        return SSD_XFER_SSD
    rows = [r for r in fetch_console_games() if r["console_id"] == ssd_id]
    if not rows:
        await update.message.reply_text(
            f"📀 <b>{SSD_NAMES[ssd_id]}</b> — ဂိမ်း မရှိသေးပါ",
            parse_mode="HTML",
        )
        return await show_ssd_menu(update, context)
    context.user_data["ssd_xfer_src"] = ssd_id
    titles = [r["game_title"] for r in rows]
    kb_rows = [[t] for t in titles] + [[BTN_BACK]]
    await update.message.reply_text(
        f"🔄 <b>{SSD_NAMES[ssd_id]}</b> မှ Transfer မည့် ဂိမ်း ရွေးပါ:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb_rows, resize_keyboard=True),
    )
    return SSD_XFER_GAME


async def step_ssd_xfer_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User chose game to transfer."""
    text = update.message.text.strip()
    if text == BTN_BACK:
        # If came from session start, go back to game prompt
        if context.user_data.pop("ssd_return_to_session", False):
            context.user_data.pop("ssd_xfer_target_cons", None)
            return await prompt_book_game(update, context)
        return await show_ssd_menu(update, context)
    ssd_id = context.user_data.get("ssd_xfer_src", "")
    rows = [r for r in fetch_console_games() if r["console_id"] == ssd_id]
    target = next((r for r in rows if r["game_title"] == text), None)
    if not target:
        await update.message.reply_text("⚠️ ဂိမ်း မတွေ့ပါ — ထပ်ရွေးပါ")
        return SSD_XFER_GAME
    context.user_data["ssd_xfer_game"] = text

    # ── Session-start shortcut: console already known, skip console picker ──
    if context.user_data.get("ssd_return_to_session"):
        target_cid = context.user_data.get("ssd_xfer_target_cons", "")
        src_lbl    = SSD_NAMES.get(ssd_id, ssd_id)
        existing   = await asyncio.to_thread(fetch_console_games)
        already    = any(
            r["console_id"].strip().upper() == target_cid.upper()
            and r["game_title"].strip().lower() == text.strip().lower()
            for r in existing
        )
        if already:
            await update.message.reply_text(
                f"⚠️ <b>\"{text}\"</b> သည် <b>{target_cid}</b> မှာ ရှိပြီးသားပါ\n"
                f"ထပ် transfer မလိုပါ",
                parse_mode="HTML",
            )
        else:
            ok = write_console_game(target_cid, text, "SSD Transfer", f"From {src_lbl}")
            if ok:
                await asyncio.to_thread(remove_console_game, ssd_id, text)
                await update.message.reply_text(
                    f"✅ <b>\"{text}\"</b> Transfer ပြီးပါပြီ\n"
                    f"📀 {src_lbl} → 🕹️ <b>{target_cid}</b>\n"
                    f"(session ပြီးရင် ↩️ Return ဖြင့် SSD ပြန်ထည့်ပါ)",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text("❌ Transfer မှတ်မရပါ — ထပ်ကြိုးစားပါ")
        context.user_data.pop("ssd_return_to_session", None)
        context.user_data.pop("ssd_xfer_target_cons", None)
        return await prompt_book_game(update, context)
    # ── Normal flow: ask which console ──────────────────────────────────────
    consoles = get_consoles_from_setting()
    kb_rows = [[c["id"]] for c in consoles] + [[BTN_BACK]]
    await update.message.reply_text(
        f"🔄 <b>\"{text}\"</b> ကို ဘယ် Console ထဲ ထည့်မည်နည်း?",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb_rows, resize_keyboard=True),
    )
    return SSD_XFER_CONS


async def step_ssd_xfer_cons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record transfer: add game to console with 'SSD Transfer' type, with duplicate check."""
    text = update.message.text.strip()
    if text == BTN_BACK:
        return await show_ssd_menu(update, context)
    ssd_id  = context.user_data.get("ssd_xfer_src", "")
    game    = context.user_data.get("ssd_xfer_game", "")
    cid     = text
    src_lbl = SSD_NAMES.get(ssd_id, ssd_id)

    # ── Duplicate check: game already on target console? ──────────────────────
    existing = await asyncio.to_thread(fetch_console_games)
    already  = any(
        r["console_id"].strip().upper() == cid.upper()
        and r["game_title"].strip().lower() == game.strip().lower()
        for r in existing
    )
    if already:
        await update.message.reply_text(
            f"⚠️ <b>\"{game}\"</b> သည် <b>{cid}</b> မှာ ရှိပြီးသားပါ\n"
            f"ထပ် transfer မလိုပါ",
            parse_mode="HTML",
        )
        return await show_ssd_menu(update, context)

    ok = write_console_game(cid, game, "SSD Transfer", f"From {src_lbl}")
    if ok:
        # ── Move: remove from SSD after writing to console ────────────────────
        await asyncio.to_thread(remove_console_game, ssd_id, game)
        await update.message.reply_text(
            f"✅ <b>\"{game}\"</b>\n"
            f"📀 {src_lbl} → 🕹️ <b>{cid}</b>\n"
            f"(SSD မှ ဖယ်ရှားပြီ — session ဆုံးသည်နှင့် Return ဖြင့် ပြန်ထည့်ပါ)",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("❌ Transfer မှတ်မရပါ — ထပ်ကြိုးစားပါ")
    return await show_ssd_menu(update, context)


# ── Console → SSD Return ───────────────────────────────────────────────────────

async def step_ssd_ret_cons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User chose console to return SSD games from."""
    text = update.message.text.strip()
    if text == BTN_BACK:
        return await show_ssd_menu(update, context)
    cid  = text
    rows = [
        r for r in fetch_console_games()
        if r["console_id"].upper() == cid.upper()
        and "SSD Transfer" in r.get("install_type", "")
    ]
    if not rows:
        await update.message.reply_text(
            f"🕹️ <b>{cid}</b> — SSD Transfer ဂိမ်း မရှိပါ",
            parse_mode="HTML",
        )
        return await show_ssd_menu(update, context)
    context.user_data["ssd_ret_cons"] = cid
    titles = [r["game_title"] for r in rows]
    kb_rows = [[t] for t in titles] + [[BTN_BACK]]
    await update.message.reply_text(
        f"↩️ <b>{cid}</b> မှ SSD ပြန်ရွေ့မည့် ဂိမ်း ရွေးပါ:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb_rows, resize_keyboard=True),
    )
    return SSD_RET_GAME


async def step_ssd_ret_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove 'SSD Transfer' entry from console (game returned to SSD)."""
    text = update.message.text.strip()
    cid  = context.user_data.get("ssd_ret_cons", "")
    if text == BTN_BACK:
        return await show_ssd_menu(update, context)
    ok = delete_console_game(cid, text)
    if ok:
        await update.message.reply_text(
            f"✅ <b>\"{text}\"</b> — 🕹️ <b>{cid}</b> မှ SSD ပြန်ရွေ့ပြီ ✔️",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("❌ မရပါ — ထပ်ကြိုးစားပါ")
    return await show_ssd_menu(update, context)


# ─── Console CRUD ──────────────────────────────────────────────────────────────

async def show_con_mgmt_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cons  = get_consoles_from_setting()
    count = len(cons)
    kb    = [
        [BTN_LIST_CONSOLE, BTN_ADD_CONSOLE],
        [BTN_DEL_CONSOLE,  BTN_BACK],
    ]
    await update.message.reply_text(
        f"⚙️ *Console စီမံ* ({count} console)\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Action ရွေးပါ ↓",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return CON_MGMT_MENU


async def step_con_mgmt_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == BTN_BACK:
        return await show_console_menu(update, context)
    if choice == BTN_LIST_CONSOLE:
        cons = get_consoles_from_setting()
        if not cons:
            await update.message.reply_text("ℹ️ Console မရှိသေးပါ")
        else:
            lines = ["📋 <b>Console စာရင်း</b>\n━━━━━━━━━━━━━━━━━━"]
            for c in cons:
                mult = f"  ×{c['mult']}" if c.get("mult") else ""
                lines.append(f"🕹️ <b>{c['id']}</b> — {c.get('type','?')}{mult}")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return await show_con_mgmt_menu(update, context)
    if choice == BTN_ADD_CONSOLE:
        context.user_data["new_con"] = {}
        await update.message.reply_text(
            "➕ *Console ထည့်*\n━━━━━━━━━━━━━━━━━━\n"
            "Console ID ရိုက်ပါ\n_(ဥပမာ: C - 11)_",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True),
        )
        return CON_ADD_ID
    if choice == BTN_DEL_CONSOLE:
        cons = get_consoles_from_setting()
        if not cons:
            await update.message.reply_text("ℹ️ ဖျက်ရန် Console မရှိပါ")
            return await show_con_mgmt_menu(update, context)
        context.user_data["del_cons"] = cons
        kb = [[c["id"]] for c in cons] + [[BTN_BACK]]
        await update.message.reply_text(
            "🗑️ *Console ဖျက်မည်*\n━━━━━━━━━━━━━━━━━━\n"
            "ဖျက်မည့် Console ရွေးပါ:",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
        )
        return CON_DEL_SELECT
    return await show_con_mgmt_menu(update, context)


async def step_con_add_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await show_con_mgmt_menu(update, context)
    # Check duplicate
    existing = {c["id"] for c in get_consoles_from_setting()}
    if text in existing:
        await update.message.reply_text(f"⚠️ <b>{text}</b> သည် ရှိပြီး ဖြစ်သည်", parse_mode="HTML")
        return CON_ADD_ID
    context.user_data["new_con"]["id"] = text
    kb = [["PS4", "PS5"], ["VR", BTN_CANCEL]]
    await update.message.reply_text(
        f"🕹️ <b>{text}</b>\n\nType ရွေးပါ:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
    )
    return CON_ADD_TYPE


async def step_con_add_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await show_con_mgmt_menu(update, context)
    context.user_data["new_con"]["type"] = text
    kb = [["1.0", "1.5", "2.0"], [BTN_CANCEL]]
    await update.message.reply_text(
        "⚖️ Rate Multiplier ရွေးပါ\n_(Base rate ပေါ် မည်မျှ မြှောက်သည်)_\n\n"
        "1.0 = Normal · 1.5 = Premium · 2.0 = VR/Pro",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
    )
    return CON_ADD_MULT


async def step_con_add_mult(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await show_con_mgmt_menu(update, context)
    try:
        mult = float(text)
    except ValueError:
        await update.message.reply_text("⚠️ ဂဏန်း ထည့်ပါ (ဥပမာ: 1.0)")
        return CON_ADD_MULT
    nc = context.user_data.get("new_con", {})
    ok = add_console_to_setting(nc.get("id",""), nc.get("type","PS4"), mult)
    if ok:
        # Update runtime VALID_CONSOLES set
        VALID_CONSOLES.add(nc.get("id",""))
        await update.message.reply_text(
            f"✅ <b>Console ထည့်ပြီ!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕹️ ID   : <b>{nc.get('id','')}</b>\n"
            f"📱 Type : <b>{nc.get('type','')}</b>\n"
            f"⚖️ Mult : <b>×{mult}</b>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("❌ Save မအောင်မြင်ပါ — GSheet စစ်ကြည့်ပါ")
    return await show_con_mgmt_menu(update, context)


async def step_con_del_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_BACK:
        return await show_con_mgmt_menu(update, context)
    cons = context.user_data.get("del_cons", [])
    target = next((c for c in cons if c["id"] == text), None)
    if not target:
        await update.message.reply_text("⚠️ Keyboard မှ ရွေးပေးပါ")
        return CON_DEL_SELECT
    ok = remove_console_from_setting(target["id"])
    if ok:
        VALID_CONSOLES.discard(target["id"])
        await update.message.reply_text(
            f"🗑️ <b>{target['id']}</b> ဖျက်ပြီ\n"
            f"<i>(Setting sheet မှ ဖယ်ထားသည်)</i>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(f"❌ ဖျက်မရပါ — GSheet စစ်ကြည့်ပါ")
    return await show_con_mgmt_menu(update, context)


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the running bot version and build date."""
    import platform
    py_ver = platform.python_version()
    built  = now_mmt().strftime("%Y-%m-%d %H:%M MMT")
    await update.message.reply_text(
        f"🤖 *PS Vibe Sales Bot*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📦 Version   : `{BOT_VERSION}`\n"
        f"🐍 Python    : `{py_ver}`\n"
        f"🕐 Server time: `{built}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ *Latest features:*\n"
        f"  • Admin CMD PIN (/payroll /kpi /setattend)\n"
        f"  • Payroll target display in hrs (not mins)\n"
        f"  • Session repeat reminder loop (5-min cycle)\n"
        f"  • Inventory cache bust after stock update\n"
        f"  • Staff breakdown + low-wallet API endpoints\n"
        f"  • Payroll business-wide play\\_mins total\n"
        f"  • Per-member exact effective rate\n"
        f"  • P&L / Cash Flow / Liability reports",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all available commands."""
    await update.message.reply_text(
        "📖 *PS Vibe Bot — Commands*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🏠 *Navigation*\n"
        "/start        — Main Menu ပြမည်\n"
        "/menu         — Main Menu ပြမည်\n"
        "/cancel       — လက်ရှိ action ဖျက်သိမ်း\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎮 *Sales*\n"
        "/sales        — 📝 New Sale _(shortcut)_\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💳 *Members*\n"
        "/member       — Member Management menu\n"
        "/newmember    — 🆕 New Member Register\n"
        "/topup        — 💰 Top Up _(shortcut)_\n"
        "/check        — 🔍 Check Member Info\n"
        "/ranks        — 📋 View Rank Tier Table\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📊 *Reports*\n"
        "/report       — 📊 Today's Report\n"
        "/kpi          — 📈 Staff KPI (ဒီနေ့)\n"
        "/payroll      — 💰 Monthly Payroll\n"
        "/setattend    — 📅 ခွင့်ယူ / နောက်ကျ မှတ်တမ်း\n"
        "/admin        — 🔧 Admin Panel (Stock/Attend/Payroll/KPI)\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📦 *Stock*\n"
        "/stock        — Stock Update menu\n"
        "/stockin      — 📥 Stock In (Restock)\n"
        "/stockout     — 📦 Stock Out\n"
        "/inventory    — 🗂 Inventory Status\n"
        "/stocktoday   — 🛒 Items sold today\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "/help         — ဤ command list",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


# ═════════════════════════════════════════
#  ERROR HANDLER
# ═════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log all Telegram errors without crashing the bot."""
    from telegram.error import Conflict
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logging.warning("Network issue (will auto-retry): %s", err)
    elif isinstance(err, Conflict):
        logging.warning("Conflict: another instance is running — will resolve automatically.")
    else:
        logging.error("Unhandled error: %s", err, exc_info=err)


# ═════════════════════════════════════════
#  STOCK UPDATE FLOW
# ═════════════════════════════════════════

async def step_stock_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify PIN — delete the PIN message then route to dest."""
    entered = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if entered == STOCK_ACCESS_PIN:
        dest = context.user_data.pop("stock_dest", "menu")
        if dest == "stockin":
            return await show_si_items(update, context)
        if dest == "stockout":
            return await show_stock_out_items(update, context)
        return await show_stock_menu(update, context)
    await update.message.reply_text(
        "❌ PIN မမှန်ကန်ပါ။\n\nMain Menu သို့ ပြန်သွားမည်။",
        reply_markup=ReplyKeyboardRemove(),
    )
    return await show_main_menu(update, context)


async def show_stock_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Stock Update sub-menu: Stock Out, Stock In only."""
    kb = [
        [BTN_STOCK_OUT],
        [BTN_STOCK_IN_M],
        [BTN_BACK_MAIN],
    ]
    await update.message.reply_text(
        "📦 *Stock Update*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Action ရွေးပါ ↓",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return STOCK_MENU


async def step_stock_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route from Stock sub-menu."""
    choice = update.message.text.strip()
    if choice == BTN_BACK_MAIN:
        return await show_main_menu(update, context)
    if choice == BTN_STOCK_OUT:
        return await show_stock_out_items(update, context)
    if choice == BTN_STOCK_IN_M:
        return await show_si_items(update, context)
    if choice == BTN_INVENTORY_VIEW:
        await update.message.reply_text("⏳ Inventory စစ်နေသည်...", reply_markup=ReplyKeyboardRemove())
        data = _replit_get("sheets/inventory")
        if not data:
            await update.message.reply_text("❌ Inventory data ရယူ၍ မရပါ။")
            return await show_stock_menu(update, context)
        items = data.get("items", [])
        STATUS_EMOJI = {"In Stock": "🟢", "Low Stock": "🟡", "Out of Stock": "🔴", "No Stock": "⚫"}
        lines = ["📦 *Inventory Status*\n━━━━━━━━━━━━━━━━━━"]
        for item in items:
            em  = STATUS_EMOJI.get(item["status"], "⚫")
            stock_qty = max(0, item.get("current_stock", 0))
            val_str = f"  _{item['inv_value']:,} Ks_" if item.get("inv_value", 0) > 0 else ""
            lines.append(f"{em} *{item['name']}*: {stock_qty} pcs{val_str}")
        total_val = sum(i["inv_value"] for i in items)
        if total_val:
            lines.append(f"\n━━━━━━━━━━━━━━━━━━\n💰 Total: *{total_val:,} Ks*")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return await show_stock_menu(update, context)
    return await show_stock_menu(update, context)


async def show_stock_out_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show food item list for manual stock-out recording."""
    food_prices = fetch_food_prices()
    context.user_data["stock_food_prices"] = food_prices
    names = list(food_prices.keys())
    rows  = [names[i: i + 2] for i in range(0, len(names), 2)]
    kb    = rows + [[BTN_BACK_MAIN]]
    await update.message.reply_text(
        "📦 *Stock Out — ထုတ်ယူမည့် ပစ္စည်းကို ရွေးပါ*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return STOCK_ITEM


async def step_stock_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()

    if choice == BTN_BACK_MAIN:
        return await show_main_menu(update, context)

    food_prices = context.user_data.get("stock_food_prices", {})
    if choice not in food_prices:
        await update.message.reply_text("❌ ပစ္စည်း မရှိပါ။ ပြန်ရွေးပါ။")
        return STOCK_ITEM

    context.user_data["stock_item"] = choice
    kb = [[BTN_BACK_MAIN]]
    await update.message.reply_text(
        f"📦 *{choice}*\n\n"
        f"ထုတ်ယူသော အရေအတွက် ထည့်ပါ (ဥပမာ: 2) -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return STOCK_QTY


async def step_stock_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == BTN_BACK_MAIN:
        return await show_main_menu(update, context)

    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ ဂဏန်း မှန်မှန်ကန်ကန် ထည့်ပါ (ဥပမာ: 2) -")
        return STOCK_QTY

    qty  = int(text)
    item = context.user_data.get("stock_item", "")
    today = now_mmt().strftime("%-m/%-d/%Y")
    ref   = "STK-" + now_mmt().strftime("%Y%m%d-%H%M%S")

    food_prices = context.user_data.get("stock_food_prices", {})
    food_costs  = fetch_food_costs()
    sell_price  = food_prices.get(item, 0)
    cost_price  = food_costs.get(item, 0)
    total_val   = sell_price * qty
    total_cogs  = cost_price * qty

    try:
        stock_sh.append_row(
            [today, ref, item, qty, sell_price, total_val, cost_price, total_cogs],
            value_input_option="USER_ENTERED",
        )
        logging.info("Stock out saved: %s x%d ref=%s", item, qty, ref)
        msg = (
            f"✅ *Stock Out မှတ်တမ်းတင်ပြီး*\n\n"
            f"📦 Item     : *{item}*\n"
            f"🔢 Qty Out  : *{qty}*\n"
            f"💰 Sell     : {sell_price:,} × {qty} = *{total_val:,} Ks*\n"
            f"📋 Ref      : `{ref}`\n"
            f"📅 Date     : {today}"
        )
        # Update K1 total inventory value
        inv_total = _update_inv_total_k1()
        if inv_total:
            msg += f"\n\n📊 Total Inv Value: *{inv_total:,} Ks*"
        # Low stock alert
        inv_data = _replit_get("sheets/inventory")
        if inv_data:
            for inv_item in inv_data.get("items", []):
                if inv_item["name"] == item:
                    remaining = max(0, inv_item.get("current_stock", 0))
                    if remaining <= 5:
                        alert_emoji = "🔴" if remaining == 0 else "🟡"
                        msg += (
                            f"\n\n{alert_emoji} *Low Stock Alert!*\n"
                            f"📦 *{item}* — လက်ကျန် *{remaining} pcs* သာကျန်တော့သည်!\n"
                            f"{'❌ Stock ကုန်သွားပြီ — အမြန်ဖြည့်ပါ!' if remaining == 0 else '⚠️ Stock ဖြည့်ရန် အချိန်ကြောင်ပြီ!'}"
                        )
                    break
    except Exception as e:
        logging.error("Stock out failed: %s", e)
        msg = f"❌ မှတ်တမ်းတင်မှု မအောင်မြင်ပါ။\n{e}"

    context.user_data.pop("stock_item", None)
    context.user_data.pop("stock_food_prices", None)

    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BTN_BACK_MAIN]], resize_keyboard=True),
    )
    return MAIN_MENU


# ═════════════════════════════════════════
#  STOCK IN (RESTOCK) FLOW
# ═════════════════════════════════════════

async def show_si_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show food item list for Stock In (restock) recording."""
    food_prices = fetch_food_prices()
    context.user_data["si_food_prices"] = food_prices
    names = list(food_prices.keys())
    rows  = [names[i: i + 2] for i in range(0, len(names), 2)]
    kb    = rows + [[BTN_BACK_MAIN]]
    await update.message.reply_text(
        "📥 *Stock In — ဝယ်ယူသော ပစ္စည်းကို ရွေးပါ*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SI_ITEM


async def step_si_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == BTN_BACK_MAIN:
        return await show_main_menu(update, context)
    food_prices = context.user_data.get("si_food_prices", {})
    if choice not in food_prices:
        await update.message.reply_text("❌ ပစ္စည်း မရှိပါ။ ပြန်ရွေးပါ။")
        return SI_ITEM
    context.user_data["si_item"] = choice
    kb = [["1", "2", "3", "5", "10"], [BTN_BACK_MAIN]]
    await update.message.reply_text(
        f"📥 *{choice}*\n\nဝယ်ယူသော အရေအတွက် ထည့်ပါ (ဥပမာ: 10) -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SI_QTY


async def step_si_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_BACK_MAIN:
        return await show_main_menu(update, context)
    try:
        qty = int(text)
    except ValueError:
        await update.message.reply_text("❌ ဂဏန်းသက်သက် ရိုက်ပေးပါ -")
        return SI_QTY
    if qty <= 0:
        await update.message.reply_text("❌ အရေအတွက် 1 နှင့်အထက် ဖြစ်ရမည် -")
        return SI_QTY
    context.user_data["si_qty"] = qty
    item = context.user_data.get("si_item", "")
    food_costs = fetch_food_costs()
    default_cost = food_costs.get(item, 0)
    hint = f" (Default: {default_cost:,} Ks)" if default_cost else ""
    kb = [[BTN_BACK_MAIN]]
    await update.message.reply_text(
        f"📥 *{item}* × {qty}\n\nတစ်ခုစျေးနှုန်း (Unit Cost) ရိုက်ပါ{hint} -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SI_COST


async def step_si_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_BACK_MAIN:
        return await show_main_menu(update, context)
    try:
        cost = int(text.replace(",", ""))
    except ValueError:
        await update.message.reply_text("❌ ဂဏန်းသက်သက် ရိုက်ပေးပါ (ဥပမာ: 2000) -")
        return SI_COST
    if cost < 0:
        await update.message.reply_text("❌ 0 နှင့်အထက် ဖြစ်ရမည် -")
        return SI_COST

    # Add this item to the cart
    item  = context.user_data.get("si_item", "")
    qty   = context.user_data.get("si_qty", 0)
    total = qty * cost
    cart  = context.user_data.setdefault("si_cart", [])
    cart.append({"item": item, "qty": qty, "cost": cost, "total": total})

    # Clear per-item temp data
    for k in ("si_item", "si_qty"):
        context.user_data.pop(k, None)

    return await show_si_cart(update, context)


async def show_si_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show running cart summary with options to add more or proceed to payment."""
    cart = context.user_data.get("si_cart", [])
    grand_total = sum(e["total"] for e in cart)
    lines = []
    for i, e in enumerate(cart, 1):
        lines.append(
            f"{i}. *{e['item']}*  ×{e['qty']}  @{e['cost']:,}  = *{e['total']:,} Ks*"
        )
    cart_text = "\n".join(lines)
    kb = [[BTN_SI_ADD], [BTN_SI_FINISH], [BTN_BACK_MAIN]]
    await update.message.reply_text(
        f"🛒 *Stock In Cart — {len(cart)} item(s)*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{cart_text}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Grand Total : *{grand_total:,} Ks*\n\n"
        f"➕ Item ထပ်ဝယ်မည်လား? ၀ယ်ပြီးလျှင် Payment ဆက်သွားပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SI_CART


async def step_si_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_BACK_MAIN:
        context.user_data.pop("si_cart", None)
        return await show_main_menu(update, context)
    if text == BTN_SI_ADD:
        return await show_si_items(update, context)
    if text == BTN_SI_FINISH:
        cart        = context.user_data.get("si_cart", [])
        grand_total = sum(e["total"] for e in cart)
        kb = [["Cash", "KPay"], [BTN_SI_SPLIT], [BTN_BACK_MAIN]]
        await update.message.reply_text(
            f"💳 *{len(cart)} items — Grand Total: {grand_total:,} Ks*\n\n"
            f"ငွေပေးချေမှု နည်းလမ်း ရွေးပါ -\n"
            f"_(ငွေခွဲပေးမည်ဆိုရင် ္'ခွဲပေး' ကို နှိပ်ပါ)_",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return SI_PAY
    await update.message.reply_text("⬆️ ပေါ်ရှိ ခလုတ်များမှ ရွေးပါ -")
    return SI_CART


async def _show_si_review(update, context):
    """Shared review screen for Stock In — called from step_si_pay and step_si_pay_split."""
    d           = context.user_data
    cart        = d.get("si_cart", [])
    grand_total = sum(e["total"] for e in cart)
    lines = [
        f"• *{e['item']}*  ×{e['qty']}  @{e['cost']:,}/pc  = {e['total']:,} Ks"
        for e in cart
    ]
    # Build payment display
    if d.get("si_pay_cash") is not None:
        cash_amt  = d["si_pay_cash"]
        kpay_amt  = d["si_pay_kpay"]
        pay_line  = f"💵 Cash  : *{cash_amt:,} Ks*\n💙 KPay  : *{kpay_amt:,} Ks*"
    else:
        pay_line  = f"💳 Payment : *{d.get('si_pay', '')}*"
    kb = [[BTN_CONFIRM_SAVE], [BTN_BACK_MAIN]]
    await update.message.reply_text(
        f"📋 *Review — Stock In ({len(cart)} items)*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{chr(10).join(lines)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Grand Total : *{grand_total:,} Ks*\n"
        f"{pay_line}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"မှန်ကန်ပါသလား? ✅ Confirm & Save နှိပ်ပါ -",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return SI_CONFIRM


async def step_si_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_BACK_MAIN:
        return await show_si_cart(update, context)

    # Split payment option
    if text == BTN_SI_SPLIT:
        cart        = context.user_data.get("si_cart", [])
        grand_total = sum(e["total"] for e in cart)
        context.user_data.pop("si_pay", None)
        context.user_data.pop("si_pay_cash", None)
        context.user_data.pop("si_pay_kpay", None)
        kb = [[BTN_BACK_MAIN]]
        await update.message.reply_text(
            f"💵 *ခွဲပေး — Grand Total: {grand_total:,} Ks*\n\n"
            f"Cash ဘယ်လောက်ပေးမည်? (ဂဏန်းသက်သက် ရိုက်ပါ)\n"
            f"KPay = {grand_total:,} - Cash ဖြင့် အလိုအလျောက် တွက်မည်",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return SI_PAY_SPLIT

    if text not in ("Cash", "KPay"):
        await update.message.reply_text("❌ Cash / KPay / ခွဲပေး မှ ရွေးပါ -")
        return SI_PAY

    context.user_data["si_pay"]      = text
    context.user_data.pop("si_pay_cash", None)
    context.user_data.pop("si_pay_kpay", None)
    return await _show_si_review(update, context)


async def step_si_pay_split(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collect Cash portion; KPay = Grand Total - Cash."""
    text = update.message.text.strip()
    if text == BTN_BACK_MAIN:
        return await show_si_cart(update, context)
    try:
        cash_amt = int(text.replace(",", ""))
    except ValueError:
        await update.message.reply_text("❌ ဂဏန်းသက်သက် ရိုက်ပေးပါ (ဥပမာ: 20000) -")
        return SI_PAY_SPLIT
    if cash_amt < 0:
        await update.message.reply_text("❌ 0 နှင့်အထက် ဖြစ်ရမည် -")
        return SI_PAY_SPLIT
    cart        = context.user_data.get("si_cart", [])
    grand_total = sum(e["total"] for e in cart)
    if cash_amt > grand_total:
        await update.message.reply_text(
            f"❌ Cash {cash_amt:,} Ks သည် Grand Total {grand_total:,} Ks ထက် မကျော်သင့်ပါ -"
        )
        return SI_PAY_SPLIT
    kpay_amt = grand_total - cash_amt
    context.user_data["si_pay_cash"] = cash_amt
    context.user_data["si_pay_kpay"] = kpay_amt
    context.user_data.pop("si_pay", None)
    return await _show_si_review(update, context)


async def step_si_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_BACK_MAIN:
        return await show_si_cart(update, context)
    if text != BTN_CONFIRM_SAVE:
        return SI_CONFIRM
    d       = context.user_data
    cart      = d.get("si_cart", [])
    today     = now_mmt().strftime("%-m/%-d/%Y")
    # Build payment string for sheet
    if d.get("si_pay_cash") is not None:
        cash_amt  = d["si_pay_cash"]
        kpay_amt  = d["si_pay_kpay"]
        payment   = f"Cash {cash_amt:,} / KPay {kpay_amt:,}"
    else:
        payment   = d.get("si_pay", "")
    try:
        for e in cart:
            stock_in_sh.append_row(
                [today, e["item"], e["qty"], e["cost"], e["total"], payment, "Bot"],
                value_input_option="USER_ENTERED",
            )
            logging.info("Stock in saved: %s x%d cost=%d", e["item"], e["qty"], e["cost"])
        grand_total = sum(e["total"] for e in cart)
        inv_total   = _update_inv_total_k1()
        total_note  = f"\n📊 Inv Value: *{inv_total:,} Ks*" if inv_total else ""
        lines = [
            f"• *{e['item']}*  ×{e['qty']}  = {e['total']:,} Ks"
            for e in cart
        ]
        # Payment display in success message
        if d.get("si_pay_cash") is not None:
            pay_display = f"💵 Cash {d['si_pay_cash']:,} Ks + 💙 KPay {d['si_pay_kpay']:,} Ks"
        else:
            pay_display = payment
        msg = (
            f"✅ *Stock In မှတ်တမ်းတင်ပြီး ({len(cart)} items)*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{chr(10).join(lines)}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 Grand Total : *{grand_total:,} Ks*\n"
            f"💳 Payment     : {pay_display}\n"
            f"📅 Date        : {today}"
            f"{total_note}"
        )
    except Exception as e:
        logging.error("Stock in failed: %s", e)
        msg = f"❌ မှတ်တမ်းတင်မှု မအောင်မြင်ပါ။\n{e}"

    for k in ("si_item", "si_qty", "si_cart", "si_pay", "si_pay_cash", "si_pay_kpay", "si_food_prices"):
        context.user_data.pop(k, None)
    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[BTN_BACK_MAIN]], resize_keyboard=True),
    )
    return MAIN_MENU


# ═════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════

def main():
    app = (
        Application.builder()
        .token(os.environ["BOT_TOKEN"])
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_pool_timeout(30)
        .build()
    )
    app.add_error_handler(error_handler)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start",      show_main_menu),
            CommandHandler("menu",       show_main_menu),
            CommandHandler("cancel",     cmd_cancel),
            CommandHandler("help",       cmd_help),
            CommandHandler("version",    cmd_version),
            # Sales
            CommandHandler("sales",      cmd_sales_direct),
            # Members
            CommandHandler("member",     cmd_member_mgmt),
            CommandHandler("newmember",  cmd_newmember),
            CommandHandler("topup",      cmd_topup),
            CommandHandler("check",      cmd_check_member),
            CommandHandler("ranks",      cmd_ranks),
            # Reports
            CommandHandler("report",     cmd_today_report),
            CommandHandler("freport",    cmd_financial_report),
            CommandHandler("broadcast",  cmd_broadcast),
            CommandHandler("kpi",        cmd_kpi_cmd),
            CommandHandler("payroll",    cmd_payroll_cmd),
            CommandHandler("setattend",  cmd_setattend_cmd),
            CommandHandler("admin",      cmd_admin),
            # Booking management
            CommandHandler("bookings",   cmd_admin_bookings),
            CommandHandler("newbooking", cmd_staff_booking),
            MessageHandler(filters.Regex(r"^/approve_\d+$"), cmd_approve_booking),
            MessageHandler(filters.Regex(r"^/reject_\d+$"),  cmd_reject_booking),
            # Stock
            CommandHandler("stock",      cmd_stock_menu),
            CommandHandler("stockin",    cmd_stockin_direct),
            CommandHandler("stockout",   cmd_stockout_direct),
            CommandHandler("inventory",  cmd_inventory),
            CommandHandler("stocktoday", cmd_stocktoday),
            # Console
            CommandHandler("console",    cmd_console_status),
        ],
        states={
            # ── Main Menu ──
            MAIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_main_menu)],

            # ── Member Management sub-menu ──
            MM_MENU:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_mm_menu)],
            MM_LOOKUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_mm_lookup)],

            # ── First Purchase flow ──
            NM_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_nm_name)],
            NM_PHONE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_nm_phone)],
            NM_EMAIL:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_nm_email)],
            NM_ID:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_nm_id)],
            NM_AMT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_nm_amt)],
            NM_GIFT_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_nm_gift_pin)],
            NM_KPAY:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_nm_kpay)],
            NM_CONFIRM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_nm_confirm)],

            # ── Top Up flow ──
            TU_MEMBER:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_tu_member)],
            TU_AMT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_tu_amt)],
            TU_KPAY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_tu_kpay)],
            TU_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_tu_confirm)],

            # ── Daily Sales flow ──
            MEMBER:          [MessageHandler(filters.TEXT & ~filters.COMMAND, step_member)],
            CONSOLE:         [MessageHandler(filters.TEXT & ~filters.COMMAND, step_console)],
            MINS:            [MessageHandler(filters.TEXT & ~filters.COMMAND, step_mins)],
            FOOD_MENU:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_food_menu)],
            FOOD_QTY:        [MessageHandler(filters.TEXT & ~filters.COMMAND, step_food_qty)],
            CONFIRM_SUMMARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_confirm)],
            KPAY_AMT:        [MessageHandler(filters.TEXT & ~filters.COMMAND, step_kpay)],
            SALE_CONFIRM:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_sale_confirm)],

            # ── Stock PIN entry ──
            STOCK_PIN:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_stock_pin)],
            # ── Stock sub-menu ──
            STOCK_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_stock_menu)],

            # ── Stock Out flow ──
            STOCK_ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_stock_item)],
            STOCK_QTY:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_stock_qty)],

            # ── Stock In (Restock) flow ──
            SI_ITEM:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_si_item)],
            SI_QTY:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_si_qty)],
            SI_COST:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_si_cost)],
            SI_CART:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_si_cart)],
            SI_PAY:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_si_pay)],
            SI_PAY_SPLIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_si_pay_split)],
            SI_CONFIRM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_si_confirm)],

            # ── Discount step ──
            DISCOUNT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_discount)],

            # ── Attendance wizard ──
            ATTEND_STAFF:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_attend_staff)],
            ATTEND_LEAVE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_attend_leave)],
            ATTEND_LATE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_attend_late)],
            ATTEND_DEDUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_attend_deduct)],

            # ── Admin Panel ──
            ADMIN_PIN:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_admin_pin)],
            ADMIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_admin_menu)],

            # ── Salary Advance flow ──
            SAL_ADV_STAFF:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_sal_adv_staff)],
            SAL_ADV_AMT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_sal_adv_amt)],
            SAL_ADV_PAY:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_sal_adv_pay)],
            SAL_ADV_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_sal_adv_confirm)],

            # ── Console Booking flow ──
            BOOK_CONSOLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_book_console)],
            BOOK_MEMBER:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_book_member)],

            # ── Console Management submenu ──
            CONSOLE_MENU:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_console_menu)],

            # ── End Session flow ──
            END_SESSION_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_end_session)],

            # ── Game Library flows ──
            GAME_MENU:          [MessageHandler(filters.TEXT & ~filters.COMMAND, step_game_menu)],
            GAME_ADD_TITLE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_game_add_title)],
            GAME_ADD_PLATFORM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_game_add_platform)],
            GAME_ADD_GENRE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_game_add_genre)],
            GAME_ADD_STATUS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_game_add_status)],
            GAME_DEL_SELECT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_game_del_select)],
            # ── Game Discs Record ──
            DISC_SELECT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_disc_select)],
            DISC_SET_QTY:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_disc_set_qty)],
            # ── Console-Game Install flows ──
            GINST_MENU:         [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ginst_menu)],
            GINST_VIEW_CONS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ginst_view_cons)],
            GINST_ADD_CONS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ginst_add_cons)],
            GINST_ADD_GAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ginst_add_game)],
            GINST_ADD_TYPE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ginst_add_type)],
            GINST_DEL_CONS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ginst_del_cons)],
            GINST_DEL_GAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ginst_del_game)],
            # ── External SSD Management flows ──
            SSD_MENU:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ssd_menu)],
            SSD_VIEW_SSD:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ssd_view)],
            SSD_ADD_SSD:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ssd_add_ssd)],
            SSD_ADD_GAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ssd_add_game)],
            SSD_ADD_TYPE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ssd_add_type)],
            SSD_DEL_SSD:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ssd_del_ssd)],
            SSD_DEL_GAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ssd_del_game)],
            SSD_XFER_SSD:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ssd_xfer_ssd)],
            SSD_XFER_GAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ssd_xfer_game)],
            SSD_XFER_CONS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ssd_xfer_cons)],
            SSD_RET_CONS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ssd_ret_cons)],
            SSD_RET_GAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ssd_ret_game)],

            # ── Console CRUD flows ──
            CON_MGMT_MENU:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_con_mgmt_menu)],
            CON_ADD_ID:         [MessageHandler(filters.TEXT & ~filters.COMMAND, step_con_add_id)],
            CON_ADD_TYPE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_con_add_type)],
            CON_ADD_MULT:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_con_add_mult)],
            CON_DEL_SELECT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_con_del_select)],

            # ── Session → Daily Sales bridge ──
            SESSION_SHORTFALL:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_session_shortfall)],
            # ── Daily Sales in-session conflict checks ──
            DS_MEMBER_IN_SESSION:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ds_member_in_session)],
            DS_CONSOLE_IN_SESSION:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_ds_console_in_session)],
            # ── Booking duplicate-session warning ──
            BOOK_DUP_WARN:          [MessageHandler(filters.TEXT & ~filters.COMMAND, step_book_dup_warn)],
            # ── Booking game selection ──
            BOOK_GAME:              [MessageHandler(filters.TEXT & ~filters.COMMAND, step_book_game)],
            # ── Booking planned duration / timer ──
            BOOK_MINS:              [MessageHandler(filters.TEXT & ~filters.COMMAND, step_book_mins)],
            # ── Game change for active session ──
            GAME_CHANGE_CONS:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_game_change_cons)],
            GAME_CHANGE_GAME:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_game_change_game)],

            # ── Staff Advance Booking flow ──
            SBK_CONSOLE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_sbk_console)],
            SBK_CUST_NAME:[MessageHandler(filters.TEXT & ~filters.COMMAND, step_sbk_cust_name)],
            SBK_DATE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_sbk_date)],
            SBK_TIME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_sbk_time)],
            SBK_DUR:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_sbk_dur)],
            SBK_GAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_sbk_game)],
            SBK_CONFIRM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_sbk_confirm)],

            # ── Finance module ──
            FINANCE_MENU:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_finance_menu)],
            OPEX_CAT:          [MessageHandler(filters.TEXT & ~filters.COMMAND, step_opex_cat)],
            OPEX_DESC:         [MessageHandler(filters.TEXT & ~filters.COMMAND, step_opex_desc)],
            OPEX_AMT:          [MessageHandler(filters.TEXT & ~filters.COMMAND, step_opex_amt)],
            OPEX_ACCT:         [MessageHandler(filters.TEXT & ~filters.COMMAND, step_opex_acct)],
            OPEX_PAY:          [MessageHandler(filters.TEXT & ~filters.COMMAND, step_opex_pay)],
            OPEX_CONFIRM:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_opex_confirm)],
            ASSET_NAME:            [MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset_name)],
            ASSET_CAT:             [MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset_cat)],
            ASSET_DATE:            [MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset_date)],
            ASSET_COST:            [MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset_cost)],
            ASSET_QTY:             [MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset_qty)],
            ASSET_LIFE:            [MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset_life)],
            ASSET_SALVAGE:         [MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset_salvage)],
            ASSET_PAY:             [MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset_pay)],
            ASSET_CONFIRM:         [MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset_confirm)],
            ASSET_DISPOSE_SEL:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset_dispose_sel)],
            ASSET_DISPOSE_DATE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset_dispose_date)],
            ASSET_DISPOSE_QTY:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset_dispose_qty)],
            ASSET_DISPOSE_PROCEEDS:[MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset_dispose_proceeds)],
            ASSET_DISPOSE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset_dispose_confirm)],
            PREPAID_DESC:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_prepaid_desc)],
            PREPAID_CAT:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_prepaid_cat)],
            PREPAID_AMT:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_prepaid_amt)],
            PREPAID_ACCT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_prepaid_acct)],
            PREPAID_START:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_prepaid_start)],
            PREPAID_END:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_prepaid_end)],
            PREPAID_CONFIRM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_prepaid_confirm)],
            ACCT_TRF_FROM:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_acct_trf_from)],
            ACCT_TRF_TO:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_acct_trf_to)],
            ACCT_TRF_AMT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_acct_trf_amt)],
            ACCT_TRF_NOTE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_acct_trf_note)],
            ACCT_TRF_CONFIRM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_acct_trf_confirm)],
            PAY_VENDOR:        [MessageHandler(filters.TEXT & ~filters.COMMAND, step_pay_vendor)],
            PAY_DESC:          [MessageHandler(filters.TEXT & ~filters.COMMAND, step_pay_desc)],
            PAY_AMT:           [MessageHandler(filters.TEXT & ~filters.COMMAND, step_pay_amt)],
            PAY_DUE:           [MessageHandler(filters.TEXT & ~filters.COMMAND, step_pay_due)],
            PAY_ACCT:          [MessageHandler(filters.TEXT & ~filters.COMMAND, step_pay_acct)],
            PAY_CONFIRM:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_pay_confirm)],
            REC_CUST:          [MessageHandler(filters.TEXT & ~filters.COMMAND, step_rec_cust)],
            REC_DESC:          [MessageHandler(filters.TEXT & ~filters.COMMAND, step_rec_desc)],
            REC_AMT:           [MessageHandler(filters.TEXT & ~filters.COMMAND, step_rec_amt)],
            REC_DUE:           [MessageHandler(filters.TEXT & ~filters.COMMAND, step_rec_due)],
            REC_ACCT:          [MessageHandler(filters.TEXT & ~filters.COMMAND, step_rec_acct)],
            REC_CONFIRM:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_rec_confirm)],
            FIN_REPORT_MENU:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_fin_report_menu)],
            CAP_ACCT:          [MessageHandler(filters.TEXT & ~filters.COMMAND, step_cap_acct)],
            CAP_AMT:           [MessageHandler(filters.TEXT & ~filters.COMMAND, step_cap_amt)],
            CAP_CONFIRM:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_cap_confirm)],
            SHARE_NAME:        [MessageHandler(filters.TEXT & ~filters.COMMAND, step_share_name)],
            SHARE_ROLE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, step_share_role)],
            SHARE_CAP:         [MessageHandler(filters.TEXT & ~filters.COMMAND, step_share_cap)],
            SHARE_OWN:         [MessageHandler(filters.TEXT & ~filters.COMMAND, step_share_own)],
            SHARE_CONFIRM:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_share_confirm)],
            PAY_SETTLE_LIST:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_pay_settle_list)],
            PAY_SETTLE_ACCT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_pay_settle_acct)],
            PAY_SETTLE_CONFIRM:[MessageHandler(filters.TEXT & ~filters.COMMAND, step_pay_settle_confirm)],
            REC_SETTLE_LIST:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_rec_settle_list)],
            REC_SETTLE_ACCT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_rec_settle_acct)],
            REC_SETTLE_CONFIRM:[MessageHandler(filters.TEXT & ~filters.COMMAND, step_rec_settle_confirm)],
            # ── Advance Payment ──
            ADVPAY_PARTY:          [MessageHandler(filters.TEXT & ~filters.COMMAND, step_advpay_party)],
            ADVPAY_DESC:           [MessageHandler(filters.TEXT & ~filters.COMMAND, step_advpay_desc)],
            ADVPAY_AMT:            [MessageHandler(filters.TEXT & ~filters.COMMAND, step_advpay_amt)],
            ADVPAY_ACCT:           [MessageHandler(filters.TEXT & ~filters.COMMAND, step_advpay_acct)],
            ADVPAY_DUE:            [MessageHandler(filters.TEXT & ~filters.COMMAND, step_advpay_due)],
            ADVPAY_NOTE:           [MessageHandler(filters.TEXT & ~filters.COMMAND, step_advpay_note)],
            ADVPAY_CONFIRM:        [MessageHandler(filters.TEXT & ~filters.COMMAND, step_advpay_confirm)],
            ADVPAY_LIST:           [MessageHandler(filters.TEXT & ~filters.COMMAND, step_advpay_list)],
            ADVPAY_SETTLE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_advpay_settle_confirm)],

        },
        fallbacks=[
            CommandHandler("cancel",     cmd_cancel),
            CommandHandler("start",      show_main_menu),
            CommandHandler("menu",       show_main_menu),
            CommandHandler("help",       cmd_help),
            CommandHandler("version",    cmd_version),
            # Sales
            CommandHandler("sales",      cmd_sales_direct),
            # Members
            CommandHandler("member",     cmd_member_mgmt),
            CommandHandler("newmember",  cmd_newmember),
            CommandHandler("topup",      cmd_topup),
            CommandHandler("check",      cmd_check_member),
            CommandHandler("ranks",      cmd_ranks),
            # Reports
            CommandHandler("report",     cmd_today_report),
            CommandHandler("freport",    cmd_financial_report),
            CommandHandler("kpi",        cmd_kpi_cmd),
            CommandHandler("payroll",    cmd_payroll_cmd),
            CommandHandler("setattend",  cmd_setattend_cmd),
            CommandHandler("admin",      cmd_admin),
            CommandHandler("broadcast",  cmd_broadcast),
            CommandHandler("finance",    cmd_finance),
            # Booking management
            CommandHandler("bookings",   cmd_admin_bookings),
            CommandHandler("newbooking", cmd_staff_booking),
            MessageHandler(filters.Regex(r"^/approve_\d+$"), cmd_approve_booking),
            MessageHandler(filters.Regex(r"^/reject_\d+$"),  cmd_reject_booking),
            # Stock
            CommandHandler("stock",      cmd_stock_menu),
            CommandHandler("stockin",    cmd_stockin_direct),
            CommandHandler("stockout",   cmd_stockout_direct),
            CommandHandler("inventory",  cmd_inventory),
            CommandHandler("stocktoday", cmd_stocktoday),
            # Console
            CommandHandler("console",    cmd_console_status),
        ],
    )

    # Group -1: custom extend reply — fires BEFORE ConversationHandler (group 0)
    # raises ApplicationHandlerStop so conv never sees the message when pending
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_extend_reply),
        group=-1,
    )

    app.add_handler(conv)

    # Global inline-button handler — works regardless of conversation state
    app.add_handler(CallbackQueryHandler(cb_extend_timer,  pattern=r"^ext:"))
    app.add_handler(CallbackQueryHandler(cb_booking_mgmt,   pattern=r"^bkm:(approve|reject):\d+$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_booking,     pattern=r"^bkc:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_with_reason, pattern=r"^bkcr:\d+:\w+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_cancel_note_input), group=10)

    # Standalone fallback handlers (outside ConversationHandler — for cold starts)
    for cmd, fn in [
        ("start",      show_main_menu),
        ("menu",       show_main_menu),
        ("cancel",     cmd_cancel),
        ("help",       cmd_help),
        ("version",    cmd_version),
        ("sales",      cmd_sales_direct),
        ("member",     cmd_member_mgmt),
        ("newmember",  cmd_newmember),
        ("topup",      cmd_topup),
        ("check",      cmd_check_member),
        ("ranks",      cmd_ranks),
        ("report",     cmd_today_report),
        ("freport",    cmd_financial_report),
        ("broadcast",  cmd_broadcast),
        ("kpi",        cmd_staff_kpi),
        ("payroll",    cmd_payroll),
        ("setattend",  cmd_setattend),
        ("admin",      cmd_admin),
        ("finance",    cmd_finance),
        ("stock",      cmd_stock_menu),
        ("stockin",    cmd_stockin_direct),
        ("stockout",   cmd_stockout_direct),
        ("inventory",  cmd_inventory),
        ("stocktoday",  cmd_stocktoday),
        ("console",     cmd_console_status),
        ("newbooking",     cmd_staff_booking),
        ("cancelbooking",  cmd_cancel_booking),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    # Register "/" dropdown list with Telegram
    async def _set_commands(application):
        await application.bot.set_my_commands([
            BotCommand("start",      "🏠 Main Menu"),
            BotCommand("menu",       "🏠 Main Menu"),
            BotCommand("sales",      "📝 New Sale (shortcut)"),
            BotCommand("member",     "💳 Member Management"),
            BotCommand("newmember",  "🆕 New Member Register"),
            BotCommand("topup",      "💰 Top Up Member"),
            BotCommand("check",      "🔍 Check Member Info"),
            BotCommand("ranks",      "📋 View Rank Tiers"),
            BotCommand("report",     "📊 Today's Report"),
            BotCommand("freport",    "💹 Financial Report (week + month)"),
            BotCommand("kpi",        "📈 Staff KPI"),
            BotCommand("payroll",    "💰 Monthly Payroll"),
            BotCommand("setattend",  "📅 Record Leave / Late"),
            BotCommand("admin",      "🔧 Admin Panel"),
            BotCommand("broadcast",  "📢 Broadcast message to customers"),
            BotCommand("stock",      "📦 Stock Update menu"),
            BotCommand("stockin",    "📥 Stock In (Restock)"),
            BotCommand("stockout",   "📦 Stock Out"),
            BotCommand("inventory",  "🗂 Inventory Status"),
            BotCommand("stocktoday", "🛒 Items sold today"),
            BotCommand("cancel",     "❌ Cancel & return"),
            BotCommand("help",       "📖 Command list"),
            BotCommand("version",    "📦 Bot version info"),
            BotCommand("console",    "🕹️ Console live status"),
            BotCommand("finance",    "💼 Finance Management"),
        ])

    app.post_init = _set_commands

    # Pre-warm config + member cache so first user interaction is instant
    logging.info("Pre-warming config and member cache...")
    _load_cfg()
    _load_members()

    # Start background cache refresh task (every 5 min)
    loop = asyncio.get_event_loop()
    loop.create_task(_bg_cache_refresh())

    logging.info("PS Vibe Bot is running...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        timeout=30,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    import subprocess

    _my_pid   = os.getpid()
    _LOCK_PATH = "/tmp/ps_vibe_bot.lock"

    # ── Step 1: Kill ALL other python3 main.py processes (no cooperation needed) ──
    try:
        _result = subprocess.run(
            ["pgrep", "-f", "python3 main.py"],
            capture_output=True, text=True,
        )
        for _pid_str in _result.stdout.strip().split("\n"):
            try:
                _pid = int(_pid_str.strip())
            except ValueError:
                continue
            if _pid == _my_pid:
                continue
            logging.warning("Duplicate bot process found (PID %d) — killing...", _pid)
            try:
                os.kill(_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(2)   # give SIGTERM a moment to land
        # Force-kill anything still alive
        for _pid_str in _result.stdout.strip().split("\n"):
            try:
                _pid = int(_pid_str.strip())
            except ValueError:
                continue
            if _pid == _my_pid:
                continue
            try:
                os.kill(_pid, signal.SIGKILL)
                logging.warning("Force-killed PID %d", _pid)
            except ProcessLookupError:
                pass   # already gone — good
    except Exception as _e:
        logging.warning("Process scan failed: %s", _e)

    # ── Step 2: Write PID lock so future restarts can identify us ─────────
    try:
        with open(_LOCK_PATH, "w") as _lf:
            _lf.write(str(_my_pid))
    except Exception:
        pass
    logging.info("Bot started — PID %d", _my_pid)
    ensure_sheet_headers()

    # ── Step 3: Clean shutdown on SIGTERM (Replit workflow stop) ──────────
    def _sigterm_handler(signum, frame):
        logging.info("SIGTERM received — shutting down (PID %d).", _my_pid)
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    # ── Step 4: Start keep-alive server & polling loop ────────────────────
    if keep_alive:
        keep_alive()
    while True:
        try:
            # Fresh event loop on every (re)start so run_polling can install
            # its signal handlers even after a previous loop was closed.
            asyncio.set_event_loop(asyncio.new_event_loop())
            main()
        except KeyboardInterrupt:
            logging.info("Bot stopped by operator.")
            break
        except Exception as exc:
            from telegram.error import Conflict
            if isinstance(exc, Conflict):
                logging.warning("Conflict detected — waiting 30 s for Telegram session to expire...")
                time.sleep(30)
            else:
                logging.error("Bot crashed: %s — restarting in 5 s...", exc, exc_info=True)
                time.sleep(5)

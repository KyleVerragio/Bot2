# server.py
from flask import Flask, request, abort
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import pandas as pd
from rapidfuzz import process
import re, time, json, os, math, datetime

# ==============================
# Config
# ==============================
USE_MMS = True                        # attach 1st image per style (if available)
FALLBACK_LINK_IN_TEXT = False           # also include clickable link in text
MAX_LISTED_STYLES = 10                 # cap refine lists
SESSION_TTL_SECS = 600                 # 10 min selection window

# Promo footer (no "Additional info:" prefix)
PROMO_FOOTER = ""

# --- Access/abuse control ---
ACCESS_MODE = "closed"                   # "open" or "closed"
SILENT_REJECT = False                  # if True, unauthorized get no reply
ADMIN_NUMBERS = {"+19173555884", "+19179939093", "+16462846597"}  # <-- admin numbers (E.164)

import os, pathlib  # add once near the top with other imports/constants

# Use the Render Disk if present; otherwise use the current folder (for local runs)
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "/var/data"))
if not DATA_DIR.exists():
    DATA_DIR = pathlib.Path(".")

# ---- Persistent file locations (now on the disk) ----
ALLOWLIST_PATH = str(DATA_DIR / "allowlist.json")
DENYLIST_PATH  = str(DATA_DIR / "denylist.json")
STATE_PATH     = str(DATA_DIR / "state.json")      # stores {"paused": bool}
METRICS_PATH   = str(DATA_DIR / "metrics.ndjson")  # append-only


RL_WINDOW_SECS = 300                   # rate limit window for unknowns
RL_MAX_PER_WINDOW = 5

# --- Pause switch (persisted) ---
GLOBAL_PAUSED_DEFAULT = False

# --- Metrics & cost estimate ---
SMS_IN_COST  = 0.0075
SMS_OUT_COST = 0.0075

# --- Twilio creds (optional; for REPORT/daily recap) ---
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER  = os.getenv("TWILIO_FROM_NUMBER", "")
ADMIN_REPORT_NUMBER = os.getenv("ADMIN_REPORT_NUMBER", "+19173555884")

# --- Cron endpoint protection ---
CRON_SECRET = os.getenv("CRON_SECRET", "CHANGE_ME_SECRET")

# ==============================
# Load & prepare your sheet
# ==============================
df = pd.read_excel("style_prices.xlsx")
df.columns = df.columns.str.strip()
df.rename(columns={"PRICE": "Price"}, inplace=True)

df["StyleNumber"] = df["StyleNumber"].astype(str).str.upper().str.strip()
df["Price"] = (
    df["Price"].astype(str).str.replace(r"[\$,]", "", regex=True).astype(float)
)

# normalized (remove hyphens/spaces; keep dots)
def _normalize(s: str) -> str:
    return str(s).upper().replace("-", "").replace(" ", "")

df["Normalized"] = df["StyleNumber"].apply(_normalize)

# Parse parts: Prefix, BaseDigits, Suffix
def parse_parts(style: str):
    s = str(style).upper().strip()
    dm = re.search(r"(\d{3,5})", s)        # first 3–5 digit group = base
    if not dm:
        return "", "", ""
    base_digits = dm.group(1)
    pm = re.match(r"^\s*([A-Z]+)", s)      # prefix at start
    prefix = pm.group(1) if pm else ""
    tail = s[dm.end():]                    # suffix right after base (optional hyphen)
    sm = re.match(r"-?([A-Z]+)", tail)
    suffix = sm.group(1) if sm else ""
    return prefix, base_digits, suffix

df[["Prefix", "BaseDigits", "Suffix"]] = df["StyleNumber"].apply(
    lambda s: pd.Series(parse_parts(s))
)

# Optional columns
for col in ("ImageURL", "ImageText", "Notes", "Notes2"):
    if col not in df.columns:
        df[col] = ""

# ATTRIBUTE (0–4); default to 1
if "ATTRIBUTE" not in df.columns:
    df["ATTRIBUTE"] = 1
df["ATTRIBUTE"] = pd.to_numeric(df["ATTRIBUTE"], errors="coerce").fillna(1).astype(int)

# ==============================
# Center-size rule lines by ATTRIBUTE (0 = no line)
# ==============================
RULE_TEXT = {
    1: ("Center-size (any shape):\n- Up to 3.00ct: no charge\n- 3.01ct and above: +$800"),
    2: ("Center-size (any shape):\n- Up to 2.50ct: no charge\n- 2.51–3.50ct: +$1,000\n- 3.51–4.50ct: +$1,400\n- 4.51ct and above: +$2,000"),
    3: ("Center-size (any shape):\n- Up to 2.50ct: no charge\n- 2.51–3.50ct: +$1,400\n- 3.51–4.50ct: +$1,800\n- 4.51ct and above: +$2,000"),
    4: ("Center-size (any shape):\n- Up to 2.50ct: no charge\n- 2.51–3.50ct: +$1,400\n- 3.51–4.50ct: +$2,000\n- 4.51ct and above: +$2,600"),
}

# ==============================
# Session (phone -> selection list)
# ==============================
SESSION = {}

def _cleanup_sessions():
    now = time.time()
    for k in list(SESSION.keys()):
        if now - SESSION[k].get("ts", 0) > SESSION_TTL_SECS:
            SESSION.pop(k, None)

def _save_session(phone, styles):
    _cleanup_sessions()
    SESSION[phone] = {"ts": time.time(), "options": styles}

def _get_session(phone):
    _cleanup_sessions()
    return SESSION.get(phone)

# ==============================
# Access/admin/rate-limit/pause/metrics
# ==============================
def _normalize_e164(n: str) -> str:
    if not n:
        return ""
    n = str(n)
    n = (
        n.strip()
         .replace(" ", "")
         .replace("-", "")
         .replace("(", "")
         .replace(")", "")
    )
    if n and not n.startswith("+") and n.isdigit() and len(n) == 10:
        n = "+1" + n
    return n


def _load_set(path: str) -> set:
    if not os.path.exists(path): return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            arr = json.load(f)
        return {_normalize_e164(x) for x in arr if isinstance(x, str) and x.strip()}
    except Exception:
        return set()

def _save_set(path: str, s: set):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(s), f, indent=2)
    except Exception:
        pass

def _load_state():
    if not os.path.exists(STATE_PATH):
        return {"paused": GLOBAL_PAUSED_DEFAULT}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"paused": GLOBAL_PAUSED_DEFAULT}

def _save_state(d: dict):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass

ALLOWLIST = _load_set(ALLOWLIST_PATH)
DENYLIST  = _load_set(DENYLIST_PATH)
STATE     = _load_state()

def is_admin(num: str) -> bool:
    return _normalize_e164(num) in ADMIN_NUMBERS

def is_authorized(num: str) -> bool:
    n = _normalize_e164(num)
    if n in ADMIN_NUMBERS:
        return True
    if n in DENYLIST:
        return False
    if ACCESS_MODE.lower() == "open":
        return True
    return n in ALLOWLIST

# rate limit for unknowns
RL_BUCKET = {}  # { num: [timestamps] }
def record_and_check_rate(num: str) -> bool:
    now = time.time()
    n = _normalize_e164(num)
    L = RL_BUCKET.setdefault(n, [])
    cutoff = now - RL_WINDOW_SECS
    while L and L[0] < cutoff:
        L.pop(0)
    L.append(now)
    return len(L) > RL_MAX_PER_WINDOW

# segment estimate
GSM7_CHARS = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
def is_gsm7(s: str) -> bool:
    try:
        for ch in s:
            if ch in GSM7_CHARS or (0x20 <= ord(ch) <= 0x7E):
                continue
            return False
        return True
    except Exception:
        return False

def estimate_segments(s: str) -> int:
    if not s: return 1
    if is_gsm7(s):
        per = 153 if len(s) > 160 else 160
    else:
        per = 67 if len(s) > 70 else 70
    return max(1, math.ceil(len(s) / per))

def log_event(direction: str, from_num: str, to_num: str, body: str, authorized: bool, route: str):
    try:
        rec = {
            "ts": time.time(),
            "iso": datetime.datetime.utcnow().isoformat() + "Z",
            "dir": direction,
            "from": _normalize_e164(from_num),
            "to": _normalize_e164(to_num),
            "authorized": authorized,
            "route": route,
            "len": len(body or ""),
            "segments": estimate_segments(body or "")
        }
        with open(METRICS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass

def compute_report(hours=24):
    cutoff = time.time() - hours*3600
    inbound = outbound = seg_in = seg_out = 0
    if not os.path.exists(METRICS_PATH):
        return {"inbound":0, "outbound":0, "seg_in":0, "seg_out":0, "cost":0.0}
    with open(METRICS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("ts", 0) < cutoff:
                continue
            if rec.get("dir") == "in":
                inbound += 1
                seg_in  += int(rec.get("segments", 1))
            elif rec.get("dir") == "out":
                outbound += 1
                seg_out  += int(rec.get("segments", 1))
    cost = seg_in*SMS_IN_COST + seg_out*SMS_OUT_COST
    return {"inbound": inbound, "outbound": outbound, "seg_in": seg_in, "seg_out": seg_out, "cost": cost}

def fmt_report(r):
    return (
        f"24h SMS Summary:\n"
        f"- Inbound: {r['inbound']} msgs / {r['seg_in']} seg\n"
        f"- Outbound: {r['outbound']} msgs / {r['seg_out']} seg\n"
        f"- Est. Cost: ${r['cost']:.2f}"
    )

def twilio_client():
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        return None
    try:
        return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception:
        return None

def send_sms(to: str, body: str):
    client = twilio_client()
    if not client or not TWILIO_FROM_NUMBER:
        return False
    try:
        client.messages.create(to=_normalize_e164(to), from_=TWILIO_FROM_NUMBER, body=body)
        return True
    except Exception:
        return False

def handle_admin_command(from_num: str, body: str):
    """
    Admin SMS commands:
      STATUS
      PAUSE / RESUME
      MODE OPEN|CLOSED
      SILENT ON|OFF   (or just 'SILENT' to toggle)
      ADD +1XXXXXXXXXX / REMOVE +1XXXXXXXXXX / LIST
      BLOCK +1XXXXXXXXXX / UNBLOCK +1XXXXXXXXXX / LISTBLOCKED
      REPORT
    Returns text or None if not admin/not a command.
    """
    global ACCESS_MODE, SILENT_REJECT, ALLOWLIST, DENYLIST, STATE

    if not is_admin(from_num):
        return None

    txt = (body or "").strip()
    if not txt: return None
    up = txt.upper()

    def _num(arg): return _normalize_e164(arg)

    if up == "STATUS":
        return f"Status: MODE={ACCESS_MODE.upper()}, SILENT={SILENT_REJECT}, PAUSED={STATE.get('paused', False)}, ALLOWLIST={len(ALLOWLIST)}, DENYLIST={len(DENYLIST)}"
    if up == "PAUSE":
        STATE["paused"] = True; _save_state(STATE)
        return "Bot paused for everyone."
    if up == "RESUME":
        STATE["paused"] = False; _save_state(STATE)
        return "Bot resumed."

    if up.startswith("MODE "):
        mode = up.split(" ", 1)[1].strip().lower()
        if mode not in {"open", "closed"}:
            return "Usage: MODE OPEN|CLOSED"
        ACCESS_MODE = mode
        return f"Mode set to {ACCESS_MODE.upper()}."

    if up == "SILENT":
        SILENT_REJECT = not SILENT_REJECT
        return f"SILENT_REJECT toggled to {SILENT_REJECT}"
    if up.startswith("SILENT "):
        val = up.split(" ", 1)[1].strip().upper()
        if val not in {"ON", "OFF"}:
            return "Usage: SILENT ON|OFF"
        SILENT_REJECT = (val == "ON")
        return f"SILENT_REJECT = {SILENT_REJECT}"

    if up == "LIST":
        return "Allow-list:\n" + ("\n".join(sorted(ALLOWLIST)) if ALLOWLIST else "(empty)")
    if up == "LISTBLOCKED":
        return "Deny-list:\n" + ("\n".join(sorted(DENYLIST)) if DENYLIST else "(empty)")
    if up.startswith("ADD "):
        num = _num(txt[4:])
        if not num: return "Usage: ADD +1XXXXXXXXXX"
        ALLOWLIST.add(num); _save_set(ALLOWLIST_PATH, ALLOWLIST)
        # Also remove from denylist if present
        if num in DENYLIST:
            DENYLIST.discard(num); _save_set(DENYLIST_PATH, DENYLIST)
        return f"Added {num}. Allow-list size: {len(ALLOWLIST)}"
    if up.startswith("BULKADD "):
        nums_text = txt[8:]
        nums_list = [_num(n.strip()) for n in nums_text.split(",")]
        nums_list = [n for n in nums_list if n]
        if not nums_list: return "Usage: BULKADD +1XXXXXXXXXX,+1XXXXXXXXXX,..."
        added_count = 0
        for num in nums_list:
            ALLOWLIST.add(num)
            if num in DENYLIST:
                DENYLIST.discard(num)
            added_count += 1
        _save_set(ALLOWLIST_PATH, ALLOWLIST)
        _save_set(DENYLIST_PATH, DENYLIST)
        return f"Added {added_count} number(s). Allow-list size: {len(ALLOWLIST)}"
    if up.startswith("REMOVE "):
        num = _num(txt[7:])
        if not num: return "Usage: REMOVE +1XXXXXXXXXX"
        existed = num in ALLOWLIST
        ALLOWLIST.discard(num); _save_set(ALLOWLIST_PATH, ALLOWLIST)
        return f"{'Removed' if existed else 'Not present:'} {num}. Allow-list size: {len(ALLOWLIST)}"

    if up.startswith("BLOCK "):
        num = _num(txt[6:])
        if not num: return "Usage: BLOCK +1XXXXXXXXXX"
        DENYLIST.add(num); _save_set(DENYLIST_PATH, DENYLIST)
        return f"Blocked {num}."
    if up.startswith("UNBLOCK "):
        num = _num(txt[8:])
        if not num: return "Usage: UNBLOCK +1XXXXXXXXXX"
        DENYLIST.discard(num); _save_set(DENYLIST_PATH, DENYLIST)
        return f"Unblocked {num}."

    if up == "REPORT":
        r = compute_report(24)
        msg = fmt_report(r)
        try:
            send_sms(ADMIN_REPORT_NUMBER, msg)
        except Exception:
            pass
        return msg

    return None

# ==============================
# Helpers (pricing UX)
# ==============================
def first_image_for_group(group_df):
    for _, r in group_df.iterrows():
        url = str(r.get("ImageURL", "")).strip()
        if url and url.lower() != "nan" and (url.startswith("http://") or url.startswith("https://")):
            return url, str(r.get("ImageText", "")).strip()
    return None, None

def _first_nonempty_note(group_df):
    for _, r in group_df.iterrows():
        note = str(r.get("Notes", "")).strip()
        if note and note.lower() != "nan":
            return note
    return ""

def _first_nonempty_note2(group_df):
    for _, r in group_df.iterrows():
        note2 = str(r.get("Notes2", "")).strip()
        if note2 and note2.lower() != "nan":
            return note2
    return ""

def format_response(matches_df, include_link_line=False):
    """
    Block layout:
      header
      [blank]
      - METAL: $price
      ...
      [blank]
      center-size rules (if any)
      [blank]
      Note: ...
      [blank]
      Notes2 content (no label)
      [blank]
      promo footer
    """
    blocks = []
    for style, group in matches_df.groupby("StyleNumber"):
        lines = [f"Pricing for {style}:", ""]

        # metals
        for _, r in group.iterrows():
            lines.append(f"- {r['METAL']}: ${int(r['Price']):,}")
        lines.append("")

        # image link (optional)
        if include_link_line:
            img_url, img_text = first_image_for_group(group)
            if img_url:
                pretty = img_text if img_text else f"{style} image"
                lines.append(f"{pretty}: {img_url}")
                lines.append("")

        # rules
        attr = int(group.iloc[0].get("ATTRIBUTE", 0))
        if attr in RULE_TEXT:
            lines.append(RULE_TEXT[attr])
            lines.append("")

        # notes
        note = _first_nonempty_note(group)
        if note:
            lines.append(f"Note: {note}")
            lines.append("")

        # notes2
        note2 = _first_nonempty_note2(group)
        if note2:
            lines.append(note2)
            lines.append("")

      
        # trim trailing blanks
        while lines and lines[-1] == "":
            lines.pop()
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)

def build_refine_prompt(display_query, style_list):
    shown = style_list[:MAX_LISTED_STYLES]
    lines = [f'Multiple matches for "{display_query}", please see list below:']
    for i, s in enumerate(shown, 1):
        lines.append(f"{i}. {s}")
    extra = len(style_list) - len(shown)
    if extra > 0:
        lines.append(f"(+{extra} more)")
    example = shown[0] if shown else display_query
    lines.append("")
    lines.append(
        f'Reply with a number from the list '
        f'(Example: Write 1 in the text box to view information for "{example}") '
        f'or a full style # (e.g., "{example}").'
    )
    return "\n".join(lines)

def get_exact_matches(q):
    qnorm = _normalize(q)
    return df[df["Normalized"] == qnorm]

def _normalize_digits_series(series):
    return series.astype(str).str.lstrip("0").replace({"": "0"})

def list_by_base_and_suffix(digits: str, suffix_q: str):
    if not digits:
        return []
    q = (digits or "").lstrip("0") or "0"
    base_col_norm = _normalize_digits_series(df["BaseDigits"])
    if suffix_q:
        m = df[(base_col_norm == q) & (df["Suffix"] == suffix_q)]
    else:
        m = df[base_col_norm == q]
    if m.empty:
        return []
    return sorted(m["StyleNumber"].drop_duplicates().tolist())

def list_by_normalized_prefix(prefix_text: str):
    norm = _normalize(prefix_text)
    if not norm:
        return []
    m = df[df["Normalized"].str.startswith(norm)]
    if m.empty:
        return []
    return sorted(m["StyleNumber"].drop_duplicates().tolist())

def smart_route(user_text: str):
    """
    Order:
      1) exact (normalized)
      2) startswith (normalized)
         - if 1 style -> exact
         - if >1 -> refine
      3) base+optional suffix list (if one, exact; else refine)
      4) normalized contains (if one, exact; else refine)
      5) fuzzy
    """
    raw = (user_text or "").strip().upper()
    if not raw:
        return ("invalid_partial", None)

    # exact
    exact = get_exact_matches(raw)
    if not exact.empty:
        return ("exact", exact)

    # short digit guard
    if re.fullmatch(r"\d{1,2}", raw):
        return ("invalid_partial", None)

    # require base (3–5 digits) for narrow paths
    dm = re.search(r"(\d{3,5})", raw)
    has_base3 = bool(dm)

    # prefix startswith
    if has_base3:
        prefix_styles = list_by_normalized_prefix(raw)
        if prefix_styles:
            if len(prefix_styles) == 1:
                one = prefix_styles[0]
                m = get_exact_matches(one)
                if m.empty:
                    m = df[df["StyleNumber"] == one]
                return ("exact", m if not m.empty else df[df["StyleNumber"] == one])
            return ("refine", (raw, prefix_styles))

    # base + suffix
    if has_base3:
        base_digits = dm.group(1)
        tail = raw[dm.end():]
        sm = re.match(r"-?([A-Z]+)", tail)
        suffix_q = sm.group(1) if sm else ""
        base_styles = list_by_base_and_suffix(base_digits, suffix_q or "")
        if base_styles:
            if len(base_styles) == 1:
                one = base_styles[0]
                m = get_exact_matches(one)
                if m.empty:
                    m = df[df["StyleNumber"] == one]
                return ("exact", m if not m.empty else df[df["StyleNumber"] == one])
            return ("refine", (raw, base_styles))

    # contains
    norm = _normalize(raw)
    partial = df[df["Normalized"].str.contains(norm)]
    if not partial.empty:
        unique = sorted(partial["StyleNumber"].drop_duplicates().tolist())
        if len(unique) == 1:
            one = unique[0]
            m = get_exact_matches(one)
            if m.empty:
                m = df[df["StyleNumber"] == one]
            return ("exact", m if not m.empty else df[df["StyleNumber"] == one])
        return ("refine", (raw, unique))

    # fuzzy
    picked = process.extractOne(norm, df["Normalized"].unique())
    if picked:
        choice, score, _ = picked
        if score > 70:
            return ("closest", df[df["Normalized"] == choice])

    return ("invalid_partial", None)

# ==============================
# Flask app
# ==============================
app = Flask(__name__)

@app.get("/")
def health():
    return "ok"

@app.get("/cron/daily_report")
def cron_daily_report():
    key = request.args.get("key", "")
    if key != CRON_SECRET:
        abort(403)
    r = compute_report(24)
    msg = fmt_report(r)
    send_sms(ADMIN_REPORT_NUMBER, msg)
    return "sent"

@app.post("/sms")
def sms_reply():
    incoming = (request.form.get("Body") or "").strip()
    from_num = request.form.get("From", "")
    to_num   = request.form.get("To", "")

    # log inbound
    log_event("in", from_num, to_num, incoming, is_authorized(from_num), "inbound_raw")

    resp = MessagingResponse()

    # carrier keywords (Twilio handles)
    OPT_KEYWORDS = {"STOP","STOPALL","UNSUBSCRIBE","CANCEL","END","QUIT","START","YES","UNSTOP"}
    if incoming.upper() in OPT_KEYWORDS:
        return str(resp)

    # admin commands (always)
    admin_reply = handle_admin_command(from_num, incoming)
    if admin_reply is not None:
        m = resp.message(admin_reply)
        log_event("out", to_num, from_num, admin_reply, True, "admin")
        return str(resp)

    # paused
    if STATE.get("paused", False):
        return str(resp)

    # access control - REMOVED AUTO-DENYLIST
    if not is_authorized(from_num):
        # Just track rate limit but don't auto-block
        record_and_check_rate(from_num)
        if SILENT_REJECT:
            return str(resp)
        denial = "This number is not authorized to use the Verragio Pricing Bot. Please contact a Verragio representative to be added or re-added."
        resp.message(denial)
        log_event("out", to_num, from_num, denial, False, "deny")
        return str(resp)

    # mid-refine?
    sess = _get_session(from_num)
    if sess and sess.get("options"):
        opts = sess["options"]
        sel = incoming.strip().upper()
        if sel.isdigit():
            idx = int(sel) - 1
            if 0 <= idx < len(opts):
                chosen = opts[idx]
                match = get_exact_matches(chosen)
                SESSION.pop(from_num, None)
                if match.empty:
                    match = df[df["StyleNumber"] == chosen]
                if not match.empty:
                    text = format_response(match, include_link_line=FALLBACK_LINK_IN_TEXT)
                    m = resp.message(text)
                    log_event("out", to_num, from_num, text, True, "exact_from_refine")
                    if USE_MMS:
                        img_url, _ = first_image_for_group(match)
                        if img_url: m.media(img_url)
                    return str(resp)
        # try as full SKU
        match = get_exact_matches(sel)
        if match.empty:
            match = df[df["StyleNumber"] == sel]
        if not match.empty:
            SESSION.pop(from_num, None)
            text = format_response(match, include_link_line=FALLBACK_LINK_IN_TEXT)
            m = resp.message(text)
            log_event("out", to_num, from_num, text, True, "exact_fullsku")
            if USE_MMS:
                img_url, _ = first_image_for_group(match)
                if img_url: m.media(img_url)
            return str(resp)
        # otherwise fresh
        SESSION.pop(from_num, None)

    # fresh routing
    kind, payload = smart_route(incoming)

    if kind == "invalid_partial":
        text = "Please enter a partial style number with at least 3 digits or a complete style # (e.g., 992, V-992, V-992R, V-992R-1.3)."
        resp.message(text)
        log_event("out", to_num, from_num, text, True, "invalid")
        return str(resp)

    if kind == "refine":
        display, styles = payload
        if len(styles) == 1:
            chosen = styles[0]
            match = get_exact_matches(chosen)
            if match.empty:
                match = df[df["StyleNumber"] == chosen]
            text = format_response(match, include_link_line=FALLBACK_LINK_IN_TEXT)
            m = resp.message(text)
            log_event("out", to_num, from_num, text, True, "exact_single_refine")
            if USE_MMS:
                img_url, _ = first_image_for_group(match)
                if img_url: m.media(img_url)
            return str(resp)

        _save_session(from_num, styles)
        text = build_refine_prompt(display, styles)
        resp.message(text)
        log_event("out", to_num, from_num, text, True, "refine")
        return str(resp)

    if kind == "exact":
        text = format_response(payload, include_link_line=FALLBACK_LINK_IN_TEXT)
        m = resp.message(text)
        log_event("out", to_num, from_num, text, True, "exact")
        if USE_MMS:
            img_url, _ = first_image_for_group(payload)
            if img_url: m.media(img_url)
        return str(resp)

    if kind == "closest":
        text = "Closest match:\n\n" + format_response(payload, include_link_line=FALLBACK_LINK_IN_TEXT)
        m = resp.message(text)
        log_event("out", to_num, from_num, text, True, "closest")
        if USE_MMS:
            img_url, _ = first_image_for_group(payload)
            if img_url: m.media(img_url)
        return str(resp)

    # fallback
    text = "Please enter a partial style number with at least 3 digits or a complete style # (e.g., 992, V-992, V-992R, V-992R-1.3)."
    resp.message(text)
    log_event("out", to_num, from_num, text, True, "invalid_fallback")
    return str(resp)

# ==============================
# CLI (local testing)
# ==============================
def run_cli():
    print("=== Verragio Price Bot (CLI) ===")
    print("Type a full style (e.g., V-992R-1.3) or base/suffix like '992', 'V-992', '992R', '954CU', 'ENG-0489OV'. 'exit' to quit.\n")
    pending = None
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break
        if q.lower() in ("exit", "quit"):
            print("Goodbye.")
            break

        if pending:
            styles = pending
            sel = q.upper()
            if sel.isdigit():
                idx = int(sel) - 1
                if 0 <= idx < len(styles):
                    chosen = styles[idx]
                    m = get_exact_matches(chosen)
                    if m.empty:
                        m = df[df["StyleNumber"] == chosen]
                    if not m.empty:
                        print("\n" + format_response(m, include_link_line=FALLBACK_LINK_IN_TEXT) + "\n")
                        pending = None
                        continue
            m = get_exact_matches(sel)
            if m.empty:
                m = df[df["StyleNumber"] == sel]
            if not m.empty:
                print("\n" + format_response(m, include_link_line=FALLBACK_LINK_IN_TEXT) + "\n")
                pending = None
                continue
            pending = None  # fall back to fresh

        kind, payload = smart_route(q)

        if kind == "invalid_partial":
            print("\nPlease enter a partial style number with at least 3 digits or a complete style # (e.g., 992, V-992, V-992R, V-992R-1.3).\n")
            continue

        if kind == "refine":
            display, styles = payload
            if len(styles) == 1:
                chosen = styles[0]
                m = get_exact_matches(chosen)
                if m.empty:
                    m = df[df["StyleNumber"] == chosen]
                print("\n" + format_response(m, include_link_line=FALLBACK_LINK_IN_TEXT) + "\n")
                continue
            pending = styles
            print("\n" + build_refine_prompt(display, styles) + "\n")
            continue

        if kind == "exact":
            print("\n" + format_response(payload, include_link_line=FALLBACK_LINK_IN_TEXT) + "\n")
            continue

        if kind == "closest":
            print("\nClosest match:\n\n" + format_response(payload, include_link_line=FALLBACK_LINK_IN_TEXT) + "\n")
            continue

        print("\nPlease enter a partial style number with at least 3 digits or a complete style # (e.g., 992, V-992, V-992R, V-992R-1.3).\n")

# ==============================
# Entrypoint
# ==============================
if __name__ == "__main__":
    import sys
    if "--cli" in sys.argv:
        run_cli()
    else:
        app.run(host="0.0.0.0", port=5000, debug=True)

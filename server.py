# server.py
# Verragio SMS Price Bot — Render-ready

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import pandas as pd
from rapidfuzz import process
import re, os, time

# ============================================
# Config (safe defaults for testing on Render)
# ============================================
ADMIN_NUMBERS = {"+19173555884"}  # <-- your iPhone in E.164 format
ACCESS_MODE = "open"              # "open" or "closed"; open = reply to anyone
SILENT_REJECT = False             # when closed, reply with a denial instead of silence
USE_MMS = False                   # send media urls (only if you host images)
FALLBACK_LINK_IN_TEXT = True      # include image link as a clickable URL
MAX_LISTED_STYLES = 10            # show up to this many options in a list
PROMO_FOOTER = "Additional info: Platinum Promotion – Platinum same price as 18KT."

# Global pause switch (kept in-memory; reset on deploy)
BOT_PAUSED = False

# Ephemeral per-number sessions for “choose a number from the list”
SESSIONS = {}  # phone -> {"ts": float, "options": [style, ...]}
SESSION_TTL = 10 * 60  # 10 minutes


# ============================================
# Load & prepare data
# ============================================
XLSX_PATH = os.environ.get("PRICE_SHEET", "style_prices.xlsx")
df = pd.read_excel(XLSX_PATH)
df.columns = df.columns.str.strip()
df.rename(columns={"PRICE": "Price"}, inplace=True)

# Required columns
df["StyleNumber"] = df["StyleNumber"].astype(str).str.upper().str.strip()

df["Price"] = (
    df["Price"]
      .astype(str)
      .str.replace(r"[\$,]", "", regex=True)
      .astype(float)
)

# Normalization for exact matching (remove hyphens/spaces; keep dots)
df["Normalized"] = (
    df["StyleNumber"]
      .str.replace("-", "", regex=False)
      .str.replace(" ", "", regex=False)
)

# Optional columns
for col in ("ImageURL", "ImageText", "Notes"):
    if col not in df.columns:
        df[col] = ""

# ATTRIBUTE (0–4); default 1
if "ATTRIBUTE" not in df.columns:
    df["ATTRIBUTE"] = 1
df["ATTRIBUTE"] = pd.to_numeric(df["ATTRIBUTE"], errors="coerce").fillna(1).astype(int)

# Parse Prefix / BaseDigits / Suffix once for fast filtering
def _parse_parts(style: str):
    s = str(style).upper().strip()
    dm = re.search(r"(\d{3,5})", s)  # first 3–5 digit group = base
    if not dm:
        return "", "", ""
    base_digits = dm.group(1)
    pm = re.match(r"^\s*([A-Z]+)", s)   # prefix at start
    prefix = pm.group(1) if pm else ""
    tail = s[dm.end():]                 # suffix right after base (hyphen optional)
    sm = re.match(r"-?([A-Z]+)", tail)
    suffix = sm.group(1) if sm else ""
    return prefix, base_digits, suffix

df[["Prefix", "BaseDigits", "Suffix"]] = df["StyleNumber"].apply(
    lambda s: pd.Series(_parse_parts(s))
)


# ============================================
# Rules text by ATTRIBUTE
# ============================================
RULE_TEXT = {
    1: (
        "Center-size (any shape):\n"
        "- Up to 3.00ct: no charge\n"
        "- 3.01ct and above: +$600"
    ),
    2: (
        "Center-size (any shape):\n"
        "- Up to 2.50ct: no charge\n"
        "- 2.51–3.50ct: +$800\n"
        "- 3.51–4.50ct: +$1,200\n"
        "- 4.51ct and above: +$1,800"
    ),
    3: (
        "Center-size (any shape):\n"
        "- Up to 2.50ct: no charge\n"
        "- 2.51–3.50ct: +$1,200\n"
        "- 3.51–4.50ct: +$1,600\n"
        "- 4.51ct and above: +$1,800"
    ),
    4: (
        "Center-size (any shape):\n"
        "- Up to 2.50ct: no charge\n"
        "- 2.51–3.50ct: +$1,200\n"
        "- 3.51–4.50ct: +$1,800\n"
        "- 4.51ct and above: +$2,400"
    ),
}


# ============================================
# Helpers
# ============================================
def _normalize_num(n: str) -> str:
    """Normalize to +E.164-ish for matching admin numbers."""
    if not n:
        return ""
    n = n.strip().replace(" ", "").replace("(", "").replace(")", "").replace("-", "")
    if n.startswith("+"):
        return n
    if n.isdigit() and len(n) == 10:
        return "+1" + n
    return n

def _cleanup_sessions():
    now = time.time()
    for k in list(SESSIONS.keys()):
        if now - SESSIONS[k]["ts"] > SESSION_TTL:
            SESSIONS.pop(k, None)

def _save_session(phone, styles):
    _cleanup_sessions()
    SESSIONS[_normalize_num(phone)] = {"ts": time.time(), "options": styles}

def _get_session(phone):
    _cleanup_sessions()
    return SESSIONS.get(_normalize_num(phone))

def first_image_for_group(group_df):
    for _, r in group_df.iterrows():
        url = str(r.get("ImageURL", "")).strip()
        if url and url.lower() != "nan" and (url.startswith("http://") or url.startswith("https://")):
            label = str(r.get("ImageText", "")).strip()
            return url, label
    return None, None

def first_note_for_group(group_df):
    for _, r in group_df.iterrows():
        note = str(r.get("Notes", "")).strip()
        if note and note.lower() != "nan":
            return note
    return ""

def format_response(matches_df, include_link_line=False):
    """
    Final pricing block(s) with your requested spacing:
    - Header
    - blank line
    - metal lines
    - blank line
    - center-size rules (if any)
    - blank line
    - Note (if any)
    - blank line
    - Additional info (promo)
    """
    blocks = []
    for style, group in matches_df.groupby("StyleNumber"):
        lines = [f"Pricing for {style}:", ""]

        # Metals & prices
        for _, r in group.iterrows():
            lines.append(f"- {r['METAL']}: ${int(r['Price']):,}")
        lines.append("")

        # Optional image link line
        if include_link_line:
            img_url, img_text = first_image_for_group(group)
            if img_url:
                pretty = img_text if img_text else f"{style} image"
                lines.append(f"{pretty}: {img_url}")
                lines.append("")

        # Center-size rules
        attr = int(group.iloc[0].get("ATTRIBUTE", 0))
        if attr in RULE_TEXT:
            lines.append(RULE_TEXT[attr])
            lines.append("")

        # Notes
        note = first_note_for_group(group)
        if note:
            lines.append(f"Note: {note}")
            lines.append("")

        # Promo
        if PROMO_FOOTER:
            lines.append(PROMO_FOOTER)

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
    lines.append("")
    example = shown[0] if shown else display_query
    lines.append(
        f'Reply with a number from the list '
        f'(Example: Write 1 in the text box to view information for "{example}") '
        f'or a full style # (e.g., "{example}").'
    )
    return "\n".join(lines)

def get_exact_matches(q):
    qnorm = q.strip().upper().replace("-", "").replace(" ", "")
    return df[df["Normalized"] == qnorm]

def parse_user_base_and_suffix(user_text: str):
    s = user_text.upper().replace(" ", "")
    dm = re.search(r"(\d{3,5})", s)
    if not dm:
        return None, None
    digits = dm.group(1)
    tail = s[dm.end():]
    sm = re.match(r"-?([A-Z]+)$", tail)  # letters to end, optional hyphen
    suffix_q = sm.group(1) if sm else ""
    return digits, suffix_q

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

def smart_route(user_text: str):
    """
    Decide how to handle the text:
      - exact match
      - base digits (+ optional suffix) -> list
      - partial contains -> list
      - fuzzy closest
      - invalid
    """
    raw = (user_text or "").strip().upper()
    if not raw:
        return ("invalid_partial", None)

    # exact
    exact = get_exact_matches(raw)
    if not exact.empty:
        return ("exact", exact)

    # very short numbers like "2"
    if re.fullmatch(r"\d{1,2}", raw):
        return ("invalid_partial", None)

    # base + optional suffix
    digits, suffix_q = parse_user_base_and_suffix(raw)
    if digits:
        styles = list_by_base_and_suffix(digits, suffix_q or "")
        if styles:
            return ("refine", (raw, styles))
        if suffix_q:
            base_styles = list_by_base_and_suffix(digits, "")
            if base_styles:
                return ("refine", (digits, base_styles))

    # partial normalized contains
    norm = raw.replace("-", "").replace(" ", "")
    partial_all = df[df["Normalized"].str.contains(norm)]
    if not partial_all.empty:
        unique_styles = sorted(partial_all["StyleNumber"].drop_duplicates().tolist())
        return ("refine", (raw, unique_styles))

    # fuzzy fallback
    picked = process.extractOne(norm, df["Normalized"].unique())
    if picked:
        choice, score, _ = picked
        if score > 70:
            return ("closest", df[df["Normalized"] == choice])

    return ("invalid_partial", None)


# ============================================
# Admin commands
# ============================================
def is_admin(num: str) -> bool:
    return _normalize_num(num) in ADMIN_NUMBERS

def handle_admin_command(from_num: str, body: str):
    global BOT_PAUSED, ACCESS_MODE, SILENT_REJECT

    txt = (body or "").strip()
    up = txt.upper()

    if up == "STATUS":
        return f"Status: MODE={ACCESS_MODE.upper()}, SILENT={SILENT_REJECT}, PAUSED={BOT_PAUSED}"

    if not is_admin(from_num):
        return None

    if up == "PAUSE":
        BOT_PAUSED = True
        return "Bot paused."
    if up == "RESUME":
        BOT_PAUSED = False
        return "Bot resumed."
    if up.startswith("MODE "):
        mode = up.split(" ", 1)[1].strip().lower()
        if mode in {"open", "closed"}:
            ACCESS_MODE = mode
            return f"Mode set to {ACCESS_MODE.upper()}."
        return "Usage: MODE OPEN|CLOSED"

    return None


# ============================================
# Flask app
# ============================================
app = Flask(__name__)

@app.get("/")
def root():
    return "ok"

@app.get("/healthz")
def healthz():
    return "OK", 200

@app.post("/sms")
def sms_reply():
    global BOT_PAUSED

    incoming = (request.form.get("Body") or "").strip()
    from_num = request.form.get("From", "")
    # to_num   = request.form.get("To", "")

    resp = MessagingResponse()

    # Twilio STOP/START handled by Twilio; we don't respond
    OPT = {"STOP","STOPALL","UNSUBSCRIBE","CANCEL","END","QUIT","START","YES","UNSTOP"}
    if incoming.upper() in OPT:
        return str(resp)

    # Admin/utility commands (STATUS is allowed for all; PAUSE/RESUME require admin)
    admin_reply = handle_admin_command(from_num, incoming)
    if admin_reply is not None:
        resp.message(admin_reply)
        return str(resp)

    # Global pause (freeze replies for everyone)
    if BOT_PAUSED:
        # Silent during pause
        return str(resp)

    # Access control
    if ACCESS_MODE == "closed" and not is_admin(from_num):
        if SILENT_REJECT:
            return str(resp)
        resp.message("This number is not authorized to use this bot.")
        return str(resp)

    # If user had a pending list and they reply with a number, resolve it
    pending = _get_session(from_num)
    if pending and incoming.isdigit():
        idx = int(incoming) - 1
        options = pending["options"]
        if 0 <= idx < len(options):
            chosen = options[idx].upper()
            exact = get_exact_matches(chosen)
            if not exact.empty:
                text = format_response(exact, include_link_line=FALLBACK_LINK_IN_TEXT)
                resp.message(text)
                return str(resp)
        # fall through (treat as fresh query)

    # Route query
    kind, payload = smart_route(incoming)

    if kind == "invalid_partial":
        resp.message(
            "Please enter a partial style number with at least 3 digits or a complete style # "
            '(e.g., 992, V-992, V-992R, V-992R-1.3).'
        )
        return str(resp)

    if kind == "refine":
        display, styles = payload
        _save_session(from_num, styles)
        resp.message(build_refine_prompt(display, styles))
        return str(resp)

    if kind == "exact":
        resp.message(format_response(payload, include_link_line=FALLBACK_LINK_IN_TEXT))
        return str(resp)

    if kind == "closest":
        resp.message("Closest match:\n\n" + format_response(payload, include_link_line=FALLBACK_LINK_IN_TEXT))
        return str(resp)

    # Fallback
    resp.message(
        "Please enter a partial style number with at least 3 digits or a complete style # "
        '(e.g., 992, V-992, V-992R, V-992R-1.3).'
    )
    return str(resp)


# ============================================
# Local dev
# ============================================
if __name__ == "__main__":
    # For local testing only; Render uses gunicorn
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)

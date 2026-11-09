"""Goods Classification Memo Manager: Streamlit UI (Arabic, right-to-left).

    pip install -r requirements.txt
    python memo_core.py create-user <username> admin     # recommended; otherwise the first visitor creates the admin
    streamlit run app.py

Environment variables
    MEMO_DB_PATH   database file (default: ./memos.db)
    MEMO_ENC_KEY   optional Fernet key; encrypts goods description + notes + memo files at rest. Generate one with
                   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

All user-facing text is Arabic. Streamlit has no RTL switch, so the layout is mirrored with the CSS in RTL_CSS;
STYLE_CSS and .streamlit/config.toml define the visual style.
"""
import inspect
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import pandas as pd
import streamlit as st

import memo_core as mc

st.set_page_config(page_title="مذكرات تصنيف البضائع", page_icon="🗂️", layout="wide")

# Right-to-left layout. Codes, memo numbers and file names stay left-to-right inside Arabic sentences,
# inputs follow the direction of what is typed, and the data grid keeps its own (left-to-right) layout.
# Right-to-left layout. Codes, memo numbers and file names stay left-to-right inside Arabic sentences,
# inputs follow the direction of what is typed, and the data grid keeps its own (left-to-right) layout.
RTL_CSS = """<style>
/* Target the app viewport block containers directly to enforce RTL without breaking cloud framing markup */
[data-testid="stAppViewBlockContainer"], [data-testid="stMainBlockContainer"], [data-testid="stHeader"] { 
    direction: rtl; 
}
code, .stApp code { direction: ltr; unicode-bidi: isolate; }
input, textarea { unicode-bidi: plaintext; }
[data-testid="stDataFrame"] { direction: ltr; }

/* Physically pin the primary sidebar container over to the right frame border */
[data-testid="stSidebar"] {
    left: auto !important;
    right: 0 !important;
}

/* Enforce the expanding slider animation state to clip outwards cleanly toward the right edge */
[data-testid="stSidebar"][aria-expanded="false"] { 
    transform: translateX(100%) !important; 
}

/* Push the main container content block to clear spacing room for the 21rem (approx 336px) right sidebar */
@media (min-width: 768px) {
    [data-testid="stAppViewContainer"] {
        display: flex;
        flex-direction: row-reverse !important;
    }
    .main .block-container, [data-testid="stMainBlockContainer"] {
        margin-right: 0 !important;
        margin-left: auto !important;
    }
}

/* Mirror the toggle button icons so the directional chevrons point accurately */
[data-testid="stSidebarCollapseButton"] button, button[data-testid="stSidebarCollapseButton"],
[data-testid="stExpandSidebarButton"] button, button[data-testid="stExpandSidebarButton"],
[data-testid="stSidebarCollapsedControl"] button, [data-testid="collapsedControl"] button,
button[aria-label="Close sidebar"], button[aria-label="Open sidebar"] { 
    transform: scaleX(-1); 
}

/* Align the closed sidebar overlay floating anchor button directly to the right sidebar edge */
[data-testid="stSidebarCollapsedControl"], [data-testid="collapsedControl"] { 
    left: auto !important; 
    right: 0.5rem !important; 
}
</style>"""


# The look (palette, cards, sidebar menu, buttons, fields) comes from the reference photos.
# Change a color once in the :root variables; .streamlit/config.toml carries the matching Streamlit theme.
STYLE_CSS = """<style>
:root {
  --navy: #0F304F; --blue: #0A5188; --link: #0B57A8; --ink: #10243E; --text: #223348; --muted: #64748B;
  --line: #E3E8EF; --field: #D5DDE7; --page: #F6F8FB;
  --shadow: 0 1px 2px rgba(15, 48, 79, .06), 0 4px 14px rgba(15, 48, 79, .05);
  --font: "IBM Plex Sans Arabic", "Tajawal", "Cairo", "Segoe UI", Tahoma, "Noto Sans Arabic", system-ui, sans-serif;
}
/* Page and typography: light grey-blue page, white cards, dark navy headings. */
.stApp { background: var(--page); color: var(--text); }
.stApp [data-testid="stAppViewContainer"], .stApp [data-testid="stMain"] { background: transparent; }
.stApp :is(div, p, label, li, a, small, strong, em, h1, h2, h3, h4, h5, h6, th, td, summary, input, textarea, button) {
  font-family: var(--font); letter-spacing: 0; }
.stApp :is(p, li) { line-height: 1.8; }
.stApp :is(h1, h2, h3, h4) { color: var(--ink); font-weight: 700; line-height: 1.4; }
.stApp h1 { font-size: 1.9rem; }
.stApp h3 { font-size: 1.45rem; }
.stApp a { color: var(--link); }
.stApp hr { border-color: var(--line); }
.stApp [data-testid="stCaptionContainer"] { color: var(--muted); }
.stApp [data-testid="stText"] { font-family: var(--font) !important; white-space: pre-wrap; color: var(--text); }
.stApp code { background: #EAF1FA; color: var(--navy); border-radius: 6px; padding: .08rem .4rem; font-size: .92em;
  font-family: var(--font); font-variant-numeric: tabular-nums; }
.stApp .block-container, .stApp [data-testid="stMainBlockContainer"] { padding: 5rem 2.5rem 4rem; max-width: 100%; }
::selection { background: rgba(10, 81, 136, .2); }
/* Top bar: white with a hairline, like the header in the photos. The sidebar stays above it. */
.stApp [data-testid="stHeader"] { background: #FFFFFF !important; border-bottom: 1px solid var(--line); }
/* Sidebar: dark navy panel with a menu list. */
.stApp [data-testid="stSidebar"] { z-index: 1000001; }
.stApp [data-testid="stSidebar"], .stApp [data-testid="stSidebar"] > div, .stApp [data-testid="stSidebarContent"] {
  background: var(--navy) !important; }
.stApp [data-testid="stSidebar"] :is(p, span, label, li, h1, h2, h3, h4, summary, small, strong):not([data-testid="stAlert"] *) {
  color: #FFFFFF !important; }
.stApp [data-testid="stSidebar"] h3 { font-size: 1.2rem; }
.stApp [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { color: #D3DEEA !important; }
.stApp [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h3 { color: #FFFFFF !important; }
.stApp [data-testid="stSidebar"] [role="radiogroup"] { gap: .2rem; }
.stApp [data-testid="stSidebar"] label[data-baseweb="radio"] { width: 100%; margin: 0; padding: .7rem .9rem;
  border-radius: 10px; cursor: pointer; transition: background .15s; }
.stApp [data-testid="stSidebar"] label[data-baseweb="radio"] > div:first-of-type:not(:has(p)) { display: none; }
.stApp [data-testid="stSidebar"] label[data-baseweb="radio"] p { color: #FFFFFF !important; font-weight: 500; font-size: 1rem; filter: grayscale(1) brightness(1.6); }
.stApp [data-testid="stSidebar"] label[data-baseweb="radio"]:hover { background: rgba(255, 255, 255, .09); }
.stApp [data-testid="stSidebar"] label[data-baseweb="radio"]:has(input:checked),
.stApp [data-testid="stSidebar"] label[data-baseweb="radio"][aria-checked="true"] { background: var(--blue); }
.stApp [data-testid="stSidebar"] label[data-baseweb="radio"]:has(input:checked) p { font-weight: 700; }
.stApp [data-testid="stSidebar"] [data-testid="stForm"] { background: transparent; border: none; box-shadow: none; padding: 0; }
.stApp [data-testid="stSidebar"] [data-testid="stExpander"] details { background: rgba(255, 255, 255, .06);
  border: 1px solid rgba(255, 255, 255, .18); box-shadow: none; }
/* Buttons: navy primary, white outlined secondary. */
.stApp :is([data-testid^="stBaseButton-primary"], [data-testid^="stBaseButton-secondary"], button[kind="primary"], button[kind="secondary"],
  button[kind="primaryFormSubmit"], button[kind="secondaryFormSubmit"]) {
  border-radius: 8px; min-height: 2.6rem; padding: .4rem 1.2rem; font-weight: 600; box-shadow: none; transition: background .15s, border-color .15s; }
.stApp :is([data-testid^="stBaseButton-primary"], button[kind="primary"], button[kind="primaryFormSubmit"]) {
  background: var(--navy) !important; border: 1px solid var(--navy) !important; color: #FFFFFF !important; }
.stApp :is([data-testid^="stBaseButton-primary"], button[kind="primary"], button[kind="primaryFormSubmit"]):hover:not(:disabled) {
  background: #0B2740 !important; border-color: #0B2740 !important; }
.stApp :is([data-testid^="stBaseButton-secondary"], button[kind="secondary"], button[kind="secondaryFormSubmit"]) {
  background: #FFFFFF !important; border: 1px solid var(--field) !important; color: var(--ink) !important; }
.stApp :is([data-testid^="stBaseButton-secondary"], button[kind="secondary"], button[kind="secondaryFormSubmit"]):hover:not(:disabled) {
  background: #F4F7FB !important; border-color: #B7C4D4 !important; }
.stApp :is([data-testid^="stBaseButton-primary"], [data-testid^="stBaseButton-secondary"]) p { color: inherit !important; font-weight: 600; }
.stApp [data-testid="stSidebar"] :is([data-testid^="stBaseButton-secondary"], button[kind="secondary"], button[kind="secondaryFormSubmit"]) {
  background: transparent !important; border: 1px solid rgba(255, 255, 255, .4) !important; color: #FFFFFF !important; }
.stApp [data-testid="stSidebar"] :is([data-testid^="stBaseButton-secondary"], button[kind="secondary"], button[kind="secondaryFormSubmit"]):hover:not(:disabled) {
  background: rgba(255, 255, 255, .12) !important; border-color: #FFFFFF !important; }
/* Fields: white, light border, 8px corners, bold labels above. */
.stApp :is([data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div) {
  background: #FFFFFF !important; border: 1px solid var(--field) !important; border-radius: 8px !important; box-shadow: none !important; }
.stApp :is([data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div):focus-within {
  border-color: var(--blue) !important; box-shadow: 0 0 0 3px rgba(10, 81, 136, .15) !important; }
.stApp :is(input, textarea) { color: var(--ink) !important; background: transparent !important; }
.stApp [data-testid="stWidgetLabel"] :is(p, label) { color: var(--ink); font-weight: 600; font-size: .92rem; }
.stApp [data-testid="stFileUploaderDropzone"] { background: #FFFFFF; border: 1.5px dashed #C3CEDB; border-radius: 12px; }
/* Cards: forms, expanders, bordered containers, tables. */
.stApp [data-testid="stForm"] { background: #FFFFFF; border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow); padding: 1.4rem; }
.stApp [data-testid="stExpander"] details { background: #FFFFFF; border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow); }
.stApp [data-testid="stExpander"] summary { font-weight: 600; color: var(--ink); }
.stApp [data-testid="stVerticalBlockBorderWrapper"] { border-color: var(--line) !important; border-radius: 12px !important; }
.stApp [data-testid="stDataFrame"] { background: #FFFFFF; border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow); overflow: hidden; }
.stApp [data-testid="stAlert"] { border-radius: 10px; border: 1px solid var(--line); }
/* Numbers in cards: white tile, centred, one accent colour per column (green, orange, blue, purple). */
.stApp [data-testid="stMetric"] { background: #FFFFFF; border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow);
  padding: 1rem; text-align: center; }
.stApp [data-testid="stMetric"] :is([data-testid="stMetricLabel"], [data-testid="stMetricValue"]) { justify-content: center; }
.stApp [data-testid="stMetricLabel"] p { color: var(--muted); font-weight: 600; font-size: .95rem; }
.stApp [data-testid="stMetricValue"], .stApp [data-testid="stMetricValue"] div { font-weight: 700; font-size: 2rem; color: var(--ink); }
.stApp :is([data-testid="stColumn"], [data-testid="column"]):nth-child(1) [data-testid="stMetricValue"],
.stApp :is([data-testid="stColumn"], [data-testid="column"]):nth-child(1) [data-testid="stMetricValue"] div { color: #17A06B; }
.stApp :is([data-testid="stColumn"], [data-testid="column"]):nth-child(2) [data-testid="stMetricValue"],
.stApp :is([data-testid="stColumn"], [data-testid="column"]):nth-child(2) [data-testid="stMetricValue"] div { color: #F28C1E; }
.stApp :is([data-testid="stColumn"], [data-testid="column"]):nth-child(3) [data-testid="stMetricValue"],
.stApp :is([data-testid="stColumn"], [data-testid="column"]):nth-child(3) [data-testid="stMetricValue"] div { color: #1F6FB8; }
.stApp :is([data-testid="stColumn"], [data-testid="column"]):nth-child(4) [data-testid="stMetricValue"],
.stApp :is([data-testid="stColumn"], [data-testid="column"]):nth-child(4) [data-testid="stMetricValue"] div { color: #6C4A9E; }
/* Tabs: blue underline on the active tab. */
.stApp button[data-baseweb="tab"] { color: var(--muted); font-weight: 600; }
.stApp button[data-baseweb="tab"][aria-selected="true"] { color: var(--ink); }
.stApp [data-baseweb="tab-highlight"] { background-color: var(--blue) !important; height: 3px !important; }
.stApp [data-baseweb="tab-border"] { background-color: var(--line) !important; }
/* Text follows the page direction (right-aligned) instead of Streamlit's left alignment; numbers in cards stay centred. */
.stApp :where(p, li, h1, h2, h3, h4, h5, h6, [data-testid="stMarkdownContainer"], [data-testid="stCaptionContainer"],
  [data-testid="stText"], [data-testid="stHeading"]):where(:not([data-testid="stMetric"] *)) { text-align: right !important; }
/* Sign-in screen: title, form card and the card's heading are centred, with room above like a login page. */
.stApp h1, .stApp [data-testid="stForm"] :is(h2, h3) { text-align: center !important; }
.stApp [data-testid="stHeading"]:has(h1), .stApp [data-testid="stForm"] [data-testid="stHeading"] { display: flex; justify-content: center; }
.stApp:not(:has([data-testid="stSidebar"])) [data-testid="stMainBlockContainer"] { padding-top: 14vh; }
/* Keyboard focus stays visible; no motion for people who turn it off. */
.stApp :is(button, a, input, textarea, [role="tab"]):focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { .stApp * { transition: none !important; animation: none !important; } }
</style>"""
# Joined with a blank line: the Markdown parser behind st.markdown must see two separate <style> blocks.
# Written as '</style><style>' on one line, it ends the first block early and prints the rest as text.
APP_CSS = RTL_CSS + "\n\n" + STYLE_CSS

IDLE_LIMIT = timedelta(minutes=30)
BADGE = {k: f"{icon} {mc.CHECK_AR[k]}" for k, icon in
         (("Match", "✅"), ("Partial", "🟡"), ("Mismatch", "🔴"), ("Incomplete", "⚪"))}
REF_ICON = {"valid": "✔", "invalid": "✘", "n/a": "–"}
ACTION_AR = {"login": "تسجيل دخول", "login_failed": "محاولة دخول فاشلة", "logout": "تسجيل خروج",
             "session_timeout": "انتهاء الجلسة", "password_change": "تغيير كلمة المرور",
             "user_create": "إضافة مستخدم", "user_update": "تعديل مستخدم", "setting": "تغيير إعداد",
             "reference_load": "تحميل القائمة المرجعية", "create": "إنشاء مذكرة", "update": "تعديل مذكرة",
             "delete": "حذف مذكرة", "import": "استيراد", "export": "تصدير", "download": "تنزيل ملف مذكرة",
             "remember_on": "تفعيل «تذكرني»", "login_remembered": "دخول تلقائي (تذكرني)"}
MIME = {"csv": "text/csv", "json": "application/json",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
FILTER_KEYS = ("f_q", "f_period", "f_from", "f_to", "f_memo_number", "f_goods_description", "f_notes",
               "f_authorized_item", "f_classified_item", "f_checks", "f_ref")
MIN_DATE = date(1970, 1, 1)

# Options whose text is also used in the program logic
P_ANY, P_30, P_12M, P_YEAR, P_CUSTOM = "أي تاريخ", "آخر 30 يومًا", "آخر 12 شهرًا", "هذه السنة", "نطاق مخصص"
REF_FILTER = {"Any": "الكل", "Any invalid": "يوجد بند غير موجود في القائمة", "All valid": "جميع البنود موجودة في القائمة"}
SRC_FILE, SRC_URL, SRC_DB = "ملف", "رابط", "استعلام قاعدة بيانات"
IMP_SKIP, IMP_UPDATE = "تخطّيها", "تحديثها"
LIST_REPLACE, LIST_ADD = "استبدالها", "الإضافة إليها"
NONE_COL, NONE_DESC = "(غير موجود في الملف)", "(بدون)"

# "Remember me" keeps a random token in a cookie (only its hash is stored on the server). Streamlit can read cookies
# (st.context.cookies) but not set them, so a small script does that. It needs an st.html that can run JavaScript.
REMEMBER_COOKIE = "memo_rm"
JS_OK = hasattr(st, "html") and "unsafe_allow_javascript" in inspect.signature(st.html).parameters


def cookie_js(token):
    """JavaScript that stores the remember-me cookie in the browser; an empty token removes it."""
    if not re.fullmatch(r"[A-Za-z0-9_-]*", token):
        raise ValueError("unexpected token format")
    age = mc.REMEMBER_DAYS * 86400 if token else 0
    return ("(function(){var w=(window.parent&&window.parent!==window)?window.parent:window;"
            "var s=w.location.protocol==='https:'?'; Secure':'';"
            f"w.document.cookie='{REMEMBER_COOKIE}={token}; max-age={age}; path=/; SameSite=Lax'+s;}})();")


def run_js(js):
    if JS_OK:
        st.html(f"<script>{js}</script>", unsafe_allow_javascript=True)


# ───────────────────────────── helpers ─────────────────────────────

def flash(msg, icon="✅"):
    """Show a toast on the next run (survives st.rerun())."""
    st.session_state["_flash"] = (msg, icon)


def code(value):
    """Inline-code a user-supplied value so it can't break the surrounding markdown."""
    return "`" + str(value).replace("`", "'") + "`"


def fmt_date(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")


def fmt_size(n):
    return f"{n / 2**20:.1f} ميغابايت" if n >= 2**20 else f"{max(1, round(n / 1024))} كيلوبايت"


def lab(key, required=True):
    """Field label shared by the forms, the import mapping and the exports."""
    return mc.FIELDS[key][0] + (" *" if required else "")


@st.cache_resource
def _init_db():
    mc.init_db()
    return True


# Everything below is cached per database revision: any write (from any session or process) bumps it.
@st.cache_data(show_spinner=False, max_entries=2)
def get_ref(rev):
    return mc.ref_map()


@st.cache_data(show_spinner=False, max_entries=2)
def get_memos(rev):
    return mc.enrich(mc.load_memos(), get_ref(rev))


@st.cache_data(show_spinner=False, max_entries=16, ttl=3600)
def export_blob(rev, ids, fmt):
    df = get_memos(rev)
    return mc.export_bytes(df[df["id"].isin(ids)], fmt)


# ───────────────────────────── sign-in ─────────────────────────────

def login_screen(first_run):
    st.title("🗂️ مذكرات تصنيف البضائع")
    _, form_col, _ = st.columns(3)  # the form card goes in the middle column
    with form_col:
        with st.form("login"):
            st.subheader("إنشاء حساب مدير النظام" if first_run else "تسجيل الدخول")
            if first_run:
                st.caption("هذه أول مرة يُشغَّل فيها البرنامج. الحساب الذي تنشئه هنا يملك جميع الصلاحيات الإدارية.")
            username = st.text_input("اسم المستخدم")
            password = st.text_input("كلمة المرور", type="password")
            confirm = st.text_input("تأكيد كلمة المرور", type="password") if first_run else password
            remember = False
            if JS_OK:
                remember = st.checkbox(f"تذكرني على هذا الجهاز لمدة {mc.REMEMBER_DAYS} يومًا", key="login_remember",
                                       help="لا تفعّل هذا الخيار على جهاز مشترك.")
            else:
                st.caption("خيار «تذكرني» يحتاج إلى إصدار أحدث من Streamlit.")
            if st.form_submit_button("إنشاء الحساب" if first_run else "دخول", type="primary"):
                try:
                    if first_run:
                        if mc.has_users():
                            raise ValueError("يوجد مدير نظام بالفعل. يرجى تسجيل الدخول.")
                        if password != confirm:
                            raise ValueError("كلمتا المرور غير متطابقتين.")
                        mc.create_user(username, password, "admin")
                    user, msg = mc.authenticate(username, password)
                    if user is None:
                        raise ValueError(msg)
                except ValueError as e:
                    st.error(str(e))
                else:
                    st.session_state.update(username=user["username"], last_seen=datetime.now())
                    if remember:
                        token = mc.create_remember_token(user["username"])
                        st.session_state.update(remembered=True, remember_token=token, _set_cookie=token)
                    st.rerun()


# ───────────────────────────── memo form (shared by "New" and "Edit") ─────────────────────────────

def memo_form(key, init, exclude_id=None, current_file=None):
    """Input widgets with live verification. Returns (record, blocked, attach).
    attach = {"file": validated upload or None, "remove": True when the current file should be deleted}."""
    rev = mc.revision()
    ref, strict = get_ref(rev), mc.get_setting("strict") == "1"
    today = date.today()
    d0 = init.get("memo_date", today)
    c1, c2 = st.columns([2, 1])
    num = c1.text_input(lab("memo_number"), init.get("memo_number", ""), key=f"{key}_num",
                        max_chars=64, placeholder="مثال: CM-2025-0001")
    dt = c2.date_input(lab("memo_date"), d0, min_value=min(MIN_DATE, d0),
                       max_value=max(today + timedelta(days=1), d0), format="DD/MM/YYYY", key=f"{key}_date")
    desc = st.text_area(lab("goods_description"), init.get("goods_description", ""), height=140, key=f"{key}_desc",
                        help="نص متعدد الأسطر: المادة، التركيب، الوظيفة، التعبئة، طريقة العرض.")
    notes = st.text_area("ملاحظات إضافية أو خصائص خاصة", init.get("notes", ""), height=90, key=f"{key}_notes")
    c3, c4 = st.columns(2)
    auth = c3.text_input(lab("authorized_item"), init.get("authorized_item", ""), key=f"{key}_auth", max_chars=40)
    cls = c4.text_input(lab("classified_item"), init.get("classified_item", ""), key=f"{key}_cls", max_chars=40)

    up = st.file_uploader(
        "استبدال ملف المذكرة" if current_file else "ملف المذكرة (PDF أو مستند Office)",
        type=sorted(mc.ATTACH_TYPES), key=f"{key}_file",
        help="اختياري. ملف واحد لكل مذكرة: PDF أو Word أو Excel أو PowerPoint أو OpenDocument أو RTF، بحد أقصى "
             f"{mc.MAX_ATTACHMENT // 2**20} ميغابايت. تنزيل المذكرة يعيد هذا الملف نفسه تمامًا.")
    attach, file_err = {"file": None, "remove": False}, None
    if up is not None:
        try:
            attach["file"] = mc.prepare_attachment(up.name, up.getvalue())
        except ValueError as e:
            file_err = str(e)
            st.error(file_err)
    elif current_file:
        attach["remove"] = st.checkbox("حذف الملف الحالي", key=f"{key}_rmfile")

    rec, errs = mc.prepare({"memo_number": num, "memo_date": dt, "goods_description": desc, "notes": notes,
                            "authorized_item": auth, "classified_item": cls})
    ref_errs = mc.ref_errors(rec, ref, strict)
    dup = mc.find_memo(rec["memo_number"], exclude_id) if rec["memo_number"] else None

    if any(v.strip() for v in (num, desc, auth, cls)):
        with st.container(border=True):
            st.markdown("**التحقق**")
            if dup:
                st.markdown(f"❌ رقم المذكرة {code(rec['memo_number'])} موجود مسبقًا "
                            f"(بتاريخ {fmt_date(dup['memo_date'])}).")
            elif rec["memo_number"]:
                st.markdown(f"✅ رقم المذكرة فريد، وسيُحفظ بالصيغة {code(rec['memo_number'])}.")
            for col in mc.CODE_COLS:
                name, k = mc.ITEM_NAME[col], mc.canon(rec[col])
                if not k:
                    continue
                if not ref:
                    st.markdown(f"ℹ️ {name}: لا توجد قائمة مرجعية محمّلة، لذلك لا يمكن التحقق منه.")
                elif k in ref:
                    st.markdown(f"✅ {name} موجود في القائمة المرجعية" + (f": {code(ref[k])}" if ref[k] else "."))
                else:
                    st.markdown(("❌" if strict else "⚠️") + f" {name} {code(rec[col])} غير موجود في القائمة المرجعية.")
            status, n = mc.consistency(rec["authorized_item"], rec["classified_item"])
            if status == "Match":
                st.markdown("✅ البندان متطابقان.")
            elif status == "Partial":
                st.markdown(f"🟡 مطابقة جزئية: عدد الخانات المتشابهة من البداية = {n}.")
            elif status == "Mismatch":
                st.markdown("🔴 صُنِّفت البضاعة تحت بند يختلف عن البند المعتمد.")
        if errs:
            st.warning("\n".join(f"- {e}" for e in errs))
    return rec, bool(errs or ref_errs or dup or file_err), attach


# ───────────────────────────── pages ─────────────────────────────

def page_new(user):
    st.subheader("مذكرة تصنيف جديدة")
    n = st.session_state.setdefault("new_n", 0)  # bumping n gives the form fresh, empty widgets
    rec, blocked, attach = memo_form(f"new{n}", {})
    if st.button("حفظ المذكرة", type="primary", disabled=blocked, key=f"new{n}_save"):
        try:
            mc.add_memo(rec, user["username"], attach["file"])
        except ValueError as e:  # includes DuplicateError
            st.error(str(e))
        else:
            st.session_state["new_n"] = n + 1
            flash(f"تم حفظ المذكرة {rec['memo_number']}" + (" مع ملفها." if attach["file"] else "."))
            st.rerun()


def memo_panel(user, row):
    st.divider()
    st.markdown(f"### المذكرة {code(row['memo_number'])}")
    st.caption(f"الإصدار {row['version']} · أُنشئت في {row['created_at']} بواسطة {row['created_by']} · "
               f"آخر تعديل في {row['updated_at']} بواسطة {row['updated_by']}")
    if row["file_name"]:
        f1, f2 = st.columns([3, 1])
        f1.markdown(f"📎 **ملف المذكرة:** {code(row['file_name'])} · {fmt_size(row['file_size'])}")
        if mc.can(user, "download"):
            try:
                stored = mc.get_attachment(int(row["id"]))
            except RuntimeError as e:
                f2.error(str(e))
            else:
                if stored:
                    f2.download_button("تنزيل ملف المذكرة", stored["data"], stored["filename"], stored["mime"],
                                       type="primary", key=f"dl_{row['id']}_{row['version']}", on_click=mc.log,
                                       args=(user["username"], "download",
                                             f"{row['memo_number']}: {stored['filename']}"))
    else:
        st.caption("لا يوجد ملف مرفق بهذه المذكرة.")
    if not mc.can(user, "write"):  # viewers get a read-only view
        st.markdown(f"**التاريخ:** {row['memo_date']:%d/%m/%Y}   **التطابق:** {BADGE[row['check']]}")
        st.markdown("**وصف البضاعة**")
        st.text(row["goods_description"])
        if row["notes"]:
            st.markdown("**ملاحظات**")
            st.text(row["notes"])
        st.markdown(f"**البند المعتمد:** {code(row['authorized_item'])}  \n"
                    f"**البند المصنف:** {code(row['classified_item'])}")
        return
    key = f"edit{row['id']}v{row['version']}"
    init = {"memo_number": row["memo_number"], "memo_date": row["memo_date"].date(),
            "goods_description": row["goods_description"], "notes": row["notes"],
            "authorized_item": row["authorized_item"], "classified_item": row["classified_item"]}
    rec, blocked, attach = memo_form(key, init, exclude_id=int(row["id"]), current_file=row["file_name"] or None)
    if st.button("حفظ التعديلات", type="primary", disabled=blocked, key=f"{key}_save"):
        try:
            changed = mc.update_memo(int(row["id"]), int(row["version"]), rec, user["username"],
                                     attachment=attach["file"], remove_file=attach["remove"])
        except (ValueError, mc.ConflictError) as e:  # ValueError includes DuplicateError
            st.error(str(e))
        else:
            flash("تم حفظ التعديلات." if changed else "لا شيء للحفظ: لم يتغير أي حقل.", "✅" if changed else "ℹ️")
            st.rerun()
    if mc.can(user, "delete"):
        with st.expander("حذف هذه المذكرة"):
            sure = st.checkbox("أدرك أن المذكرة وملفها المرفق سيُحذفان نهائيًا.", key=f"{key}_del_ok")
            if st.button("حذف المذكرة", disabled=not sure, key=f"{key}_del"):
                mc.delete_memo(int(row["id"]), user["username"])
                flash(f"تم حذف المذكرة {row['memo_number']}.", "🗑️")
                st.rerun()


def page_memos(user):
    rev = mc.revision()
    df = get_memos(rev)
    st.subheader("مذكرات التصنيف")
    if df.empty:
        st.info("لا توجد مذكرات بعد. أضف أول مذكرة من صفحة **مذكرة جديدة**، أو استورد ملفًا من صفحة **استيراد**.")
        return

    m = st.columns(4)
    m[0].metric("المذكرات", f"{len(df):,}")
    m[1].metric("غير المطابقة", int((df["check"] == "Mismatch").sum()))
    m[2].metric("المطابقة الجزئية", int((df["check"] == "Partial").sum()))
    m[3].metric("خارج القائمة المرجعية", int(((df["auth_ref"] == "invalid") | (df["cls_ref"] == "invalid")).sum()))

    s1, s2 = st.columns([6, 1])
    q = s1.text_input("بحث في جميع الحقول", key="f_q", label_visibility="collapsed",
                      placeholder='ابحث في جميع الحقول، مثال:  8471  حاسوب   ·   "قماش قطني"   ·   03/2025')
    s2.button("مسح التصفية", on_click=lambda: [st.session_state.pop(k, None) for k in FILTER_KEYS])

    today = date.today()
    start = end = None
    with st.expander("تصفية متقدمة"):
        a, b, c, d = st.columns(4)
        period = a.selectbox("تاريخ المذكرة", [P_ANY, P_30, P_12M, P_YEAR, P_CUSTOM], key="f_period")
        per_field = {"memo_number": b.text_input("رقم المذكرة يحتوي على", key="f_memo_number"),
                     "goods_description": c.text_input("وصف البضاعة يحتوي على", key="f_goods_description"),
                     "notes": d.text_input("الملاحظات تحتوي على", key="f_notes")}
        e, f, g, h = st.columns(4)
        per_field["authorized_item"] = e.text_input("البند المعتمد يبدأ بـ", key="f_authorized_item")
        per_field["classified_item"] = f.text_input("البند المصنف يبدأ بـ", key="f_classified_item")
        checks = g.multiselect("التطابق", list(BADGE), format_func=BADGE.get, key="f_checks", placeholder="اختر قيمة أو أكثر")
        ref_state = h.selectbox("فحص القائمة المرجعية", list(REF_FILTER), format_func=REF_FILTER.get, key="f_ref")
        if period == P_30:
            start, end = today - timedelta(days=30), today
        elif period == P_12M:
            start, end = today - timedelta(days=365), today
        elif period == P_YEAR:
            start, end = date(today.year, 1, 1), today
        elif period == P_CUSTOM:
            r1, r2, _, _ = st.columns(4)
            lim = {"min_value": MIN_DATE, "max_value": today + timedelta(days=1), "format": "DD/MM/YYYY"}
            start = r1.date_input("من", date(today.year, 1, 1), key="f_from", **lim)
            end = r2.date_input("إلى", today, key="f_to", **lim)
            if start > end:
                st.error("يجب ألا يكون تاريخ «من» بعد تاريخ «إلى».")
                start = end = None

    view = mc.filter_memos(df, q, per_field, start, end, checks, ref_state)
    st.caption(f"عدد النتائج: {len(view):,} من أصل {len(df):,}. اختر صفًا لعرض تفاصيله وتنزيل ملفه.")
    table = pd.DataFrame({
        "رقم المذكرة": view["memo_number"], "التاريخ": view["memo_date"].dt.date,
        "وصف البضاعة": view["goods_description"], "البند المعتمد": view["authorized_item"],
        "البند المصنف": view["classified_item"], "التطابق": view["check"].map(BADGE),
        "المعتمد في القائمة": view["auth_ref"].map(REF_ICON), "المصنف في القائمة": view["cls_ref"].map(REF_ICON),
        "المرفق": view["file_name"].map(lambda n: f"📎 {n}" if n else "")})
    event = st.dataframe(
        table, hide_index=True, on_select="rerun", selection_mode="single-row",
        column_config={"التاريخ": st.column_config.DateColumn("التاريخ", format="DD/MM/YYYY"),
                       "وصف البضاعة": st.column_config.TextColumn("وصف البضاعة", width="large")})

    if mc.can(user, "export") and len(view):
        x1, x2, _ = st.columns([2, 2, 3])
        label = x1.radio("تصدير هذه النتائج بصيغة", ["CSV", "Excel", "JSON"], horizontal=True, key="exp_fmt")
        ext = {"CSV": "csv", "Excel": "xlsx", "JSON": "json"}[label]
        x2.download_button(f"تنزيل النتائج ({len(view):,})", export_blob(rev, tuple(view["id"].tolist()), ext),
                           f"مذكرات_{today:%Y%m%d}.{ext}", MIME[ext], on_click=mc.log,
                           args=(user["username"], "export", f"عدد الصفوف {len(view)} · {ext}"))

    rows = event.selection.rows
    if rows:
        memo_panel(user, view.iloc[rows[0]])


def page_import(user):
    st.subheader("استيراد المذكرات")
    st.caption("الصيغ المقبولة: CSV (بترميز UTF-8) أو Excel (xlsx) أو JSON (قائمة سجلات). التواريخ بصيغة "
               "DD/MM/YYYY أو YYYY-MM-DD. احفظ رموز البنود كنص حتى لا تضيع الأصفار في بدايتها.")
    st.download_button("تنزيل نموذج CSV فارغ", mc.template_csv(), "نموذج_الاستيراد.csv", "text/csv")
    n = st.session_state.setdefault("imp_n", 0)
    up = st.file_uploader("اختر ملفًا", type=["csv", "xlsx", "xlsm", "json"], key=f"imp{n}_file")
    if up is None:
        return
    try:
        df = mc.read_table(up.name, up.getvalue())
    except ValueError as e:
        st.error(str(e))
        return

    st.markdown(f"عدد الصفوف: **{len(df):,}** · عدد الأعمدة: **{len(df.columns)}**. حدّد العمود المقابل لكل حقل:")
    guess = mc.guess_mapping(df.columns)
    opts = [NONE_COL, *df.columns]
    mapping, cols = {}, st.columns(3)
    for i, k in enumerate(mc.FIELDS):
        sel = cols[i % 3].selectbox(lab(k, k in mc.REQUIRED), opts,
                                    index=opts.index(guess[k]) if guess[k] else 0,
                                    key=f"imp{n}_{up.name}_{up.size}_{k}")
        mapping[k] = None if sel == NONE_COL else sel
    missing = [mc.FIELDS[k][0] for k in mc.REQUIRED if not mapping[k]]
    if missing:
        st.error("اربط الحقول المطلوبة: " + "، ".join(missing) + ".")
        return

    update = st.radio("إذا كان رقم المذكرة موجودًا مسبقًا", [IMP_SKIP, IMP_UPDATE], horizontal=True,
                      key=f"imp{n}_mode") == IMP_UPDATE
    plan = mc.analyze_import(df, mapping, get_ref(mc.revision()), mc.get_setting("strict") == "1")
    counts = plan["status"].value_counts()
    n_new, n_old, n_bad = (int(counts.get(s, 0)) for s in ("new", "exists", "error"))
    c1, c2, c3 = st.columns(3)
    c1.metric("مذكرات جديدة", n_new)
    c2.metric("موجودة مسبقًا", n_old)
    c3.metric("مرفوضة", n_bad)
    if n_bad:
        bad = plan.loc[plan["status"] == "error", ["row", "memo_number", "problem"]]
        bad = bad.rename(columns={"row": "رقم الصف", "memo_number": "رقم المذكرة", "problem": "المشكلة"})
        with st.expander(f"الصفوف المرفوضة ({n_bad})", expanded=True):
            st.dataframe(bad, hide_index=True)
            safe = bad.assign(**{"رقم المذكرة": bad["رقم المذكرة"].map(mc.sheet_safe)})
            st.download_button("تنزيل الصفوف المرفوضة (CSV)", safe.to_csv(index=False).encode("utf-8-sig"),
                               "الصفوف_المرفوضة.csv", "text/csv")
    with st.expander("معاينة الصفوف التي ستُستورد"):
        ok = plan.loc[plan["status"] != "error"].drop(columns="problem").head(200)
        ok = ok.assign(status=ok["status"].map({"new": "جديدة", "exists": "موجودة"}))
        st.dataframe(ok.rename(columns={"row": "رقم الصف", "status": "الحالة",
                                        **{k: v[0] for k, v in mc.FIELDS.items()}}), hide_index=True)
    total = n_new + (n_old if update else 0)
    if st.button(f"استيراد ({total:,})", type="primary", disabled=total == 0, key=f"imp{n}_go"):
        done = mc.commit_import(plan, "update" if update else "skip", user["username"])
        st.session_state["imp_n"] = n + 1  # resets the uploader
        flash(f"اكتمل الاستيراد. عدد المذكرات: {done:,}.")
        st.rerun()


def page_reference(user):
    st.subheader("القائمة المرجعية")
    st.caption("القائمة الرسمية لبنود التصنيف المعتمدة، ويتم التحقق من بنود المذكرات مقابلها. "
               "تُستخدم الروابط وبيانات الاتصال بقواعد البيانات مرة واحدة ولا تُحفظ.")
    ref = get_ref(mc.revision())
    c1, c2 = st.columns([1, 3])
    c1.metric("البنود المحمّلة", f"{len(ref):,}")
    c2.caption(mc.get_setting("ref_source", "لم تُحمَّل أي قائمة بعد."))
    strict_now = mc.get_setting("strict") == "1"
    strict = st.toggle("الوضع الصارم: منع حفظ المذكرات التي لا توجد بنودها في القائمة", value=strict_now,
                       key="strict_toggle", help="يُطبَّق على المذكرات الجديدة والتعديلات والاستيراد بعد تحميل قائمة.")
    if strict != strict_now:
        mc.set_setting("strict", "1" if strict else "0", user["username"])
        st.rerun()

    st.markdown("#### تحميل القائمة أو تحديثها")
    src = st.radio("المصدر", [SRC_FILE, SRC_URL, SRC_DB], horizontal=True, key="ref_src")
    df, label = None, ""
    if src == SRC_FILE:
        up = st.file_uploader("ملف CSV أو Excel أو JSON بصف واحد لكل بند", type=["csv", "xlsx", "xlsm", "json"],
                              key="ref_file")
        if up is not None:
            try:
                df, label = mc.read_table(up.name, up.getvalue()), f"ملف {up.name}"
            except ValueError as e:
                st.error(str(e))
    else:
        if src == SRC_URL:
            url = st.text_input("رابط (https://…) لملف CSV أو Excel أو JSON", key="ref_url")
            go = st.button("جلب القائمة")
        else:
            db_url = st.text_input("عنوان SQLAlchemy", type="password", key="ref_dburl",
                                   placeholder="postgresql+psycopg://user:password@host/database")
            query = st.text_area("استعلام SELECT: العمود الأول = رمز البند، والثاني (اختياري) = الوصف",
                                 key="ref_query")
            go = st.button("تشغيل الاستعلام")
        if go:
            try:
                if src == SRC_URL:
                    u = urlparse(url)
                    st.session_state["ref_fetched"] = (src, mc.read_url(url), f"رابط {u.scheme}://{u.hostname}{u.path}")
                else:
                    st.session_state["ref_fetched"] = (src, mc.read_sql(db_url, query), "استعلام قاعدة بيانات")
            except ValueError as e:
                st.error(str(e))
        fetched = st.session_state.get("ref_fetched")
        if fetched and fetched[0] == src:
            df, label = fetched[1], fetched[2]

    if df is not None:
        cols = list(df.columns)
        st.caption(f"عدد الصفوف المقروءة: {len(df):,}.")
        guess_code = next((i for i, c in enumerate(cols) if any(
            w in c.lower() for w in ("code", "item", "tariff", "hs", "رمز", "كود", "بند"))), 0)
        guess_desc = next((i for i, c in enumerate(cols) if i != guess_code and any(
            w in c.lower() for w in ("desc", "name", "title", "وصف", "اسم", "بيان"))), None)
        x1, x2, x3 = st.columns(3)
        code_col = x1.selectbox("عمود رمز البند", cols, index=guess_code, key=f"ref_code_{label}")
        desc_opts = [NONE_DESC, *cols]
        desc_col = x2.selectbox("عمود الوصف", desc_opts,
                                index=0 if guess_desc is None else guess_desc + 1, key=f"ref_desc_{label}")
        replace = x3.radio("القائمة الحالية", [LIST_REPLACE, LIST_ADD], key="ref_mode") == LIST_REPLACE
        st.dataframe(df.head(8), hide_index=True)
        if st.button("تحميل في القائمة المرجعية", type="primary"):
            try:
                n = mc.load_reference(df, code_col, None if desc_col == NONE_DESC else desc_col, replace,
                                      user["username"], label)
            except ValueError as e:
                st.error(str(e))
            else:
                flash(f"تم تحميل القائمة. عدد البنود: {n:,}.")
                st.rerun()

    st.markdown("#### البحث عن بند")
    term = st.text_input("الرمز أو الوصف", key="ref_q")
    if term:
        found = mc.search_reference(term).rename(columns={"code": "رمز البند", "description": "الوصف"})
        st.dataframe(found, hide_index=True)


def page_admin(user):
    users_tab, audit_tab = st.tabs(["المستخدمون", "سجل التدقيق"])
    with users_tab:
        users = mc.list_users()
        users["active"] = users["active"].astype(bool)
        st.dataframe(users.assign(role=users["role"].map(mc.ROLE_AR)).rename(columns={
            "username": "اسم المستخدم", "role": "الدور", "active": "نشط", "fails": "محاولات فاشلة",
            "locked_until": "مقفل حتى"}), hide_index=True)
        left, right = st.columns(2)
        with left:
            with st.form("add_user", clear_on_submit=True):
                st.markdown("**إضافة مستخدم**")
                name = st.text_input("اسم المستخدم")
                role = st.selectbox("الدور", list(mc.ROLES), index=1, format_func=mc.ROLE_AR.get)
                pw = st.text_input("كلمة المرور الأولية (10 أحرف على الأقل)", type="password")
                if st.form_submit_button("إضافة المستخدم"):
                    try:
                        mc.create_user(name, pw, role, actor=user["username"])
                    except ValueError as e:
                        st.error(str(e))
                    else:
                        flash(f"تمت إضافة المستخدم {name.strip().lower()}.")
                        st.rerun()
        with right:
            st.markdown("**تعديل مستخدم**")
            who = st.selectbox("المستخدم", list(users["username"]), key="eu_who")
            cur = users.set_index("username").loc[who]
            roles = list(mc.ROLES)
            role = st.selectbox("الدور", roles, index=roles.index(cur["role"]), format_func=mc.ROLE_AR.get,
                                key=f"eu_role_{who}")
            active = st.checkbox("الحساب نشط", value=bool(cur["active"]), key=f"eu_active_{who}")
            pw = st.text_input("كلمة مرور جديدة (اتركها فارغة للإبقاء على الحالية)", type="password",
                               key=f"eu_pw_{who}")
            if st.button("حفظ المستخدم", key=f"eu_save_{who}"):
                try:
                    mc.update_user(who, user["username"], role=role, active=active, password=pw or None)
                except ValueError as e:
                    st.error(str(e))
                else:
                    flash(f"تم تحديث المستخدم {who}.")
                    st.rerun()
    with audit_tab:
        log = mc.read_audit()
        f1, f2 = st.columns(2)
        who = f1.multiselect("المستخدم", sorted(log["username"].dropna().unique()))
        acts = f2.multiselect("الإجراء", sorted(log["action"].unique()), format_func=lambda a: ACTION_AR.get(a, a))
        if who:
            log = log[log["username"].isin(who)]
        if acts:
            log = log[log["action"].isin(acts)]
        shown = log.assign(action=log["action"].map(lambda a: ACTION_AR.get(a, a))).rename(columns={
            "ts": "الوقت", "username": "المستخدم", "action": "الإجراء", "detail": "التفاصيل"})
        st.dataframe(shown, hide_index=True)
        safe = shown.copy()
        for c in safe.columns:
            safe[c] = safe[c].fillna("").astype(str).map(mc.sheet_safe)
        st.download_button("تنزيل سجل التدقيق (CSV)", safe.to_csv(index=False).encode("utf-8-sig"),
                           "سجل_التدقيق.csv", "text/csv")


# ───────────────────────────── app shell ─────────────────────────────

PAGES = {"📋 المذكرات": ("view", page_memos), "➕ مذكرة جديدة": ("write", page_new),
         "📥 استيراد": ("import", page_import), "📚 القائمة المرجعية": ("reference", page_reference),
         "🔐 المستخدمون والتدقيق": ("admin", page_admin)}


def main():
    st.markdown(APP_CSS, unsafe_allow_html=True)
    _init_db()
    username = st.session_state.get("username")
    if not username:  # a new browser session: sign in from the "remember me" cookie when there is a valid one
        token = getattr(st.context, "cookies", {}).get(REMEMBER_COOKIE)
        restored = mc.restore_remembered(token)
        if restored:
            username = restored["username"]
            st.session_state.update(username=username, last_seen=datetime.now(), remembered=True, remember_token=token)
    user = mc.get_user(username) if username else None
    if username and user is None:  # deactivated or deleted while signed in
        st.session_state.clear()
        st.session_state["_clear_cookie"] = True
        st.warning("لم يعد حسابك نشطًا. يرجى تسجيل الدخول من جديد.")
    elif (user and not st.session_state.get("remembered")  # remembered devices are not signed out for inactivity
          and datetime.now() - st.session_state.get("last_seen", datetime.now()) > IDLE_LIMIT):
        mc.log(user["username"], "session_timeout")
        st.session_state.clear()
        user = None
        st.warning("تم تسجيل خروجك بسبب عدم النشاط لمدة 30 دقيقة.")
    if "_set_cookie" in st.session_state:  # just signed in with "remember me": store the cookie in the browser
        run_js(cookie_js(st.session_state.pop("_set_cookie")))
    if st.session_state.pop("_clear_cookie", False):
        run_js(cookie_js(""))
    if user is None:
        login_screen(first_run=not mc.has_users())
        return
    st.session_state["last_seen"] = datetime.now()
    if "_flash" in st.session_state:
        msg, icon = st.session_state.pop("_flash")
        st.toast(msg, icon=icon)

    allowed = [label for label, (perm, _) in PAGES.items() if mc.can(user, perm)]
    with st.sidebar:
        st.markdown(f"### 🗂️ مذكرات التصنيف\nالمستخدم الحالي: **{user['username']}** ({mc.ROLE_AR[user['role']]})")
        page = st.radio("التنقل", allowed, label_visibility="collapsed", key="nav")
        with st.expander("تغيير كلمة المرور"):
            with st.form("pw_form", clear_on_submit=True):
                old = st.text_input("كلمة المرور الحالية", type="password")
                new = st.text_input("كلمة المرور الجديدة (10 أحرف على الأقل)", type="password")
                if st.form_submit_button("تحديث كلمة المرور"):
                    try:
                        mc.change_password(user["username"], old, new)
                    except ValueError as e:
                        st.error(str(e))
                    else:
                        st.success("تم تحديث كلمة المرور.")
        if st.button("تسجيل الخروج"):
            mc.forget_token(st.session_state.get("remember_token"))  # this device stops being remembered
            mc.log(user["username"], "logout")
            st.session_state.clear()
            st.session_state["_clear_cookie"] = True
            st.rerun()
    PAGES[page][1](user)


if __name__ == "__main__":
    main()

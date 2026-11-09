"""Data layer for the Goods Classification Memo Manager.

Everything that is not UI lives here: SQLite storage, validation, verification, search, import/export,
authentication and audit logging. It has no Streamlit dependency, so it can be unit-tested or scripted.

Create the first administrator from the command line (recommended before exposing the app):
    python memo_core.py create-user <username> admin
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import unicodedata
import urllib.request
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from functools import lru_cache
from urllib.parse import urlparse

import pandas as pd

DB_PATH = os.environ.get("MEMO_DB_PATH", "memos.db")
ENC_KEY = os.environ.get("MEMO_ENC_KEY", "")  # optional Fernet key: encrypts description + notes at rest

ROLES = {"viewer": 1, "editor": 2, "admin": 3}
PERMS = {"view": "viewer", "download": "viewer", "write": "editor", "import": "editor", "export": "editor",
         "delete": "admin", "reference": "admin", "admin": "admin"}
# Arabic display names. The English keys are the stable codes that are stored and compared in the program.
ROLE_AR = {"viewer": "مشاهد", "editor": "محرر", "admin": "مدير النظام"}
CHECK_AR = {"Match": "مطابق", "Partial": "مطابق جزئيًا", "Mismatch": "غير مطابق", "Incomplete": "غير مكتمل"}
REF_AR = {"valid": "موجود", "invalid": "غير موجود", "n/a": "لا توجد قائمة"}
ITEM_NAME = {"authorized_item": "البند المعتمد", "classified_item": "البند المصنف"}
PBKDF2_ROUNDS, MAX_FAILS, LOCK_MINUTES = 600_000, 5, 15
PARTIAL_MIN = 4  # items sharing >= 4 leading characters (an HS heading) count as a "Partial" match
MAX_IMPORT_ROWS = 50_000
MAX_ATTACHMENT = 25 * 1024 * 1024  # bytes per memo file
REMEMBER_DAYS = 30                 # how long a "remember me" sign-in lasts
MAX_REMEMBER_TOKENS = 5            # remembered devices per user; the oldest is signed out when a sixth is added
ATTACH_TYPES = {  # extension -> (MIME type, content kinds accepted for it). Macro-enabled formats are left out on purpose.
    "pdf": ("application/pdf", {"pdf"}),
    "doc": ("application/msword", {"ole", "rtf"}),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", {"docx"}),
    "rtf": ("application/rtf", {"rtf"}),
    "odt": ("application/vnd.oasis.opendocument.text", {"odt"}),
    "xls": ("application/vnd.ms-excel", {"ole"}),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", {"xlsx"}),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet", {"ods"}),
    "ppt": ("application/vnd.ms-powerpoint", {"ole"}),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", {"pptx"}),
    "odp": ("application/vnd.oasis.opendocument.presentation", {"odp"}),
}

FIELDS = {  # key: (Arabic label, accepted import-header aliases; English headers from earlier exports still import)
    "memo_number": ("رقم مذكرة التصنيف",
                    ("Classification Memo Number", "memono", "memoid", "number", "رقم المذكرة", "رقم", "المذكرة")),
    "memo_date": ("تاريخ مذكرة التصنيف",
                  ("Classification Memo Date", "date", "memodate", "تاريخ المذكرة", "التاريخ")),
    "goods_description": ("وصف البضاعة", ("Goods Description", "description", "goods", "الوصف", "البضاعة")),
    "notes": ("ملاحظات / خصائص خاصة",
              ("Notes / Special Properties", "notes", "remarks", "specialproperties", "additionalnotes",
               "ملاحظات", "الملاحظات")),
    "authorized_item": ("بند التصنيف المعتمد",
                        ("Authorized Classification Item", "authorizeditem", "authorizedcode", "authorized",
                         "البند المعتمد", "بند معتمد", "المعتمد")),
    "classified_item": ("بند التصنيف الذي صنفت بموجبه البضاعة",
                        ("Classified Under Item", "classified", "classifieditem", "classifiedcode",
                         "classifiedunder", "actualitem", "البند المصنف", "بند مصنف", "المصنف")),
}
REQUIRED = ("memo_number", "memo_date", "goods_description", "authorized_item", "classified_item")
CODE_COLS = ("authorized_item", "classified_item")
TEXT_COLS = ("memo_number", "goods_description", "notes", "authorized_item", "classified_item")
ENC_COLS = ("goods_description", "notes")
EXPORT_COLS = {**{k: v[0] for k, v in FIELDS.items()}, "check": "التطابق",
               "auth_ref": "البند المعتمد ضمن القائمة المرجعية", "cls_ref": "البند المصنف ضمن القائمة المرجعية"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS memos(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memo_number TEXT NOT NULL UNIQUE,
  memo_date TEXT NOT NULL,
  goods_description TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  authorized_item TEXT NOT NULL,
  classified_item TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  created_by TEXT, created_at TEXT, updated_by TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_memos_date ON memos(memo_date);
CREATE TABLE IF NOT EXISTS ref_items(
  key TEXT PRIMARY KEY, code TEXT NOT NULL, description TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('viewer','editor','admin')),
  active INTEGER NOT NULL DEFAULT 1, fails INTEGER NOT NULL DEFAULT 0, locked_until TEXT);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, username TEXT, action TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS attachments(
  memo_id INTEGER PRIMARY KEY REFERENCES memos(id) ON DELETE CASCADE,
  filename TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
  data BLOB NOT NULL, uploaded_by TEXT, uploaded_at TEXT);
CREATE TABLE IF NOT EXISTS remember_tokens(
  token_hash TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
"""
INSERT_SQL = """INSERT INTO memos(memo_number, memo_date, goods_description, notes, authorized_item,
    classified_item, created_by, created_at, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"""
UPSERT_SQL = """ ON CONFLICT(memo_number) DO UPDATE SET memo_date=excluded.memo_date,
    goods_description=excluded.goods_description, notes=excluded.notes,
    authorized_item=excluded.authorized_item, classified_item=excluded.classified_item,
    version=version+1, updated_by=excluded.updated_by, updated_at=excluded.updated_at"""


class DuplicateError(ValueError):
    """The memo number is already used by another memo."""


class ConflictError(RuntimeError):
    """The memo was changed or deleted by someone else since it was loaded."""


# ───────────────────────────── normalisation ─────────────────────────────

# Arabic-Indic and Eastern Arabic-Indic digits -> ASCII, so "٢٠٢٤/١٢" and "2024/12" are the same identifier.
_DIGITS = {base + i: str(i) for base in (0x0660, 0x06F0) for i in range(10)}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def text(v) -> str:
    """Free text -> clean str: None/NaN -> '', unified newlines, trimmed, spreadsheet-escape apostrophe removed."""
    if v is None or v is pd.NA or v is pd.NaT or (isinstance(v, float) and v != v):
        return ""
    s = str(v).replace("\r\n", "\n").replace("\r", "\n").strip()
    return s[1:] if s[:1] == "'" and s[1:2] in ("=", "+", "-", "@") else s


def ident(v) -> str:
    """Identifier normalisation (memo numbers, item codes): NFKC, ASCII digits, upper case, single spaces."""
    return " ".join(unicodedata.normalize("NFKC", text(v)).translate(_DIGITS).upper().split())


def canon(code) -> str:
    """Comparison key for classification items: '8471.30.00' == '8471 30 00' == '84713000'."""
    return re.sub(r"[\W_]+", "", ident(code))


def fold(s) -> str:
    """Search folding: case-insensitive, accent/diacritic-insensitive, ASCII digits."""
    s = unicodedata.normalize("NFKD", str(s)).translate(_DIGITS).casefold()
    return "".join(ch for ch in s if not unicodedata.combining(ch))


_DATE_RX = (  # (pattern, group numbers for year, month, day)
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)"), (1, 2, 3)),
    (re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?!\d)"), (3, 2, 1)),
)


def parse_date(v) -> date | None:
    """date/datetime objects, YYYY-MM-DD and DD/MM/YYYY (also '-' or '.' separators, optional time part)."""
    if v is pd.NaT:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = text(v).translate(_DIGITS)
    for rx, (y, m, d) in _DATE_RX:
        hit = rx.match(s)
        if hit:
            try:
                return date(int(hit[y]), int(hit[m]), int(hit[d]))
            except ValueError:
                return None
    return None


def consistency(authorized, classified) -> tuple[str, int]:
    """Compare the authorized item with the item actually used -> (Match|Partial|Mismatch|Incomplete, shared prefix)."""
    a, b = canon(authorized), canon(classified)
    if not a or not b:
        return "Incomplete", 0
    if a == b:
        return "Match", len(a)
    n = len(os.path.commonprefix([a, b]))
    return ("Partial" if n >= PARTIAL_MIN else "Mismatch"), n


# ───────────────────────────── validation ─────────────────────────────

def prepare(raw: dict) -> tuple[dict, list[str]]:
    """Normalise a raw record and return (record, structural errors). Dates come back as ISO strings."""
    d = parse_date(raw.get("memo_date"))
    rec = {"memo_number": ident(raw.get("memo_number")),
           "memo_date": d.isoformat() if d else None,
           "goods_description": text(raw.get("goods_description")),
           "notes": text(raw.get("notes")),
           "authorized_item": ident(raw.get("authorized_item")),
           "classified_item": ident(raw.get("classified_item"))}
    errs = []
    if not rec["memo_number"]:
        errs.append("رقم المذكرة مطلوب.")
    elif len(rec["memo_number"]) > 64:
        errs.append("رقم المذكرة لا يجوز أن يتجاوز 64 حرفًا.")
    if d is None:
        errs.append("تاريخ المذكرة مفقود أو غير صالح (استخدم DD/MM/YYYY).")
    elif not date(1970, 1, 1) <= d <= date.today() + timedelta(days=1):
        errs.append("يجب أن يقع تاريخ المذكرة بين 01/01/1970 واليوم.")
    if len(rec["goods_description"]) < 3:
        errs.append("وصف البضاعة مطلوب.")
    if len(rec["goods_description"]) > 20_000 or len(rec["notes"]) > 20_000:
        errs.append("الوصف والملاحظات لا يجوز أن يتجاوز كل منهما 20,000 حرف.")
    for col in CODE_COLS:
        if not 2 <= len(canon(rec[col])) <= 20:
            errs.append(f"{ITEM_NAME[col]} مفقود أو غير صالح.")
    return rec, errs


def ref_errors(rec: dict, ref: dict | None, strict: bool) -> list[str]:
    """Strict mode only: items that are not in the reference list block saving."""
    if not (strict and ref):
        return []
    return [f"{ITEM_NAME[col]} {rec[col]} غير موجود في القائمة المرجعية."
            for col in CODE_COLS if canon(rec[col]) and canon(rec[col]) not in ref]


# ───────────────────────────── database ─────────────────────────────

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    fresh = not os.path.exists(DB_PATH)
    with db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
    if fresh and os.name == "posix":
        os.chmod(DB_PATH, 0o600)  # owner-only


def _bump(con) -> None:
    """Revision counter: the UI caches by it, so any writer (any process) invalidates every cache."""
    con.execute("INSERT INTO meta VALUES('rev','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")


def revision() -> int:
    with db() as con:
        r = con.execute("SELECT value FROM meta WHERE key='rev'").fetchone()
    return int(r[0]) if r else 0


def get_setting(key: str, default: str = "") -> str:
    with db() as con:
        r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(key: str, value: str, user: str) -> None:
    with db() as con:
        con.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        shown = f"الوضع الصارم: {'مفعّل' if value == '1' else 'معطّل'}" if key == "strict" else f"{key}={value}"
        _audit(con, user, "setting", shown)


@lru_cache(maxsize=1)
def _fernet():
    if not ENC_KEY:
        return None
    from cryptography.fernet import Fernet  # pip install cryptography
    return Fernet(ENC_KEY.encode())


_MARK = "~enc1~"


def _enc(s: str) -> str:
    f = _fernet()
    return _MARK + f.encrypt(s.encode()).decode() if f and s else s


def _decrypt(token: bytes) -> bytes:
    f = _fernet()
    if f is None:
        raise RuntimeError("توجد بيانات مشفّرة، لكن متغير البيئة MEMO_ENC_KEY غير مضبوط.")
    try:
        return f.decrypt(token)
    except Exception:
        raise RuntimeError("تعذّر فك تشفير البيانات المخزّنة. هل MEMO_ENC_KEY هو المفتاح نفسه الذي استُخدم في التشفير؟") from None


def _dec(s: str) -> str:
    return _decrypt(s[len(_MARK):].encode()).decode() if s.startswith(_MARK) else s


def _enc_bytes(b: bytes) -> bytes:
    f = _fernet()
    return _MARK.encode() + f.encrypt(b) if f else b


def _dec_bytes(b: bytes) -> bytes:
    m = _MARK.encode()
    return _decrypt(b[len(m):]) if b.startswith(m) else b


# ───────────────────────────── audit & users ─────────────────────────────

def _audit(con, user, action: str, detail: str = "") -> None:
    con.execute("INSERT INTO audit(ts, username, action, detail) VALUES (?,?,?,?)", (now(), user, action, detail))


def log(user: str, action: str, detail: str = "") -> None:
    with db() as con:
        _audit(con, user, action, detail)


def read_audit(limit: int = 2000) -> pd.DataFrame:
    with db() as con:
        return pd.read_sql_query("SELECT ts, username, action, detail FROM audit ORDER BY id DESC LIMIT ?",
                                 con, params=(limit,))


def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, rounds, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(calc.hex(), dk)
    except Exception:
        return False


def _policy(pw: str, username: str) -> None:
    if len(pw) < 10:
        raise ValueError("يجب ألا تقل كلمة المرور عن 10 أحرف.")
    if pw.lower() == username.lower():
        raise ValueError("يجب أن تختلف كلمة المرور عن اسم المستخدم.")


def has_users() -> bool:
    with db() as con:
        return con.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def get_user(username: str) -> dict | None:
    """Re-read on every run so a deactivated account or role change takes effect immediately."""
    with db() as con:
        r = con.execute("SELECT username, role FROM users WHERE username=? AND active=1", (username,)).fetchone()
    return dict(r) if r else None


def can(user: dict, perm: str) -> bool:
    return ROLES[user["role"]] >= ROLES[PERMS[perm]]


def create_user(username: str, password: str, role: str = "viewer", actor: str | None = None) -> None:
    u = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.@-]{3,40}", u):
        raise ValueError("اسم المستخدم من 3 إلى 40 خانة: أحرف إنجليزية وأرقام والرموز . _ @ -")
    if role not in ROLES:
        raise ValueError("دور غير معروف.")
    _policy(password, u)
    try:
        with db() as con:
            con.execute("INSERT INTO users(username, pw_hash, role) VALUES (?,?,?)", (u, hash_pw(password), role))
            _audit(con, actor or u, "user_create", f"{u} ({ROLE_AR[role]})")
    except sqlite3.IntegrityError:
        raise ValueError("اسم المستخدم موجود مسبقًا.") from None


def update_user(username: str, actor: str, role: str | None = None, active: bool | None = None,
                password: str | None = None) -> None:
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            raise ValueError("مستخدم غير معروف.")
        new_role = role or row["role"]
        new_active = row["active"] if active is None else int(active)
        if row["role"] == "admin" and row["active"] and (new_role != "admin" or not new_active):
            if con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0] <= 1:
                raise ValueError("لا يمكن إلغاء صلاحية آخر مدير نظام نشط أو تعطيل حسابه.")
        sets, args = ["role=?", "active=?", "fails=0", "locked_until=NULL"], [new_role, new_active]
        if password:
            _policy(password, username)
            sets.append("pw_hash=?")
            args.append(hash_pw(password))
        con.execute(f"UPDATE users SET {', '.join(sets)} WHERE username=?", (*args, username))
        if password or not new_active:
            con.execute("DELETE FROM remember_tokens WHERE username=?", (username,))
        _audit(con, actor, "user_update", f"{username}: {ROLE_AR[new_role]} · {'نشط' if new_active else 'معطّل'}"
                                          f"{' · إعادة تعيين كلمة المرور' if password else ''}")


def change_password(username: str, old: str, new: str) -> None:
    with db() as con:
        row = con.execute("SELECT pw_hash FROM users WHERE username=?", (username,)).fetchone()
        if row is None or not check_pw(old, row["pw_hash"]):
            raise ValueError("كلمة المرور الحالية غير صحيحة.")
        _policy(new, username)
        con.execute("UPDATE users SET pw_hash=? WHERE username=?", (hash_pw(new), username))
        con.execute("DELETE FROM remember_tokens WHERE username=?", (username,))
        _audit(con, username, "password_change")


def list_users() -> pd.DataFrame:
    with db() as con:
        return pd.read_sql_query(
            "SELECT username, role, active, fails, locked_until FROM users ORDER BY username", con)


def authenticate(username: str, password: str) -> tuple[dict | None, str]:
    """Salted PBKDF2 check with lock-out after repeated failures. One generic message for every failure."""
    generic = "بيانات الدخول غير صحيحة، أو أن الحساب مقفل مؤقتًا."
    u = username.strip().lower()
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        if row is None:
            hash_pw(password)  # keep timing similar for unknown users
            _audit(con, u[:40], "login_failed", "مستخدم غير معروف")
            return None, generic
        locked = bool(row["locked_until"]) and row["locked_until"] > now()
        valid = check_pw(password, row["pw_hash"])  # always computed: timing must not reveal the lock state
        if row["active"] and not locked and valid:
            con.execute("UPDATE users SET fails=0, locked_until=NULL WHERE username=?", (u,))
            _audit(con, u, "login")
            return {"username": u, "role": row["role"]}, ""
        fails = row["fails"] + 1
        lock = None
        if fails >= MAX_FAILS:
            lock = (datetime.now() + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec="seconds")
            fails = 0
        con.execute("UPDATE users SET fails=?, locked_until=? WHERE username=?", (fails, lock, u))
        _audit(con, u, "login_failed", "تم قفل الحساب" if lock else "")
        return None, generic



def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_remember_token(username: str) -> str:
    """"Remember me": returns a random token for the browser cookie. Only its SHA-256 hash is stored."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=REMEMBER_DAYS)).isoformat(timespec="seconds")
    with db() as con:
        con.execute("DELETE FROM remember_tokens WHERE expires_at <= ?", (now(),))  # housekeeping
        con.execute("INSERT INTO remember_tokens VALUES (?,?,?,?)", (_token_hash(token), username, now(), expires))
        con.execute("""DELETE FROM remember_tokens WHERE username=? AND rowid NOT IN
                       (SELECT rowid FROM remember_tokens WHERE username=? ORDER BY rowid DESC LIMIT ?)""",
                    (username, username, MAX_REMEMBER_TOKENS))
        _audit(con, username, "remember_on", f"لمدة {REMEMBER_DAYS} يومًا")
    return token


def restore_remembered(token: str | None) -> dict | None:
    """Sign in from a "remember me" cookie. None when the token is unknown, expired or its account is disabled."""
    if not token or len(token) > 200:
        return None
    with db() as con:
        r = con.execute("""SELECT u.username, u.role FROM remember_tokens t JOIN users u ON u.username = t.username
                           WHERE t.token_hash=? AND t.expires_at > ? AND u.active=1""",
                        (_token_hash(token), now())).fetchone()
        if r is None:
            return None
        _audit(con, r["username"], "login_remembered")
    return {"username": r["username"], "role": r["role"]}


def forget_token(token: str | None) -> None:
    """Sign out of "remember me" on one device (the token stops working immediately)."""
    if token:
        with db() as con:
            con.execute("DELETE FROM remember_tokens WHERE token_hash=?", (_token_hash(token),))


# ───────────────────────────── memos ─────────────────────────────

def _params(rec: dict, user: str) -> tuple:
    t = now()
    return (rec["memo_number"], rec["memo_date"], _enc(rec["goods_description"]), _enc(rec["notes"]),
            rec["authorized_item"], rec["classified_item"], user, t, user, t)


def find_memo(number: str, exclude_id: int | None = None) -> dict | None:
    """Duplicate check on the normalised memo number (optionally ignoring the memo being edited)."""
    with db() as con:
        r = con.execute("SELECT id, memo_date FROM memos WHERE memo_number=? AND id IS NOT ?",
                        (number, exclude_id)).fetchone()
    return dict(r) if r else None


def add_memo(rec: dict, user: str, attachment: dict | None = None) -> int:
    """Insert a memo and its optional file in one transaction. Returns the new memo id."""
    try:
        with db() as con:
            memo_id = con.execute(INSERT_SQL, _params(rec, user)).lastrowid
            if attachment:
                _put_attachment(con, memo_id, attachment, user)
            _bump(con)
            _audit(con, user, "create",
                   rec["memo_number"] + (f" [الملف={attachment['filename']}]" if attachment else ""))
            return memo_id
    except sqlite3.IntegrityError as e:  # the UNIQUE index is the real guard against races
        if "UNIQUE" not in str(e):
            raise
        raise DuplicateError(f"رقم المذكرة {rec['memo_number']} موجود مسبقًا.") from None


def update_memo(memo_id: int, version: int, rec: dict, user: str, attachment: dict | None = None,
                remove_file: bool = False) -> bool:
    """Optimistic locking on `version`. A new `attachment` replaces the current file; `remove_file` deletes it.
    Returns False when nothing changed."""
    try:
        with db() as con:
            old = con.execute("SELECT * FROM memos WHERE id=?", (memo_id,)).fetchone()
            if old is None or old["version"] != version:
                raise ConflictError("تم تعديل هذه المذكرة أو حذفها بواسطة مستخدم آخر. أعد تحميلها ثم حاول مرة أخرى.")
            changed = [FIELDS[k][0] for k in FIELDS if rec[k] != (_dec(old[k]) if k in ENC_COLS else old[k])]
            if attachment:
                changed.append(f"الملف={attachment['filename']}")
            elif remove_file and con.execute("SELECT 1 FROM attachments WHERE memo_id=?", (memo_id,)).fetchone():
                changed.append("حذف الملف")
            if not changed:
                return False
            p = _params(rec, user)
            cur = con.execute("""UPDATE memos SET memo_number=?, memo_date=?, goods_description=?, notes=?,
                                 authorized_item=?, classified_item=?, version=version+1, updated_by=?, updated_at=?
                                 WHERE id=? AND version=?""", (*p[:6], user, p[7], memo_id, version))
            if cur.rowcount == 0:  # someone else saved between our read and our write
                raise ConflictError("تم تعديل هذه المذكرة أو حذفها بواسطة مستخدم آخر. أعد تحميلها ثم حاول مرة أخرى.")
            if attachment:
                _put_attachment(con, memo_id, attachment, user)
            elif remove_file:
                con.execute("DELETE FROM attachments WHERE memo_id=?", (memo_id,))
            _bump(con)
            _audit(con, user, "update", f"{rec['memo_number']} [{'، '.join(changed)}]")
            return True
    except sqlite3.IntegrityError as e:
        if "UNIQUE" not in str(e):
            raise
        raise DuplicateError(f"رقم المذكرة {rec['memo_number']} موجود مسبقًا.") from None


def delete_memo(memo_id: int, user: str) -> None:
    with db() as con:
        row = con.execute("SELECT memo_number FROM memos WHERE id=?", (memo_id,)).fetchone()
        if row:
            con.execute("DELETE FROM attachments WHERE memo_id=?", (memo_id,))
            con.execute("DELETE FROM memos WHERE id=?", (memo_id,))
            _bump(con)
            _audit(con, user, "delete", row["memo_number"])


def load_memos() -> pd.DataFrame:
    """All memos plus file metadata (name, size). File contents are never loaded here."""
    with db() as con:
        df = pd.read_sql_query("""SELECT m.*, a.filename AS file_name, a.size AS file_size
                                  FROM memos m LEFT JOIN attachments a ON a.memo_id = m.id""", con)
    for c in ENC_COLS:
        df[c] = df[c].map(_dec)
    df["memo_date"] = pd.to_datetime(df["memo_date"])
    df["file_name"] = df["file_name"].fillna("")
    df["file_size"] = df["file_size"].fillna(0).astype(int)
    return df.sort_values(["memo_date", "memo_number"], ascending=[False, True]).reset_index(drop=True)


# ───────────────────────────── memo files ─────────────────────────────

_ODF = {"application/vnd.oasis.opendocument.text": "odt", "application/vnd.oasis.opendocument.spreadsheet": "ods",
        "application/vnd.oasis.opendocument.presentation": "odp"}
_OOXML = (("word/", "docx"), ("xl/", "xlsx"), ("ppt/", "pptx"))


def safe_filename(name) -> str:
    """Drop any directory part plus unsafe or invisible characters; Unicode (e.g. Arabic) names are kept."""
    base = re.split(r"[\\/]", str(name))[-1]
    base = "".join(c for c in unicodedata.normalize("NFC", base) if c.isprintable() and c not in '<>:"|?*')
    base = base.strip(" .")
    if len(base) > 120:
        stem, dot, ext = base.rpartition(".")
        base = (stem[:120 - len(ext) - 1] + dot + ext) if dot else base[:120]
    return base


def _sniff(data: bytes) -> str | None:
    """Identify a document from its content, never from its name."""
    head = data[:1024]
    if b"%PDF-" in head:
        return "pdf"
    if head.lstrip()[:5] == b"{\\rtf":
        return "rtf"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"  # legacy .doc / .xls / .ppt
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:  # reads the directory only; nothing is extracted
                names = zf.namelist()
                if any(n.lower().endswith("vbaproject.bin") for n in names):
                    return "macro"
                if "mimetype" in names:  # OpenDocument
                    with zf.open("mimetype") as f:
                        return _ODF.get(f.read(100).decode("ascii", "ignore").strip())
                if "[Content_Types].xml" in names:  # Office Open XML
                    return next((kind for prefix, kind in _OOXML if any(n.startswith(prefix) for n in names)), None)
        except Exception:
            return None
    return None


def prepare_attachment(name: str, data: bytes) -> dict:
    """Check an uploaded document by extension, size and real content. Returns {filename, mime, data} or raises ValueError."""
    filename = safe_filename(name)
    ext = filename.rpartition(".")[2].lower() if "." in filename else ""
    if ext not in ATTACH_TYPES:
        raise ValueError("نوع الملف غير مقبول. استخدم PDF أو Word أو Excel أو PowerPoint أو OpenDocument أو RTF "
                         "(الملفات المفعّلة بالماكرو غير مسموحة).")
    if not data:
        raise ValueError("الملف فارغ.")
    if len(data) > MAX_ATTACHMENT:
        raise ValueError(f"حجم الملف يتجاوز {MAX_ATTACHMENT // 2**20} ميغابايت.")
    kind = _sniff(data)
    if kind == "macro":
        raise ValueError("الملفات التي تحتوي على ماكرو غير مقبولة.")
    mime, kinds = ATTACH_TYPES[ext]
    if kind not in kinds:
        raise ValueError(f"محتوى هذا الملف لا يطابق امتداده «{ext}». "
                         "افتحه في برنامجه ثم احفظه من جديد وأعد المحاولة.")
    return {"filename": filename, "mime": mime, "data": data}


def _put_attachment(con, memo_id: int, att: dict, user: str) -> None:
    att = prepare_attachment(att["filename"], att["data"])  # re-checked here so the database never holds unchecked files
    data = att["data"]
    con.execute("""INSERT INTO attachments(memo_id, filename, mime, size, sha256, data, uploaded_by, uploaded_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(memo_id) DO UPDATE SET filename=excluded.filename, mime=excluded.mime,
                   size=excluded.size, sha256=excluded.sha256, data=excluded.data,
                   uploaded_by=excluded.uploaded_by, uploaded_at=excluded.uploaded_at""",
                (memo_id, att["filename"], att["mime"], len(data), hashlib.sha256(data).hexdigest(),
                 _enc_bytes(data), user, now()))


def get_attachment(memo_id: int) -> dict | None:
    """The stored file, byte for byte as uploaded (verified against its SHA-256), or None."""
    with db() as con:
        r = con.execute("""SELECT filename, mime, size, sha256, data, uploaded_by, uploaded_at
                           FROM attachments WHERE memo_id=?""", (memo_id,)).fetchone()
    if r is None:
        return None
    data = _dec_bytes(bytes(r["data"]))
    if hashlib.sha256(data).hexdigest() != r["sha256"]:
        raise RuntimeError("فشل الملف المخزّن في فحص السلامة، ولذلك لا يمكن تنزيله.")
    return {**{k: r[k] for k in ("filename", "mime", "size", "sha256", "uploaded_by", "uploaded_at")}, "data": data}


# ───────────────────────────── reference list ─────────────────────────────

def ref_map() -> dict[str, str]:
    """canonical item key -> description, for O(1) validity checks."""
    with db() as con:
        return {r["key"]: r["description"] for r in con.execute("SELECT key, description FROM ref_items")}


def search_reference(term: str = "", limit: int = 200) -> pd.DataFrame:
    like = f"%{term.strip()}%"
    with db() as con:
        return pd.read_sql_query(
            "SELECT code, description FROM ref_items WHERE code LIKE ? OR key LIKE ? OR description LIKE ? "
            "ORDER BY key LIMIT ?", con, params=(like, f"%{canon(term)}%", like, limit))


def load_reference(df: pd.DataFrame, code_col: str, desc_col: str | None, replace: bool,
                   user: str, source: str) -> int:
    """Store the authorised item list locally (fast, works offline). Re-run it to refresh from the source."""
    descs = df[desc_col] if desc_col else [""] * len(df)
    items = {}
    for code, desc in zip(df[code_col], descs):
        code = ident(code)
        if len(canon(code)) >= 2:
            items[canon(code)] = (canon(code), code, text(desc))
    if not items:
        raise ValueError("لم يُعثر على أي رموز بنود صالحة في العمود المحدد.")
    with db() as con:
        if replace:
            con.execute("DELETE FROM ref_items")
        con.executemany("INSERT INTO ref_items VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                        "code=excluded.code, description=excluded.description", list(items.values()))
        con.execute("INSERT INTO meta VALUES('ref_source',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"{source} · عدد البنود: {len(items):,} · {now()}",))
        _bump(con)
        _audit(con, user, "reference_load", f"{source}: عدد البنود {len(items)}، {'استبدال القائمة' if replace else 'إضافة إلى القائمة'}")
    return len(items)


def read_url(url: str) -> pd.DataFrame:
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("يُسمح بروابط http(s) فقط.")
    path = urlparse(url).path
    name = path if path.lower().rpartition(".")[2] in ("csv", "xlsx", "xlsm", "json") else "list.csv"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "memo-manager"}),
                                    timeout=30) as r:
            data = r.read(50_000_001)
    except Exception as e:
        raise ValueError(f"تعذّر تنزيل الملف: {e}") from e
    if len(data) > 50_000_000:
        raise ValueError("حجم الملف يتجاوز 50 ميغابايت.")
    return read_table(name, data)


def read_sql(db_url: str, query: str) -> pd.DataFrame:
    """Read a reference list from any SQLAlchemy-supported database (use a read-only account)."""
    q = query.strip().rstrip(";")
    if not q.lower().startswith("select") or ";" in q:
        raise ValueError("يُسمح بعبارة SELECT واحدة فقط.")
    try:
        import sqlalchemy as sa  # pip install sqlalchemy + your database driver
        engine = sa.create_engine(db_url)
        try:
            return pd.read_sql(sa.text(q), engine).fillna("").astype(str)
        finally:
            engine.dispose()
    except ImportError:
        raise ValueError("ثبّت sqlalchemy (ومشغّل قاعدة البيانات) لاستخدام هذا المصدر.") from None
    except Exception as e:
        raise ValueError(f"فشل استعلام قاعدة البيانات: {e}") from e


# ───────────────────────────── search ─────────────────────────────

def enrich(df: pd.DataFrame, ref: dict) -> pd.DataFrame:
    """Add verification columns and pre-folded search columns (computed once per data revision)."""
    df = df.copy()
    res = [consistency(a, c) for a, c in zip(df["authorized_item"], df["classified_item"])]
    df["check"] = [r[0] for r in res]
    df["common"] = [r[1] for r in res]
    for col, dst in (("authorized_item", "auth_ref"), ("classified_item", "cls_ref")):
        df[dst] = ["n/a" if not ref else ("valid" if canon(c) in ref else "invalid") for c in df[col]]
    for col in TEXT_COLS:
        df["_f_" + col] = df[col].map(fold)
    for col in CODE_COLS:
        df["_k_" + col] = df[col].map(canon)
    df["_f_file_name"] = df["file_name"].map(fold)
    dates = df["memo_date"].dt.strftime("%d/%m/%Y %Y-%m-%d")
    df["_blob"] = (df["_f_memo_number"] + " " + df["_f_goods_description"] + " " + df["_f_notes"] + " "
                   + df["_f_authorized_item"] + " " + df["_f_classified_item"] + " "
                   + df["_k_authorized_item"].str.lower() + " " + df["_k_classified_item"].str.lower() + " "
                   + df["_f_file_name"] + " " + dates)
    return df


def filter_memos(df: pd.DataFrame, q: str = "", per_field: dict | None = None, start=None, end=None,
                 checks=None, ref_state: str = "Any") -> pd.DataFrame:
    """Global search (every term must match somewhere; "quoted phrases" supported) plus per-field filters."""
    m = pd.Series(True, index=df.index)
    for a, b in re.findall(r'"([^"]+)"|(\S+)', fold(q)):
        m &= df["_blob"].str.contains(a or b, regex=False)
    for col, val in (per_field or {}).items():
        if col in CODE_COLS:  # hierarchical codes: "starts with" on the canonical form
            if canon(val):
                m &= df["_k_" + col].str.startswith(canon(val))
        elif fold(val).strip():
            m &= df["_f_" + col].str.contains(fold(val).strip(), regex=False)
    if start:
        m &= df["memo_date"] >= pd.Timestamp(start)
    if end:
        m &= df["memo_date"] <= pd.Timestamp(end)
    if checks:
        m &= df["check"].isin(checks)
    if ref_state == "Any invalid":
        m &= (df["auth_ref"] == "invalid") | (df["cls_ref"] == "invalid")
    elif ref_state == "All valid":
        m &= (df["auth_ref"] == "valid") & (df["cls_ref"] == "valid")
    return df[m]


# ───────────────────────────── export / import ─────────────────────────────

def sheet_safe(v: str) -> str:
    """Neutralise spreadsheet formula injection: cells starting with = + - @ are prefixed with an apostrophe."""
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


def export_bytes(df: pd.DataFrame, fmt: str) -> bytes:
    """fmt: csv (UTF-8 with BOM, opens correctly in Excel), xlsx, json."""
    out = df[list(EXPORT_COLS)].copy()
    out["check"] = out["check"].map(CHECK_AR)
    out["auth_ref"] = out["auth_ref"].map(REF_AR)
    out["cls_ref"] = out["cls_ref"].map(REF_AR)
    if fmt != "json":
        for c in TEXT_COLS:
            out[c] = out[c].map(sheet_safe)
    if fmt == "csv":
        out["memo_date"] = out["memo_date"].dt.strftime("%d/%m/%Y")
    elif fmt == "json":
        out["memo_date"] = out["memo_date"].dt.strftime("%Y-%m-%d")
    out = out.rename(columns=EXPORT_COLS)
    if fmt == "csv":
        return out.to_csv(index=False).encode("utf-8-sig")
    if fmt == "json":
        return json.dumps(out.to_dict("records"), ensure_ascii=False, indent=2).encode("utf-8")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        out.to_excel(xw, index=False, sheet_name="المذكرات")
        ws = xw.sheets["المذكرات"]
        ws.sheet_view.rightToLeft = True  # columns run right to left, like the Arabic interface
        for (cell,) in ws.iter_rows(min_row=2, min_col=2, max_col=2):  # memo_date column: real dates, DD/MM/YYYY
            cell.number_format = "DD/MM/YYYY"
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for col, width in zip("ABCDEFGHI", (24, 14, 60, 40, 24, 24, 14, 22, 22)):
            ws.column_dimensions[col].width = width
    return buf.getvalue()


def template_csv() -> bytes:
    return (",".join(v[0] for v in FIELDS.values()) + "\n").encode("utf-8-sig")


def read_table(name: str, data: bytes) -> pd.DataFrame:
    """CSV (UTF-8, delimiter auto-detected), Excel or JSON (list of records) -> all-text DataFrame."""
    ext = name.lower().rpartition(".")[2]
    if ext not in ("csv", "xlsx", "xlsm", "json"):
        raise ValueError("نوع الملف غير مدعوم. استخدم CSV أو XLSX أو JSON.")
    try:
        if ext == "csv":
            df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding="utf-8-sig",
                             sep=None, engine="python")
        elif ext == "json":
            df = pd.DataFrame(json.loads(data.decode("utf-8-sig")))
        else:
            df = pd.read_excel(io.BytesIO(data), dtype=str, keep_default_na=False)
    except UnicodeDecodeError:
        raise ValueError("الملف ليس بترميز UTF-8. احفظه بصيغة «CSV UTF-8» ثم أعد المحاولة.") from None
    except Exception as e:
        raise ValueError(f"تعذّرت قراءة الملف: {e}") from e
    if len(df) > MAX_IMPORT_ROWS:
        raise ValueError(f"يحتوي الملف على أكثر من {MAX_IMPORT_ROWS:,} صف. قسّمه واستورده على دفعات.")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _squash(s) -> str:
    return re.sub(r"[\W_]+", "", str(s).casefold())


def guess_mapping(columns) -> dict[str, str | None]:
    """Match file headers to memo fields by name (labels, field keys and common aliases)."""
    by_name = {_squash(c): c for c in columns}
    return {key: next((by_name[_squash(n)] for n in (key, label, *aliases) if _squash(n) in by_name), None)
            for key, (label, aliases) in FIELDS.items()}


def analyze_import(df: pd.DataFrame, mapping: dict, ref: dict | None = None, strict: bool = False) -> pd.DataFrame:
    """Validate every row without touching the database. status: new | exists | error."""
    with db() as con:
        existing = {r[0] for r in con.execute("SELECT memo_number FROM memos")}
    seen, out = {}, []
    for i, r in enumerate(df.to_dict("records")):
        raw = {k: (r.get(c) if c else "") for k, c in mapping.items()}
        if not any(text(v) for v in raw.values()):
            continue  # blank line
        n = i + 2  # spreadsheet row number (row 1 is the header)
        try:
            rec, errs = prepare(raw)
        except Exception:
            rec, errs = {k: "" for k in FIELDS}, ["تعذّرت قراءة الصف."]
        errs += ref_errors(rec, ref, strict)
        if not errs and rec["memo_number"] in seen:
            errs.append(f"مكرر للصف {seen[rec['memo_number']]} في هذا الملف.")
        elif not errs:
            seen[rec["memo_number"]] = n
        status = "error" if errs else ("exists" if rec["memo_number"] in existing else "new")
        out.append({"row": n, "status": status, "problem": " ".join(errs), **rec})
    return pd.DataFrame(out, columns=["row", "status", "problem", *FIELDS])


def commit_import(plan: pd.DataFrame, mode: str, user: str) -> int:
    """mode 'skip': add only new memos. mode 'update': also overwrite memos whose number already exists."""
    ok = plan[plan["status"] != "error"]
    if mode != "update":
        ok = ok[ok["status"] == "new"]
    if ok.empty:
        return 0
    sql = INSERT_SQL + (UPSERT_SQL if mode == "update" else " ON CONFLICT(memo_number) DO NOTHING")
    with db() as con:
        con.executemany(sql, [_params(r, user) for r in ok.to_dict("records")])
        _bump(con)
        _audit(con, user, "import", f"عدد الصفوف {len(ok)}، {'تحديث الموجود' if mode == 'update' else 'تخطي الموجود'}")
    return len(ok)


if __name__ == "__main__":
    import getpass
    import sys
    if len(sys.argv) == 4 and sys.argv[1] == "create-user":
        init_db()
        create_user(sys.argv[2], getpass.getpass("Password (min. 10 characters): "), sys.argv[3], actor="cli")
        print("User created.")
    else:
        print("usage: python memo_core.py create-user USERNAME viewer|editor|admin")

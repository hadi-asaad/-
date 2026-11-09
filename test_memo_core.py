"""Tests for memo_core. Run:  python test_memo_core.py   (or: pytest -q)"""
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from datetime import date, timedelta

import pandas as pd

import memo_core as mc


def fresh_db():
    mc.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
    mc.init_db()


def memo(num="CM-001", d="05/03/2024", desc="Laptop computer, 14 inch", auth="8471.30.00", cls="8471.30.00", notes=""):
    rec, errs = mc.prepare({"memo_number": num, "memo_date": d, "goods_description": desc, "notes": notes,
                            "authorized_item": auth, "classified_item": cls})
    assert not errs, errs
    return rec


def test_normalisation():
    assert mc.ident("  cm- ٢٠٢٤ /  ۰۱ ") == "CM- 2024 / 01"
    assert mc.canon("8471.30.00") == mc.canon("8471 30 00") == "84713000"
    assert mc.fold("Café ÉCLAIR") == "cafe eclair"
    assert mc.text(None) == "" and mc.text(float("nan")) == "" and mc.text(pd.NA) == ""
    assert mc.text("'=SUM(A1)") == "=SUM(A1)" and mc.text("'hello") == "'hello"


def test_dates():
    assert mc.parse_date("05/03/2024") == date(2024, 3, 5)          # day first
    assert mc.parse_date("2024-03-05") == date(2024, 3, 5)
    assert mc.parse_date("2024-03-05 00:00:00") == date(2024, 3, 5)  # what Excel cells become
    assert mc.parse_date("5.3.2024 14:30") == date(2024, 3, 5)
    assert mc.parse_date("٠٥/٠٣/٢٠٢٤") == date(2024, 3, 5)
    assert mc.parse_date(pd.Timestamp("2024-03-05")) == date(2024, 3, 5)
    assert mc.parse_date("31/02/2024") is None and mc.parse_date("5/3/24") is None
    assert mc.parse_date("") is None and mc.parse_date(None) is None and mc.parse_date(pd.NaT) is None


def test_consistency():
    assert mc.consistency("8471.30.00", "84713000") == ("Match", 8)
    assert mc.consistency("8471.30", "8471.50.00")[0] == "Partial"
    assert mc.consistency("8471", "8517")[0] == "Mismatch"
    assert mc.consistency("", "8517")[0] == "Incomplete"


def test_prepare_validation():
    _, errs = mc.prepare({})
    assert len(errs) == 5
    _, errs = mc.prepare({"memo_number": "x", "memo_date": date.today() + timedelta(days=30),
                          "goods_description": "abc", "authorized_item": "1", "classified_item": "8471"})
    assert any("بين" in e for e in errs) and any("المعتمد" in e for e in errs)
    rec, errs = mc.prepare({"memo_number": "x" * 65, "memo_date": "2024-01-01", "goods_description": "abc",
                            "authorized_item": "8471", "classified_item": "8471"})
    assert any("64" in e for e in errs)
    assert mc.ref_errors(memo(), {"84713000": ""}, strict=True) == []
    assert len(mc.ref_errors(memo(cls="9999.99"), {"84713000": ""}, strict=True)) == 1
    assert mc.ref_errors(memo(cls="9999.99"), {}, strict=True) == []   # nothing to verify against
    assert mc.ref_errors(memo(cls="9999.99"), {"84713000": ""}, strict=False) == []


def test_users_and_lockout():
    fresh_db()
    assert not mc.has_users()
    for bad in ("short", "admin-admin"):
        try:
            mc.create_user("admin-admin" if bad != "short" else "bob", bad, "admin")
            raise AssertionError("policy not enforced")
        except ValueError:
            pass
    mc.create_user("Admin", "correct horse battery", "admin")
    mc.create_user("eve", "another long password", "viewer", actor="admin")
    try:
        mc.create_user("ADMIN", "correct horse battery", "admin")
        raise AssertionError("duplicate user allowed")
    except ValueError:
        pass
    u, msg = mc.authenticate("admin", "correct horse battery")
    assert u == {"username": "admin", "role": "admin"} and not msg
    assert mc.authenticate("admin", "nope")[0] is None
    assert mc.authenticate("ghost", "nope")[0] is None
    for _ in range(mc.MAX_FAILS):
        mc.authenticate("eve", "wrong")
    assert mc.authenticate("eve", "another long password")[0] is None        # locked, even with right password
    mc.update_user("eve", "admin", role="editor")                           # admin edit unlocks
    assert mc.authenticate("eve", "another long password")[0]["role"] == "editor"
    # last-admin protection
    for kwargs in ({"role": "viewer"}, {"active": False}):
        try:
            mc.update_user("admin", "admin", **kwargs)
            raise AssertionError("last admin removed")
        except ValueError:
            pass
    mc.update_user("eve", "admin", active=False)
    assert mc.get_user("eve") is None and mc.authenticate("eve", "another long password")[0] is None
    mc.change_password("admin", "correct horse battery", "a brand new passphrase")
    assert mc.authenticate("admin", "a brand new passphrase")[0]
    try:
        mc.change_password("admin", "wrong", "whatever long enough")
        raise AssertionError
    except ValueError:
        pass
    with mc.db() as con:
        stored = con.execute("SELECT pw_hash FROM users WHERE username='admin'").fetchone()[0]
    assert stored.startswith("pbkdf2_sha256$") and "passphrase" not in stored
    actions = set(mc.read_audit()["action"])
    assert {"login", "login_failed", "user_create", "user_update", "password_change"} <= actions
    assert mc.can({"role": "editor"}, "write") and not mc.can({"role": "editor"}, "delete")
    assert not mc.can({"role": "viewer"}, "export") and mc.can({"role": "admin"}, "admin")


def test_crud_duplicates_and_conflicts():
    fresh_db()
    r0 = mc.revision()
    mc.add_memo(memo("cm-001"), "u")
    assert mc.revision() == r0 + 1
    for variant in ("CM-001", " cm-001 ", "Cm-001"):
        try:
            mc.add_memo(memo(variant), "u")
            raise AssertionError("duplicate allowed")
        except mc.DuplicateError:
            pass
    assert mc.find_memo("CM-001")["id"] == 1 and mc.find_memo("CM-001", exclude_id=1) is None
    mc.add_memo(memo("CM-002"), "u")
    df = mc.load_memos()
    row = df[df.memo_number == "CM-001"].iloc[0]
    assert mc.update_memo(int(row.id), int(row.version), memo("CM-001", desc="Laptop, 15 inch"), "ed") is True
    try:   # stale version
        mc.update_memo(int(row.id), int(row.version), memo("CM-001", desc="Other"), "ed2")
        raise AssertionError("lost update")
    except mc.ConflictError:
        pass
    try:   # renaming onto an existing number
        mc.update_memo(int(row.id), int(row.version) + 1, memo("CM-002"), "ed")
        raise AssertionError("duplicate via rename")
    except mc.DuplicateError:
        pass
    assert mc.update_memo(int(row.id), int(row.version) + 1, memo("CM-001", desc="Laptop, 15 inch"), "ed") is False
    audit = mc.read_audit()
    assert any(mc.FIELDS["goods_description"][0] in d for d in audit["detail"].dropna())
    mc.delete_memo(int(row.id), "admin")
    assert len(mc.load_memos()) == 1 and mc.find_memo("CM-001") is None
    mc.delete_memo(999, "admin")  # deleting a missing memo is a no-op


def seed():
    fresh_db()
    rows = [("CM-001", "05/03/2024", "Laptop computer, 14 inch\nwith battery", "8471.30.00", "8471.30.00", "Special: lithium"),
            ("CM-002", "17/11/2023", "Cotton fabric, woven", "5208.12.00", "5208.12.10", ""),
            ("CM-003", "01/01/2025", "Café espresso machine", "8419.81", "8516.71", "Électrique"),
            ("CM-004", "2025-06-30", "Unknown widget", "9999.99.99", "9999.99.99", "")]
    for r in rows:
        mc.add_memo(memo(r[0], r[1], r[2], r[3], r[4], r[5]), "u")
    return mc.enrich(mc.load_memos(), {})


def test_search_and_filters():
    df = seed()
    assert list(df.memo_number) == ["CM-004", "CM-003", "CM-001", "CM-002"]      # newest first
    f = lambda **k: sorted(mc.filter_memos(df, **k).memo_number)
    assert f(q="laptop") == ["CM-001"]
    assert f(q="LAPTOP lithium") == ["CM-001"]                                    # AND across fields
    assert f(q='"cotton fabric"') == ["CM-002"] and f(q='"fabric cotton"') == []
    assert f(q="cafe") == ["CM-003"] and f(q="electrique") == ["CM-003"]          # accent-insensitive
    assert f(q="847130") == ["CM-001"] and f(q="8471.30") == ["CM-001"]           # code in any format
    assert f(q="03/2024") == ["CM-001"] and f(q="2025-06") == ["CM-004"]          # dates in both formats
    assert f(q="cm-00") == ["CM-001", "CM-002", "CM-003", "CM-004"]
    assert f(per_field={"authorized_item": "5208"}) == ["CM-002"]
    assert f(per_field={"classified_item": "8516 71"}) == ["CM-003"]
    assert f(per_field={"notes": "lithium", "memo_number": "001"}) == ["CM-001"]
    assert f(start=date(2024, 1, 1), end=date(2025, 1, 1)) == ["CM-001", "CM-003"]  # inclusive
    assert f(checks=["Mismatch"]) == ["CM-003"] and f(checks=["Partial"]) == ["CM-002"]
    assert f(checks=["Match"]) == ["CM-001", "CM-004"]
    assert len(f(q="zzz")) == 0
    empty = mc.enrich(mc.load_memos().iloc[0:0], {})
    assert len(mc.filter_memos(empty, q="x", per_field={"notes": "x", "authorized_item": "1"},
                               start=date(2020, 1, 1), checks=["Match"])) == 0


def test_reference_list_and_strict():
    df = seed()
    ref_df = pd.DataFrame({"code": ["8471.30.00", "8471 50", "5208.12.00", "8419.81.00"],
                           "desc": ["Portable computers", "Processing units", "Cotton fabric", "Cooking appliances"]})
    assert mc.load_reference(ref_df, "code", "desc", True, "admin", "unit test") == 4
    ref = mc.ref_map()
    assert ref["84713000"] == "Portable computers" and "847150" in ref
    e = mc.enrich(mc.load_memos(), ref).set_index("memo_number")
    assert e.loc["CM-001", ["auth_ref", "cls_ref"]].tolist() == ["valid", "valid"]
    assert e.loc["CM-002", ["auth_ref", "cls_ref"]].tolist() == ["valid", "invalid"]
    assert e.loc["CM-004", ["auth_ref", "cls_ref"]].tolist() == ["invalid", "invalid"]
    assert sorted(mc.filter_memos(e.reset_index(), ref_state="Any invalid").memo_number) == ["CM-002", "CM-003", "CM-004"]
    assert sorted(mc.filter_memos(e.reset_index(), ref_state="All valid").memo_number) == ["CM-001"]
    assert len(mc.search_reference("cotton")) == 1 and len(mc.search_reference("847130")) == 1
    assert len(mc.search_reference("")) == 4
    # merge keeps old rows, replace drops them
    mc.load_reference(pd.DataFrame({"c": ["9999.99.99"]}), "c", None, False, "admin", "merge")
    assert len(mc.ref_map()) == 5
    mc.load_reference(pd.DataFrame({"c": ["9999.99.99"]}), "c", None, True, "admin", "replace")
    assert len(mc.ref_map()) == 1
    try:
        mc.load_reference(pd.DataFrame({"c": ["", "x"]}), "c", None, True, "admin", "bad")
        raise AssertionError
    except ValueError:
        pass
    assert "reference_load" in set(mc.read_audit()["action"])


def test_export_formats_and_round_trip():
    seed()
    mc.add_memo(memo("CM-005", "01/02/2024", "=HYPERLINK(\"http://evil\")\nline two", "8471", "8471", "-negative start"), "u")
    ref = {"84713000": "x"}
    df = mc.enrich(mc.load_memos(), ref)
    for fmt in ("csv", "xlsx", "json"):
        blob = mc.export_bytes(df, fmt)
        assert blob
        back = mc.read_table(f"memos.{fmt}", blob)
        assert len(back) == 5, fmt
        m = mc.guess_mapping(back.columns)
        assert all(m[k] for k in mc.FIELDS), (fmt, m)          # headers we export are recognised on import
        plan = mc.analyze_import(back, m)
        assert (plan.status == "exists").all(), (fmt, plan[["row", "status", "problem"]])
        got = plan.set_index("memo_number")
        assert got.loc["CM-005", "goods_description"].startswith('=HYPERLINK("http://evil")\nline two')  # round-trips unchanged
        assert got.loc["CM-005", "notes"] == "-negative start"
        assert got.loc["CM-001", "memo_date"] == "2024-03-05"
    # the exported files must not contain live formulas / unescaped injections
    csv_text = mc.export_bytes(df, "csv").decode("utf-8-sig")
    assert csv_text.startswith(f"{mc.FIELDS['memo_number'][0]},{mc.FIELDS['memo_date'][0]}")
    assert "'=HYPERLINK" in csv_text and ",05/03/2024," in csv_text
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(mc.export_bytes(df, "xlsx"))).active
    cell = [c for row in ws.iter_rows(min_row=2) for c in row if isinstance(c.value, str) and "HYPERLINK" in c.value][0]
    assert cell.data_type == "s" and cell.value.startswith("'=")
    date_cell = ws.cell(row=2, column=2)
    assert date_cell.number_format == "DD/MM/YYYY" and date_cell.value is not None
    assert ws.freeze_panes == "A2"
    assert json.loads(mc.export_bytes(df, "json"))[0][mc.FIELDS["memo_date"][0]] == "2025-06-30"
    # empty export still works
    assert mc.export_bytes(df.iloc[0:0], "xlsx") and mc.export_bytes(df.iloc[0:0], "csv")


def test_import_analysis_and_commit():
    seed()
    csv = ("Memo No;Date;Description;Remarks;Authorized;Classified\n"        # semicolon-delimited, other headers
           "CM-001;05/03/2024;Updated laptop text;;8471.30.00;8471.30.00\n"  # exists
           "CM-010;٠١/٠٢/٢٠٢٤;Arabic-digit date;;8471;8471\n"                # new (Arabic-Indic digits)
           "cm-010;02/02/2024;Same number again;;8471;8471\n"                 # duplicate within file
           "CM-011;31/02/2024;Bad date;;8471;8471\n"                          # error
           "CM-012;01/02/2024;;;8471;8471\n"                                   # missing description
           "CM-013;01/02/2024;Short code;;8;8471\n"                            # malformed code
           ";;;;;\n"                                                           # blank
           "CM-014;01/02/2024;\"Multi\nline, with comma\";note;8517.12;8517.13\n").encode("utf-8")
    df = mc.read_table("x.csv", csv)
    m = mc.guess_mapping(df.columns)
    assert m == {"memo_number": "Memo No", "memo_date": "Date", "goods_description": "Description",
                 "notes": "Remarks", "authorized_item": "Authorized", "classified_item": "Classified"}
    plan = mc.analyze_import(df, m)
    st = plan.set_index("memo_number").status.to_dict()
    assert plan.status.value_counts().to_dict() == {"error": 4, "new": 2, "exists": 1}, plan[["row", "memo_number", "status", "problem"]]
    assert plan[plan.memo_number == "CM-010"].iloc[0].memo_date == "2024-02-01"
    assert "مكرر للصف" in plan[(plan.status == "error") & (plan.row == 4)].iloc[0].problem
    assert st["CM-001"] == "exists" and st["CM-014"] == "new"
    strict_plan = mc.analyze_import(df, m, ref={"85171200": ""}, strict=True)
    assert (strict_plan.status == "error").sum() > 4
    # skip mode: only new rows
    assert mc.commit_import(plan, "skip", "ed") == 2
    assert mc.find_memo("CM-010") and mc.find_memo("CM-014")
    before = mc.load_memos().set_index("memo_number").loc["CM-001"]
    assert before.goods_description.startswith("Laptop computer")          # untouched in skip mode
    # update mode: existing overwritten, version bumped
    plan2 = mc.analyze_import(df, m)
    assert plan2.status.value_counts().to_dict() == {"error": 4, "exists": 3}
    assert mc.commit_import(plan2, "update", "ed") == 3
    after = mc.load_memos().set_index("memo_number").loc["CM-001"]
    assert after.goods_description == "Updated laptop text" and after.version == before.version + 1
    assert "Multi\nline, with comma" == mc.load_memos().set_index("memo_number").loc["CM-014"].goods_description
    assert mc.commit_import(plan2.iloc[0:0], "update", "ed") == 0
    # bad inputs
    for name, data in (("x.txt", b"a"), ("x.csv", b""), ("x.json", b"{not json"), ("x.xlsx", b"not a zip"),
                       ("x.csv", "Memo\nΩ".encode("cp1253"))):
        try:
            mc.read_table(name, data)
            raise AssertionError(name)
        except ValueError:
            pass
    mc.MAX_IMPORT_ROWS, old = 2, mc.MAX_IMPORT_ROWS
    try:
        mc.read_table("x.csv", b"a\n1\n2\n3\n")
        raise AssertionError
    except ValueError:
        pass
    mc.MAX_IMPORT_ROWS = old


def test_encryption_at_rest():
    from cryptography.fernet import Fernet
    fresh_db()
    mc.ENC_KEY = Fernet.generate_key().decode()
    mc._fernet.cache_clear()
    try:
        mc.add_memo(memo("CM-ENC", desc="Highly confidential description", notes="secret note"), "u")
        raw = sqlite3.connect(mc.DB_PATH).execute("SELECT goods_description, notes, memo_number FROM memos").fetchone()
        assert raw[0].startswith("~enc1~") and "confidential" not in raw[0] and "secret" not in raw[1]
        assert raw[2] == "CM-ENC"
        df = mc.load_memos()
        assert df.iloc[0].goods_description == "Highly confidential description" and df.iloc[0].notes == "secret note"
        row = df.iloc[0]
        assert mc.update_memo(int(row.id), int(row.version), memo("CM-ENC", desc="Highly confidential description",
                                                                 notes="secret note"), "u") is False  # unchanged
        mc.ENC_KEY = ""
        mc._fernet.cache_clear()
        try:
            mc.load_memos()
            raise AssertionError("loaded encrypted data without key")
        except RuntimeError as e:
            assert "MEMO_ENC_KEY" in str(e)
    finally:
        mc.ENC_KEY = ""
        mc._fernet.cache_clear()


def test_settings_and_db_file():
    fresh_db()
    assert mc.get_setting("strict", "0") == "0"
    mc.set_setting("strict", "1", "admin")
    assert mc.get_setting("strict") == "1"
    if os.name == "posix":
        assert oct(os.stat(mc.DB_PATH).st_mode & 0o777) == "0o600"
    # SQL injection attempts are inert (parameterised queries)
    mc.add_memo(memo("X'); DROP TABLE memos;--"), "u")
    assert len(mc.load_memos()) == 1
    for bad in ("DELETE FROM memos", "select 1; drop table memos"):
        try:
            mc.read_sql("sqlite://", bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    try:
        mc.read_url("file:///etc/passwd")
        raise AssertionError
    except ValueError:
        pass


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def xlsx_bytes():
    from openpyxl import Workbook
    wb, buf = Workbook(), io.BytesIO()
    wb.active["A1"] = "memo"
    wb.save(buf)
    return buf.getvalue()


PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<<>>\n%%EOF\n" + os.urandom(2000)
DOCX = make_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>"})
PPTX = make_zip({"[Content_Types].xml": b"<Types/>", "ppt/presentation.xml": b"<p:presentation/>"})
ODT = make_zip({"mimetype": b"application/vnd.oasis.opendocument.text", "content.xml": b"<office/>"})
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + os.urandom(600)
RTF = b"{\\rtf1\\ansi Hello}"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc as e:
        return e
    raise AssertionError(f"{exc.__name__} not raised")


def test_memo_file_validation():
    ok = [("memo.pdf", PDF, "application/pdf"), ("Memo.DOCX", DOCX, DOCX_MIME),
          ("m.xlsx", xlsx_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
          ("m.pptx", PPTX, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
          ("m.odt", ODT, "application/vnd.oasis.opendocument.text"), ("m.doc", OLE, "application/msword"),
          ("m.doc", RTF, "application/msword"), ("m.xls", OLE, "application/vnd.ms-excel"),
          ("m.ppt", OLE, "application/vnd.ms-powerpoint"), ("m.rtf", RTF, "application/rtf")]
    for name, data, mime in ok:
        att = mc.prepare_attachment(name, data)
        assert att == {"filename": name, "mime": mime, "data": data}, name
    macro_docx = make_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"x", "word/vbaProject.bin": b"x"})
    bad = [("evil.exe", b"MZ" + bytes(100)), ("m.docm", DOCX), ("noext", PDF), ("", PDF), ("m.pdf", b""),
           ("m.pdf", DOCX), ("m.docx", xlsx_bytes()), ("m.docx", PDF), ("m.docx", OLE), ("m.pdf", b"x" * 2000 + b"%PDF-"),
           ("m.xlsx", b"PK\x03\x04garbage"), ("m.pdf", b"<html><script>alert(1)</script></html>"),
           ("m.odt", make_zip({"mimetype": b"application/vnd.oasis.opendocument.spreadsheet"}))]
    for name, data in bad:
        raises(ValueError, mc.prepare_attachment, name, data)
    assert "تحتوي على ماكرو" in str(raises(ValueError, mc.prepare_attachment, "m.docx", macro_docx))
    assert "لا يطابق" in str(raises(ValueError, mc.prepare_attachment, "m.pdf", DOCX))
    mc.MAX_ATTACHMENT, old = len(PDF) - 1, mc.MAX_ATTACHMENT
    try:
        assert "يتجاوز" in str(raises(ValueError, mc.prepare_attachment, "m.pdf", PDF))
    finally:
        mc.MAX_ATTACHMENT = old
    # file names: no directories, no control or invisible characters, Unicode kept, bounded length
    assert mc.safe_filename("../../etc/passwd.pdf") == "passwd.pdf"
    assert mc.safe_filename("C:\\Users\\x\\memo.docx") == "memo.docx"
    assert mc.safe_filename("me\x00mo\n\u200f.pdf") == "memo.pdf"
    assert mc.safe_filename("  ..hidden.pdf ") == "hidden.pdf"
    assert mc.safe_filename("مذكرة تصنيف ٢٠٢٤.pdf") == "مذكرة تصنيف ٢٠٢٤.pdf"
    long_name = mc.safe_filename("a" * 300 + ".pdf")
    assert len(long_name) == 120 and long_name.endswith(".pdf")
    assert mc.prepare_attachment("../../x/Agreement.pdf", PDF)["filename"] == "Agreement.pdf"


def test_memo_file_round_trip():
    fresh_db()
    mid = mc.add_memo(memo("CM-F1"), "u", mc.prepare_attachment("Agreement 2025.pdf", PDF))
    plain = mc.add_memo(memo("CM-F2"), "u")
    got = mc.get_attachment(mid)
    assert got["data"] == PDF and got["filename"] == "Agreement 2025.pdf" and got["mime"] == "application/pdf"
    assert got["size"] == len(PDF) and got["sha256"] == hashlib.sha256(PDF).hexdigest() and got["uploaded_by"] == "u"
    assert mc.get_attachment(plain) is None and mc.get_attachment(9999) is None
    df = mc.load_memos().set_index("memo_number")
    assert df.loc["CM-F1", "file_name"] == "Agreement 2025.pdf" and df.loc["CM-F1", "file_size"] == len(PDF)
    assert df.loc["CM-F2", "file_name"] == "" and df.loc["CM-F2", "file_size"] == 0
    assert "data" not in df.columns and "sha256" not in df.columns   # file contents never ride along with the table
    e = mc.enrich(mc.load_memos(), {})
    assert list(mc.filter_memos(e, q="agreement 2025").memo_number) == ["CM-F1"]  # searchable by file name
    assert any("الملف=Agreement 2025.pdf" in d for d in mc.read_audit()["detail"].dropna())
    # a memo and its file are saved together: a rejected file leaves no memo behind
    raises(ValueError, mc.add_memo, memo("CM-F3"), "u", {"filename": "x.pdf", "mime": "application/pdf", "data": b"nope"})
    assert mc.find_memo("CM-F3") is None
    # duplicate memo number: the file is not stored either
    raises(mc.DuplicateError, mc.add_memo, memo("CM-F1"), "u", mc.prepare_attachment("other.pdf", PDF))
    assert mc.get_attachment(mid)["filename"] == "Agreement 2025.pdf"


def test_memo_file_replace_remove_delete():
    fresh_db()
    mid = mc.add_memo(memo("CM-F1"), "u", mc.prepare_attachment("a.pdf", PDF))
    v = int(mc.load_memos().iloc[0].version)
    # replacing only the file still counts as a change and bumps the version
    assert mc.update_memo(mid, v, memo("CM-F1"), "ed", attachment=mc.prepare_attachment("b.docx", DOCX)) is True
    got = mc.get_attachment(mid)
    assert got["filename"] == "b.docx" and got["data"] == DOCX and got["mime"] == DOCX_MIME
    assert int(mc.load_memos().iloc[0].version) == v + 1
    # stale version: nothing is written and the current file stays
    raises(mc.ConflictError, mc.update_memo, mid, v, memo("CM-F1"), "ed2", attachment=mc.prepare_attachment("c.pdf", PDF))
    assert mc.get_attachment(mid)["filename"] == "b.docx"
    v += 1
    assert mc.update_memo(mid, v, memo("CM-F1"), "ed") is False                       # nothing changed
    assert mc.update_memo(mid, v, memo("CM-F1"), "ed", remove_file=True) is True      # remove
    assert mc.get_attachment(mid) is None and mc.load_memos().iloc[0].file_name == ""
    v += 1
    assert mc.update_memo(mid, v, memo("CM-F1"), "ed", remove_file=True) is False     # nothing left to remove
    # a new upload wins over the remove flag
    assert mc.update_memo(mid, v, memo("CM-F1"), "ed", attachment=mc.prepare_attachment("d.pdf", PDF), remove_file=True)
    assert mc.get_attachment(mid)["filename"] == "d.pdf"
    mc.delete_memo(mid, "admin")
    with mc.db() as con:
        assert con.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    # the foreign key cleans up even when a memo row is removed directly
    mid = mc.add_memo(memo("CM-F2"), "u", mc.prepare_attachment("a.pdf", PDF))
    with mc.db() as con:
        con.execute("DELETE FROM memos WHERE id=?", (mid,))
        assert con.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    details = " ".join(mc.read_audit()["detail"].dropna())
    assert "الملف=b.docx" in details and "حذف الملف" in details


def test_memo_file_encryption_and_integrity():
    from cryptography.fernet import Fernet
    fresh_db()
    plain_id = mc.add_memo(memo("CM-P"), "u", mc.prepare_attachment("plain.pdf", PDF))  # stored before any key exists
    mc.ENC_KEY = Fernet.generate_key().decode()
    mc._fernet.cache_clear()
    try:
        enc_id = mc.add_memo(memo("CM-E"), "u", mc.prepare_attachment("secret.pdf", PDF))
        con = sqlite3.connect(mc.DB_PATH)
        raw = dict(con.execute("SELECT memo_id, data FROM attachments").fetchall())
        assert raw[plain_id] == PDF                                                  # old row untouched
        assert raw[enc_id].startswith(b"~enc1~") and PDF[:40] not in raw[enc_id]
        assert mc.get_attachment(enc_id)["data"] == PDF and mc.get_attachment(plain_id)["data"] == PDF
        # tampering is detected, encrypted or not
        con.execute("UPDATE attachments SET data=? WHERE memo_id=?", (PDF[:-1] + b"X", plain_id))
        con.execute("UPDATE attachments SET data=? WHERE memo_id=?", (raw[enc_id][:-3] + b"AAA", enc_id))
        con.commit()
        con.close()
        assert "السلامة" in str(raises(RuntimeError, mc.get_attachment, plain_id))
        assert "فك تشفير" in str(raises(RuntimeError, mc.get_attachment, enc_id))
        mc.ENC_KEY = ""
        mc._fernet.cache_clear()
        assert "MEMO_ENC_KEY" in str(raises(RuntimeError, mc.get_attachment, enc_id))
    finally:
        mc.ENC_KEY = ""
        mc._fernet.cache_clear()


def test_existing_database_is_upgraded():
    fresh_db()
    mc.add_memo(memo("CM-OLD"), "u")
    with mc.db() as con:
        con.execute("DROP TABLE attachments")      # a database created before memo files existed
    mc.init_db()                                   # what app start-up does
    row = mc.load_memos().iloc[0]
    assert row.file_name == "" and row.file_size == 0
    assert mc.update_memo(int(row.id), int(row.version), memo("CM-OLD"), "u",
                          attachment=mc.prepare_attachment("late.pdf", PDF))
    assert mc.get_attachment(int(row.id))["filename"] == "late.pdf"


def has_arabic(text):
    return bool(re.search(r"[\u0600-\u06FF]", str(text)))


def test_arabic_labels_aliases_and_export_values():
    assert all(has_arabic(label) for label, _ in mc.FIELDS.values())
    assert all(has_arabic(h) for h in mc.EXPORT_COLS.values())
    assert set(mc.CHECK_AR) == {"Match", "Partial", "Mismatch", "Incomplete"} and all(map(has_arabic, mc.CHECK_AR.values()))
    assert set(mc.ROLE_AR) == set(mc.ROLES) and set(mc.REF_AR) == {"valid", "invalid", "n/a"}
    # Arabic headers, the labels themselves and the English headers of earlier exports all map to the same fields
    ar = ["رقم المذكرة", "التاريخ", "الوصف", "الملاحظات", "البند المعتمد", "البند المصنف"]
    assert mc.guess_mapping(ar) == dict(zip(mc.FIELDS, ar))
    assert mc.guess_mapping([v[0] for v in mc.FIELDS.values()]) == {k: v[0] for k, v in mc.FIELDS.items()}
    old = ["Classification Memo Number", "Classification Memo Date", "Goods Description", "Notes / Special Properties",
           "Authorized Classification Item", "Classified Under Item"]
    assert mc.guess_mapping(old) == dict(zip(mc.FIELDS, old))
    # an Arabic CSV imports end to end (Arabic digits in the date and the item codes)
    fresh_db()
    csv = ("رقم المذكرة,التاريخ,الوصف,البند المعتمد,البند المصنف\n"
           "م-٢٠٢٥-١,٠٥/٠٣/٢٠٢٥,قماش قطني منسوج,٥٢٠٨.١٢.٠٠,5208.12.00\n").encode("utf-8")
    df = mc.read_table("x.csv", csv)
    plan = mc.analyze_import(df, mc.guess_mapping(df.columns))
    assert plan.status.tolist() == ["new"] and plan.memo_number[0] == "م-2025-1" and plan.memo_date[0] == "2025-03-05"
    mc.commit_import(plan, "skip", "u")
    e = mc.enrich(mc.load_memos(), {"52081200": "قماش"})
    assert e.check[0] == "Match" and list(mc.filter_memos(e, q="قطني").memo_number) == ["م-2025-1"]
    # exports carry Arabic values and an Arabic, right-to-left worksheet
    csv_text = mc.export_bytes(e, "csv").decode("utf-8-sig")
    assert mc.CHECK_AR["Match"] in csv_text and mc.REF_AR["valid"] in csv_text and "Match" not in csv_text
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(mc.export_bytes(e, "xlsx"))).active
    assert ws.title == "المذكرات" and ws.sheet_view.rightToLeft is True
    assert ws["A1"].value == mc.FIELDS["memo_number"][0] and ws["A2"].value == "م-2025-1"
    assert json.loads(mc.export_bytes(e, "json"))[0][mc.EXPORT_COLS["check"]] == mc.CHECK_AR["Match"]
    # the audit trail is Arabic too
    mc.create_user("sara", "a long enough password", "editor", actor="admin")
    mc.set_setting("strict", "1", "admin")
    details = " ".join(mc.read_audit()["detail"].dropna())
    assert mc.ROLE_AR["editor"] in details and "الوضع الصارم: مفعّل" in details and "عدد الصفوف" in details


def test_every_message_is_arabic():
    fresh_db()
    mc.create_user("admin", "a long enough password", "admin")
    pw = "a long enough password"
    msgs = list(mc.prepare({})[1])
    msgs += mc.prepare({"memo_number": "x" * 70, "memo_date": "2099-01-01", "goods_description": "x" * 30_000,
                        "authorized_item": "1", "classified_item": "1"})[1]
    msgs += mc.ref_errors(memo(cls="9999.99"), {"84713000": ""}, True)
    macro = make_zip({"[Content_Types].xml": b"x", "word/vbaProject.bin": b"x"})
    calls = [(mc.create_user, ("ab", pw), {}), (mc.create_user, ("bob", "short"), {}),
             (mc.create_user, ("bob", pw, "boss"), {}), (mc.create_user, ("admin", pw), {}),
             (mc.create_user, ("bob", "bob"), {}), (mc.update_user, ("ghost", "admin"), {}),
             (mc.update_user, ("admin", "admin"), {"active": False}), (mc.change_password, ("admin", "wrong", "x" * 12), {}),
             (mc.read_table, ("x.txt", b"a"), {}), (mc.read_table, ("x.csv", "Ω".encode("cp1253")), {}),
             (mc.read_table, ("x.json", b"{bad"), {}), (mc.read_url, ("ftp://example.org/x.csv",), {}),
             (mc.read_sql, ("sqlite://", "DROP TABLE x"), {}), (mc.read_sql, ("sqlite://", "select 1; select 2"), {}),
             (mc.load_reference, (pd.DataFrame({"c": [""]}), "c", None, True, "admin", "x"), {}),
             (mc.prepare_attachment, ("x.exe", PDF), {}), (mc.prepare_attachment, ("x.pdf", b""), {}),
             (mc.prepare_attachment, ("x.pdf", DOCX), {}), (mc.prepare_attachment, ("x.docx", macro), {})]
    for fn, args, kwargs in calls:
        msgs.append(str(raises(ValueError, fn, *args, **kwargs)))
    msgs.append(mc.authenticate("admin", "wrong")[1])
    mid = mc.add_memo(memo("CM-1"), "u")
    msgs.append(str(raises(mc.DuplicateError, mc.add_memo, memo("CM-1"), "u")))
    msgs.append(str(raises(mc.ConflictError, mc.update_memo, mid, 99, memo("CM-1"), "u")))
    df = mc.read_table("x.csv", b"Memo No,Date,Description,Authorized,Classified\n"
                                b"A,01/01/2024,abc def,8471,8471\nA,01/01/2024,abc def,8471,8471\n")
    plan = mc.analyze_import(df, mc.guess_mapping(df.columns))
    msgs += plan[plan.status == "error"].problem.tolist()
    mc.MAX_ATTACHMENT, old_max = 5, mc.MAX_ATTACHMENT
    try:
        msgs.append(str(raises(ValueError, mc.prepare_attachment, "x.pdf", PDF)))
    finally:
        mc.MAX_ATTACHMENT = old_max
    mc.MAX_IMPORT_ROWS, old_rows = 1, mc.MAX_IMPORT_ROWS
    try:
        msgs.append(str(raises(ValueError, mc.read_table, "x.csv", b"a\n1\n2\n")))
    finally:
        mc.MAX_IMPORT_ROWS = old_rows
    assert len(msgs) > 30 and all(msgs), len(msgs)
    english = [m for m in msgs if not has_arabic(m)]
    assert not english, english


def test_remember_me_tokens():
    fresh_db()
    pw = "a long enough password"
    mc.create_user("sara", pw, "editor")
    mc.create_user("omar", pw, "viewer")
    token = mc.create_remember_token("sara")
    assert re.fullmatch(r"[A-Za-z0-9_-]{40,}", token)                       # URL-safe, so it is safe in a cookie
    with mc.db() as con:
        rows = con.execute("SELECT * FROM remember_tokens").fetchall()
    assert len(rows) == 1 and rows[0]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in " ".join(str(v) for v in tuple(rows[0]))              # only the hash is stored
    assert mc.restore_remembered(token) == {"username": "sara", "role": "editor"}
    for bad in (None, "", "nope", "x" * 500, token[:-1], token + "A"):
        assert mc.restore_remembered(bad) is None
    actions = set(mc.read_audit()["action"])
    assert {"remember_on", "login_remembered"} <= actions
    # expiry
    with mc.db() as con:
        con.execute("UPDATE remember_tokens SET expires_at=?", ("2000-01-01T00:00:00",))
    assert mc.restore_remembered(token) is None
    # expired rows are purged when the next token is created, and each user keeps at most MAX_REMEMBER_TOKENS
    tokens = [mc.create_remember_token("sara") for _ in range(mc.MAX_REMEMBER_TOKENS + 2)]
    other = mc.create_remember_token("omar")
    with mc.db() as con:
        per_user = dict(con.execute("SELECT username, COUNT(*) FROM remember_tokens GROUP BY username").fetchall())
    assert per_user == {"sara": mc.MAX_REMEMBER_TOKENS, "omar": 1}
    assert mc.restore_remembered(tokens[-1]) and mc.restore_remembered(tokens[0]) is None   # the oldest device dropped
    # signing out of one device leaves the others alone
    mc.forget_token(tokens[-1])
    mc.forget_token(None)
    assert mc.restore_remembered(tokens[-1]) is None and mc.restore_remembered(tokens[-2]) and mc.restore_remembered(other)
    # a password change signs the user out everywhere
    mc.change_password("sara", pw, "a different long password")
    assert all(mc.restore_remembered(t) is None for t in tokens)
    assert mc.restore_remembered(other)["username"] == "omar"                  # other users are unaffected
    # an administrator resetting a password, or deactivating the account, does the same
    t1 = mc.create_remember_token("omar")
    mc.update_user("omar", "admin", role="editor")                             # a role change keeps the sign-ins
    assert mc.restore_remembered(t1)["role"] == "editor"
    mc.update_user("omar", "admin", password="a reset long password")
    assert mc.restore_remembered(t1) is None and mc.restore_remembered(other) is None
    t2 = mc.create_remember_token("omar")
    mc.update_user("omar", "admin", active=False)
    assert mc.restore_remembered(t2) is None


def css_blocks_ok(text):
    """The Markdown parser behind st.markdown must see only <style> blocks, each ending alone on its last line.
    Anything else is rendered as visible text (and `*/ ... /*` comment marks turn into italics)."""
    inside = False
    for line in text.split("\n"):
        if not inside:
            if not line.strip():
                continue
            if not line.lstrip(" ").lower().startswith("<style"):
                return False
            inside = True
        if "</style>" in line.lower():
            if line.lower().split("</style>", 1)[1].strip():
                return False
            inside = False
    return not inside


def test_css_survives_markdown():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
    if not os.path.exists(path):
        return
    src = open(path, encoding="utf-8").read()
    ns = {}
    for name in ("RTL_CSS", "STYLE_CSS"):
        a = src.index(f'{name} = """')
        exec(src[a:src.index('"""', a + len(name) + 6) + 3], ns)
    exec(re.search(r"^APP_CSS = .*$", src, flags=re.M).group(), ns)
    assert css_blocks_ok(ns["APP_CSS"]) and css_blocks_ok(ns["RTL_CSS"]) and css_blocks_ok(ns["STYLE_CSS"])
    assert f"st.markdown(APP_CSS," in src                                   # the joined string is what gets injected
    assert not css_blocks_ok(ns["RTL_CSS"] + ns["STYLE_CSS"])                  # the old '</style><style>' junction is caught
    assert not css_blocks_ok("<style>a{}</style>\ntext\n<style>b{}</style>")
    try:
        from markdown_it import MarkdownIt                                     # optional: a real CommonMark parser
    except ImportError:
        return
    md = MarkdownIt("commonmark", {"html": True})
    assert [t.type for t in md.parse(ns["APP_CSS"]) if t.level == 0] == ["html_block", "html_block"]
    assert "paragraph_open" in [t.type for t in md.parse(ns["RTL_CSS"] + ns["STYLE_CSS"]) if t.level == 0]


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print("ok  ", name)
    print(f"\n{len(tests)} tests passed")

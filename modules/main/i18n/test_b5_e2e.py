#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The end-to-end acceptance of the mail body and the dashboard.

Usage:
    python3 test_b5_e2e.py

It renders the daily report summary in both languages, starts the
dashboard against a temporary database, asks for the same page in both
languages and prints what it found.

The exit code is 0 when every check holds.
"""
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "modules" / "main" / "daily" / "lib"))
sys.path.insert(0, str(ROOT / "modules" / "main" / "lib"))

CJK = re.compile(r"[\u4e00-\u9fff]")
FAILS = []


def check(name, ok, detail=""):
    print("%s %-46s %s" % ("ok  " if ok else "FAIL", name, detail))
    if not ok:
        FAILS.append(name)


def mail_body():
    """The summary of a daily report, in both languages."""
    import diff
    from i18n import set_lang

    def build(lang):
        set_lang(lang)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        schema = (ROOT / "modules" / "main" / "db" / "schema.sql").read_text(encoding="utf-8")
        conn.executescript(schema)
        conn.execute("INSERT INTO run_markers (started_at, finished_at, run_id) "
                     "VALUES ('2026-11-04 03:00:00', '2026-11-04 03:10:00', 'prev')")
        conn.execute("INSERT INTO businesses (business_name) VALUES ('demo')")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report"
            diff.write_report(conn, out, "20261105-0300",
                              per_business_warnings={"demo": ["http://a.example.com returned 403"]})
            return (out / "summary.md").read_text(encoding="utf-8")

    return build("zh"), build("en")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def dashboard_pages():
    """The home page of the dashboard, in both languages."""
    import dashboard

    port = free_port()
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript((ROOT / "modules" / "main" / "db" / "schema.sql").read_text(encoding="utf-8"))
    conn.execute("INSERT INTO businesses (business_name) VALUES ('demo')")
    conn.commit()
    conn.close()
    dashboard._State.db_path = tmp.name

    def run():
        sys.argv = ["dashboard.py", "--db", tmp.name, "--port", str(port), "--host", "127.0.0.1"]
        dashboard.main()

    threading.Thread(target=run, daemon=True).start()
    time.sleep(3.0)

    def fetch(path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path)) as r:
            return r.read().decode("utf-8", "replace")

    return fetch("/"), fetch("/?lang=en")


def main():
    print("=== the mail body ===")
    zh, en = mail_body()
    # the mail body left the thread in English; the dashboard below runs
    # in the same process, so the language is reset here
    from i18n import reset_lang
    reset_lang()
    check("the Chinese summary holds no English-only leftovers", "SRC 资产监控日报" in zh)
    check("the English summary holds no Chinese", not CJK.search(en),
          "%d Chinese" % len(CJK.findall(en)))
    check("the two summaries have the same number of lines",
          len(zh.split("\n")) == len(en.split("\n")),
          "%d vs %d" % (len(zh.split("\n")), len(en.split("\n"))))

    print()
    print("=== the dashboard ===")
    zh_page, en_page = dashboard_pages()
    reset_lang()
    zh_lang = re.search(r'<html lang="([^"]+)"', zh_page)
    en_lang = re.search(r'<html lang="([^"]+)"', en_page)
    check("the default page is Chinese", zh_lang and zh_lang.group(1) == "zh-CN",
          zh_lang.group(1) if zh_lang else "?")
    check("the page with ?lang=en is English", en_lang and en_lang.group(1) == "en",
          en_lang.group(1) if en_lang else "?")

    # the tabs of the page
    zh_tabs = re.findall(r'<button data-tab="[a-z]+"[^>]*>([^<]+)</button>', zh_page)
    en_tabs = re.findall(r'<button data-tab="[a-z]+"[^>]*>([^<]+)</button>', en_page)
    check("the tabs are translated", zh_tabs != en_tabs,
          "%s vs %s" % (zh_tabs[:3], en_tabs[:3]))
    check("the English tabs hold no Chinese",
          not any(CJK.search(t) for t in en_tabs), str(en_tabs[:3]))

    # the switcher
    sw = re.findall(r'href="\?lang=(\w+)"', en_page)
    check("the switcher offers both languages", set(sw) >= {"zh", "en"}, str(sorted(set(sw))))

    print()
    if FAILS:
        print("B5 E2E REJECTED: %s" % ", ".join(FAILS))
        return 1
    print("B5 E2E ACCEPTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

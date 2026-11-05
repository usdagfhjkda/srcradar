#!/usr/bin/env python3
"""Report the user-visible output that still holds Chinese.

Usage:
    python3 check_no_chinese.py [repo]

It reads every python file of the tree and reports the string literals
that hold Chinese, except the ones that are not output:
- the fingerprint bucket names: they are the keys the aggregate counts
  by, so they stay literal
- the pattern that matches the output of the scanner tool: it has to
  keep matching the Chinese of that tool
- the stylesheet: it is a CSS block, not output
- a single character that is a comparison bound, not a text

Exit code 0 when nothing is left.
"""
import io
import re
import sys
import tokenize
from pathlib import Path

CJK = re.compile(r"[\u4e00-\u9fff]")
SQ = chr(39)
DQ = chr(34)

ALLOWED = {
    "unique (1 \u5b50\u57df)", "2-10 \u5b50\u57df", "11-50 \u5b50\u57df",
    "\u226551 \u5b50\u57df (\u6279\u91cf/\u9ed8\u8ba4\u9875)",
}
ALLOWED_SINGLE = {"\u4e00"}
# the regular expression that matches a Chinese title
ALLOWED_REGEX = {"*[\u4e00-\u9fa5]*"}
# the scope values of the database: they are data, the check of the
# schema compares them
ALLOWED_DATA = {"\u53ef\u6d4b\u8d44\u4ea7", "\u975e\u53ef\u6d4b\u8d44\u4ea7"}
# the name of a language: the switcher offers it as a label
ALLOWED_LANG = {"\u4e2d\u6587"}
# the markers that match the output of the URLFinder tool: they are the
# data of the comparison, not output
ALLOWED_MARKERS = {
    "\u7591\u4f3c\u5371\u9669", "\u5371\u9669\u8def\u7531", "\u5df2\u8df3\u8fc7\u9a8c\u8bc1",
}
# the tests hold the text they check for, that is the point of a test
TEST_FILES = ("modules/main/i18n/test_",)
PATTERNS = ("SCAN_RES_OK_RE",)
CSS_MARKERS = ("box-sizing", ".urls-table", ".ant-table")


def body_of(ts):
    """The text of a string literal, without the prefix and the quotes."""
    cut = ts
    for q in (SQ * 3, DQ * 3, SQ, DQ):
        if cut.startswith(q):
            cut = cut[len(q):]
            break
    for q in (SQ * 3, DQ * 3, SQ, DQ):
        if cut.endswith(q):
            cut = cut[:-len(q)]
            break
    # the prefix: only when it is followed by a quote or a brace, so
    # that the u of "unique" is not taken for a prefix
    for pre in ("fr", "rb", "br", "f", "F", "r", "R", "b", "B", "u", "U"):
        if cut.startswith(pre) and len(cut) > len(pre) and cut[len(pre)] in "{[(":
            cut = cut[len(pre):]
            break
    return cut


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    bad = []
    for path in sorted(root.rglob("*.py")):
        if ".git" in path.parts:
            continue
        # the path is compared relative to the root, so that the check
        # holds whatever the root is
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        rel = str(rel)
        if "db_align" in rel or "ymicp" in rel:
            continue
        # a test holds the text it checks for
        if any(rel.startswith(p) for p in TEST_FILES):
            continue
        src = path.read_text(encoding="utf-8")
        lines = src.split("\n")
        try:
            toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
        except (tokenize.TokenError, IndentationError):
            continue
        for tt, ts, st, en, _ in toks:
            if tt != tokenize.STRING or not CJK.search(ts):
                continue
            body = body_of(ts)
            if body in ALLOWED or body in ALLOWED_SINGLE or body in ALLOWED_REGEX:
                continue
            if body in ALLOWED_DATA or body in ALLOWED_LANG:
                continue
            if body in ALLOWED_MARKERS:
                continue
            if any(m in body for m in CSS_MARKERS):
                continue
            # the name of the pattern can be on the same line as the
            # literal, so the whole line counts. The position of the
            # token is a (line, column) pair, so the line is taken from
            # the lines of the source.
            line_no = st[0]
            if 1 <= line_no <= len(lines):
                line = lines[line_no - 1]
            else:
                line = ""
            if any(p in line for p in PATTERNS):
                continue
            # the SQL that creates the scopes table holds the scope
            # values in a CHECK constraint: they are data
            if "CREATE TABLE" in body or "scope_name TEXT" in body:
                continue
            bad.append("%s:%d %s" % (path, st[0], ts[:70]))
    if bad:
        print("FAIL: the user-visible output still holds Chinese:")
        for b in bad[:30]:
            print("   %s" % b)
        return 1
    print("ok: the user-visible output holds no Chinese")
    return 0


if __name__ == "__main__":
    sys.exit(main())

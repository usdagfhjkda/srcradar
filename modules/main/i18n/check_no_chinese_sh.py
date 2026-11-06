#!/usr/bin/env python3
"""Report the Chinese of the shell scripts that is not a comment.

Usage:
    python3 check_no_chinese_sh.py [repo]

A shell line holds a comment when the # stands outside of a string. The
text of a line that holds a lookup is skipped: the key of a lookup is
not output.

Exit code 0 when nothing is left.
"""
import re
import sys
from pathlib import Path

CJK = re.compile(r"[\u4e00-\u9fff]")
# the scripts of the tree, the ones without an extension included
SCRIPT_NAMES = ("srcradar", "install.sh", "init.sh")
EXCLUDE = ("db_align", "ymicp")

# The values of the database are data, not output: the names of the scopes
# and the type of the entity live in the schema and cannot be translated.
ALLOWED_DATA = ("\u53ef\u6d4b\u8d44\u4ea7", "\u975e\u53ef\u6d4b\u8d44\u4ea7", "\u4f01\u4e1a")

# The keywords that match the title of an external tool are markers, not
# output: the title of the tool stays as it is.
ALLOWED_MARKERS = ("web\u5e94\u7528\u9632\u706b\u5899",)


def only_allowed(text):
    """True when every Chinese of the text belongs to an allowed value."""
    rest = text
    values = sorted(ALLOWED_DATA + ALLOWED_MARKERS, key=len, reverse=True)
    for value in values:
        rest = rest.replace(value, "")
    return not CJK.search(rest)


def strip_comment(line):
    """The line without its comment, and the comment itself."""
    out = []
    quote = None
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            out.append(c)
            if c == "\\":
                out.append(line[i + 1] if i + 1 < len(line) else "")
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
            out.append(c)
        elif c == "#":
            # a # at the start of a word starts a comment
            if i == 0 or line[i - 1] in " \t":
                return "".join(out), line[i:]
            out.append(c)
        else:
            out.append(c)
        i += 1
    return "".join(out), ""


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    bad = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
        if any(x in rel for x in EXCLUDE):
            continue
        if path.suffix != ".sh" and path.name not in SCRIPT_NAMES:
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(src.split("\n"), 1):
            code, comment = strip_comment(line)
            # a line that holds a lookup: the key is not the output
            if re.search(r"\bt\s+[a-z][a-z0-9_.]+|\bt\(", code):
                continue
            if CJK.search(code) and not only_allowed(code):
                bad.append("%s:%d %s" % (rel, n, code.strip()[:90]))
    if bad:
        print("FAIL: the shell scripts still hold Chinese:")
        for b in bad[:40]:
            print("   %s" % b)
        print("   (and %d more)" % max(0, len(bad) - 40))
        return 1
    print("ok: the shell scripts hold no Chinese outside the comments")
    return 0


if __name__ == "__main__":
    sys.exit(main())

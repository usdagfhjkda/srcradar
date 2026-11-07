#!/usr/bin/env bash
# The acceptance gate of the i18n work.
#
# It checks:
#   1. the zh and en tables hold the same keys
#   2. the placeholders of the two tables match
#   3. every lookup in the tree hits a key
#   4. the user-visible output holds no Chinese any more
#   5. the files are LF only
#   6. every placeholder of a body is really filled by its call site
#      (a body that holds a shell-style $VAR passes 1-5 and prints the
#      variable name to the user, so this check is what catches it)
#
# Usage: bash modules/main/i18n/verify_i18n.sh [repo]
#
# It exits 0 when everything holds, 1 otherwise.

set -uo pipefail

REPO="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
cd "$REPO" || exit 1

FAIL=0
note() { printf '%s\n' "$*"; }
bad() { printf 'FAIL: %s\n' "$*"; FAIL=1; }

# 1. the same keys
python3 - <<'PY' || FAIL=1
import json, sys
zh = json.load(open("modules/main/i18n/locales/zh.json", encoding="utf-8"))
en = json.load(open("modules/main/i18n/locales/en.json", encoding="utf-8"))
missing = sorted(set(zh) - set(en))
extra = sorted(set(en) - set(zh))
if missing:
    print("FAIL: the keys missing from en.json: %s" % missing[:20]); sys.exit(1)
if extra:
    print("FAIL: the keys missing from zh.json: %s" % extra[:20]); sys.exit(1)
print("ok: the two tables hold the same %d keys" % len(zh))
PY

# 2. the same placeholders
python3 - <<'PY' || FAIL=1
import json, re, sys
zh = json.load(open("modules/main/i18n/locales/zh.json", encoding="utf-8"))
en = json.load(open("modules/main/i18n/locales/en.json", encoding="utf-8"))
ph = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
bad = []
for k in sorted(zh):
    a = set(ph.findall(zh[k]))
    b = set(ph.findall(en[k]))
    if a != b:
        bad.append((k, sorted(a), sorted(b)))
if bad:
    for k, a, b in bad[:20]:
        print("FAIL: %s: zh=%s en=%s" % (k, a, b))
    sys.exit(1)
print("ok: the placeholders match in every key")
PY

# 3. every lookup hits a key
python3 - <<'PY' || FAIL=1
import json, re, sys, pathlib
zh = json.load(open("modules/main/i18n/locales/zh.json", encoding="utf-8"))
keys = set(zh)
pat = re.compile(r"""\bt\(\s*["']([a-zA-Z0-9_.]+)["']""")
shell_pat = re.compile(r"""\bt\s+([a-zA-Z0-9_.]+)""")
missing = {}
for path in pathlib.Path(".").rglob("*"):
    if path.suffix not in (".py", ".sh") or ".git" in path.parts:
        continue
    if "db_align" in str(path) or "ymicp" in str(path):
        continue
    # the implementation of the lookup itself is not a caller
    if str(path).endswith("modules/main/i18n/i18n.py"):
        continue
    # the placeholder checker holds the patterns of the lookups
    if str(path).endswith("modules/main/i18n/check_placeholders.py"):
        continue
    try:
        src = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    for m in pat.finditer(src):
        k = m.group(1)
        if k not in keys:
            missing.setdefault(str(path), set()).add(k)
    if path.suffix == ".sh":
        for m in shell_pat.finditer(src):
            k = m.group(1)
            # a key looks like "module.file.name"; anything else is a
            # shell builtin that happens to be called t
            if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+", k):
                continue
            if k not in keys:
                missing.setdefault(str(path), set()).add(k)
if missing:
    for p, ks in list(missing.items())[:20]:
        print("FAIL: %s: %s" % (p, sorted(ks)))
    sys.exit(1)
print("ok: every lookup hits a key")
PY

# 4. the user-visible output holds no Chinese
python3 "$REPO/modules/main/i18n/check_no_chinese.py" "$REPO" || FAIL=1
python3 "$REPO/modules/main/i18n/check_no_chinese_sh.py" "$REPO" || FAIL=1

# 5. the line endings
if grep -rlU $'\r' --include="*.py" --include="*.sh" --include="*.json" \
        modules/ install.sh init.sh srcradar 2>/dev/null | grep -v db_align | grep -v ymicp | head -5 | grep -q .; then
    bad "some files hold CRLF"
else
    note "ok: the files are LF only"
fi

# 6. every placeholder is filled by its call site
python3 "$REPO/modules/main/i18n/check_placeholders.py" "$REPO" || FAIL=1

if [ "$FAIL" -eq 0 ]; then
    echo "I18N ACCEPTED"
else
    echo "I18N REJECTED"
fi
exit "$FAIL"

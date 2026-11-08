#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check that every placeholder of the message tables is really filled.

The acceptance gate of the i18n work checks that the two tables hold the same
keys, that their placeholders match and that every lookup hits a key. It does
NOT check that the lookup FILLS the placeholder: a body that holds a
shell-style `$VAR` (data, never expanded by the shell) passes every one of
those checks and prints the variable name to the user.

This check closes that hole:

  1. a body may not hold a `$VAR` that the call site passes under that name
     (the value can never arrive)
  2. every placeholder of a body must be filled by its call site
  3. the two tables must hold the same placeholders for the same key

A body may hold a `$` that is deliberately literal (a hint the user copies,
the name of an environment variable). Those are listed in LITERAL.

The call sites are read with a scanner that understands
  - shell:   t key name=value name=value
  - python:  t("key", {"name": value})   and   t("key", some_var)

Usage: python3 check_placeholders.py [repo]
"""
import json
import os
import re
import sys

REPO = sys.argv[1] if len(sys.argv) > 1 else "."
os.chdir(REPO)

ZH = json.load(open("modules/main/i18n/locales/zh.json", encoding="utf-8"))
EN = json.load(open("modules/main/i18n/locales/en.json", encoding="utf-8"))

# the bodies whose $ is deliberately literal: the pre-i18n source printed it
# literally too (a command the user copies, the name of an env variable)
LITERAL = {
    "root.install.all_installed",
    "main.truncate_recon_db.arg_db",
}

DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)")

# ---- the shell call sites ---------------------------------------------------
# the value may hold a command substitution with quotes inside it, so the
# argument scanner walks the string instead of using a regex
def shell_call_args(src):
    """yields (key, {argument names}) for every `t key name=value` in src"""
    for m in re.finditer(r"(?<![A-Za-z0-9_])t ([a-zA-Z0-9_.]+)\b", src):
        key = m.group(1)
        i = m.end()
        names = set()
        while True:
            while i < len(src) and src[i] in " \t":
                i += 1
            am = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", src[i:])
            if not am:
                break
            name = am.group(1)
            i += am.end()
            # the value: a quoted string, or a bare word
            if i < len(src) and src[i] in "\"'":
                quote = src[i]
                i += 1
                while i < len(src) and src[i] != quote:
                    if src[i] == "\\":
                        i += 1
                    elif src[i] == "$" and src[i:i + 2] == "$(":
                        # a command substitution inside the quotes may hold
                        # quotes of its own
                        depth = 0
                        while i < len(src):
                            if src[i] == "(":
                                depth += 1
                            elif src[i] == ")":
                                depth -= 1
                                if depth == 0:
                                    i += 1
                                    break
                            i += 1
                        continue
                    i += 1
                i += 1
            else:
                while i < len(src) and src[i] not in " \t\n":
                    # a command substitution may hold quotes
                    if src[i] == "$" and src[i:i + 2] == "$(":
                        depth = 0
                        while i < len(src):
                            if src[i] == "(":
                                depth += 1
                            elif src[i] == ")":
                                depth -= 1
                                if depth == 0:
                                    i += 1
                                    break
                            i += 1
                        continue
                    i += 1
            names.add(name)
            # the next argument must be separated by a space
            j = i
            while j < len(src) and src[j] in " \t":
                j += 1
            if j < len(src) and src[j] == "\n":
                break
            if j == i:
                break
            i = j
        yield key, names


# ---- the python call sites --------------------------------------------------
def balanced(src, start):
    depth = 0
    i = start
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        elif ch in "\"'":
            quote = ch
            i += 1
            while i < len(src) and src[i] != quote:
                i += 1
        i += 1
    return -1


PY_CALL = re.compile(r"_?t\(\s*['\"]([a-zA-Z0-9_.]+)['\"]")


def python_call_args(src):
    for km in PY_CALL.finditer(src):
        key = km.group(1)
        rest = src[km.end():]
        # skip the whitespace
        j = 0
        while j < len(rest) and rest[j] in " \t\r\n":
            j += 1
        if j < len(rest) and rest[j] == ",":
            j += 1
        while j < len(rest) and rest[j] in " \t\r\n":
            j += 1
        names = set()
        if j < len(rest) and rest[j] == "{":
            end = balanced(rest, j)
            if end > 0:
                names = set(re.findall(r"['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*:", rest[j:end]))
        else:
            # t("key", some_var) - the names live in the variable, so the
            # placeholders of the body cannot be checked here
            vm = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", rest[j:])
            if vm:
                names.add(vm.group(1))
        yield key, names


def source_files():
    """every shell and python file of the working tree

    The scan must not depend on the git index: a file that is not tracked
    yet (a new script) is still part of the tree the gate checks.
    """
    skip_dirs = {".git", "__pycache__", ".ruff_cache", ".pytest_cache",
                 ".mypy_cache", ".hermes", "vendor", ".vendor"}
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            if name.endswith((".sh", ".py")):
                yield os.path.join(root, name)[2:]  # drop the leading ./


passed = {}
for path in source_files():
    try:
        src = open(path, encoding="utf-8").read()
    except (UnicodeDecodeError, OSError):
        continue
    # a shell file may hold a python heredoc, so BOTH shapes are scanned
    for key, names in shell_call_args(src):
        passed.setdefault(key, set()).update(names)
    for key, names in python_call_args(src):
        passed.setdefault(key, set()).update(names)

# the python call sites that pass a VARIABLE: the body of such a key cannot
# be checked by name, so the key is exempt from check 2
variable_call = set()
for path in source_files():
    try:
        src = open(path, encoding="utf-8").read()
    except (UnicodeDecodeError, OSError):
        continue
    for km in PY_CALL.finditer(src):
        key = km.group(1)
        rest = src[km.end():]
        j = 0
        while j < len(rest) and rest[j] in " \t\r\n":
            j += 1
        if j < len(rest) and rest[j] == ",":
            j += 1
        while j < len(rest) and rest[j] in " \t\r\n":
            j += 1
        if j < len(rest) and rest[j] != "{":
            variable_call.add(key)

problems = []

for key in sorted(ZH):
    z = ZH[key]
    e = EN.get(key, "")
    names = passed.get(key, set())

    # 1. a $VAR that the caller passes cannot arrive
    if key not in LITERAL:
        for var in sorted(set(DOLLAR.findall(z))):
            if var in names:
                problems.append(
                    "the body holds $%s but the caller passes it: the value can "
                    "never arrive" % var)
                break

    # 2. every placeholder must be filled
    if key not in variable_call:
        for ph in sorted(set(PLACEHOLDER.findall(z))):
            if ph not in names:
                problems.append("the placeholder {%s} is never filled" % ph)

    # 3. the two tables must hold the same placeholders
    if set(PLACEHOLDER.findall(z)) != set(PLACEHOLDER.findall(e)):
        problems.append("the two tables hold different placeholders")

if problems:
    for p in problems:
        print("FAIL: %s" % p)
    print("I18N PLACEHOLDER CHECK REJECTED")
    sys.exit(1)

print("ok: every placeholder is filled and no body holds an unreachable $VAR")
print("I18N PLACEHOLDER CHECK ACCEPTED")

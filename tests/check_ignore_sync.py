#!/usr/bin/env python3
"""check that .dockerignore covers every rule of .gitignore.

Run it from the root of the repository:  python3 tests/check_ignore_sync.py
It exits 1 when a rule of .gitignore is not covered by the .dockerignore.


The two files use different matching rules, so the rules cannot be compared
as strings:

  - a gitignore pattern WITHOUT a slash matches at EVERY depth
    ("logs/" covers modules/main/daily/logs/ as well)
  - a dockerignore pattern is matched against the path relative to the
    context root, so "logs/" covers the TOP LEVEL only

A gitignore rule is covered by the .dockerignore when:
  - the rule is anchored (it has a slash): the .dockerignore has the same
    path, or the "**/" version of it
  - the rule is bare (it has no slash): the .dockerignore has the "**/"
    version of it. The same bare pattern is NOT enough, it would only cover
    the top level.

The rules listed in EXCEPTIONS are intentional: the build context needs
something the repository does not.
"""
import os
import sys

EXCEPTIONS = {
    # the Dockerfile copies .vendor/ into the image, so it must stay in the
    # build context even though the repository does not track it
    ".vendor",
}


def read(path):
    rules = []
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        neg = s.startswith("!")
        if neg:
            s = s[1:]
        rules.append((neg, s.rstrip("/")))
    return rules


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
git = read(os.path.join(ROOT, ".gitignore"))
dock = read(os.path.join(ROOT, ".dockerignore"))

# the docker rules: the "**/X" forms and the anchored forms
dock_star = set()
dock_anchored = set()
for neg, p in dock:
    if p.startswith("**/"):
        dock_star.add(p)
        dock_anchored.add(p[3:])
        dock_anchored.add(p)
    elif "/" in p:
        dock_anchored.add(p)
    else:
        dock_anchored.add(p)          # a bare docker rule covers the top level

missing = []
for neg, p in git:
    if p in EXCEPTIONS:
        continue
    if "/" in p:
        ok = (p in dock_anchored) or (("**/" + p) in dock_star)
    else:
        ok = ("**/" + p) in dock_star
    if not ok:
        missing.append((neg, p))

# the rules of the .dockerignore that the .gitignore does not have
git_anchored = set()
git_bare = set()
for neg, p in git:
    if "/" in p:
        git_anchored.add(p)
    else:
        git_bare.add(p)

extra = []
for neg, p in dock:
    core = p[3:] if p.startswith("**/") else p
    if "/" in core:
        if core in git_anchored:
            continue
    else:
        if core in git_bare:
            continue
    extra.append(p)

print("=== the rule sets ===")
print("  .gitignore:    %d rules" % len(git))
print("  .dockerignore: %d rules" % len(dock))
print()
print("=== the .gitignore rules that the .dockerignore does NOT cover ===")
if not missing:
    print("  (none - every rule of .gitignore is covered)")
else:
    for neg, p in missing:
        print("  %-52s %s" % (p, "(negated)" if neg else ""))
print()
print("=== the rules of the .dockerignore that the .gitignore does NOT have ===")
print("  (the build context needs more than the repository - these are the extra ones)")
if not extra:
    print("  (none)")
else:
    for p in extra:
        print("  %s" % p)
print()
sys.exit(1 if missing else 0)

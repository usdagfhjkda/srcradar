#!/usr/bin/env python3
"""i18n bootstrap: locate the srcradar repository root from any script.

The repository root is the directory that holds both install.sh and
modules/. Walking up from the caller's own location finds it from any
depth, so every caller uses the same snippet and moving a file does not
break the lookup.

This module is imported by i18n.py and by the shell-side i18n.sh; it is
kept separate so both sides share one definition of "where is the root".
"""
import sys
from pathlib import Path

# Markers that identify the repository root.
ROOT_MARKERS = ("install.sh", "modules")


def find_repo_root(start=None):
    """Walk up from `start` and return the repository root, or None.

    `start` defaults to the directory of this file. A directory qualifies
    as the root when it holds every marker in ROOT_MARKERS.
    """
    if start is None:
        start = Path(__file__).resolve().parent
    elif isinstance(start, str):
        start = Path(start)
    else:
        start = Path(start).resolve()

    for candidate in [start, *start.parents]:
        if all((candidate / marker).exists() for marker in ROOT_MARKERS):
            return candidate
    return None


def i18n_dir(start=None):
    """Directory holding the i18n module, or None when the root is unknown."""
    root = find_repo_root(start)
    if root is None:
        return None
    return root / "modules" / "main" / "i18n"


def bootstrap_sys_path(start=None):
    """Put the i18n module on sys.path and return its directory.

    Returns None when the i18n module cannot be located, in which case
    the caller falls back to its own behaviour.
    """
    target = i18n_dir(start)
    if target is None:
        return None
    path_str = str(target)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
    return target


if __name__ == "__main__":
    root = find_repo_root()
    if root is None:
        print("repository root not found", file=sys.stderr)
        sys.exit(1)
    print(root)
    print("i18n:", i18n_dir())

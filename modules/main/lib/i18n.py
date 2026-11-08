#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
modules/main/lib/i18n.py — public entry point for the i18n module.

The implementation lives in modules/main/i18n/. This module is the stable
import location, reached the same way callers already reach
modules/main/lib/load_config.py:

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
    from i18n import t

    print(t("dashboard.tab.status"))

The implementation directory is located by walking up to the repository
root, so this entry point does not depend on its own depth.

The implementation is loaded under a private module name rather than by a
plain "from i18n import ...". This file is itself imported as "i18n"
whenever modules/main/lib is on sys.path, so a plain import would resolve
back to this file instead of the implementation.
"""
import importlib.util
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent

# The implementation sits at <root>/modules/main/i18n.
while not (_LIB_DIR / "i18n" / "i18n.py").is_file() and _LIB_DIR != _LIB_DIR.parent:
    _LIB_DIR = _LIB_DIR.parent

_IMPL_DIR = _LIB_DIR / "i18n"
_IMPL_FILE = _IMPL_DIR / "i18n.py"

if not _IMPL_FILE.is_file():
    raise ImportError(
        "cannot locate modules/main/i18n from %s" % Path(__file__).resolve().parent
    )

# The implementation imports bootstrap.py from its own directory.
if str(_IMPL_DIR) not in sys.path:
    sys.path.insert(0, str(_IMPL_DIR))

_spec = importlib.util.spec_from_file_location("_srcradar_i18n_impl", _IMPL_FILE)
if _spec is None or _spec.loader is None:
    raise ImportError("cannot load the i18n implementation from %s" % _IMPL_FILE)

_impl = importlib.util.module_from_spec(_spec)
sys.modules["_srcradar_i18n_impl"] = _impl
_spec.loader.exec_module(_impl)

# Re-export the public API of the implementation module.
DEFAULT_LANG = _impl.DEFAULT_LANG
KNOWN_LANGS = _impl.KNOWN_LANGS
load_table = _impl.load_table
normalize_lang = _impl.normalize_lang
resolve_lang = _impl.resolve_lang
set_lang = _impl.set_lang
get_lang = _impl.get_lang
reset_lang = _impl.reset_lang
t = _impl.t

__all__ = [
    "DEFAULT_LANG",
    "KNOWN_LANGS",
    "get_lang",
    "load_table",
    "normalize_lang",
    "reset_lang",
    "resolve_lang",
    "set_lang",
    "t",
]

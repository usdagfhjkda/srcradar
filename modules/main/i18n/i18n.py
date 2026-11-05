#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
i18n.py — python-side interface of the srcradar language module
           (modules/main/i18n)

Responsibilities:
    - locate the repository root (where config/lang.conf lives)
    - resolve the active language
    - look translations up in the message table

Language resolution:
    1. SRCDAR_LANG environment variable
    2. the lang field in config/lang.conf
    3. zh as the default

Message table: locales/<lang>.json next to this file.
Missing key: the active language is tried first, then the default
             language, then the key name itself.

Placeholder convention (named, identical on the shell side):
    a table value may contain {run_id} / {count}; the caller passes
    name=value
    python: t("key", {"run_id": "x"})
    shell : t key run_id=x

Command line (used by the shell integration and for manual checks):
    python3 i18n.py                 print the active language
    python3 i18n.py lang [zh|en]    print a language (omit to get the
                                    active one)
    python3 i18n.py root            print the repository root
    python3 i18n.py dump [zh|en]    emit the shell assignment block
    python3 i18n.py t KEY [name=value ...]  print one translation

The command line deliberately offers no language override — overriding
goes through SRCDAR_LANG. Otherwise "--lang en" would be ambiguous
between "query en" and "use en for the following command".
"""
import json
import os
import sys
import threading
from pathlib import Path

# The bootstrap module holds the single definition of "where is the
# repository root", shared with the shell side.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bootstrap import find_repo_root  # noqa: E402

MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_LANG = "zh"
KNOWN_LANGS = ("zh", "en")
CONFIG_FILENAME = "lang.conf"
NL = chr(10)
SQ = "'" + chr(92) + "''"

_ALIASES = {
    "zh": "zh", "zh-cn": "zh", "zh_cn": "zh", "zh-hans": "zh", "cn": "zh", "chinese": "zh",
    "en": "en", "en-us": "en", "en_us": "en", "english": "en",
}

_table_cache = {}
_active_lang = None


def normalize_lang(value):
    """Map a raw language string onto a known code; None when unknown."""
    if not value:
        return None
    key = str(value).strip().lower().replace(" ", "")
    return _ALIASES.get(key)


def repo_root():
    """The repository root, or None when it cannot be located.

    Delegates to bootstrap.py so the python and shell sides agree on what
    the root is.
    """
    return find_repo_root()


def config_dir():
    """Directory holding lang.conf; SRCDAR_CONFIG_DIR overrides it
    (test and container scenarios)."""
    override = os.environ.get("SRCDAR_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    root = repo_root()
    if root is None:
        return None
    return root / "config"


def config_path():
    d = config_dir()
    return d / CONFIG_FILENAME if d is not None else None


def _lang_from_config():
    path = config_path()
    if path is None or not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in raw.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "lang":
            return normalize_lang(value)
    return None


def resolve_lang():
    """Active language: environment variable > config file > default."""
    from_env = normalize_lang(os.environ.get("SRCDAR_LANG", ""))
    if from_env:
        return from_env
    from_cfg = _lang_from_config()
    if from_cfg:
        return from_cfg
    return DEFAULT_LANG


def load_table(lang):
    """Raw message table for one language (cached)."""
    if lang not in _table_cache:
        path = MODULE_DIR / "locales" / (lang + ".json")
        data = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = {}
            if isinstance(loaded, dict):
                data = {str(k): str(v) for k, v in loaded.items()}
        _table_cache[lang] = data
    return _table_cache[lang]


def _format(template, args):
    """Substitute placeholders. A single dict argument selects the named
    form, otherwise the positional form is used."""
    out = template
    if len(args) == 1 and isinstance(args[0], dict):
        for name, value in args[0].items():
            out = out.replace("{" + str(name) + "}", str(value))
    else:
        for i, value in enumerate(args):
            out = out.replace("{" + str(i) + "}", str(value))
    return out


def t(key, *args):
    """Translate one key.

    The language is resolved on the first call and then cached for the
    lifetime of the process. Call set_lang() to switch it.
    """
    lang = get_lang()
    value = load_table(lang).get(key)
    if value is None:
        value = load_table(DEFAULT_LANG).get(key)
    if value is None:
        value = key
    if args:
        value = _format(value, args)
    return value




# The language of the current thread. The dashboard serves the requests
# concurrently and the language is a per-request setting, so it cannot
# live in a module global: one request would change the language of
# another one. The process default stays the language of the
# configuration.
_thread_state = threading.local()


def set_lang(lang, *, thread_local: bool = True):
    """Force the language for this process or for this thread.

    With thread_local (the default) the language belongs to the calling
    thread and the other threads keep theirs. Without it the language is
    process wide, which is what a command line tool wants.

    Returns the resolved language.
    """
    resolved = normalize_lang(lang) or DEFAULT_LANG
    if thread_local:
        _thread_state.lang = resolved
    global _active_lang
    _active_lang = resolved
    return resolved


def get_lang() -> str:
    """The language of the calling thread, resolved on the first call."""
    lang = getattr(_thread_state, "lang", None)
    if lang is not None:
        return lang
    global _active_lang
    if _active_lang is None:
        _active_lang = resolve_lang()
    return _active_lang


def reset_lang() -> None:
    """Drop the language of the calling thread.

    The next lookup resolves it again from the configuration. A request
    handler calls this when it is done, so that the thread does not keep
    the language of the request it just served.
    """
    _thread_state.lang = None
    global _active_lang
    _active_lang = None



def dump_shell(lang):
    """Emit the shell assignment block: the active language's table plus
    the default language as a fallback (the active language wins).

    Values are escaped so that each entry occupies exactly one line:
    a newline becomes the two-character sequence backslash-n, and a
    backslash is doubled. The shell side reverses this after eval. This
    matters because the loader reads the block one line at a time, and a
    raw multi-line value would reach eval with an unterminated quote.
    """
    lines = []
    seen = set()
    for table in (load_table(lang), load_table(DEFAULT_LANG)):
        for key, value in table.items():
            if key in seen:
                continue
            seen.add(key)
            lines.append("I18N['" + key.replace("'", SQ) + "']='"
                         + _shell_escape(value) + "'")
    return NL.join(lines) + NL


# Sentinel used to carry a newline through the one-line shell assignment
# format. JSON forbids raw control characters in string values, so a table
# value can never contain one; using a sentinel keeps the restore step on
# the shell side unambiguous.
SHELL_NEWLINE = "\\x01"


def _shell_escape(value):
    """Escape a value for the one-line shell assignment format.

    A backslash is doubled and a newline becomes the sentinel sequence.
    The shell side reverses this after eval.
    """
    out = value.replace("\\", "\\\\")
    out = out.replace("\n", SHELL_NEWLINE)
    return out.replace("'", SQ)


def main(argv):
    if not argv:
        print(resolve_lang())
        return 0

    # Command line shapes (unambiguous):
    #   i18n.py                      print the active language
    #   i18n.py lang [zh|en]         print a language (omit for the active one)
    #   i18n.py root                 print the repository root
    #   i18n.py dump [lang]          emit the shell assignment block
    #   i18n.py t KEY [name=value]  translate one key
    #
    # A language override goes through the SRCDAR_LANG environment
    # variable and takes no command line slot — otherwise "--lang en"
    # would be ambiguous between "query en" and "use en for the next
    # command".
    cmd = argv[0]
    rest = list(argv[1:])

    if cmd == "lang":
        if rest:
            resolved = normalize_lang(rest[0])
            if resolved is None:
                print("unknown language (expected zh or en)", file=sys.stderr)
                return 1
            print(resolved)
        else:
            print(resolve_lang())
        return 0
    if cmd == "root":
        root = repo_root()
        print(root if root else "")
        return 0
    if cmd == "dump":
        target = resolve_lang()
        if rest:
            resolved = normalize_lang(rest[0])
            if resolved is None:
                print("unknown language (expected zh or en)", file=sys.stderr)
                return 1
            target = resolved
        sys.stdout.write(dump_shell(target))
        return 0
    if cmd == "t":
        if not rest:
            print("usage: i18n.py t KEY [name=value ...]", file=sys.stderr)
            return 1
        merged = {}
        positional = []
        for pair in rest[1:]:
            name, sep, value = pair.partition("=")
            if sep:
                merged[name] = value
            else:
                positional.append(pair)
        if merged:
            print(t(rest[0], merged))
        elif positional:
            print(t(rest[0], *positional))
        else:
            print(t(rest[0]))
        return 0
    print("unknown command: " + cmd, file=sys.stderr)
    print("usage: i18n.py [lang|root|dump|t] ...", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# i18n.sh — shell-side interface of the srcradar language module
#            (modules/main/i18n/i18n.sh)
#
# Usage:
#   source <repo>/modules/main/i18n/i18n.sh
#   t <key> [name=value ...]      translate a key (with trailing newline)
#   traw <key> [name=value ...]   translate a key (without trailing newline)
#   i18n_lang                      print the active language
#   i18n_load [lang]               (re)load the message table
#
# Language resolution (identical to i18n.py):
#   1. SRCDAR_LANG environment variable
#   2. the lang field in config/lang.conf
#   3. en as the default
#
# Message table: modules/main/i18n/locales/<lang>.json
# Loading strategy: i18n.py dump exports every key/value pair once, and
#                   they are stored in the I18N associative array. A
#                   later t call does not fork python again — it only
#                   does bash string substitution.
#
# Placeholders: a table value may contain {run_id} / {count}; the caller
#               passes name=value pairs.
#               Example: t install.module_ok area=main name=pdtm
#
# The I18N associative array requires bash 4+; load_config.sh already uses
# the same mechanism.
#
# ---------------------------------------------------------------------------
# How a caller sources this file
# ---------------------------------------------------------------------------
# Every caller uses the same snippet, which walks up from the caller's own
# directory until it finds the repository root. The root is the directory
# that holds both install.sh and modules/. This works from any depth and
# survives moving a file:
#
#   _i18n_self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && while [ ! -d modules/main/i18n ] && [ "$PWD" != "/" ]; do cd ..; done && pwd)/modules/main/i18n/i18n.sh"
#   [ -f "$_i18n_self" ] && source "$_i18n_self"
#
# ---------------------------------------------------------------------------

# ---- locate the repository root ----
# Walk up until a directory holds both modules/ and install.sh.
# The optional argument is the directory to start from; it defaults to
# this file's own directory.
# shellcheck disable=SC2120
_i18n_find_root() {
    local d="${1:-}"
    [ -n "$d" ] || d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    while [ "$d" != "/" ]; do
        if [ -d "$d/modules" ] && [ -f "$d/install.sh" ]; then
            printf '%s' "$d"
            return 0
        fi
        d="$(dirname "$d")"
    done
    return 1
}

I18N_ROOT="${I18N_ROOT:-$(_i18n_find_root || true)}"
I18N_DIR="${I18N_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# Print the path of this file, for callers that need to source it.
i18n_locate() {
    printf '%s' "${I18N_DIR}/i18n.sh"
}

# ---- language resolution ----
i18n_config_dir() {
    if [ -n "${SRCDAR_CONFIG_DIR:-}" ]; then
        printf '%s' "$SRCDAR_CONFIG_DIR"
    elif [ -n "${I18N_ROOT:-}" ]; then
        printf '%s/config' "$I18N_ROOT"
    else
        printf '%s' ""
    fi
}

# Map a language alias onto a known code; print nothing when unrecognised.
_i18n_normalize() {
    case "$(printf '%s' "${1:-}" | tr 'A-Z' 'a-z' | tr -d ' ')" in
        zh|zh-cn|zh_cn|zh-hans|cn|chinese) printf 'zh' ;;
        en|en-us|en_us|english)             printf 'en' ;;
        *)                                  printf '' ;;
    esac
}

# Read the lang field out of config/lang.conf.
_i18n_conf_lang() {
    local cfg_dir lang
    cfg_dir="$(i18n_config_dir)"
    [ -n "$cfg_dir" ] || return 0
    [ -f "$cfg_dir/lang.conf" ] || return 0
    lang="$(sed -n 's/^[[:space:]]*lang[[:space:]]*=[[:space:]]*//p' \
            "$cfg_dir/lang.conf" 2>/dev/null | head -1 | tr -d '"'\''[:space:]')"
    [ -n "$lang" ] && printf '%s' "$lang"
}

# Active language: SRCDAR_LANG > config/lang.conf > en.
i18n_resolve_lang() {
    local lang
    lang="$(_i18n_normalize "${SRCDAR_LANG:-}")"
    [ -n "$lang" ] && { printf '%s' "$lang"; return 0; }
    lang="$(_i18n_normalize "$(_i18n_conf_lang)")"
    [ -n "$lang" ] && { printf '%s' "$lang"; return 0; }
    printf 'en'
}

i18n_lang() {
    i18n_resolve_lang
}

# ---- message table loading ----
# I18N maps a key onto its translation. Keys missing from the active
# language fall back to the default language (merged in by i18n.py).
#
# set -u caveat (measured on bash 5.1):
#   An associative array that has been declared but not yet assigned makes
#   both ${#I18N[@]} and ${I18N[key]} raise "unbound variable". Hence:
#     - emptiness is tested with the "${I18N[*]+x}" guard, not ${#I18N[@]}
#     - every lookup carries a :- default
#   Once the table is loaded the array is non-empty and ${#I18N[@]} is fine.
declare -A I18N 2>/dev/null || true
I18N_LOADED=""

# i18n_load accepts an optional [lang]; without one it follows the
# resolution chain above. In-tree callers all use the no-argument form,
# but the parameter is part of the public API.
# shellcheck disable=SC2120
i18n_load() {
    local lang="${1:-}"
    [ -n "$lang" ] || lang="$(i18n_resolve_lang)"
    [ -n "$lang" ] || lang="en"

    if [ ! -f "$I18N_DIR/i18n.py" ]; then
        printf '[i18n] ERROR: i18n.py not found at %s\n' "$I18N_DIR" >&2
        return 3
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        printf '[i18n] ERROR: python3 not found in PATH\n' >&2
        return 3
    fi

    # Drop the previous table so switching language leaves no stale keys.
    # The guard uses * rather than @: inside [ ], @ is treated as an array
    # operand by shellcheck (SC2198).
    if [ -n "${I18N[*]+x}" ]; then
        local k
        for k in "${!I18N[@]}"; do unset "I18N[$k]"; done
    fi

    local dump
    if ! dump="$(python3 "$I18N_DIR/i18n.py" dump "$lang" 2>/dev/null)"; then
        printf '[i18n] ERROR: failed to load the message table (lang=%s)\n' "$lang" >&2
        return 3
    fi
    if [ -z "$dump" ]; then
        printf '[i18n] WARN: message table is empty (lang=%s)\n' "$lang" >&2
    fi

    # The export uses the I18N['key']='value' form; eval it in bulk to
    # fill the associative array. Each entry is exactly one line: the
    # python side escapes newlines as backslash-n so that eval always
    # sees a terminated quote.
    local line
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        eval "$line"
    done <<< "$dump"

    I18N_LOADED="$lang"
    return 0
}

# Reverse the escaping applied by i18n.py when it exports the table:
# a literal backslash-n becomes a real newline, and a doubled backslash
# becomes a single one. The backslash handling runs first so that a
# value that genuinely contains backslash-n keeps both characters.
_i18n_unescape() {
    local s="$1"
    # Undo the backslash doubling first.
    s="${s//\\\\/\\}"
    # Then restore the newline sentinel. A table value can never
    # contain a raw control character, so this cannot fire on a
    # literal backslash-n that was part of the original text.
    s="${s//\\x01/$'\n'}"
    printf '%s' "$s"
}

# ---- translation ----
# Usage: t <key> [name=value ...]
# A missing key resolves to the key name itself, so an absent translation
# is obvious rather than silently blank.
t() {
    local key="${1:-}"
    [ -n "$key" ] || return 1
    shift || true

    if [ -z "$I18N_LOADED" ]; then
        i18n_load || return 3
    fi

    local text="${I18N[$key]:-$key}"
    text="$(_i18n_unescape "$text")"

    # Placeholder substitution: {name} -> value
    local pair name value
    for pair in "$@"; do
        name="${pair%%=*}"
        value="${pair#*=}"
        [ "$name" = "$pair" ] && continue
        text="${text//\{$name\}/$value}"
    done

    printf '%s\n' "$text"
}

# Same as t but without the trailing newline.
# Not named tr: that would shadow the coreutils tr command.
traw() {
    local key="${1:-}"
    [ -n "$key" ] || return 1
    shift || true

    if [ -z "$I18N_LOADED" ]; then
        i18n_load || return 3
    fi

    local text="${I18N[$key]:-$key}"
    text="$(_i18n_unescape "$text")"
    local pair name value
    for pair in "$@"; do
        name="${pair%%=*}"
        value="${pair#*=}"
        [ "$name" = "$pair" ] && continue
        text="${text//\{$name\}/$value}"
    done

    printf '%s' "$text"
}

#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# modules/main/lib/resolve_resolvers.sh — the DNS resolver list in effect.
#
# The lists live in modules/main/pdtm/resolvers/, one file per set:
#
#   cn      the resolvers of the domestic operators
#   global  the public resolvers of the world
#
# The choice is the resolver_set field of config/pdtm.conf:
#
#   auto      the language decides (zh -> cn, otherwise -> global)
#   cn        the domestic set
#   global    the global set
#   custom    the resolvers_file field (a path of the operator)
#
# The precedence of the effective path:
#
#   1. $RESOLVERS_FILE (the environment, an absolute path)
#   2. $resolver_set (the field of the conf, or the environment)
#   3. $resolvers_file (the field of the conf, an absolute path)
#   4. the global set
#
# The caller has usually loaded the conf already (load_config_set), so the
# two fields are in the scope of the caller; this file only reads them.
#
# The rules 1 and 3 keep a deployment that brings its own list working: it
# writes the path into the resolvers_file field and the list is used as it
# is, no matter which set the operator picked.
#
# Interface:
#   resolve_resolvers_set    -> the set in effect (cn | global | custom)
#   resolve_resolvers_file   -> the absolute path of the list in effect
#
# The file returns 1 when no list can be found; the caller reports it.

# ---- the directory that holds the sets -------------------------------------
_rr_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_rr_root="$_rr_lib_dir"
while [ ! -d "$_rr_root/modules/main/pdtm/resolvers" ] && [ "$_rr_root" != "/" ]; do
    _rr_root="$(dirname "$_rr_root")"
done
_RR_SETS_DIR="$_rr_root/modules/main/pdtm/resolvers"

# ---- the language (the auto mode follows it) -------------------------------
# The lookup is not always loaded (the installer does not need it), so it is
# loaded here when it is missing.
if ! declare -F i18n_lang >/dev/null 2>&1; then
    # shellcheck source=./i18n.sh
    source "$_rr_lib_dir/i18n.sh" 2>/dev/null || true
fi

_rr_lang() {
    if declare -F i18n_lang >/dev/null 2>&1; then
        i18n_lang
        return 0
    fi
    printf 'en'
}

# ---- the set in effect -----------------------------------------------------
resolve_resolvers_set() {
    local set="${resolver_set:-}"
    case "$set" in
        auto)
            # the language decides; a language that is not zh means the
            # targets are not the domestic ones
            if [ "$(_rr_lang)" = "zh" ]; then
                printf 'cn'
            else
                printf 'global'
            fi
            ;;
        cn|global|custom)
            printf '%s' "$set"
            ;;
        *)
            # no choice, or a value that is not known: the default
            printf 'global'
            ;;
    esac
}

# ---- the path of the list in effect -----------------------------------------
resolve_resolvers_file() {
    local set
    local f

    # 1. the environment
    if [ -n "${RESOLVERS_FILE:-}" ] && [ -f "${RESOLVERS_FILE}" ]; then
        printf '%s' "${RESOLVERS_FILE}"
        return 0
    fi

    # 2. the choice
    set="${resolver_set:-}"
    if [ -n "$set" ]; then
        case "$set" in
            auto)  set="$(resolve_resolvers_set)" ;;
            custom)
                # the path of the operator; it has to be there
                if [ -n "${resolvers_file:-}" ] && [ -f "${resolvers_file}" ]; then
                    printf '%s' "${resolvers_file}"
                    return 0
                fi
                return 1
                ;;
            cn|global) ;;
            *)     set="" ;;
        esac
        if [ -n "$set" ]; then
            f="$_RR_SETS_DIR/$set"
            if [ -f "$f" ]; then
                printf '%s' "$f"
                return 0
            fi
        fi
    fi

    # 3. the field of the conf (a list that the deployment brought)
    if [ -n "${resolvers_file:-}" ] && [ -f "${resolvers_file}" ]; then
        printf '%s' "${resolvers_file}"
        return 0
    fi

    # 4. the default
    f="$_RR_SETS_DIR/global"
    if [ -f "$f" ]; then
        printf '%s' "$f"
        return 0
    fi
    return 1
}

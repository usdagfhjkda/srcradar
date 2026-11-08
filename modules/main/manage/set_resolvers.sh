#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# set_resolvers.sh — show or change the DNS resolver list that is in effect.
#
# Usage:
#   ./manage/set_resolvers.sh                    # show the list in effect
#   ./manage/set_resolvers.sh --set cn           # the domestic resolvers
#   ./manage/set_resolvers.sh --set global       # the public resolvers of the world
#   ./manage/set_resolvers.sh --set auto         # the language decides
#
# The lists live in modules/main/pdtm/resolvers/, one file per set:
#
#   cn      the resolvers of the domestic operators
#   global  the public resolvers of the world
#
# The domestic resolvers answer the foreign names differently from the
# public ones, and the difference shows in the assets that are collected.
# The set that is in effect is written to config/pdtm.conf, as the
# resolver_set field, and the path of the list that it resolves to is
# written as the resolvers_file field.
#
# The auto mode follows the language: the Chinese language means the
# domestic set, every other language means the global set. The language is
# the field that modules/main/manage/set_lang.sh writes.
#
# Rules:
#   - without --set nothing is written; the list in effect is shown
#   - the resolver_set field is never overwritten by the installer, so an
#     install does not reset the choice of the operator
#   - the resolvers_file field is derived, so it is rewritten on every
#     change
#   - a deployment that brings its own list writes the path into the
#     resolvers_file field; such a path is used as it is
#
# Exit codes:
#   0  success
#   1  bad argument or unknown set
#   2  the config or the list could not be found

set -euo pipefail

# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/i18n.sh"
# shellcheck source=modules/main/lib/resolve_resolvers.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/resolve_resolvers.sh"


SET_ARG=""

usage() {
    sed -n '3,8p' "$0"
    exit "${1:-0}"
}

# ---- the config directory: the same resolution as the lookup ---------------
# The root is the one the lookup computed (I18N_ROOT of i18n.sh), so the two
# sides cannot drift apart. RECON_ROOT is the modules/ directory, not the
# root, so it cannot be used here.
config_dir() {
    if [ -n "${SRCDAR_CONFIG_DIR:-}" ]; then
        printf '%s' "$SRCDAR_CONFIG_DIR"
    elif [ -n "${I18N_ROOT:-}" ]; then
        printf '%s/config' "$I18N_ROOT"
    else
        printf '%s' ""
    fi
}

# The sets that the script accepts, and the file that each one names.
set_file() {
    case "$1" in
        cn)     printf '%s/cn' "$_RR_SETS_DIR" ;;
        global) printf '%s/global' "$_RR_SETS_DIR" ;;
        *)      printf '%s' "" ;;
    esac
}

while [ $# -gt 0 ]; do
    case "$1" in
        --set)  [ $# -ge 2 ] || { echo "$(t main.set_resolvers.arg_needs_value)" >&2; usage 1; }
                 SET_ARG="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "$(t main.set_resolvers.unknown_arg arg="$1")" >&2; usage 1 ;;
    esac
done

CONF_DIR="$(config_dir)"
if [ -z "$CONF_DIR" ]; then
    echo "$(t main.set_resolvers.root_missing)" >&2
    exit 2
fi
CONF_FILE="$CONF_DIR/pdtm.conf"

# ---- the conf: read the two fields ------------------------------------------
conf_get() {
    [ -f "$CONF_FILE" ] || return 0
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$CONF_FILE" 2>/dev/null \
        | head -1 | tr -d '\042\047[:space:]'
}

resolver_set="$(conf_get resolver_set)"
resolvers_file="$(conf_get resolvers_file)"

# The helper reads the two fields from the scope, so they are exported to it
# as the variables it expects.
export resolver_set resolvers_file

# ---- show --------------------------------------------------------------------
CURRENT_SET="$(resolve_resolvers_set)"
CURRENT_FILE="$(resolve_resolvers_file || true)"

if [ -z "$SET_ARG" ]; then
    if [ -n "$CURRENT_FILE" ]; then
        echo "$(t main.set_resolvers.current set="$CURRENT_SET")"
        echo "$(t main.set_resolvers.path path="$CURRENT_FILE")"
    else
        echo "$(t main.set_resolvers.current set="$CURRENT_SET")"
        echo "$(t main.set_resolvers.list_missing dir="$_RR_SETS_DIR")" >&2
    fi
    if [ "$resolver_set" = "auto" ]; then
        echo "$(t main.set_resolvers.auto_hint lang="$(i18n_lang)")"
    fi
    exit 0
fi

# ---- switch ------------------------------------------------------------------
NEW_SET="$SET_ARG"
case "$NEW_SET" in
    auto|cn|global) ;;
    *)
        echo "$(t main.set_resolvers.unknown_set set="$NEW_SET")" >&2
        exit 1
        ;;
esac

# The file of the new set has to be there before the choice is written, so a
# broken choice is refused instead of leaving the pipeline without a list.
if [ "$NEW_SET" != "auto" ]; then
    NEW_FILE="$(set_file "$NEW_SET")"
    if [ -z "$NEW_FILE" ] || [ ! -f "$NEW_FILE" ]; then
        echo "$(t main.set_resolvers.list_missing dir="$_RR_SETS_DIR")" >&2
        exit 2
    fi
fi

# The conf is written atomically: a temporary file and a rename, so a crash
# cannot leave a half-written config behind.
mkdir -p "$CONF_DIR"
TMP="$(mktemp "$CONF_DIR/.pdtm.conf.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

if [ -f "$CONF_FILE" ]; then
    # the two fields are replaced, every other field is kept as it is
    awk '/^[[:space:]]*resolver_set=/ { next }
         /^[[:space:]]*resolvers_file=/ { next }
         { print }' "$CONF_FILE" > "$TMP"
else
    : > "$TMP"
fi

# The path that the new set resolves to: the auto mode resolves to the set
# that the language decides, the other modes to their own file.
resolver_set="$NEW_SET"
if [ "$NEW_SET" = "auto" ]; then
    resolver_set="auto"
    RESOLVED_FILE="$(resolve_resolvers_file || true)"
else
    RESOLVED_FILE="$(set_file "$NEW_SET")"
fi

{
    printf 'resolver_set=%s\n' "$NEW_SET"
    printf 'resolvers_file=%s\n' "$RESOLVED_FILE"
} >> "$TMP"

mv -f "$TMP" "$CONF_FILE"
trap - EXIT
chmod 600 "$CONF_FILE"

if [ "$NEW_SET" = "auto" ]; then
    echo "$(t main.set_resolvers.changed_auto file="$RESOLVED_FILE")"
elif [ "$NEW_SET" = "$CURRENT_SET" ]; then
    echo "$(t main.set_resolvers.unchanged set="$NEW_SET")"
else
    echo "$(t main.set_resolvers.changed set="$NEW_SET")"
fi
echo "$(t main.set_resolvers.path path="$RESOLVED_FILE")"

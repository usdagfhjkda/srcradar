#!/usr/bin/env bash
#
# check_wildcard.sh - use dnsx to determine whether a glob domain has wildcard DNS enabled
#
# How it works: for a glob pattern containing `*` (e.g. `*.example.com` / `cc-*.example.com` /
#       `aaa.*.bbb.com`), replace `*` with a random label and probe it over DNS; if it
#       resolves, the domain is a wildcard. For an exact single subdomain without `*`
#       (e.g. `demo.scanme.sh`) no wildcard check is needed
#       (it is one concrete host, there is no "wildcard" semantics).
#
# Usage:
#   ./check_wildcard.sh target.txt
#   cat target.txt | ./check_wildcard.sh
#
# Output:
#   wildcard.txt     -> glob patterns with wildcard DNS enabled
#   no_wildcard.txt  -> no wildcard DNS / exact single subdomain (no check needed)
#
set -euo pipefail
# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/i18n.sh"

# ---- load config/pdtm.conf (priority: env > config > script default) ----
SCRIPT_DIR_CW="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR_CW/../lib/load_config.sh"
load_config_set "$SCRIPT_DIR_CW/../../../config/pdtm.conf" "$SCRIPT_DIR_CW/../lib"

# ---- config ----
INPUT="${1:-/dev/stdin}"
WILDCARD_OUT="wildcard.txt"
NORMAL_OUT="no_wildcard.txt"
# resolver source stays consistent with the ./resolvers file (see README section 8-11)
resolvers_file="${resolvers_file:-resolvers}"
RESOLVERS="$(tr '\n' ',' < "$resolvers_file" | sed 's/,$//')"
[ -z "$RESOLVERS" ] && { echo "[-] $(t main.check_wildcard.resolvers_unreadable file="$resolvers_file")" >&2; exit 1; }
PROBES=3

# ---- dependency check ----
command -v dnsx >/dev/null 2>&1 || {
  echo "$(t main.check_wildcard.dnsx_missing)" >&2
  exit 1
}

# truncate the output files
: > "$WILDCARD_OUT"
: > "$NORMAL_OUT"

# generate a random string (used to replace * in the glob)
rand_label() {
  head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-16
}

while IFS= read -r line || [ -n "$line" ]; do
  # strip whitespace, skip empty lines and comments
  line="$(echo "$line" | tr -d '[:space:]')"
  [ -z "$line" ] && continue
  case "$line" in \#*) continue ;; esac

  is_wildcard="no"

  if [[ "$line" == *\** ]]; then
    # glob pattern containing *: replace * with a random label, then probe with dnsx
    # e.g. *.example.com       -> <rand>.example.com
    #     cc-*.example.com    -> cc-<rand>.example.com
    #     aaa.*.bbb.com       -> aaa.<rand>.bbb.com
    for _ in $(seq "$PROBES"); do
      probe="${line//\*/$(rand_label)}"
      result="$(echo "$probe" | dnsx -silent -a -r "$RESOLVERS" -rl 30 -t 10 2>/dev/null || true)"
      if [ -n "$result" ]; then
        is_wildcard="yes"
        break
      fi
    done
  else
    # exact single subdomain: no "wildcard" semantics, goes straight to no_wildcard
    is_wildcard="no"
  fi

  if [ "$is_wildcard" = "yes" ]; then
    echo "$line" >> "$WILDCARD_OUT"
    echo "$(t main.check_wildcard.wildcard_line line="$line")"
  else
    echo "$line" >> "$NORMAL_OUT"
    if [[ "$line" == *\** ]]; then
      echo "$(t main.check_wildcard.normal_line line="$line")"
    else
      echo "$(t main.check_wildcard.precise_line line="$line")"
    fi
  fi
done < "$INPUT"

echo
echo "$(t main.check_wildcard.done)"
_i18n_val=$(wc -l < "$WILDCARD_OUT")
echo "$(t main.check_wildcard.wildcard_out out_file="$WILDCARD_OUT" count="$_i18n_val")"
_i18n_val=$(wc -l < "$NORMAL_OUT")
echo "$(t main.check_wildcard.normal_out out_file="$NORMAL_OUT" count="$_i18n_val")"

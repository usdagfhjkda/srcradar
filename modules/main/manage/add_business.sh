#!/usr/bin/env bash
# add_business.sh — bootstrap a new business: business row + config + optional
# seed companies + optional scope.
#
# Usage:
#   ./manage/add_business.sh -n <business name>
#                            [--enabled 0|1] [--web 0|1] [--tcp 0|1] [--icp 0|1]
#                            [-s <seed.tsv>] [-i <input_dir>] [-d <db>]
#                            [--keep-input]                   # v0.1.0+: keep the files in the -i dir
#                            [--auto] [--ymicp-base URL] [--ymicp-user U]
#                            [--ymicp-pass P] [--ymicp-pages N]
#
# --auto and -i are mutually exclusive (auto generates target.txt internally
# and imports it into scopes)
#
# Default config: enabled=1, web=1, tcp=0, icp=1
#
# Steps (the order is fixed):
#   1. INSERT OR IGNORE INTO businesses (business_name)               -> biz_id
#   2. INSERT OR IGNORE INTO recon_business_config                   (the flags or the defaults)
#   3. if -s: parse the seed TSV -> INSERT OR IGNORE INTO companies
#   4. if -i: call ../pdtm/scope_import.sh -b <biz> -i <dir>          (scope import)
#
# Note: step 2 uses INSERT OR IGNORE - an existing config row is not
#       overwritten by the flags. To adjust the config afterwards, use
#       manage/set_config.sh.
#
# seed TSV format (a header is required, the columns are separated by a tab):
#   name<TAB>group[<any extra columns, ignored>]
#   example: ExampleCoSubsidiaryLtd<TAB>core
# The extra columns (pid / legal_person / status and the like, fields from
# the old format) are ignored - if those metadata are needed later, they
# should move to a separate biz_seeds table.
#
# input_dir format: see ../pdtm/scope_import.sh (it needs target.txt, and
# exclude.txt is optional). Afterwards, by hand:
#   cd ../db_align && ./bin/db_align -n '<business name>' -all
#   (pulls the holding tree + the ICP / WeChat / mini-program assets; it may
#    trip the AQC risk control, so it does not run automatically here)
#
# Known constraints:
#   - companies.unit_name is globally UNIQUE (it carries no business_id). If
#     the name in the seed already exists under another business, the INSERT
#     OR IGNORE skips that row (no rebinding). The schema fix is in
#     README, "known issues".
#   - recon_business_config INSERT OR IGNORE: an existing row stays as it is,
#     which protects against a later operator edit.

set -euo pipefail
# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/i18n.sh"

# The pdtm tool paths are hardcoded (as in scan.sh: the head of the Ubuntu
#  .bashrc returns early in the `case $-` branch, so sourcing ~/.bashrc never
#  reaches the export later on - hardcoding is the most reliable).
# --auto goes through scope_import -> check_wildcard.sh needs dnsx, so set PATH.
export PATH="$PATH:$HOME/.pdtm/go/bin"

RECON_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DB="${DB:-${RECON_DB:-$RECON_ROOT/main/db/recon.sqlite3}}"

NAME=""
SEED=""
INPUT_DIR=""
ENABLED=1
WEB=1
TCP=0
ICP=1

# --auto: the ymicp prerequisite (search the business name -> write mapp_records -> pull the domain into target.txt -> import)
AUTO=0
AUTO_INPUT_DIR=""
KEEP_INPUT=0
YMICP_BASE="http://127.0.0.1:16181"
YMICP_USER="${YMICP_USER:-admin}"
YMICP_PASS="${YMICP_PASS:-}"
YMICP_PAGES=1

usage() {
    sed -n '2,16p' "$0"
    exit "${1:-0}"
}

# Validate flag value is 0 or 1; $1=flag name (e.g. --web), $2=value
check_01() {
    if [ "$2" != "0" ] && [ "$2" != "1" ]; then
        echo "[$1] must be 0 or 1, got: $2" >&2; exit 1
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        -n|--name)    NAME="$2"; shift 2 ;;
        -s|--seed)    SEED="$2"; shift 2 ;;
        -i|--input)   INPUT_DIR="$2"; shift 2 ;;
        -d|--db)      DB="$2"; shift 2 ;;
        --auto)       AUTO=1; shift ;;
        --keep-input)  KEEP_INPUT=1; shift ;;
        --ymicp-base) YMICP_BASE="$2"; shift 2 ;;
        --ymicp-user) YMICP_USER="$2"; shift 2 ;;
        --ymicp-pass) YMICP_PASS="$2"; shift 2 ;;
        --ymicp-pages) YMICP_PAGES="$2"; shift 2 ;;
        --enabled)    check_01 "$1" "$2"; ENABLED="$2"; shift 2 ;;
        --web)        check_01 "$1" "$2"; WEB="$2";     shift 2 ;;
        --tcp)        check_01 "$1" "$2"; TCP="$2";     shift 2 ;;
        --icp)        check_01 "$1" "$2"; ICP="$2";     shift 2 ;;
        -h|--help)    usage 0 ;;
        *) echo "unknown arg: $1" >&2; usage 1 ;;
    esac
done

[ -n "$NAME" ] || { echo "$(t main.add_business.arg_name_required)" >&2; usage 1; }
[ -f "$DB" ]  || { echo "[-d] db not found: $DB" >&2; exit 1; }
if [ "$AUTO" = "1" ] && [ -n "$INPUT_DIR" ]; then
    echo "$(t main.add_business.auto_and_input_exclusive)" >&2; exit 1
fi
if [ "$KEEP_INPUT" = "1" ] && [ "$AUTO" = "1" ]; then
    echo "$(t main.add_business.keep_input_auto_exclusive)" >&2; exit 1
fi
if [ "$KEEP_INPUT" = "1" ] && [ -z "$INPUT_DIR" ]; then
    echo "$(t main.add_business.keep_input_needs_input)" >&2
fi

# ---- step 1+2: business row + config bootstrap ----
read -r BID CUR_ENABLED CUR_WEB CUR_TCP CUR_ICP < <(python3 - "$DB" "$NAME" "$ENABLED" "$WEB" "$TCP" "$ICP" <<'PY'
import sqlite3, sys
db, biz, en, web, tcp, icp = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
conn = sqlite3.connect(db)
conn.execute("INSERT OR IGNORE INTO businesses (business_name) VALUES (?)", (biz,))
bid = int(conn.execute("SELECT id FROM businesses WHERE business_name=?", (biz,)).fetchone()[0])
# INSERT OR IGNORE - an existing config row stays as it is, which protects against a later operator edit
conn.execute(
    "INSERT OR IGNORE INTO recon_business_config (business_id, enabled, web, tcp, icp) "
    "VALUES (?, ?, ?, ?, ?)",
    (bid, en, web, tcp, icp),
)
conn.commit()
row = conn.execute(
    "SELECT business_id, enabled, web, tcp, icp FROM recon_business_config WHERE business_id=?",
    (bid,),
).fetchone()
print(" ".join(str(x) for x in row))
PY
)
echo "[add_biz] step 1-2: business id=$BID name='$NAME' config=enabled=$CUR_ENABLED,web=$CUR_WEB,tcp=$CUR_TCP,icp=$CUR_ICP"
if [ "$CUR_ENABLED" != "$ENABLED" ] || [ "$CUR_WEB" != "$WEB" ] || [ "$CUR_TCP" != "$TCP" ] || [ "$CUR_ICP" != "$ICP" ]; then
    echo "$(t main.add_business.config_exists)"
fi

# ---- step 3: seed companies (optional) ----
if [ -n "$SEED" ]; then
    [ -f "$SEED" ] || { echo "[-s] seed file not found: $SEED" >&2; exit 1; }
    python3 - "$DB" "$BID" "$SEED" <<'PY'
import csv, sqlite3, sys
from datetime import datetime, timezone
db, bid, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
conn = sqlite3.connect(db)
now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
ok = skip = fail = 0
with open(path, encoding="utf-8", newline="") as f:
    rdr = csv.DictReader(f, delimiter="\t")
    if not rdr.fieldnames or "name" not in rdr.fieldnames:
        sys.exit(f"[-s] seed TSV must have 'name' column (header required); got fieldnames={rdr.fieldnames}")
    for r in rdr:
        name = (r.get("name") or "").strip()
        if not name or name.startswith("#"):
            skip += 1
            continue
        grp = (r.get("group") or "").strip() or None
        try:
            conn.execute(
                'INSERT OR IGNORE INTO companies (unit_name, business_id, nature_name, "group", created_at, updated_at) '
                "VALUES (?, ?, '企业', ?, ?, ?)",
                (name, bid, grp, now, now),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                ok += 1
            else:
                skip += 1
        except Exception as e:
            print(f"  [-s] fail name={name!r}: {e}", file=sys.stderr)
            fail += 1
conn.commit()
print(f"[seed] inserted={ok} skip={skip} fail={fail}")
PY
fi

# ---- step 3.5: the --auto prerequisite (ymicp /query/web -> target.txt -> import) ----
if [ "$AUTO" = "1" ]; then
    YMICP_SCRIPT="$RECON_ROOT/public/ymicp/icp_mapp_query.py"
    [ -f "$YMICP_SCRIPT" ] || { echo "$(t main.add_business.ymicp_script_missing path="$YMICP_SCRIPT")" >&2; exit 1; }

    AUTO_INPUT_DIR="$(mktemp -d)"
    AUTO_TARGET="$AUTO_INPUT_DIR/target.txt"
    trap 'rm -rf "$AUTO_INPUT_DIR"' EXIT

    echo "$(t main.add_business.ymicp_query NAME="$NAME" YMICP_BASE="$YMICP_BASE" YMICP_PAGES="$YMICP_PAGES")"

    # icp_mapp_query.py --endpoint web --out-domains <file>:
    #   - calls /query/web (the website filing, not the mini program /query/mapp)
    #   - takes the domain field of every record, deduplicates it and writes <file>
    #   - does not write mapp_records (the web filing and the mapp_records table
    #     have different semantics)
    YMICP_ARGS=(--endpoint web --out-domains "$AUTO_TARGET"
                --base "$YMICP_BASE" --user "$YMICP_USER"
                --pages "$YMICP_PAGES"
                --db "$DB")
    [ -n "$YMICP_PASS" ] && YMICP_ARGS+=(--pass "$YMICP_PASS")

    if ! printf '%s\n' "$NAME" | python3 "$YMICP_SCRIPT" "${YMICP_ARGS[@]}"; then
        rc=$?
        echo "$(t main.add_business.ymicp_failed rc="$rc")" >&2
        echo "$(t main.add_business.temp_dir_kept AUTO_INPUT_DIR="$AUTO_INPUT_DIR")" >&2
        trap - EXIT
        exit $rc
    fi

    if [ ! -s "$AUTO_TARGET" ]; then
        echo "$(t main.add_business.target_empty)"
        echo "$(t main.add_business.temp_dir_kept_debug AUTO_INPUT_DIR="$AUTO_INPUT_DIR")"
        trap - EXIT  # the temporary directory is kept, so that the user can cat it
    else
        n=$(wc -l < "$AUTO_TARGET")
        echo "$(t main.add_business.target_count n="$n")"
    fi
fi

# ---- step 4: scope import (optional) ----
# The entry point: -i passed straight through, or the AUTO_INPUT_DIR that --auto generated
EFFECTIVE_INPUT_DIR=""
if [ "$AUTO" = "1" ]; then
    EFFECTIVE_INPUT_DIR="$AUTO_INPUT_DIR"
elif [ -n "$INPUT_DIR" ]; then
    EFFECTIVE_INPUT_DIR="$INPUT_DIR"
fi
if [ -n "$EFFECTIVE_INPUT_DIR" ]; then
    [ -d "$EFFECTIVE_INPUT_DIR" ]   || { echo "[-i] input dir not found: $EFFECTIVE_INPUT_DIR" >&2; exit 1; }
    [ -f "$EFFECTIVE_INPUT_DIR/target.txt" ] || { echo "$(t main.add_business.target_missing dir="$EFFECTIVE_INPUT_DIR")" >&2; exit 1; }
    echo "[add_biz] step 4: scope_import (source: $([ "$AUTO" = "1" ] && echo --auto || echo -i))"
    if [ "$KEEP_INPUT" = "1" ]; then
        "$RECON_ROOT/main/pdtm/scope_import.sh" -b "$NAME" -i "$EFFECTIVE_INPUT_DIR" -d "$DB" --keep-files
    else
        "$RECON_ROOT/main/pdtm/scope_import.sh" -b "$NAME" -i "$EFFECTIVE_INPUT_DIR" -d "$DB"
    fi
fi

cat <<EOF

[add_biz] done.
  next:
    cd $RECON_ROOT/public/db_align && ./bin/db_align -n '$NAME' -all
    $(t main.add_business.auto_note)
EOF

#!/usr/bin/env bash
# scope_import.sh - import only the scope (measurable / not measurable) + the
# business row, without running the pipeline
#
# Usage:
#   ./scope_import.sh -b example.com -i ./input/
#   ./scope_import.sh -b example.com -i ./input/ --dry-run
#   ./scope_import.sh -b example.com -i ./input/ --keep-files
#   ./scope_import.sh -b example.com -i ./input/ --skip-check-wildcard
#
# What it does:
#   - -i is required and gives the directory (it holds target.txt, exclude.txt
#     is optional)
#   - copies to the local target.txt / exclude.txt (mirrors pipeline.sh prepare_input)
#   - runs check_wildcard.sh by default -> is_wildcard=1 for a wildcard hit, 0
#       otherwise; --skip-check-wildcard skips it and leaves every
#       is_wildcard at 0
#   - bootstraps the businesses + scopes tables, INSERT OR IGNORE the business row
#   - upserts the scope (measurable assets -> target.txt; not measurable ->
#     exclude.txt) and overwrites is_wildcard for a wildcard hit (the same as
#     import_scan_results.upsert_scope)
#   - does not run scan / scanner / import / finalize
#   - trap cleanup: by default the local target.txt / exclude.txt / wildcard.txt /
#       no_wildcard.txt are removed; --keep-files forces them to stay; a
#       failure keeps them by default (for the investigation)
#
# When it fits:
#   - only the business and the scope should be registered, without scanning yet
#   - quick data loading while debugging the schema / the DB
#
set -euo pipefail
# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/i18n.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BUSINESS=
INPUT_PATH=
# DB: the env var > RECON_DB > the default ../db/recon.sqlite3 (works both in docker and bare)
DB="${DB:-${RECON_DB:-../db/recon.sqlite3}}"
DRY_RUN=
KEEP_FILES=
SKIP_WILDCARD=
INPUT_GENERATED=0
WILDCARD_OUT=wildcard.txt
NO_WILDCARD_OUT=no_wildcard.txt

# ===== arguments =====
while [ $# -gt 0 ]; do
    case "$1" in
        -b|--business)            BUSINESS="$2"; shift 2 ;;
        -i|--input)               INPUT_PATH="$2"; shift 2 ;;
        -d|--db)                  DB="$2"; shift 2 ;;
        --dry-run)                DRY_RUN=1; shift ;;
        --keep-files)             KEEP_FILES=1; shift ;;
        --skip-check-wildcard)    SKIP_WILDCARD=1; shift ;;
        -h|--help)
            sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "$(t main.scope_import.unknown_arg arg="$1")" >&2; exit 1 ;;
    esac
done

[ -n "$BUSINESS" ]   || { echo "$(t main.scope_import.business_required)" >&2; exit 1; }
[ -n "$INPUT_PATH" ] || { echo "$(t main.scope_import.input_required)" >&2; exit 1; }
[ -d "$INPUT_PATH" ] || { echo "$(t main.scope_import.input_not_a_dir path="$INPUT_PATH")" >&2; exit 1; }
[ -f "$INPUT_PATH/target.txt" ] || { echo "$(t main.scope_import.target_missing path="$INPUT_PATH")" >&2; exit 1; }

DB_DIR="$(dirname "$DB")"
mkdir -p "$DB_DIR"

# copied to the local working dir (mirrors pipeline.sh prepare_input).
# the -ef check avoids a "copy from the same source / to the same target"
# error. Given that INPUT_PATH already passed the -d validation:
#   - INPUT_PATH/target.txt exists -> -ef is true when "./ is INPUT_PATH", so
#     the copy is skipped
#   - in every other case it is copied to the local pdtm/
# the -ef check stays as a fallback (it avoids the same-source copy error), but
# under a docker bind mount it misjudges, so the copy is forced with cp -f.
# --keep-files is then needed to keep target.txt.
if [ "$INPUT_PATH/target.txt" -ef "target.txt" ]; then
    cp -f "$INPUT_PATH/target.txt" "target.txt"
else
    cp -f "$INPUT_PATH/target.txt" "target.txt"
fi
INPUT_GENERATED=1
if [ -f "$INPUT_PATH/exclude.txt" ]; then
    if [ "$INPUT_PATH/exclude.txt" -ef "exclude.txt" ]; then
        :
    else
        cp "$INPUT_PATH/exclude.txt" "exclude.txt"
    fi
else
    : > "exclude.txt"
fi

echo "$(t main.scope_import.business BUSINESS="$BUSINESS")"
echo "$(t main.scope_import.input_dir INPUT_PATH="$INPUT_PATH")"

# check_wildcard: probe whether the domains in target.txt resolve as a wildcard;
# the result goes to wildcard.txt / no_wildcard.txt.
# It runs by default (three dnsx probes per domain, no subfinder/alterx, so it
# still means "not a scan");
# when it is skipped - use --skip-check-wildcard to leave is_wildcard at 0.
TMP_LOG=$(mktemp)
if [ -z "$SKIP_WILDCARD" ]; then
    if ! ./check_wildcard.sh "target.txt" > "$TMP_LOG" 2>&1; then
        echo "$(t main.scope_import.wildcard_failed TMP_LOG="$TMP_LOG")" >&2
        tail -10 "$TMP_LOG" >&2
        exit 1
    fi
    W_N=$(wc -l < "$WILDCARD_OUT" 2>/dev/null || echo 0)
    N_N=$(wc -l < "$NO_WILDCARD_OUT" 2>/dev/null || echo 0)
    echo "$(t main.scope_import.wildcard_counts W_N="$W_N" N_N="$N_N")"
else
    : > "$WILDCARD_OUT"
    echo "$(t main.scope_import.wildcard_skipped)"
fi

# ===== prepare the scope data =====
TMP_T=$(mktemp); TMP_E=$(mktemp)
# by default the local target.txt / exclude.txt / wildcard.txt / no_wildcard.txt
# are removed; --keep-files keeps them; a failure keeps them by default (for
# the investigation).
cleanup() {
    local rc=$?
    rm -f "$TMP_T" "$TMP_E" "$TMP_LOG"
    if [ "$rc" -ne 0 ]; then
        if [ "$INPUT_GENERATED" = "1" ]; then
            echo "$(t main.scope_import.failed rc="$rc")" >&2
        fi
        return 0
    fi
    if [ -n "$KEEP_FILES" ]; then
        [ "$INPUT_GENERATED" = "1" ] && echo "[+] $(t main.scope_import.keep_files)"
        return 0
    fi
    if [ "$INPUT_GENERATED" = "1" ]; then
        rm -f target.txt exclude.txt "$WILDCARD_OUT" "$NO_WILDCARD_OUT"
        echo "$(t main.scope_import.cleaned_local)"
        # v0.1.0: also rm INPUT_PATH/target.txt + exclude.txt if INPUT_PATH is
        # an external dir (not cwd inode). Failure path above does NOT do
        # this - see README §8-14. Pass --keep-files to retain both.
        if [ -n "$INPUT_PATH" ] && [ -d "$INPUT_PATH" ]; then
            if [ "$INPUT_PATH/target.txt" -ef "target.txt" ]; then
                : # same inode — already removed by the rm above
            else
                if [ -f "$INPUT_PATH/target.txt" ]; then
                    rm -f "$INPUT_PATH/target.txt"
                    echo "$(t main.scope_import.cleaned_external_target INPUT_PATH="$INPUT_PATH")"
                fi
                if [ -f "$INPUT_PATH/exclude.txt" ]; then
                    rm -f "$INPUT_PATH/exclude.txt"
                    echo "$(t main.scope_import.cleaned_external_exclude INPUT_PATH="$INPUT_PATH")"
                fi
                # rmdir if now empty (best-effort; not fatal if non-empty)
                rmdir "$INPUT_PATH" 2>/dev/null &&                     echo "[+] $(t main.scope_import.cleaned path="$INPUT_PATH")"
            fi
        fi
    fi
}
trap cleanup EXIT

python3 - "$TMP_T" "$TMP_E" <<'PY'
import sys
from pathlib import Path
ft, fe = sys.argv[1], sys.argv[2]

def clean(line):
    line = line.strip().lstrip("﻿")
    if not line or line.startswith("#"): return None
    return line.split()[0].strip().lower().rstrip(".")

def collect(path):
    out, seen = [], set()
    if not path.exists(): return out
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        v = clean(ln)
        if v and v not in seen: out.append(v); seen.add(v)
    return out

targets  = collect(Path("target.txt"))
excludes = collect(Path("exclude.txt"))
# write the trailing newline, so that wc -l and the Python len() agree
Path(ft).write_text(("\n".join(targets)  + "\n") if targets  else "", encoding="utf-8")
Path(fe).write_text(("\n".join(excludes) + "\n") if excludes else "", encoding="utf-8")
print(f"[scope_read] target={len(targets)} exclude={len(excludes)}")
PY

T_LINES=$(wc -l < "$TMP_T")
E_LINES=$(wc -l < "$TMP_E")

if [ "$T_LINES" -eq 0 ]; then
    echo "$(t main.scope_import.target_empty)" >&2
    exit 1
fi

if [ -n "$DRY_RUN" ]; then
    echo "$(t main.scope_import.dry_run)"
    echo "$(t main.scope_import.dry_run_measurable T_LINES="$T_LINES")"
    echo "$(t main.scope_import.dry_run_not_measurable E_LINES="$E_LINES")"
    echo "    DB: $DB"
    echo "$(t main.scope_import.dry_run_done)"
    exit 0
fi

# ===== bootstrap + insert =====
python3 - "$DB" "$BUSINESS" "$TMP_T" "$TMP_E" <<'PY'
import sys, sqlite3
from datetime import datetime, timezone
from pathlib import Path

# The i18n lookup lives in modules/main/lib, which is not on sys.path at
# this depth. Walk up to the repo root (the directory that holds
# "modules") and add the lib directory to the path.
for _p in Path.cwd().resolve().parents:
    if (_p / "modules").is_dir():
        sys.path.insert(0, str(_p / "modules" / "main" / "lib"))
        break
from i18n import t as _t
db, biz, ft, fe = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

sys.path.insert(0, ".")
import import_scan_results

def read(p):
    if not Path(p).exists(): return []
    return [ln for ln in Path(p).read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]

targets  = read(ft)
excludes = read(fe)
now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

conn = sqlite3.connect(db)
conn.execute("PRAGMA foreign_keys = ON")

# the same behaviour as pipeline.sh bootstrap_db: the businesses table is created by the shared side / the upstream project
cols = [r[1] for r in conn.execute("PRAGMA table_info(businesses)").fetchall()]
if "business_name" not in cols:
    sys.exit(_t("main.scope_import.db_schema_missing", {"db": db}))

# the private schema of this project, such as scopes
import_scan_results.create_tables(conn)

# INSERT OR IGNORE the business row
conn.execute("INSERT OR IGNORE INTO businesses (business_name) VALUES (?)", (biz,))
bid = int(conn.execute("SELECT id FROM businesses WHERE business_name=?", (biz,)).fetchone()[0])

# read the wildcard.txt that check_wildcard wrote and build the set of wildcard domains (host normalization: strip the trailing dot, lower)
wp = Path("wildcard.txt")
if wp.exists():
    wildcards = {
        ln.strip().lower().rstrip(".")
        for ln in wp.read_text(encoding="utf-8", errors="replace").splitlines()
        if ln.strip()
    }
else:
    wildcards = set()

def upsert(scope, asset):
    asset_norm = asset.strip().lower().rstrip(".")
    is_wc = 1 if asset_norm in wildcards else 0
    conn.execute(
        "INSERT INTO scopes (business_id, scope_name, asset, is_wildcard, created_at, updated_at, fetched_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(business_id, scope_name, asset) DO UPDATE SET "
        "  updated_at=excluded.updated_at, fetched_at=excluded.fetched_at, "
        "  is_wildcard=excluded.is_wildcard",
        (bid, scope, asset, is_wc, now, now, now),
    )

for a in targets:  upsert("可测资产", a)
for a in excludes: upsert("非可测资产", a)
conn.commit()

# read the row back after the insert and print the confirmed count
counts = {row[0]: row[1] for row in conn.execute(
    "SELECT scope_name, COUNT(*) FROM scopes WHERE business_id=? GROUP BY scope_name",
    (bid,),
)}
conn.close()
_scope_counts = {
    'targets': len(targets), 'excludes': len(excludes),
    'measurable': counts.get('可测资产', 0), 'not_measurable': counts.get('非可测资产', 0),
}
print(
    f"[scope_import] business='{biz}' id={bid}: "
    f"{_t('main.scope_import.counts', _scope_counts)}"
)
PY

echo "$(t main.scope_import.done)"

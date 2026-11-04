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
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
done

[ -n "$BUSINESS" ]   || { echo "需要 -b/--business 指定业务名" >&2; exit 1; }
[ -n "$INPUT_PATH" ] || { echo "需要 -i/--input 指定输入目录 (含 target.txt)" >&2; exit 1; }
[ -d "$INPUT_PATH" ] || { echo "-i 不是目录: $INPUT_PATH" >&2; exit 1; }
[ -f "$INPUT_PATH/target.txt" ] || { echo "目录 $INPUT_PATH 缺少 target.txt" >&2; exit 1; }

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

echo "[*] 业务: $BUSINESS"
echo "[*] 输入目录: $INPUT_PATH"

# check_wildcard: probe whether the domains in target.txt resolve as a wildcard;
# the result goes to wildcard.txt / no_wildcard.txt.
# It runs by default (three dnsx probes per domain, no subfinder/alterx, so it
# still means "not a scan");
# when it is skipped - use --skip-check-wildcard to leave is_wildcard at 0.
TMP_LOG=$(mktemp)
if [ -z "$SKIP_WILDCARD" ]; then
    if ! ./check_wildcard.sh "target.txt" > "$TMP_LOG" 2>&1; then
        echo "[!] check_wildcard 失败 (详细见 $TMP_LOG):" >&2
        tail -10 "$TMP_LOG" >&2
        exit 1
    fi
    W_N=$(wc -l < "$WILDCARD_OUT" 2>/dev/null || echo 0)
    N_N=$(wc -l < "$NO_WILDCARD_OUT" 2>/dev/null || echo 0)
    echo "[*] check_wildcard: $W_N 个泛解析, $N_N 个非泛解析"
else
    : > "$WILDCARD_OUT"
    echo "[*] --skip-check-wildcard: 跳过, 所有 is_wildcard=0"
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
            echo "[!] 失败 (exit=$rc), 保留 target.txt / exclude.txt / wildcard.txt / no_wildcard.txt 供排查 (--keep-files 同效)" >&2
        fi
        return 0
    fi
    if [ -n "$KEEP_FILES" ]; then
        [ "$INPUT_GENERATED" = "1" ] && echo "[+] --keep-files: 保留 target.txt / exclude.txt / wildcard.txt / no_wildcard.txt"
        return 0
    fi
    if [ "$INPUT_GENERATED" = "1" ]; then
        rm -f target.txt exclude.txt "$WILDCARD_OUT" "$NO_WILDCARD_OUT"
        echo "[+] 已清理本地 target.txt / exclude.txt / wildcard.txt / no_wildcard.txt (--keep-files 可保留)"
        # v0.1.0: also rm INPUT_PATH/target.txt + exclude.txt if INPUT_PATH is
        # an external dir (not cwd inode). Failure path above does NOT do
        # this - see README §8-14. Pass --keep-files to retain both.
        if [ -n "$INPUT_PATH" ] && [ -d "$INPUT_PATH" ]; then
            if [ "$INPUT_PATH/target.txt" -ef "target.txt" ]; then
                : # same inode — already removed by the rm above
            else
                if [ -f "$INPUT_PATH/target.txt" ]; then
                    rm -f "$INPUT_PATH/target.txt"
                    echo "[+] 已清理外部目录的 target.txt: $INPUT_PATH/target.txt"
                fi
                if [ -f "$INPUT_PATH/exclude.txt" ]; then
                    rm -f "$INPUT_PATH/exclude.txt"
                    echo "[+] 已清理外部目录的 exclude.txt: $INPUT_PATH/exclude.txt"
                fi
                # rmdir if now empty (best-effort; not fatal if non-empty)
                rmdir "$INPUT_PATH" 2>/dev/null &&                     echo "[+] 已清理空目录: $INPUT_PATH"
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
    echo "[!] target.txt 为空,拒绝入库 (新增业务至少需要 1 条可测资产)" >&2
    exit 1
fi

if [ -n "$DRY_RUN" ]; then
    echo "[*] --dry-run: 以下为预览, 未写库, 未查 businesses 表"
    echo "    可测资产 (target.txt): $T_LINES 条"
    echo "    非可测资产 (exclude.txt): $E_LINES 条"
    echo "    DB: $DB"
    echo "[+] dry-run 完成 (本地 target.txt / exclude.txt 仍会被 trap 清理, --keep-files 可保留)"
    exit 0
fi

# ===== bootstrap + insert =====
python3 - "$DB" "$BUSINESS" "$TMP_T" "$TMP_E" <<'PY'
import sys, sqlite3
from datetime import datetime, timezone
from pathlib import Path
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
    sys.exit(f"[scope_import] 错误: {db} 缺少 businesses 表或缺 business_name 列, 请先在共享库中建好")

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
print(
    f"[scope_import] business='{biz}' id={bid}: "
    f"输入 targets={len(targets)} excludes={len(excludes)}; "
    f"DB 可测={counts.get('可测资产', 0)} 非可测={counts.get('非可测资产', 0)}"
)
PY

echo "[+] 完成 - 仅入库 scope, 未扫描"

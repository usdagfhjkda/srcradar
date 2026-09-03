#!/usr/bin/env bash
# add_business.sh — bootstrap a new business: business row + config + optional
# seed companies + optional scope.
#
# Usage:
#   ./manage/add_business.sh -n <业务名>
#                            [--enabled 0|1] [--web 0|1] [--tcp 0|1] [--icp 0|1]
#                            [-s <seed.tsv>] [-i <input_dir>] [-d <db>]
#
# 默认 config: enabled=1, web=1, tcp=0, icp=1
#
# Steps (顺序固定):
#   1. INSERT OR IGNORE INTO businesses (business_name)               → biz_id
#   2. INSERT OR IGNORE INTO recon_business_config                   (用 flag 或默认值)
#   3. 若 -s: 解析 seed TSV → INSERT OR IGNORE INTO companies
#   4. 若 -i: 调 ../pdtm/scope_import.sh -b <biz> -i <dir>            (scope 灌库)
#
# 注意: step 2 用 INSERT OR IGNORE — 已存在的 config 行不会被 flag 覆盖。
#       若要事后调整 config, 用 manage/set_config.sh。
#
# seed TSV 格式（header 必填，列分隔 \t）:
#   name<TAB>group[TAB<任意附加列，忽略>]
#   例: ExampleCo子公司有限公司<TAB>核心
# 多余列（pid / legal_person / status 等老格式里的字段）会被忽略 —
# 若以后要保留这些元数据，建议迁到独立 biz_seeds 表。
#
# input_dir 格式: 见 ../pdtm/scope_import.sh（需含 target.txt，可选 exclude.txt）。
# 之后手动:
#   cd ../db_align && ./bin/db_align -n '<业务名>' -all
#   （拉控股树 + ICP / 公众号 / 小程序资产; 可能触发 AQC 风控, 不在 add_business 内自动跑）
#
# 已知约束:
#   - companies.unit_name 是全局 UNIQUE（不带 business_id）。若 seed 里的 name
#     已存在于另一业务下, INSERT OR IGNORE 会跳过该行（不重绑）。schema 修复见 README §已知问题。
#   - recon_business_config INSERT OR IGNORE: 已存在行不动, 保护 operator 后调。

set -euo pipefail

RECON_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${RECON_DB:-$RECON_ROOT/db/recon.sqlite3}"

NAME=""
SEED=""
INPUT_DIR=""
ENABLED=1
WEB=1
TCP=0
ICP=1

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
        --enabled)    check_01 "$1" "$2"; ENABLED="$2"; shift 2 ;;
        --web)        check_01 "$1" "$2"; WEB="$2";     shift 2 ;;
        --tcp)        check_01 "$1" "$2"; TCP="$2";     shift 2 ;;
        --icp)        check_01 "$1" "$2"; ICP="$2";     shift 2 ;;
        -h|--help)    usage 0 ;;
        *) echo "unknown arg: $1" >&2; usage 1 ;;
    esac
done

[ -n "$NAME" ] || { echo "[-n <业务名>] is required" >&2; usage 1; }
[ -f "$DB" ]  || { echo "[-d] db not found: $DB" >&2; exit 1; }

# ---- step 1+2: business row + config bootstrap ----
read -r BID CUR_ENABLED CUR_WEB CUR_TCP CUR_ICP < <(python3 - "$DB" "$NAME" "$ENABLED" "$WEB" "$TCP" "$ICP" <<'PY'
import sqlite3, sys
db, biz, en, web, tcp, icp = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
conn = sqlite3.connect(db)
conn.execute("INSERT OR IGNORE INTO businesses (business_name) VALUES (?)", (biz,))
bid = int(conn.execute("SELECT id FROM businesses WHERE business_name=?", (biz,)).fetchone()[0])
# INSERT OR IGNORE — 已有 config 行不动, 保护 operator 后调
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
    echo "[add_biz]   (注: 已有 config 行, 你给的 flag 未应用; 改用 manage/set_config.sh)"
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

# ---- step 4: scope import (optional) ----
if [ -n "$INPUT_DIR" ]; then
    [ -d "$INPUT_DIR" ]   || { echo "[-i] input dir not found: $INPUT_DIR" >&2; exit 1; }
    [ -f "$INPUT_DIR/target.txt" ] || { echo "[-i] $INPUT_DIR 缺少 target.txt" >&2; exit 1; }
    echo "[add_biz] step 4: scope_import"
    "$RECON_ROOT/pdtm/scope_import.sh" -b "$NAME" -i "$INPUT_DIR" -d "$DB"
fi

cat <<EOF

[add_biz] done.
  next:
    cd $RECON_ROOT/db_align && ./bin/db_align -n '$NAME' -all
    (拉控股树 + ICP / 公众号 / 小程序; 可能耗时较长, 注意 AQC 风控)
EOF

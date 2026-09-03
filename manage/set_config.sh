#!/usr/bin/env bash
# set_config.sh — view or update recon_business_config for one business.
#
# Usage:
#   ./manage/set_config.sh -n <业务名>                              # 仅查看当前 config
#   ./manage/set_config.sh -n <业务名> --disable                    # enabled=0
#   ./manage/set_config.sh -n <业务名> --enable                     # enabled=1
#   ./manage/set_config.sh -n <业务名> --web 0 --icp 1              # 多个字段
#
# 规则:
#   - -n 必填; 至少一个变更 flag; 不给任何 flag = 只读
#   - --enable / --disable 二选一, 互斥
#   - --web/--tcp/--icp 值必须 0 或 1
#   - 业务行必须在; 不存在时直接报错 (add_business.sh 才会创建)
#   - 仅修改你给的字段, 其它字段不动

set -euo pipefail

RECON_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${RECON_DB:-$RECON_ROOT/db/recon.sqlite3}"

NAME=""
ENABLE_FLAG=""   # "", "enable", "disable"
WEB=""
TCP=""
ICP=""

usage() {
    sed -n '2,10p' "$0"
    exit "${1:-0}"
}

# Validate value is 0 or 1
check_01() {
    if [ "$2" != "0" ] && [ "$2" != "1" ]; then
        echo "[$1] must be 0 or 1, got: $2" >&2; exit 1
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        -n|--name)    NAME="$2"; shift 2 ;;
        --enable)     ENABLE_FLAG="enable"; shift ;;
        --disable)    ENABLE_FLAG="disable"; shift ;;
        --web)        check_01 "$1" "$2"; WEB="$2"; shift 2 ;;
        --tcp)        check_01 "$1" "$2"; TCP="$2"; shift 2 ;;
        --icp)        check_01 "$1" "$2"; ICP="$2"; shift 2 ;;
        -d|--db)      DB="$2"; shift 2 ;;
        -h|--help)    usage 0 ;;
        *) echo "unknown arg: $1" >&2; usage 1 ;;
    esac
done

[ -n "$NAME" ] || { echo "[-n <业务名>] is required" >&2; usage 1; }
[ -f "$DB" ]  || { echo "[-d] db not found: $DB" >&2; exit 1; }

python3 - "$DB" "$NAME" "$ENABLE_FLAG" "$WEB" "$TCP" "$ICP" <<'PY'
import sqlite3, sys
db, biz, enable_flag, web, tcp, icp = sys.argv[1:7]

conn = sqlite3.connect(db)
row = conn.execute(
    "SELECT id FROM businesses WHERE TRIM(business_name)=?", (biz,)
).fetchone()
if row is None:
    sys.exit(f"[set_config] business '{biz}' 不存在; 先跑 manage/add_business.sh")
bid = int(row[0])

cur = conn.execute(
    "SELECT enabled, web, tcp, icp FROM recon_business_config WHERE business_id=?",
    (bid,),
).fetchone()
if cur is None:
    sys.exit(f"[set_config] business_id={bid} 无 config 行; 先跑 manage/add_business.sh")
before = dict(enabled=int(cur[0]), web=int(cur[1]), tcp=int(cur[2]), icp=int(cur[3]))

updates = {}
if enable_flag == "enable":  updates["enabled"] = 1
elif enable_flag == "disable": updates["enabled"] = 0
if web != "": updates["web"] = int(web)
if tcp != "": updates["tcp"] = int(tcp)
if icp != "": updates["icp"] = int(icp)

def fmt(d):
    return f"enabled={d['enabled']} web={d['web']} tcp={d['tcp']} icp={d['icp']}"

print(f"[set_config] before: {fmt(before)}")

if not updates:
    print("[set_config] no flags given; nothing changed")
    sys.exit(0)

sets = ", ".join(f"{k}=?" for k in updates)
conn.execute(
    f"UPDATE recon_business_config SET {sets} WHERE business_id=?",
    (*updates.values(), bid),
)
conn.commit()

after_row = conn.execute(
    "SELECT enabled, web, tcp, icp FROM recon_business_config WHERE business_id=?",
    (bid,),
).fetchone()
after = dict(enabled=int(after_row[0]), web=int(after_row[1]), tcp=int(after_row[2]), icp=int(after_row[3]))
print(f"[set_config] after:  {fmt(after)}")

changed = [k for k in updates if before[k] != after[k]]
unchanged = [k for k in updates if before[k] == after[k]]
if changed:
    print(f"[set_config] changed: {', '.join(changed)}")
if unchanged:
    print(f"[set_config] unchanged (already had this value): {', '.join(unchanged)}")
PY

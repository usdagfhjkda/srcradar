#!/usr/bin/env bash
# scope_import.sh - 仅入库 scope (可测/非可测) + 业务行, 不跑流水线
#
# 用法:
#   ./scope_import.sh -b example.com -i ./input/
#   ./scope_import.sh -b example.com -i ./input/ --dry-run
#   ./scope_import.sh -b example.com -i ./input/ --keep-files
#   ./scope_import.sh -b example.com -i ./input/ --skip-check-wildcard
#
# 行为:
#   - 必须 -i 提供目录(含 target.txt, exclude.txt 可选)
#   - cp 到本地 target.txt / exclude.txt (mirror pipeline.sh prepare_input)
#   - 默认跑 check_wildcard.sh → 命中泛解析的 is_wildcard=1, 否则 0
#       --skip-check-wildcard 跳过, 全部 is_wildcard=0
#   - bootstrap businesses + scopes 表, INSERT OR IGNORE 业务行
#   - upsert scope (可测资产 → target.txt; 非可测资产 → exclude.txt),
#     命中泛解析的覆写 is_wildcard (与 import_scan_results.upsert_scope 一致)
#   - 不跑 scan / scanner / import / finalize
#   - trap cleanup: 默认清理本地 target.txt / exclude.txt / wildcard.txt / no_wildcard.txt;
#       --keep-files 强制保留; 失败默认保留 (供排查)
#
# 适用场景:
#   - 只想登记业务和范围, 暂不扫描
#   - 调试 schema / DB 时, 快速灌入数据
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BUSINESS=
INPUT_PATH=
DB=../db/recon.sqlite3
DRY_RUN=
KEEP_FILES=
SKIP_WILDCARD=
INPUT_GENERATED=0
WILDCARD_OUT=wildcard.txt
NO_WILDCARD_OUT=no_wildcard.txt

# ===== 参数 =====
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

# cp 到本地 working dir (mirror pipeline.sh prepare_input).
# -ef 检查避免「cp 同源/目标」报错. 在 INPUT_PATH 已通过 -d 校验的前提下:
#   - INPUT_PATH/target.txt 存在 → -ef 在「./ 就是 INPUT_PATH」时为真,跳过 cp
#   - 其他情况 cp 到 pdtm/ 本地
if [ "$INPUT_PATH/target.txt" -ef "target.txt" ]; then
    :
else
    cp "$INPUT_PATH/target.txt" "target.txt"
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

# check_wildcard: 探测 target.txt 里的域是否泛解析, 结果写到 wildcard.txt / no_wildcard.txt.
# 默认跑 (用 dnsx 三探针/域, 不涉及 subfinder/alterx, 仍有「不算扫描」语义);
# 跳过的 - 可能用 --skip-check-wildcard 让 is_wildcard 留 0.
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

# ===== 准备 scope 数据 =====
TMP_T=$(mktemp); TMP_E=$(mktemp)
# 默认清理本地 target.txt / exclude.txt / wildcard.txt / no_wildcard.txt;
# --keep-files 保留; 失败默认保留 (供排查).
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
# 写尾换行, 让 wc -l 与 Python len() 一致
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

# ===== bootstrap + 插入 =====
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

# 与 pipeline.sh bootstrap_db 行为一致: businesses 表由共享方/上游项目建
cols = [r[1] for r in conn.execute("PRAGMA table_info(businesses)").fetchall()]
if "business_name" not in cols:
    sys.exit(f"[scope_import] 错误: {db} 缺少 businesses 表或缺 business_name 列, 请先在共享库中建好")

# 建 scopes 等本项目私有 schema
import_scan_results.create_tables(conn)

# INSERT OR IGNORE 业务行
conn.execute("INSERT OR IGNORE INTO businesses (business_name) VALUES (?)", (biz,))
bid = int(conn.execute("SELECT id FROM businesses WHERE business_name=?", (biz,)).fetchone()[0])

# 读 check_wildcard 输出的 wildcard.txt, 建泛解析域 set (host normalization: strip trailing dot, lower)
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

# 落库后回查, 输出确认数
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

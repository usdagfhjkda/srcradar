#!/usr/bin/env bash
# pipeline.sh - 串联 check_wildcard → scan → scanner → import → finalize_scope
#
# Usage:
#   pipeline.sh -b NAME [flags]
#
# Flags:
# REQUIRED:
#   -b, --business string         业务名(从 DB 读 scope,或仅作入参传给 import)
#
# INPUT:
#   -i, --input string            输入目录,含 target.txt + exclude.txt(覆盖 DB)
#   -t, --target string           target 文件路径(默认 target.txt)
#
# PATHS:
#   -d, --db string               DB 路径(默认 ../db/recon.sqlite3)
#   -o, --scan-dir string         scanner 输出目录(默认 scan_results)
#
# STAGES:
#       --tcp                     启用 TCP 端口扫描(透传 scanner.sh -all,naabu 被动+主动)
#       --skip-check-wildcard     跳过 check_wildcard.sh
#       --skip-scan               跳过 scan.sh
#       --skip-scanner            跳过 scanner.sh
#       --skip-import             跳过 import_scan_results.py
#       --dry-run-import          import 阶段 dry-run,不写 DB
#
# DEBUG:
#       --debug int               中间产物保留级别:0=清理,1=保留(默认 1)
#       --keep-on-fail            仅失败时保留中间产物(已默认覆盖,留作兼容)
#
# Examples:
#   pipeline.sh -b example.com                          从 DB 读 scope
#   pipeline.sh -b example.com -i ./input/              从 ./input/ 读 target+exclude
#   pipeline.sh -b example.com --skip-check-wildcard    跳过泛解析探测
#   pipeline.sh -b example.com --dry-run-import         跑全流程但不写库
#   pipeline.sh -b example.com --debug 0                跑完清理中间文件
#   pipeline.sh -b example.com --tcp                   启用 TCP 端口扫描
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ===== 默认 =====
TARGET_FILE=target.txt
EXCLUDE_FILE=exclude.txt
WILDCARD_OUT=wildcard.txt
SCAN_DIR=scan_results
DB=../db/recon.sqlite3
DB_DIR="$(dirname "$DB")"

BUSINESS=
INPUT_PATH=
DRY_RUN_IMPORT=
ENABLE_TCP=
SKIP_WILDCARD=
SKIP_SCAN=
SKIP_SCANNER=
SKIP_IMPORT=
KEEP_ON_FAIL=
KEEP_OUTPUT=1   # 默认保留 -- 防止 import 阶段异常时数据被销毁。--debug 0 关闭。

# 标记:是否由本脚本生成 target.txt / exclude.txt;且 import 实际跑过才删除
INPUT_GENERATED=0
IMPORT_RAN=0

# ===== 参数 =====
while [ $# -gt 0 ]; do
    case "$1" in
        -b|--business)         BUSINESS="$2"; shift 2 ;;
        -i|--input)            INPUT_PATH="$2"; shift 2 ;;
        -t|--target)           TARGET_FILE="$2"; shift 2 ;;
        -d|--db)               DB="$2"; shift 2 ;;
        -o|--scan-dir)         SCAN_DIR="$2"; shift 2 ;;
        --skip-check-wildcard) SKIP_WILDCARD=1; shift ;;
        --skip-scan)           SKIP_SCAN=1; shift ;;
        --skip-scanner)        SKIP_SCANNER=1; shift ;;
        --skip-import)         SKIP_IMPORT=1; shift ;;
        --dry-run-import)      DRY_RUN_IMPORT=1; shift ;;
        --tcp)                  ENABLE_TCP=1; shift ;;
        --keep-on-fail)        KEEP_ON_FAIL=1; shift ;;
        --debug)
            # 默认就是 --debug (KEEP_OUTPUT=1)。--debug 0 显式关闭,回到清理旧行为。
            case "${2:-}" in
                0) KEEP_OUTPUT=0; shift 2 ;;
                1) shift 2 ;;   # 显式 1 → 默认就是, 但还是要消费掉这个 token
                *) ;;             # --debug 后没东西 (末尾) 或跟的是另一个 flag: 不消费, 下一轮循环处理
            esac
            ;;
        -h|--help)
            sed -n '2,37p' "$0"; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
done

[ -n "$BUSINESS" ] || { echo "需要 -b/--business 指定业务名" >&2; exit 1; }

# ===== 防御: scan_results 残留检测 =====
# 防止上一次 run(尤其 --debug 1 / 手动跑)留的 JSON/TXT 被本次 import 当新结果吃掉,
# 把别业务的 subdomain 错挂到本业务名下。检测到非空目录直接拒绝跑,要求人工清理。
if [ -d "$SCAN_DIR" ] && [ -n "$(ls -A "$SCAN_DIR" 2>/dev/null)" ]; then
    echo "[-] 检测到 $SCAN_DIR/ 残留文件:" >&2
    ls -la "$SCAN_DIR/" >&2
    echo "" >&2
    echo "    这通常是上一次 pipeline 用 --debug 1 或手动跑留下的;" >&2
    echo "    如果直接跑本次,import 会把里面所有 host(包括别业务的)挂到 $BUSINESS 名下。" >&2
    echo "    请先手动清理:" >&2
    echo "      rm -rf $SCAN_DIR && mkdir -p $SCAN_DIR" >&2
    exit 2
fi
mkdir -p "$DB_DIR"

# ===== 准备 target.txt / exclude.txt =====
# 优先级: -i 指定 > 从 DB 读 > 报错
# -i 始终是目录,必读 target.txt + exclude.txt(缺 exclude 则空)
prepare_input() {
    if [ -n "$INPUT_PATH" ]; then
        [ -d "$INPUT_PATH" ] || { echo "-i 不是目录: $INPUT_PATH (必须是含 target.txt + exclude.txt 的目录)" >&2; exit 1; }
        [ -f "$INPUT_PATH/target.txt" ] || { echo "目录 $INPUT_PATH 缺少 target.txt" >&2; exit 1; }
        # 当 -i 是 ./ 或脚本所在目录时,$INPUT_PATH/target.txt 与 $TARGET_FILE 指向同一 inode,
        # 直接 cp 会报 "are the same file"。用 -ef 判定后跳过复制,让 import 阶段原样读取。
        if [ "$INPUT_PATH/target.txt" -ef "$TARGET_FILE" ]; then
            :
        else
            cp "$INPUT_PATH/target.txt" "$TARGET_FILE"
        fi
        if [ -f "$INPUT_PATH/exclude.txt" ]; then
            if [ "$INPUT_PATH/exclude.txt" -ef "$EXCLUDE_FILE" ]; then
                :
            else
                cp "$INPUT_PATH/exclude.txt" "$EXCLUDE_FILE"
            fi
        else
            : > "$EXCLUDE_FILE"
        fi
        INPUT_GENERATED=1
    else
        command -v python3 >/dev/null || { echo "未提供 -i 需要 python3 读 DB (其 sqlite3 stdlib)" >&2; exit 1; }
        [ -f "$DB" ] || { echo "未提供 -i 且 DB 不存在: $DB" >&2; exit 1; }

        # 用 python3 + sqlite3 stdlib 从 DB 读 scope,避免依赖 sqlite3 CLI 二进制;
        # 同一脚本的 bootstrap_db/finalize_scope 都已用该模式。
        TMP_T=$(mktemp); TMP_E=$(mktemp)
        BIZ_ID=$(python3 - "$DB" "$BUSINESS" "$TMP_T" "$TMP_E" <<'PY'
import sqlite3, sys
db, biz, ft, fe = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT id FROM businesses WHERE TRIM(business_name)=? LIMIT 1", (biz,)
    ).fetchone()
    if row is None:
        sys.exit(0)  # 业务不存在:以空 BIZ_ID 回到原「无业务」错误分支
    bid = int(row[0])
    with open(ft, "w", encoding="utf-8") as f:
        for (a,) in conn.execute(
            "SELECT asset FROM scopes WHERE business_id=? AND scope_name='可测资产' ORDER BY id",
            (bid,),
        ):
            f.write(a.rstrip("\n") + "\n")
    with open(fe, "w", encoding="utf-8") as f:
        for (a,) in conn.execute(
            "SELECT asset FROM scopes WHERE business_id=? AND scope_name='非可测资产' ORDER BY id",
            (bid,),
        ):
            f.write(a.rstrip("\n") + "\n")
    print(bid)
except Exception as e:
    sys.stderr.write(f"[db-read error] {e}\n")
    sys.exit(2)
PY
)
        if [ -z "$BIZ_ID" ]; then
            rm -f "$TMP_T" "$TMP_E"
            echo "DB 中无业务 '$BUSINESS' (businesses.business_name),请先插入或用 -i 提供输入" >&2
            exit 1
        fi

        if [ ! -s "$TMP_T" ]; then
            rm -f "$TMP_T" "$TMP_E"
            echo "DB 中业务 '$BUSINESS' 没有可测资产 (scopes.scope_name='可测资产')" >&2
            exit 1
        fi

        cp "$TMP_T" "$TARGET_FILE"
        if [ -s "$TMP_E" ]; then
            cp "$TMP_E" "$EXCLUDE_FILE"
        else
            : > "$EXCLUDE_FILE"
        fi
        rm -f "$TMP_T" "$TMP_E"
        INPUT_GENERATED=1
    fi

    [ -s "$TARGET_FILE" ] || { echo "生成的 target.txt 为空" >&2; exit 1; }
    [ -f "$EXCLUDE_FILE" ] || : > "$EXCLUDE_FILE"
}

cleanup_input() {
    # 默认保留全部中间产物(target/exclude/wildcard/dnsx_output/alive/alterx/scan_results),
    # 防止 import binding error / 网络中断等异常下数据被销毁,便于人工或重跑 import。
    # --debug 0 关闭保留 → 跑完强制清理 (旧的默认行为)。
    # --keep-on-fail 仅在失败时保留 (默认已覆盖,留作兼容)。
    local rc=$?
    if [ "$KEEP_OUTPUT" = "1" ]; then
        return 0
    fi
    if [ $rc -ne 0 ] && [ -n "$KEEP_ON_FAIL" ]; then
        echo "[!] 流水线失败 (exit=$rc),保留中间文件供排查"
        return 0
    fi

    # import 跳过时仍清理中间产物(用户已明确不要入库)
    if [ $rc -ne 0 ] && [ -n "$SKIP_IMPORT" ]; then
        :  # 跳过 import 是刻意行为,失败时也清理
    fi

    local f
    for f in \
        "$TARGET_FILE" "$EXCLUDE_FILE" \
        "$WILDCARD_OUT" no_wildcard.txt \
        alive.txt alterx.txt dnsx_output.txt \
        filter.regex dnsx_output.txt.tmp \
        wildcard.txt.bak no_wildcard.txt.bak dnsx_output.txt.bak
    do
        [ -e "$f" ] && rm -f "$f"
    done

    if [ -d "$SCAN_DIR" ]; then
        rm -rf "$SCAN_DIR"
        echo "[+] 清理: $SCAN_DIR/"
    fi

    if [ "$INPUT_GENERATED" = 1 ]; then
        echo "[+] 清理临时输入: $TARGET_FILE, $EXCLUDE_FILE"
    fi
}
trap cleanup_input EXIT

# ===== DB bootstrap:确保 businesses 表 + 所有 schema 都存在并插入业务行 =====
# scan.sh 阶段的 permutation_cache.py 会读 permutation_state 表,
# 而 import_scan_results.create_tables() 才会建它,故提前 bootstrap。
bootstrap_db() {
    # businesses 是与其它项目共用的表,只 INSERT 不 CREATE 不 ALTER;
    # 缺失时报错让用户/上游项目先建,避免本项目偷偷定义共享 schema。
    # 仅负责建本项目私有的其他表(scopes/web_hashes/web_subdomains/tcp_assets/permutation_state)。
    python3 - "$DB" "$BUSINESS" <<'PY' | tee /tmp/.bootstrap_db.log
import sqlite3, sys
sys.path.insert(0, ".")
import import_scan_results
db, biz = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(db)
conn.execute("PRAGMA foreign_keys = ON")
# 校验 businesses 表已存在(由共享方/上游项目创建)
cols = [r[1] for r in conn.execute("PRAGMA table_info(businesses)").fetchall()]
if "business_name" not in cols:
    sys.exit(f"[bootstrap_db] 错误: {db} 缺少 businesses 表或缺 business_name 列,请先在共享库中建好")
# 建本项目私有 schema(scopes/web_hashes/.../permutation_state)
import_scan_results.create_tables(conn)
# 仅插入业务行,不触动 businesses 表其它列
conn.execute("INSERT OR IGNORE INTO businesses (business_name) VALUES (?)", (biz,))
conn.commit()
row = conn.execute("SELECT id FROM businesses WHERE business_name=?", (biz,)).fetchone()
print(f"[bootstrap_db] business='{biz}' id={row[0]} (schema OK)")
print(row[0])
conn.close()
PY
    rc=${PIPESTATUS[0]}
    BUSINESS_ID=$(tail -n1 /tmp/.bootstrap_db.log)
    rm -f /tmp/.bootstrap_db.log
    [ $rc -eq 0 ] || exit $rc
    export BUSINESS_ID
}

# ===== 步骤 =====
check_wildcard() { ./check_wildcard.sh "$TARGET_FILE"; }
scan() {
    # scan.sh 依赖 DB/BUSINESS_ID/RESOLVERS/WORDLIST/HASH 等 env
    export DB BUSINESS_ID RESOLVERS WORDLIST WORDLIST_HASH
    ./scan.sh
}
scanner()        { [ -n "$ENABLE_TCP" ] && ./scanner.sh -all || ./scanner.sh; }
import() {
    python3 import_scan_results.py \
        --business      "$BUSINESS" \
        --db            "$DB" \
        --scan-dir      "$SCAN_DIR" \
        --target-file   "$TARGET_FILE" \
        --exclude-file  "$EXCLUDE_FILE" \
        --wildcard-file "$WILDCARD_OUT" \
        ${DRY_RUN_IMPORT:+--dry-run}
}

# 收尾:显式再 upsert 一遍 scope,保证删除输入文件前 scope 已落库
# (import 阶段已做一遍,这里是 final commit,幂等;只动 scopes 表,不动 web/tcp)
finalize_scope() {
    python3 - "$DB" "$BUSINESS" "$TARGET_FILE" "$EXCLUDE_FILE" "$WILDCARD_OUT" <<'PY'
import sys, sqlite3
from datetime import datetime, timezone
from pathlib import Path
db, biz, tf, ef, wf = sys.argv[1:6]

def clean(line):
    line = line.strip().lstrip("﻿")
    if not line or line.startswith("#"): return None
    return line.split()[0].strip().lower().rstrip(".")

def read(p):
    if not Path(p).exists(): return []
    out, seen = [], set()
    for ln in Path(p).read_text(encoding="utf-8", errors="replace").splitlines():
        v = clean(ln)
        if v and v not in seen: out.append(v); seen.add(v)
    return out

targets  = read(tf)
excludes = read(ef)
wildcards = read(wf) if Path(wf).exists() else []
wset = set(wildcards)

now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
conn = sqlite3.connect(db)
conn.execute("PRAGMA foreign_keys = ON")
row = conn.execute("SELECT id FROM businesses WHERE business_name=?", (biz,)).fetchone()
if not row:
    conn.execute("INSERT INTO businesses (business_name) VALUES (?)", (biz,))
    row = conn.execute("SELECT id FROM businesses WHERE business_name=?", (biz,)).fetchone()
bid = int(row[0])

def upsert(scope, asset, w):
    conn.execute(
        "INSERT INTO scopes (business_id, scope_name, asset, is_wildcard, created_at, updated_at, fetched_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(business_id, scope_name, asset) DO UPDATE SET "
        "  updated_at=excluded.updated_at, fetched_at=excluded.fetched_at, is_wildcard=excluded.is_wildcard",
        (bid, scope, asset, 1 if asset in wset else 0, now, now, now),
    )

for a in targets:  upsert("可测资产", a, wset)
for a in excludes: upsert("非可测资产", a, wset)
conn.commit()
conn.close()
print(f"[finalize_scope] {biz}: targets={len(targets)} excludes={len(excludes)} wildcards={len(wildcards)} (committed)")
PY
}

# ===== 执行 =====
prepare_input
echo "[*] target: $(wc -l < "$TARGET_FILE") 条 | exclude: $(wc -l < "$EXCLUDE_FILE") 条"
bootstrap_db

echo "[1/4] check_wildcard"; [ -z "$SKIP_WILDCARD" ] && check_wildcard || echo "  跳过"
echo "[2/4] scan";           [ -z "$SKIP_SCAN" ]      && scan           || echo "  跳过"
echo "[3/4] scanner";        [ -z "$SKIP_SCANNER" ]   && scanner        || echo "  跳过"
if [ -z "$SKIP_IMPORT" ]; then
    echo "[4/5] import"
    import
    IMPORT_RAN=1
else
    echo "[4/5] import  跳过 (--skip-import,数据未入库,不清理临时输入)"
fi

# 最后一步:再次 upsert scope 提交,确保删输入文件前 scope 已落库
if [ "$IMPORT_RAN" = 1 ] && [ -z "$DRY_RUN_IMPORT" ]; then
    echo "[5/5] finalize_scope"
    finalize_scope
fi

echo "[+] 流水线完成"

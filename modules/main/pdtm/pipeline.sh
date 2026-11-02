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
#   -d, --db string               DB 路径(默认 ${DB:-${RECON_DB:-../db/recon.sqlite3}})
#   -o, --scan-dir string         scanner 输出目录(默认 scan_results)
#
# STAGES:
#       --tcp                     启用 TCP 端口扫描(透传 scanner.sh -all,naabu 被动+主动)
#       --skip-check-wildcard     跳过 check_wildcard.sh
#       --skip-scan               跳过 scan.sh(同时跳过内嵌 brute 阶段)
#       --skip-brute              仅跳过 scan.sh 内嵌 brute 阶段(alterx 不受影响)
#       --force-brute             强制本轮跑 brute,忽略 30 天 cadence(FORCE_BRUTE=1)
#       --brute-mode top|rolling  brute 模式: top=SecLists 2w(默认),rolling=Jhaddix-clean
#       --skip-scanner            跳过 scanner.sh
#       --skip-import             跳过 import_scan_results.py
#       --skip-reverify           跳过 reverify 阶段(默认开)
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

# 2026-10-03: source load_config.sh + resolve_tools.sh —
# httpx/dnsx 路径从 config/pdtm.conf 自动读取(env 覆盖 > conf 注入 > 默认 fallback)。
source "$SCRIPT_DIR/../lib/load_config.sh"
load_config_set "$SCRIPT_DIR/../../../config/pdtm.conf" "$SCRIPT_DIR/../lib"
source "$SCRIPT_DIR/lib/resolve_tools.sh"

# ===== 默认 =====
TARGET_FILE=target.txt
EXCLUDE_FILE=exclude.txt
WILDCARD_OUT=wildcard.txt
SCAN_DIR=scan_results
DB=${DB:-${RECON_DB:-../db/recon.sqlite3}}
DB_DIR="$(dirname "$DB")"

BUSINESS=
INPUT_PATH=
DRY_RUN_IMPORT=
ENABLE_TCP=
SKIP_WILDCARD=
SKIP_SCAN=
SKIP_BRUTE=        # 仅跳过 brute,alterx + subfinder 仍跑
SKIP_SCANNER=
SKIP_IMPORT=
SKIP_REVERIFY=     # 跳过 reverify 阶段(默认跑)
FORCE_BRUTE=       # 强制跑 brute,忽略 30 天 cadence
BRUTE_MODE=        # brute 模式,空=默认 top
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
        --skip-brute)          SKIP_BRUTE=1; shift ;;
        --force-brute)         FORCE_BRUTE=1; shift ;;
        --brute-mode)          BRUTE_MODE="$2"; shift 2 ;;
        --skip-scanner)        SKIP_SCANNER=1; shift ;;
        --skip-import)         SKIP_IMPORT=1; shift ;;
        --skip-reverify)       SKIP_REVERIFY=1; shift ;;
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

# ===== 防御: pdtm CWD 跨 run 运行时文件残留检测 =====
# 防上一次 run(尤其 KEEP_OUTPUT=1 / ctrl-c / 中途失败)在 pdtm CWD
# 留下 scan.sh 中间产物,本次 scanner.sh 把它们当输入读到错误业务的
# 子域。
#
# 不放 filter.regex / targets.regex / excludes.regex / *.bak ---
# 这些是 scan.sh 阶段每跑必建的内部 state,KEEP_OUTPUT=1 模式下正常
# 保留;放进本检测会"自拦"。
POLLUTED=()
for f in dnsx_output.txt dnsx_output.txt.try alive.txt alterx.txt \
         brute.txt resume.cfg no_wildcard.txt wildcard.txt \
         dnsx_output.txt.bak; do
    [ -e "$f" ] && POLLUTED+=("$f")
done
if [ "${#POLLUTED[@]}" -gt 0 ]; then
    echo "[-] 检测到 pdtm CWD 跨 run 运行时文件残留(下个 run 会被 scanner 当输入):" >&2
    for f in "${POLLUTED[@]}"; do
        echo "    $f" >&2
    done
    echo "" >&2
    echo "    这通常是上一次 pipeline KEEP_OUTPUT=1 / ctrl-c / 中途失败留下的," >&2
    echo "    或上一次跑的不是本业务的内容。如果直接跑,scanner 会把别业务的" >&2
    echo "    子域当成本业务的扫描输入。处理方式:" >&2
    echo "      rm -f ${POLLUTED[*]}" >&2
    exit 2
fi

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
        wildcard.txt.bak no_wildcard.txt.bak dnsx_output.txt.bak \
        dnsx_output.txt.try brute.txt resume.cfg
    do
        [ -e "$f" ] && rm -f "$f"
    done

    if [ -d "$SCAN_DIR" ]; then
        rm -rf "$SCAN_DIR"
        echo "[+] 清理: $SCAN_DIR/"
    fi

    if [ "$INPUT_GENERATED" = 1 ]; then
        echo "[+] 清理临时输入: $TARGET_FILE, $EXCLUDE_FILE"
        # v0.1.0: also rm INPUT_PATH/target.txt + exclude.txt when INPUT_PATH
        # is an external dir. Mirrors scope_import.sh behaviour. Failed
        # runs already rm these by virtue of unconditional trap.
        if [ -n "$INPUT_PATH" ] && [ -d "$INPUT_PATH" ]; then
            if [ "$INPUT_PATH/target.txt" -ef "$TARGET_FILE" ]; then
                : # same inode — already removed above
            else
                [ -f "$INPUT_PATH/target.txt" ] &&                     rm -f "$INPUT_PATH/target.txt" &&                     echo "[+] 清理外部目录的 target.txt: $INPUT_PATH/target.txt"
                [ -f "$INPUT_PATH/exclude.txt" ] &&                     rm -f "$INPUT_PATH/exclude.txt" &&                     echo "[+] 清理外部目录的 exclude.txt: $INPUT_PATH/exclude.txt"
                rmdir "$INPUT_PATH" 2>/dev/null &&                     echo "[+] 清理空目录: $INPUT_PATH"
            fi
        fi
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
    # brute 阶段控制
    [ -n "$SKIP_BRUTE" ]   && export SKIP_BRUTE=1
    [ -n "$FORCE_BRUTE" ]  && export FORCE_BRUTE=1
    [ -n "$BRUTE_MODE" ]   && export BRUTE_MODE
    ./scan.sh
}
scanner()        { [ -n "$ENABLE_TCP" ] && ./scanner.sh -all ${SCANNER_PORTS:+-p "$SCANNER_PORTS"} || ./scanner.sh ${SCANNER_PORTS:+-p "$SCANNER_PORTS"}; }
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

# -----------------------------------------------------------------------
# reverify (2026-10-03 移位重构)
#
# 原位置: scanner.sh 阶段 3.6 (行 380-489),在 scanner 内部跑。
#   缺陷: scanner 阶段仍在写新数据,DB 里 is_active=0 同时含"昨天失活"
#   和"今天扫描后真正失活",reverify 读到的是混合状态,语义错。
#
# 新位置: import 完成之后、finalize_scope 之前。
#   此时 DB 里 is_active=0 = 今天扫描后真正失活的资产(import 的
#   "全清场 + 重新激活" 已经处理了大部分漏激活),reverify 只兜剩下的
#   少数漏激活。语义清晰,与 cron 日报对齐。
#
# 流程:
#   1) 从 DB 读本业务失活候选 (is_active=0 AND last_seen>=7d AND ips!='[]')
#      用 web_subdomains.ips 字段(2026-10-03 schema 演进,从 httpx JSONL 写)
#   2) 写 targets.lst (subdomain:port 每行),反向索引 target→sid
#   3) 一次 httpx -l 批喂(对齐 stage 3.5 CDN+LB 参数,-rate-limit 50 -timeout 5)
#   4) JSONL 流式解析 → 命中(200-299 + cl>1000 + 非 WAF + not failed) → UPDATE is_active=1
#   5) 失败 try/except 隔离,不致命;落 reverify_hits.tsv 给人工审计
#
# 失败模式: DB 无候选 / httpx 不存在 / httpx 探测全失败 → 静默退出 0
# -----------------------------------------------------------------------
reverify() {
    python3 - "$DB" "$BUSINESS" "$SCAN_DIR" <<'PYREVERIFY'
import json, os, sqlite3, subprocess, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

db_path, biz, scan_dir = sys.argv[1], sys.argv[2], sys.argv[3]

# 2026-10-03: httpx 路径(env > default)。
HTTPX_BIN = os.environ.get("HTTPX_BIN") or str(Path.home() / ".pdtm/go/bin/httpx")
if not Path(HTTPX_BIN).exists():
    print(f"[reverify] 跳过: httpx 不存在 ({HTTPX_BIN})", file=sys.stderr)
    sys.exit(0)

# 读候选: 失活 (is_active=0)。
# 按 spec (用户 2026-10-04), 失活唯一充要条件 = is_active=0, 跟 last_seen 无关。
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
try:
    rows = conn.execute("""
        SELECT ws.id, ws.subdomain, ws.port, ws.ips,
               wh.business_id, b.business_name
          FROM web_subdomains ws
          JOIN web_hashes wh ON wh.id = ws.hash_id
          JOIN businesses b  ON b.id = wh.business_id
         WHERE ws.is_active = 0
         ORDER BY ws.id
    """).fetchall()
except sqlite3.OperationalError as exc:
    print(f"[reverify] DB 读失败(可能 schema 未演进): {exc}", file=sys.stderr)
    sys.exit(0)

if not rows:
    print(f"[reverify] biz={biz}: 候选 0 行,跳过")
    sys.exit(0)

print(f"[reverify] biz={biz}: 候选 {len(rows)} 行")

scan_dir_p = Path(scan_dir)
scan_dir_p.mkdir(parents=True, exist_ok=True)
hits_path = scan_dir_p / "reverify_hits.tsv"

WAF_KW = ("waf", "firewall", "web应用防火墙", "blocked", "denied")

def is_hit(j: dict) -> bool:
    sc = j.get("status_code") or 0
    cl = j.get("content_length") or 0
    title_l = (j.get("title") or "").lower()
    is_waf = any(k in title_l for k in WAF_KW)
    return (200 <= sc < 300) and cl > 1000 and not is_waf and not j.get("failed")

def run_httpx(input_path: Path, host_header: str | None) -> list[dict]:
    args = [str(HTTPX_BIN),
            "-sc", "-cl", "-title", "-random-agent",
            "-follow-redirects", "-max-redirects", "3",
            "-rate-limit", "50", "-timeout", "5",
            "-silent", "-no-color", "-json"]
    if host_header:
        args += ["-H", f"Host: {host_header}"]
    args += ["-l", str(input_path)]
    try:
        proc = subprocess.run(args, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        print(f"[reverify][warn] httpx timeout after 600s on {input_path.name}", file=sys.stderr)
        return []
    except Exception as exc:
        print(f"[reverify] httpx 启动失败: {exc}", file=sys.stderr)
        return []
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out

# ---------- pass 1: 复用 cached IPs (绕过 DNS) ----------
# 利用 httpx input 格式 `<custom_host>,<target>` (runner.go:1827-1829):
#   - 一个 input file 装所有 (subdomain, ip, port) 三元组, 1 次 httpx 调起
#   - 每行格式 = "<subdomain>,<ip>:<port>"  (无 http:// 前缀, 见 httpx
#     case `!HasPrefixAny(http://, https://) && ContainsAny(",")`)
#   - httpx 内部把 <subdomain> 当 CustomHost 写到 req.Host, TCP 连 <ip:port>
#   - 不传 -H, 不需要 per-sub 拆分
# 命中判断: httpx JSONL 输出 host 字段 = <ip> (Runner.analyze line 2704
#   `Host: parsed.Hostname()`), 用 (ip, port) lookup 反查回 sid.
pass1_hits: set[int] = set()
ipport_to_sids: dict[tuple[str, int], list[int]] = defaultdict(list)
p1_input_lines: list[str] = []

for r in rows:
    ips_field = r["ips"]
    if not ips_field or ips_field in ("[]", "null"):
        continue
    try:
        ips = json.loads(ips_field)
    except (ValueError, TypeError):
        continue
    if not isinstance(ips, list) or not ips:
        continue
    valid_ips = [ip for ip in ips if isinstance(ip, str) and ip]
    if not valid_ips:
        continue
    for ip in valid_ips:
        # httpx input 格式: <custom_host>,<target>  (target 不带 scheme)
        p1_input_lines.append(f"{r['subdomain']},{ip}:{r['port']}\n")
        ipport_to_sids[(ip, r["port"])].append(r["id"])

p1_workdir = scan_dir_p / "reverify_pass1"
p1_workdir.mkdir(exist_ok=True)
f = p1_workdir / "all_targets.txt"
f.write_text("".join(p1_input_lines), encoding="utf-8")
results = run_httpx(f, host_header=None)
f.unlink(missing_ok=True)
p1_workdir.rmdir()

for j in results:
    if not is_hit(j):
        continue
    ip = j.get("host", "")
    port = j.get("port", 0)
    sids = ipport_to_sids.get((ip, port), [])
    pass1_hits.update(sids)

print(f"[reverify] pass1 (复用 IP, batch) 命中 {len(pass1_hits)} 行")

# ---------- pass 2: 默认 DNS (独立查询) ----------
pass2_candidates = [r for r in rows if r["id"] not in pass1_hits]
pass2_hits: set[int] = set()
if pass2_candidates:
    p2_path = scan_dir_p / "reverify_pass2.txt"
    target_to_sid = {(r["subdomain"], r["port"]): r["id"] for r in pass2_candidates}
    with p2_path.open("w", encoding="utf-8") as f:
        for r in pass2_candidates:
            f.write(f"{r['subdomain']}:{r['port']}\n")
    results = run_httpx(p2_path, host_header=None)
    p2_path.unlink(missing_ok=True)
    for j in results:
        if not is_hit(j):
            continue
        host = j.get("host", "")
        port = j.get("port", 0)
        sid = target_to_sid.get((host, port))
        if sid is not None:
            pass2_hits.add(sid)

print(f"[reverify] pass2 (默认 DNS) 命中 {len(pass2_hits)} 行")

# ---------- 汇总: 复活 + 标记永久失活 ----------
all_hits = pass1_hits | pass2_hits
all_misses = [r["id"] for r in rows if r["id"] not in all_hits]

now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")

with hits_path.open("w", encoding="utf-8") as out:
    out.write("sid\tpass\thit_miss\n")
    for sid in pass1_hits:
        out.write(f"{sid}\t1\thit\n")
    for sid in pass2_hits:
        out.write(f"{sid}\t2\thit\n")
    for sid in all_misses:
        out.write(f"{sid}\t2\tmiss\n")

if all_hits:
    placeholders = ",".join("?" for _ in all_hits)
    # 2026-10-07 refactor: DEPRECATED change_type = 0 写入 (commit 1 后续).
    # 原因: import 阶段不应写 change_type=0,按分工 "diff 写 0/2"。
    # 原 SQL (回滚时把 if False: 改成 if True: 并去掉 -- 注释):
    #   UPDATE web_subdomains SET is_active = 1, change_type = 0, last_seen = ?
    #   WHERE id IN (...) AND is_active = 0
    # 现在的行为: 仍然 UPDATE is_active=1 + last_seen, 但不写 change_type (留给 diff 阶段)。
    if False:  # DEPRECATED 2026-10-07 — see commit message
        conn.execute(
            f"UPDATE web_subdomains SET is_active = 1, change_type = 0, last_seen = ? "
            f"WHERE id IN ({placeholders}) AND is_active = 0",
            [now, *all_hits],
        )
    n_reactivated = conn.execute(
        f"UPDATE web_subdomains SET is_active = 1, last_seen = ? "
        f"WHERE id IN ({placeholders}) AND is_active = 0",
        [now, *all_hits],
    ).rowcount
else:
    n_reactivated = 0

# 2026-10-15 refactor: 删 reverify 直接写 change_type=8.
# 原因: ct=8 唯一来源应该是 diff.py, 条件 = pre.is_active=1 AND current.is_active=0
#        AND reverify pass1+pass2 都 miss. pipeline.sh 只负责 is_active 切换,
#        写 ct 是 diff 阶段.
# n_deactivated 保留 = 0 占位 (print 引用, 实际写 ct 在 diff.py).
n_deactivated = 0

conn.commit()
print(f"[reverify] biz={biz}: 复活 {n_reactivated} / 候选 {len(rows)} 行 "
      f"(pass1_hit={len(pass1_hits)}, pass2_hit={len(pass2_hits)}, miss=ct8 留给 diff 阶段)")

conn.close()


PYREVERIFY
}

# ===== 执行 =====
prepare_input
echo "[*] target: $(wc -l < "$TARGET_FILE") 条 | exclude: $(wc -l < "$EXCLUDE_FILE") 条"
bootstrap_db

echo "[1/4] check_wildcard"; [ -z "$SKIP_WILDCARD" ] && check_wildcard || echo "  跳过"
echo "[2/4] scan";           [ -z "$SKIP_SCAN" ]      && scan           || echo "  跳过"
if [ -z "$SKIP_SCAN" ] && [ -n "$SKIP_BRUTE" ]; then
    echo "    (brute 阶段: --skip-brute,alterx 仍跑)"
fi
echo "[3/4] scanner";        [ -z "$SKIP_SCANNER" ]   && scanner        || echo "  跳过"
if [ -z "$SKIP_IMPORT" ]; then
    echo "[4/5] import"
    import
    IMPORT_RAN=1
else
    echo "[4/5] import  跳过 (--skip-import,数据未入库,不清理临时输入)"
fi

# 复核今天扫描后真正失活的资产,兜 import 的少数漏激活
# 默认开, --skip-reverify 跳过。失败 try/except 隔离,不致命。
if [ -z "$SKIP_REVERIFY" ] && [ "$IMPORT_RAN" = 1 ]; then
    echo "[4.5/5] reverify"
    # 2026-10-03: 解析 httpx 路径(env > conf > 默认),export 给 Python subprocess。
    HTTPX_BIN=$(resolve_tool httpx "$HOME/.pdtm/go/bin/httpx")
    export HTTPX_BIN
    if ! reverify; then
        echo "[!] reverify 失败 (非致命,继续 finalize_scope)"
    fi
else
    echo "[4.5/5] reverify  跳过"
fi

# 最后一步:再次 upsert scope 提交,确保删输入文件前 scope 已落库
if [ "$IMPORT_RAN" = 1 ] && [ -z "$DRY_RUN_IMPORT" ]; then
    echo "[5/5] finalize_scope"
    finalize_scope
fi

echo "[+] 流水线完成"

#!/usr/bin/env bash
# daily_monitor.sh - top-level orchestrator.
#
# Sequence:
#   1. For each non-empty business, run requested stages (errors isolated)
#   2. Run diff.py — reads change_type>0 rows from recon.sqlite3 directly,
#      classifies by bitmask, atomically resets change_type=0 and stamps
#      a new run_marker row (see lib/migrate_change_type.sql).
#
# No JSON snapshots: the diff is now O(changed rows) instead of O(full DB).
# This replaces the old "before/after snapshot + JSON diff" pipeline that
# was OOM'ing at ~374k web_subdomains rows (Aug 16, 2026).
#
# Cron: 0 3 * * *  flock -n .lock daily_monitor.sh -type pdtm[,icp[,enscan]]
#
# Stages (single comma-separated -type; default = pdtm if none given):
#   pdtm   — ../pdtm/pipeline.sh             (子域 / 端口 / 指纹扫描)
#   icp    — ../ymicp/icp_mapp_query.py      (小程序 / 公众号 备案刷新)
#   enscan — ../db_align/bin/db_align        (主体 / 子公司 / 资产 拉新)
#
# Manual:
#   ./daily_monitor.sh                       # -type pdtm (default)
#   ./daily_monitor.sh dryrun                # skip all stages; just run diff
#   ./daily_monitor.sh -type pdtm,icp        # two stages per business
#   ./daily_monitor.sh -type pdtm,icp,enscan # three stages
#   ./daily_monitor.sh -type pdtm,icp,daily-url   # +每日 URL 扫描(用户 2026-08-26)
#
# daily-url 不在 cron 里(用户拍板)。enscan 同理不在 cron 里(仅手动)。

set -u
set -o pipefail

RECON_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DAILY="$RECON_ROOT/main/daily"
DB="$RECON_ROOT/main/db/recon.sqlite3"

RUN_ID="${RUN_ID:-$(date '+%Y%m%d-%H%M%S')}"
LOG_FILE="$DAILY/logs/${RUN_ID}.log"
SNAP_DIR="$DAILY/snapshots"
REPORT_DIR="$DAILY/reports/$RUN_ID"

mkdir -p "$DAILY/logs" "$SNAP_DIR" "$DAILY/reports"

# Export so the Python libs can pick up the same log/run-id
export RUN_ID LOG_FILE RECON_ROOT

ts() { date '+%Y-%m-%d %H:%M:%S'; }
say() { echo "[$(ts)] [daily_monitor] $*" | tee -a "$LOG_FILE" >&2; }

# Parse args. -type takes a SINGLE comma-separated value; multiple -type
# args are rejected (typo guard). TYPES is kept as a space-joined STRING
# (not a bash array) so the env export survives to the child shell —
# bash arrays are not properly inherited by subshells, which previously
# caused `-type pdtm,icp` to silently run zero stages.
DRYRUN=0
VALID_TYPES=(pdtm icp enscan daily-url)
TYPE_RAW=""
TYPE_SEEN=0
while [ $# -gt 0 ]; do
    arg="$1"
    case "$arg" in
        dryrun) DRYRUN=1 ;;
        -type)
            if [ "$TYPE_SEEN" -eq 1 ]; then
                echo "[-type] only takes one value; use comma-separated form like -type pdtm,icp" >&2
                exit 1
            fi
            TYPE_SEEN=1
            shift
            [ $# -eq 0 ] && { echo "[-type] requires a value (one of: ${VALID_TYPES[*]})" >&2; exit 1; }
            TYPE_RAW="$1"
            ;;
        *) echo "unknown arg: $arg" >&2; exit 1 ;;
    esac
    shift
done

if [ -n "$TYPE_RAW" ]; then
    TYPES=""
    IFS=',' read -ra _parts <<< "$TYPE_RAW"
    for p in "${_parts[@]}"; do
        p="${p#"${p%%[![:space:]]*}"}"
        p="${p%"${p##*[![:space:]]}"}"
        [ -z "$p" ] && { echo "[-type] empty token in '$TYPE_RAW'" >&2; exit 1; }
        case " ${VALID_TYPES[*]} " in
            *" $p "*) : ;;
            *) echo "unknown stage '$p' in -type $TYPE_RAW (valid: ${VALID_TYPES[*]})" >&2; exit 1 ;;
        esac
        case " $TYPES " in
            *" $p "*) ;;
            *) TYPES="${TYPES:+$TYPES }$p" ;;
        esac
    done
else
    TYPES="pdtm"
fi
export TYPES

if [ "$DRYRUN" -eq 1 ]; then
    say "DRYRUN: skipping stages ($TYPES); just diff"
else
    say "stages: $TYPES"
fi

say "run_id=$RUN_ID db=$DB report=$REPORT_DIR"

# Capture run-start timestamp BEFORE pipeline runs. The trigger for
# "true reactivation" classification compares OLD.last_seen against this;
# if we let the diff stamp it at end-of-run, the window would be too
# narrow (everything that was scanned today has last_seen < now).
RUN_START_AT="$(date '+%Y-%m-%dT%H:%M:%S%:z')"
export RUN_START_AT

# ---- 0-pre. final_url schema 迁移 (2026-10-28) ----
# 给 web_subdomains / web_subdomains_pre 加 final_url 列: httpx 跟随重定向后
# 的最终落点。T4 的 STEP 1 快字段比较用它替代不稳定的 url 列 (url 的形态
# 逐日会在 域名 <-> IP 之间翻转, 补进去会把形态抖动误判成变更)。
#
# 必须跑在 step 0 快照之前: 快照的 INSERT/SELECT 显式引用 final_url 列,
# 列不存在时 sqlite3 报 "no such column", 整个快照失败。
# 幂等: 缺列才 ALTER TABLE。
if [ -f "$(dirname "$DB")/migrate_final_url.py" ]; then
    if python3 "$(dirname "$DB")/migrate_final_url.py" --db "$DB" --apply \
            2>>"$LOG_FILE"; then
        say "step 0-pre: final_url schema migration ok (idempotent)"
    else
        say "step 0-pre: final_url migration apply failed (rc=$?), continuing"
    fi
fi

# ---- 0-pre. technologies 形态回填 (2026-10-29) ----
# 把历史存量里 Python list 字面量形态的 technologies ("['X']") 回填成规范
# 形式 ("X"): 导入侧已直接写规范形式, 但存量未回填时新旧行形态不一致,
# STEP 1 的 technologies 快字段比较会误判成变更 (body 其实逐字节相同)。
#
# 必须跑在 step 0 快照之前: 快照把主表整表复制到快照表, 回填要在复制
# 之前完成, 否则快照表里仍是旧形态。
# 幂等: 已是规范形态的行不会被再次改动。
if [ -f "$(dirname "$DB")/migrate_tech_normalize.py" ]; then
    if python3 "$(dirname "$DB")/migrate_tech_normalize.py" --db "$DB" --apply \
            2>>"$LOG_FILE"; then
        say "step 0-pre: technologies normalize ok (idempotent)"
    else
        say "step 0-pre: technologies normalize failed (rc=$?), continuing"
    fi
fi

# ---- 0. web_subdomains_pre 快照 (2026-10-08) ----
# 触发时机: daily_monitor 开始, step 1 (per-business) 之前。
# 把当前 web_subdomains 整表 truncate + INSERT 到 web_subdomains_pre。
# _pre 只保留"最近一次 snapshot",不堆历史。
# 失败 || true 兜底, snapshot 不阻塞主流程。
# sqlite3 CLI 在 daily_monitor 已有依赖 (system init / 检查路径), 直接 sqlite3 调用。
if command -v sqlite3 >/dev/null 2>&1; then
    SNAP_START="$(date '+%s.%N')"
    if sqlite3 "$DB" <<SQL 2>>"$LOG_FILE"
CREATE TABLE IF NOT EXISTS web_subdomains_pre (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL,
    hash_id INTEGER NOT NULL,
    subdomain TEXT NOT NULL,
    port INTEGER NOT NULL,
    url TEXT,
    status_code INTEGER,
    content_length INTEGER,
    title TEXT,
    technologies TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    raw_json TEXT,
    change_type INTEGER NOT NULL DEFAULT 0,
    ips TEXT DEFAULT '[]',
    prev_body          BLOB,
    curr_body          BLOB,
    content_type       TEXT,
    final_url          TEXT,
    last_comparison_ratio REAL,
    last_comparison_kind  TEXT,
    last_comparison_at    TEXT,
    _snapshot_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (business_id, subdomain, port)
);
DELETE FROM web_subdomains_pre;
INSERT INTO web_subdomains_pre
    (id, business_id, hash_id, subdomain, port, url, status_code, content_length,
     title, technologies, first_seen, last_seen, fetched_at, is_active, raw_json,
     change_type, ips, prev_body, curr_body, content_type, final_url,
     last_comparison_ratio, last_comparison_kind, last_comparison_at)
SELECT id, business_id, hash_id, subdomain, port, url, status_code, content_length,
     title, technologies, first_seen, last_seen, fetched_at, is_active, raw_json,
     change_type, ips, prev_body, curr_body, content_type, final_url,
     last_comparison_ratio, last_comparison_kind, last_comparison_at
FROM web_subdomains;
SQL
    then
        SNAP_ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM web_subdomains_pre")
        SNAP_END="$(date '+%s.%N')"
        SNAP_ELAPSED=$(python3 -c "print(f'{${SNAP_END}-${SNAP_START}:.2f}')" 2>/dev/null || echo "n/a")
        say "step 0/5: web_subdomains_pre snapshot ok (rows=$SNAP_ROWS, elapsed=${SNAP_ELAPSED}s)"
    else
        say "step 0/5: web_subdomains_pre snapshot failed (rc=$?), continuing"
    fi
else
    say "step 0/5: sqlite3 CLI missing, snapshot skipped"
fi

PER_BIZ_WARN_FILE="$(mktemp)"
trap 'rm -f "$PER_BIZ_WARN_FILE"' EXIT

if [ "$DRYRUN" -eq 0 ]; then
    say "step 1/4: run stages [$TYPES] per business"
    # FIX-2026-07-29: switch from `done < <(python3 ...)` (which silently
    # drops lines on multi-business dbs) to mapfile + bash array iteration.
    mapfile -t BIZ_LINES < <(python3 - "$DB" <<'PY'
import sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(db)
for bid, name in conn.execute(
    "SELECT id, business_name FROM businesses WHERE TRIM(business_name) != '' ORDER BY id"
):
    print(f"{bid}\t{name}")
PY
)
    for line in "${BIZ_LINES[@]}"; do
        biz_id="${line%%$'\t'*}"
        biz_name="${line#*$'\t'}"
        # Skip blank/whitespace business_name rows (the table allows empty
        # strings — those are operator mistakes, not real businesses).
        if [ -z "$biz_name" ]; then
            say "  skip biz_id=$biz_id (empty business_name)"
            continue
        fi
        # Per-business config gate (recon_business_config). Drives both the
        # master enable and per-stage eligibility:
        #   web/tcp → pdtm stage (pipeline.sh runs subdomain + port scans)
        #   icp     → icp stage  (ymicp refreshes 小程序/公众号 备案)
        #   enscan  → ungated    (db_align is a data-refresh, not a scan)
        # Missing config row = opt-out (all-zero), so a newly added business
        # stays silent until an operator explicitly enables it.
        CONFIG_LINE="$(python3 - "$DB" "$biz_name" <<'PY' 2>/dev/null
import sqlite3, sys
db, biz = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(db)
row = conn.execute(
    "SELECT r.enabled, r.web, r.tcp, r.icp FROM recon_business_config r "
    "JOIN businesses b ON b.id = r.business_id "
    "WHERE TRIM(b.business_name) = ?", (biz,)
).fetchone()
if row is None:
    print("0\t0\t0\t0")
else:
    print("\t".join(str(int(x)) for x in row))
PY
)"
        IFS=$'\t' read -r CFG_ENABLED CFG_WEB CFG_TCP CFG_ICP <<< "$CONFIG_LINE"
        if [ "${CFG_ENABLED:-0}" = "0" ]; then
            say "  skip biz='$biz_name' (config: enabled=0)"
            continue
        fi
        # Filter requested stages by per-business config.
        FILTERED=""
        for st in $TYPES; do
            case "$st" in
                pdtm)
                    if [ "${CFG_WEB:-0}" = "1" ] || [ "${CFG_TCP:-0}" = "1" ]; then
                        FILTERED="${FILTERED:+$FILTERED }$st"
                    fi
                    ;;
                icp)
                    if [ "${CFG_ICP:-0}" = "1" ]; then
                        FILTERED="${FILTERED:+$FILTERED }$st"
                    fi
                    ;;
                enscan)
                    FILTERED="${FILTERED:+$FILTERED }$st"
                    ;;
            esac
        done
        if [ -z "$FILTERED" ]; then
            say "  skip biz='$biz_name' (config: no stages enabled for requested types)"
            continue
        fi
        say "  >> biz='$biz_name' (id=$biz_id) config=en${CFG_ENABLED},w${CFG_WEB},t${CFG_TCP},i${CFG_ICP} stages=$FILTERED"
        rc=0
        # Override TYPES in the child's env so run_one_business.sh sees the
        # filtered list, not the parent's unfiltered $TYPES (its precedence
        # is env > CLI). CLI -type still passed as a fallback for direct
        # invocations where env isn't set.
        #
        # ENABLE_TCP carries the per-business recon_business_config.tcp
        # flag through to run_one_business.sh → pipeline.sh --tcp. Without
        # this, the per-business tcp=1 setting is read but never honored
        # (the pdtm stage ran web scans only, silently skipping naabu).
        TYPE_ARG="-type $(echo $FILTERED | tr ' ' ',')"
        TYPES="$FILTERED" ENABLE_TCP="$CFG_TCP" WARNINGS_FILE="$PER_BIZ_WARN_FILE" \
            "$DAILY/run_one_business.sh" $TYPE_ARG "$biz_name" 2>>"$LOG_FILE" || rc=$?
        # Stage exit codes are bit-mask: bit0=pdtm, bit1=enscan(db_align), bit2=icp(ymicp), bit3=daily-url.
        # Decode and emit one warning line per failed stage.
        FAILED_STAGES=""
        [ $((rc & 1)) -ne 0 ] && FAILED_STAGES="${FAILED_STAGES:+$FAILED_STAGES,}pdtm"
        [ $((rc & 2)) -ne 0 ] && FAILED_STAGES="${FAILED_STAGES:+$FAILED_STAGES,}enscan"
        [ $((rc & 4)) -ne 0 ] && FAILED_STAGES="${FAILED_STAGES:+$FAILED_STAGES,}icp"
        [ $((rc & 8)) -ne 0 ] && FAILED_STAGES="${FAILED_STAGES:+$FAILED_STAGES,}daily-url"
        if [ "$rc" -eq 0 ]; then
            say "  << biz='$biz_name' ok"
        elif [ -z "$FAILED_STAGES" ]; then
            say "  << biz='$biz_name' unexpected rc=$rc"
            echo "$biz_name	unexpected_rc=$rc" >> "$PER_BIZ_WARN_FILE"
        else
            say "  << biz='$biz_name' FAILED: $FAILED_STAGES (rc=$rc)"
            echo "$biz_name	${FAILED_STAGES}_failed" >> "$PER_BIZ_WARN_FILE"
        fi
    done
fi

# ---- 1.5. schema migration (idempotent, 2026-10-01) ----
# 触发时机: 各业务 scan + 入库完成 (含 _apply_reverify_hits),
# 但 diff.py 还没读 ct 之前。schema 必须先 apply,否则 scan_count
# 列不存在的 diff.py 守卫会 SQL ERROR。
# Bug fix (2026-10-02): 之前用 $DB_DIR 但脚本里没定义,导致 set -u 下
# 直接 exit。今天 03:00 cron 跑就因此死在 line 228, diff/notify 全没跑。
# 改用 dirname "$DB" 取脚本所在目录 (与 DB 文件同级)。

# ---- 1.3b. 3 包 schema 迁移 (2026-10-25) ----
# 必须跑在 step 1.4 (algo_diff_cpp) 之前: algo_diff_cpp 的 T4 会读
# web_subdomains.curr_body_1 / curr_body_2 两列 (三包样本)。列不存在时它
# 虽然会降级到 2 包路径, 但那样 3 包方案就完全不生效。
# 幂等: 缺列才 ALTER TABLE。
if [ -f "$(dirname "$DB")/migrate_3pkg_body.py" ]; then
    if python3 "$(dirname "$DB")/migrate_3pkg_body.py" --apply --db "$DB" \
            2>>"$LOG_FILE"; then
        say "step 1.3b: 3pkg schema migration ok (idempotent)"
    else
        say "step 1.3b: 3pkg migration apply failed (rc=$?), continuing"
    fi
fi

# ---- 1.4. algo_diff_cpp (T1/T2/T3/T4 change_type decision) ----
# 触发时机: step 1 (per-business scanner + import) 之后, schema migration 之前.
# 关键: 必须跑在 import_scan_results.py reset change_type=0 之后,
# 让 algo_diff_cpp 处理刚 import 的新 row (ct=0 → ct=10 → T4 → 终态 0/2/4/6/8).
# 早期版本这里叫 step 0.5 (在 snapshot 后, scanner 前), 但那时 cur 跟 pre.curr_body
# 都是 round 1 body, T4 算 ratio=1.0 误判 similar → 0, 错过 ct2 真实变化.
# algo_diff_cpp 内部循环所有 business, 1 次调用 = 全表.
# Binary 缺失则跳过 (跟 snapshot 同样的兜底).
# 失败 || true 兜底, 不阻塞主流程.
ALGO_DIFF_CPP_BIN="$RECON_ROOT/public/algo_diff_cpp/bin/algo_diff_cpp"
if [ -x "$ALGO_DIFF_CPP_BIN" ]; then
    # 默认值必须先解析成普通变量: 前缀赋值 (VAR=x cmd) 只对 fork 出的子进程可见,
    # shell 自己在同一条命令里展开 "${HTTPX_BIN:-}" 时看不到它, 会传空字符串
    # (shellcheck SC2097/SC2098)。三包发包靠 --httpx-path 拿路径, 传空就静默失效。
    # 默认留空: 让 C++ 从 pdtm.conf 读 httpx_bin (单一事实源)。
    # 原默认值 "/usr/local/bin/httpx" 在本机不存在, 而本命令又显式传了
    # --httpx-path —— C++ 收到非空路径就不再走 pdtm.conf 兜底, 三包取样
    # 会去起一个不存在的二进制而静默失败。
    # PATCH-3PKG-HTTPX-2026-10-27
    # httpx 路径从 config/pdtm.conf 解析 (单一事实源), 解析链与 scanner.sh 一致:
    #   env HTTPX_BIN > conf httpx_bin > 默认 ~/.pdtm/go/bin/httpx
    # 用 lib 里现成的 load_config.sh (解析 conf) + resolve_tools.sh (优先级),
    # 不自己手搓 conf 解析。
    # 解析不出可执行文件时留空: C++ 侧会继续走自己的 pdtm.conf 兜底,
    # 两边都拿不到才降级 2 包 (并打日志), 不会静默。
    HTTPX_BIN_EFF="${HTTPX_BIN:-}"
    PDTMCONF_EFF="${PDTMCONF:-$RECON_ROOT/../config/pdtm.conf}"
    RESOLVERS_EFF="${RESOLVERS_FILE:-}"
    if [ -f "$RECON_ROOT/main/lib/load_config.sh" ] &&
       [ -f "$RECON_ROOT/main/pdtm/lib/resolve_tools.sh" ]; then
        # shellcheck disable=SC1090
        source "$RECON_ROOT/main/lib/load_config.sh"
        # shellcheck disable=SC1090
        source "$RECON_ROOT/main/pdtm/lib/resolve_tools.sh"
        # 把 conf 里的 httpx_bin / resolvers_file 注入本脚本 scope
        load_config_set "$PDTMCONF_EFF" "$RECON_ROOT/main/lib"
        if [ -z "$HTTPX_BIN_EFF" ]; then
            HTTPX_BIN_EFF="$(resolve_tool httpx "$HOME/.pdtm/go/bin/httpx")"
        fi
        if [ -z "$RESOLVERS_EFF" ]; then
            RESOLVERS_EFF="${resolvers_file:-}"
        fi
    fi
    if [ -n "$HTTPX_BIN_EFF" ] && [ ! -x "$HTTPX_BIN_EFF" ]; then
        say "step 1.4: WARN httpx 不可执行, 三包取样将降级 (path=$HTTPX_BIN_EFF)"
        HTTPX_BIN_EFF=""
    fi
    # 三包方案: 默认开启 (2 = 额外取样 2 次, 连同原扫描样本共 3 包)。
    # 用"位置对应覆盖率"判定动态噪声, 不依赖 anchors —— anchors 的锚点数
    # 会随 body 内容偶然变化而波动, 同一资产可能在 ct=0 / ct=2 之间翻转。
    # 设 FETCH_SAMPLES=0 可临时关闭 (走 2 包路径)。
    FETCH_SAMPLES_EFF="${FETCH_SAMPLES:-2}"
    if (cd "$(dirname "$ALGO_DIFF_CPP_BIN")" && \
         PDTMCONF="$PDTMCONF_EFF" \
         HTTPX_BIN="$HTTPX_BIN_EFF" \
         "./algo_diff_cpp" "$DB" --run-id "$RUN_ID" \
         --httpx-path "$HTTPX_BIN_EFF" \
         --resolvers-path "$RESOLVERS_EFF" \
         --fetch-samples "$FETCH_SAMPLES_EFF" 2>>"$LOG_FILE"); then
        say "step 1.4: algo_diff_cpp ok"
    else
        say "step 1.4: algo_diff_cpp failed (rc=$?), continuing"
    fi
else
    say "step 1.4: algo_diff_cpp binary missing, skip"
fi

if [ -f "$(dirname "$DB")/migrate_business_scan_count.py" ]; then
    if python3 "$(dirname "$DB")/migrate_business_scan_count.py" --apply --db "$DB" \
            2>>"$LOG_FILE"; then
        say "step 1.5: schema migration ok (idempotent)"
    else
        say "step 1.5: migration apply failed (rc=$?), continuing"
    fi
fi

# ---- 2. diff (SQL-direct, atomic) ----
# DEPRECATED 2026-10-08: 临时关闭 monitor 中的 diff / algo_diff 调度。
# 调度逻辑(compile 关掉, 调用脚本仍存在, 可被外部 CLI / 测试直接调用):
#   diff.py          modules/main/daily/lib/diff.py
#   algo_diff/run.py modules/main/daily/lib/algo_diff/run.py
#   algo_diff/rescan.py modules/main/daily/lib/algo_diff/rescan.py
# 改回原状: 把下面三个 if ! ... fi && 之间的注释去掉即可。
# step 2 (diff.py):
# 2026-10-12: 解除 10-08 临时 DEPRECATED, 恢复自动调度.
# 冷启动守护 (scan_count > 3 才写 ct) 继续保留, 不会假报.
mkdir -p "$REPORT_DIR"
PER_BIZ_WARNINGS=""
if [ -s "$PER_BIZ_WARN_FILE" ]; then
    PER_BIZ_WARNINGS="$PER_BIZ_WARN_FILE"
fi

if ! WARNINGS_FILE="$PER_BIZ_WARNINGS" RUN_START_AT="$RUN_START_AT" python3 "$DAILY/lib/diff.py" "$DB" "$REPORT_DIR" "$RUN_ID" 2>>"$LOG_FILE"; then
    DIFF_RC=$?
    say "diff failed (rc=$DIFF_RC); change_type NOT reset, will retry next run"
    say "done. rc=$DIFF_RC report=$REPORT_DIR"
    exit "$DIFF_RC"
fi

# ---- 3. algo_diff (dynamic-response change detection) ----
# Main pass: re-judges change_type using 8-step structural similarity algorithm.
# Reads body from web_subdomains.raw_json (written by import_scan_results.py
# during [4/5] import step). Async second-pass (rescan) is a separate stage
# scheduled after this main run completes.
# DEPRECATED 2026-10-08: 临时关闭 algo_diff main pass 调度。
# say "step 3/4: algo_diff main pass"
# if ! python3 "$DAILY/lib/algo_diff/run.py" "$DB" 2>>"$LOG_FILE"; then
#     say "algo_diff main pass failed (rc=$?); non-fatal, continuing"
# fi

# ---- 4. algo_diff async second pass ----
# DEPRECATED 2026-10-08: 临时关闭 algo_diff rescan 调度。
# say "step 4/4: algo_diff async rescan"
# if ! python3 "$DAILY/lib/algo_diff/rescan.py" "$DB" --batch 200 2>>"$LOG_FILE"; then
#     say "algo_diff rescan failed (rc=$?); non-fatal, pending rows persist for next run"
# fi

# ---- 5. optional notify 推送(只总变更 > 0 时;失败 || true 兜底) ----
# modules/public/notify 是可选插件;模块不存在 / install.sh 没跑 / config
# 没填真值 → 全部静默跳过。notify_send.sh 内部已经处理 REPLACE_WITH_* 拒发。
# 注意:hook 插在 algo_diff rescan 之后,因为 diff.py:write_report 是 aggregate 报告,
# 跟 algo_diff 各自处理不同表,顺序不影响语义,但 notify 应该在所有数据写入完成后跑。
NOTIFY_SEND="$RECON_ROOT/public/notify/notify_send.sh"
SUMMARY_MD="$REPORT_DIR/summary.md"
if [ -x "$NOTIFY_SEND" ] && [ -f "$SUMMARY_MD" ]; then
    DIFF_TOTAL="$(grep -oE '本次变更总数: \*\*[0-9]+\*\*' "$SUMMARY_MD" 2>/dev/null         | grep -oE '[0-9]+' | head -1)"
    DIFF_TOTAL="${DIFF_TOTAL:-0}"
    if [ "${DIFF_TOTAL:-0}" -gt 0 ]; then
        # 用户 2026-09-28:如果只有 deactivated ↘ 行(没有 added + ↻ + ✎),
        # 视为噪声(全是 persist 全清场导致),不发邮件。
        # summary.md 段头格式: `## <table>  (+N / ↻N / ↘N / ✎N)`
        # 2026-10-02: 原 grep '+[0-9]+' 等太宽松,会把 summary 里其它位置
        # (response_hash / run_id / 时间戳里的数字) 都算入,ADDED 错算。
        # 改用 Python 锚定段头格式严格求和。
        read -r ADDED_TOTAL REACT_TOTAL DEACT_TOTAL CHANGED_TOTAL < <(
            python3 - "$SUMMARY_MD" <<'PYEOF'
import re, sys
totals = [0, 0, 0, 0]  # +N / ↻N / ↘N / ✎N
with open(sys.argv[1], encoding="utf-8") as f:
    for line in f:
        m = re.match(r"^## .*?\s+\(\+(\d+) / ↻(\d+) / ↘(\d+) / ✎(\d+)\)\s*$", line)
        if m:
            for i, v in enumerate(m.groups()):
                totals[i] += int(v)
print(*totals)
PYEOF
        )
        REAL_TOTAL=$(( ADDED_TOTAL + REACT_TOTAL + CHANGED_TOTAL ))
        if [ "$DEACT_TOTAL" -gt 0 ] && [ "$REAL_TOTAL" -eq 0 ]; then
            say "step 5/5: notify skip (only deactivated, deact=$DEACT_TOTAL)"
        else
            say "step 5/5: notify push (total=$DIFF_TOTAL, added=$ADDED_TOTAL re=$REACT_TOTAL chg=$CHANGED_TOTAL deact=$DEACT_TOTAL)"
            DATE_PH="$(date -d "${RUN_START_AT}" '+%Y-%m-%d' 2>/dev/null || echo "$RUN_ID")"
            bash "$NOTIFY_SEND" "$SUMMARY_MD" "(all enabled businesses)" "$RUN_ID" "$DATE_PH" \
                2>>"$LOG_FILE" || say "notify push failed (rc=$?), continuing"
        fi
    else
        say "step 5/5: notify skip (no changes)"
    fi
fi

say "done. rc=0 report=$REPORT_DIR"
exit 0

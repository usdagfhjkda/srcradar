#!/usr/bin/env bash
# run_one_business.sh - run selected stages for a single business.
#
# Usage: run_one_business.sh -type <stages> <business_name>
#
# Stages is a single comma-separated value matching the IDs used by
# daily_monitor.sh and install_cron.sh:
#   pdtm     — ../pdtm/pipeline.sh
#   icp      — ../ymicp/icp_mapp_query.py
#   enscan   — ../db_align/bin/db_align
#   daily-url— ../pdtm/scan_urls.py (按 web_subdomain_scan_schedule 跑)
#
# Default if no -type given: pdtm.
#
# Order matters and is fixed: enscan first (writes scopes), then pdtm
# (consumes scopes), then icp (only needs companies), then daily-url
# (only reads web_subdomain_scan_schedule — 与其它阶段无依赖,但放最末
# 因为依赖 run_markers(由 diff 阶段写入)的复活判定语义)。
# Caller's -type ordering is ignored — stages always run in this internal order.
#
# Onboarding guard: if the business exists in `businesses` but has zero
# '可测资产' scopes, pdtm is skipped and a "needs_onboarding" line is
# appended to WARNINGS_FILE (set by daily_monitor.sh). enscan / icp
# still run, since neither needs scopes to function.
#
# Exit code is a 4-bit mask (用户 2026-08-26 加 daily-url):
#   bit 0 (1) — pdtm failed
#   bit 1 (2) — enscan (db_align) failed
#   bit 2 (4) — icp (ymicp) failed
#   bit 3 (8) — daily-url failed
#   0 — all requested stages ok / skipped (no error)
#   15 — all four failed
# needs_onboarding exits 0 (no pdtm run = no pdtm error bit).
# daily-url 没有 schedule 行的业务 = "空 schedule,无 sub" → 视为 skipped,不设 bit3。
# The orchestrator (daily_monitor.sh) decodes the bits per-stage.
#
# TYPES env var (space-joined string) is honored when set by the
# orchestrator; CLI -type wins if TYPES is unset. TYPES is a string
# (not a bash array) so the env export survives — bash arrays do not
# inherit properly across subshells.

set -u

# Parse single -type with comma-separated value; reject multiple -type.
# -onesite 是互斥的轻量子命令：跳过 enscan/pdtm/icp 三阶段，直接 subprocess 跑
# httpx 写 web_hashes + web_subdomains，不清空 is_active=1 其它行。
VALID_TYPES_STR="pdtm icp enscan daily-url"
TYPE_RAW=""
TYPE_SEEN=0
ONESITE=0
ONESITE_HOSTS=""
while [ $# -gt 0 ]; do
    case "$1" in
        -type)
            if [ "$TYPE_SEEN" -eq 1 ]; then
                echo "[-type] only takes one value; use comma-separated form like -type pdtm,icp" >&2
                exit 1
            fi
            TYPE_SEEN=1
            shift
            [ $# -eq 0 ] && { echo "[-type] requires a value (one of: $VALID_TYPES_STR)" >&2; exit 1; }
            TYPE_RAW="$1"
            ;;
        -onesite)
            ONESITE=1
            shift
            [ $# -eq 0 ] && { echo "[-onesite] requires <hosts_file> argument (one domain per line, # for comments)" >&2; exit 1; }
            ONESITE_HOSTS="$1"
            ;;
        *) break ;;
    esac
    shift
done

# -onesite 与 -type 互斥
if [ "$ONESITE" = "1" ] && [ -n "$TYPE_RAW" ]; then
    echo "[-onesite] cannot be combined with -type" >&2
    exit 1
fi

# Resolve TYPES with precedence: env > CLI > default.
if [ -n "${TYPES:-}" ]; then
    RUN_TYPES="$TYPES"
elif [ -n "$TYPE_RAW" ]; then
    _build=""
    IFS=',' read -ra _parts <<< "$TYPE_RAW"
    for p in "${_parts[@]}"; do
        p="${p#"${p%%[![:space:]]*}"}"
        p="${p%"${p##*[![:space:]]}"}"
        [ -z "$p" ] && { echo "[-type] empty token in '$TYPE_RAW'" >&2; exit 1; }
        case " $VALID_TYPES_STR " in
            *" $p "*) : ;;
            *) echo "unknown stage '$p' in -type $TYPE_RAW (valid: $VALID_TYPES_STR)" >&2; exit 1 ;;
        esac
        case " $_build " in
            *" $p "*) ;;
            *) _build="${_build:+$_build }$p" ;;
        esac
    done
    RUN_TYPES="$_build"
else
    RUN_TYPES="pdtm"
fi
export RUN_TYPES

if [ $# -lt 1 ]; then
    echo "usage: $0 [-type pdtm[,icp[,enscan[,daily-url]]] | -onesite <hosts_file>] <business_name>" >&2
    exit 1
fi

BIZ="$1"
RECON_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAILY="$RECON_ROOT/daily"
PDTM="$RECON_ROOT/pdtm"
DBALIGN="$RECON_ROOT/db_align"
YMICP="$RECON_ROOT/ymicp"
DB="$RECON_ROOT/db/recon.sqlite3"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] [run_one] $*" >&2; }

# ---- -onesite 早返回：跳过 enscan/pdtm/icp 整段，直接 subprocess 调 scan-onesite ----
# 不动 bit-mask 退出码逻辑；ONESITE 路径独立以 ONESITE_RC 退出（0 = ok，非 0 = 错）。
# 业务名与原脚本一致：第 1 个 positional arg。
if [ "$ONESITE" = "1" ]; then
    log "onesite begin: biz=$BIZ hosts_file=$ONESITE_HOSTS"
    (cd "$PDTM" && python3 import_scan_results.py scan-onesite \
        --business "$BIZ" --hosts-file "$ONESITE_HOSTS" --db "$DB") 2>&1
    ONESITE_RC=$?
    log "onesite exit: rc=$ONESITE_RC for biz=$BIZ"
    exit "$ONESITE_RC"
fi

run_stage_pdtm() {
    log "pdtm begin: biz=$BIZ tcp=$ENABLE_TCP"
    # --tcp enables naabu passive+active port scanning inside scanner.sh
    # (default mode = web discovery only, skips TCP). Driven by
    # recon_business_config.tcp via ENABLE_TCP (env from orchestrator or
    # GUARD_TCP fallback).
    local -a pdtm_args=(-b "$BIZ" --debug 0)
    [ "${ENABLE_TCP:-0}" = "1" ] && pdtm_args+=(--tcp)
    (cd "$PDTM" && ./pipeline.sh "${pdtm_args[@]}" 2>&1)
}
run_stage_enscan() {
    log "enscan (db_align) begin: biz=$BIZ"
    "$DBALIGN/bin/db_align" -n "$BIZ" -all -scope -delay 2 \
        -db "$DB" \
        -enscan "$RECON_ROOT/ENScan_GO/ENScan" 2>&1
}
run_stage_icp() {
    log "icp (ymicp) begin: biz=$BIZ"
    # ymicp business mode: -b reads companies from DB and iterates them.
    # --sleep 30s default avoids ymicp server rate-limit (qps:0.5 +
    # jitter:1500ms on the server side). Override on CLI only when
    # debugging; cron keeps the default.
    python3 "$YMICP/icp_mapp_query.py" -b "$BIZ" --db "$DB" 2>&1 | \
        tee -a "$DAILY/logs/icp_${BIZ}_$(date +%Y%m%d).log"
}

# ---- daily-url stage (用户 2026-08-26 拍板) ----
# 读 web_subdomain_scan_schedule 表,按 enabled=1 的 subdomain 跑 scan_urls.py
# - 每 batch ≤ DAILY_URL_BATCH_MAX 个子域(用户拍板:5万,文档常量)
# - 失败保留旧 last_run_at;成功才 UPDATE(下次以"上次成功"为基线)
# - 无 schedule 行 = "空 schedule" → 日志一行 + 返回 0,不算失败
# - 跟 scan-onesite 不同:scan_urls.py 接多个 subdomain + 自动 batch(每批 50
#   hosts 上限;此处 DAILY_URL_BATCH_MAX 应远小于此,避免单 subprocess 超时)
DAILY_URL_BATCH_MAX=50000  # 用户拍板(2026-08-26):见 README"daily-url batch 上限"
run_stage_daily_url() {
    log "daily-url begin: biz=$BIZ batch_max=$DAILY_URL_BATCH_MAX"
    # 1) 读 schedule(enabled=1)
    mapfile -t SCHED_LINES < <(python3 - "$DB" "$BIZ" <<'PY' 2>/dev/null
import sqlite3, sys
db, biz = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(db)
for sub, sources in conn.execute("""
    SELECT subdomain, sources
      FROM web_subdomain_scan_schedule
     WHERE business_id = (SELECT id FROM businesses WHERE TRIM(business_name)=?)
       AND enabled = 1
     ORDER BY subdomain
""", (biz,)):
    print(f"{sub}\t{sources}")
PY
)
    if [ "${#SCHED_LINES[@]}" -eq 0 ]; then
        log "daily-url: no schedule rows for biz='$BIZ' (no sub opted in)"
        return 0
    fi

    # 2) 分 batch
    n_subs=${#SCHED_LINES[@]}
    n_batches=$(( (n_subs + DAILY_URL_BATCH_MAX - 1) / DAILY_URL_BATCH_MAX ))
    log "daily-url: $n_subs sub(s), $n_batches batch(es)"

    # 3) 写 last_run_at 标记函数(success 才调)
    _mark_run_at() {
        # 一次性把整个 batch 的 sub 写上 success 时间(写在 orchestrator
        # 最后,避免单 sub fail 时前面 success 的 sub 不被记录)
        :
    }

    # 4) 按 batch 跑
    local batch_idx=0
    local batch_failed=0
    while [ $batch_idx -lt $n_subs ]; do
        local end=$(( batch_idx + DAILY_URL_BATCH_MAX ))
        [ $end -gt $n_subs ] && end=$n_subs

        # 抽 batch 内的 subdomain + sources
        local batch_hosts=""
        local batch_sources=""
        local i=$batch_idx
        while [ $i -lt $end ]; do
            local line="${SCHED_LINES[$i]}"
            local sub="${line%%$'\t'*}"
            local src="${line#*$'\t'}"
            if [ -z "$batch_hosts" ]; then
                batch_hosts="$sub"
                batch_sources="$src"
            else
                batch_hosts="$batch_hosts"$'\n'"$sub"
                # sources 在 batch 内不一致 → 暂时以第一个为准(各 sub 独立
                # source 集合是后期扩展;第一版简化)
                :
            fi
            i=$((i + 1))
        done

        # 写 tmpfile 给 scan_urls.py
        local tmp_hosts
        tmp_hosts="$(mktemp)"
        printf '%s\n' "$batch_hosts" > "$tmp_hosts"

        log "daily-url batch: $((batch_idx+1))-$end / $n_subs, sources=$batch_sources"

        # 跑 scan_urls.py
        (cd "$PDTM" && python3 scan_urls.py scan-urls \
            --business "$BIZ" \
            --hosts-file "$tmp_hosts" \
            --db "$DB" \
            --sources "$batch_sources") 2>&1
        local batch_rc=$?
        rm -f "$tmp_hosts"

        if [ "$batch_rc" -ne 0 ]; then
            log "daily-url batch $((batch_idx+1))-$end FAILED (rc=$batch_rc)"
            batch_failed=$((batch_failed + 1))
        else
            # 成功后:为这个 batch 涉及的所有 sub 写 last_run_at
            local i=$batch_idx
            while [ $i -lt $end ]; do
                local line="${SCHED_LINES[$i]}"
                local sub="${line%%$'\t'*}"
                python3 - "$DB" "$BIZ" "$sub" <<'PY' 2>/dev/null
import sqlite3, sys
from datetime import datetime
db, biz, sub = sys.argv[1], sys.argv[2], sys.argv[3]
conn = sqlite3.connect(db)
now = datetime.now().astimezone().isoformat(timespec="seconds")
conn.execute("""
    UPDATE web_subdomain_scan_schedule
       SET last_run_at = ?, updated_at = ?
     WHERE business_id = (SELECT id FROM businesses WHERE TRIM(business_name)=?)
       AND subdomain = ?
""", (now, now, biz, sub))
conn.commit()
PY
                i=$((i + 1))
            done
        fi

        batch_idx=$end
    done

    if [ "$batch_failed" -gt 0 ]; then
        log "daily-url: $batch_failed batch(es) failed"
        return 1
    fi
    log "daily-url ok: $n_subs sub(s) scanned"
    return 0
}

# ---- onboarding guard (pdtm-only) ----
# If the business exists in `businesses` but has zero '可测资产' scopes,
# pdtm is skipped with a "needs_onboarding" warning. enscan and icp
# don't depend on scopes, so they still run.
#
# Also fetch recon_business_config.tcp so we can pass --tcp to pipeline.sh
# when the operator opted this business into TCP port scanning. The
# orchestrator (daily_monitor.sh) may pre-set ENABLE_TCP; the DB lookup
# here is the fallback for direct invocations.
if [ -f "$DB" ]; then
    eval "$(python3 - "$DB" "$BIZ" <<'PY' 2>/dev/null
import sqlite3, sys
db, biz = sys.argv[1], sys.argv[2]
try:
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT id FROM businesses WHERE TRIM(business_name)=? LIMIT 1", (biz,)
    ).fetchone()
    if row is None:
        print(f"GUARD_BIZ_ID=")
    else:
        bid = int(row[0])
        n = conn.execute(
            "SELECT COUNT(*) FROM scopes WHERE business_id=? AND scope_name='可测资产'",
            (bid,),
        ).fetchone()[0]
        n_companies = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE business_id=?",
            (bid,),
        ).fetchone()[0]
        cfg = conn.execute(
            "SELECT tcp FROM recon_business_config WHERE business_id=?",
            (bid,),
        ).fetchone()
        tcp_flag = int(cfg[0]) if cfg and cfg[0] is not None else 0
        print(f"GUARD_BIZ_ID={bid}")
        print(f"GUARD_SCOPE_COUNT={n}")
        print(f"GUARD_COMPANY_COUNT={n_companies}")
        print(f"GUARD_TCP={tcp_flag}")
except Exception:
    print("GUARD_BIZ_ID=")
PY
)"
fi

# ENABLE_TCP precedence: orchestrator env > DB lookup > off. The
# orchestrator reads CFG_TCP once per business and exports it; this
# wrapper does NOT re-query the DB when env is set.
ENABLE_TCP="${ENABLE_TCP:-${GUARD_TCP:-0}}"

# ---- run requested stages in fixed order (enscan → pdtm → icp) ----
# Order: enscan BEFORE pdtm (pdtm needs scopes that enscan populates).
# icp runs last — it only reads companies, no dependency on others.
# Helper: check if a stage token is in RUN_TYPES (space-joined string).
has_stage() {
    case " $RUN_TYPES " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

# Per-stage rc/skipped tracking using parallel vars (avoiding assoc arrays
# for portability with /bin/sh fallback).
PDTM_RC=0; ENS_RC=0; ICP_RC=0; URL_RC=0
PDTM_SKIP=0; ENS_SKIP=0; ICP_SKIP=0; URL_SKIP=0

if has_stage enscan; then
    if [ -n "${GUARD_BIZ_ID:-}" ] && [ "${GUARD_SCOPE_COUNT:-0}" = "0" ]; then
        : # (no skip for enscan — it can run regardless of scope count)
    fi
    if run_stage_enscan; then
        log "enscan ok for biz=$BIZ"
    else
        rc=$?
        log "enscan FAILED (rc=$rc) for biz=$BIZ"
        ENS_RC=$rc
    fi
else
    ENS_SKIP=1
fi

if has_stage pdtm; then
    if [ -n "${GUARD_BIZ_ID:-}" ] && [ "${GUARD_SCOPE_COUNT:-0}" = "0" ]; then
        log "biz='$BIZ' exists (id=$GUARD_BIZ_ID) but has 0 可测资产 scopes → needs_onboarding; skipping pdtm"
        if [ -n "${WARNINGS_FILE:-}" ]; then
            printf '%s\tneeds_onboarding (no 可测资产 scope; run pipeline.sh -b %s -i <input_dir>)\n' \
                "$BIZ" "$BIZ" >> "$WARNINGS_FILE"
        fi
        PDTM_SKIP=1
    elif run_stage_pdtm; then
        log "pdtm ok for biz=$BIZ"
    else
        rc=$?
        log "pdtm FAILED (rc=$rc) for biz=$BIZ"
        PDTM_RC=$rc
    fi
else
    PDTM_SKIP=1
fi

if has_stage icp; then
    # icp guard: business exists in DB but no companies registered →
    # icp_mapp_query.py would print "暂无子公司" and exit 1. Treat as
    # needs_onboarding (skip, don't fail) — same convention as the pdtm
    # scope guard above. pdtm still runs; icp genuinely has nothing to do
    # without companies to iterate.
    if [ -n "${GUARD_BIZ_ID:-}" ] && [ "${GUARD_COMPANY_COUNT:-0}" = "0" ]; then
        log "biz='$BIZ' exists (id=$GUARD_BIZ_ID) but has 0 companies → needs_onboarding_companies; skipping icp"
        if [ -n "${WARNINGS_FILE:-}" ]; then
            printf '%s\tneeds_onboarding_companies (no companies; run db_align -n %s -all)\n' \
                "$BIZ" "$BIZ" >> "$WARNINGS_FILE"
        fi
        ICP_SKIP=1
    elif run_stage_icp; then
        log "icp ok for biz=$BIZ"
    else
        rc=$?
        log "icp FAILED (rc=$rc) for biz=$BIZ"
        ICP_RC=$rc
    fi
else
    ICP_SKIP=1
fi

# daily-url stage:读 web_subdomain_scan_schedule 跑 URL 资产扫描。
# 顺序:放最末(enscan→pdtm→icp→daily-url)。无 schedule 行 = "空 schedule,无 sub"
# → 视为 skipped,不设 bit3。任一 batch 失败 → 设 bit3。
if has_stage daily-url; then
    if run_stage_daily_url; then
        log "daily-url ok for biz=$BIZ"
    else
        rc=$?
        log "daily-url FAILED (rc=$rc) for biz=$BIZ"
        URL_RC=$rc
    fi
else
    URL_SKIP=1
fi

# Bit-mask: bit0=pdtm, bit1=enscan, bit2=icp, bit3=daily-url.
# Skipped stages don't set the bit.
RC=0
[ "$PDTM_RC" -ne 0 ] && RC=$((RC | 1))
[ "$ENS_RC"  -ne 0 ] && RC=$((RC | 2))
[ "$ICP_RC"  -ne 0 ] && RC=$((RC | 4))
[ "${URL_RC:-0}" -ne 0 ] && RC=$((RC | 8))
exit "$RC"
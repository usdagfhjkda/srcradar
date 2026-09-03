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

RECON_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAILY="$RECON_ROOT/daily"
DB="$RECON_ROOT/db/recon.sqlite3"

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

# ---- 1. per-business run ----
PER_BIZ_WARN_FILE="$(mktemp)"
trap 'rm -f "$PER_BIZ_WARN_FILE"' EXIT

if [ "$DRYRUN" -eq 0 ]; then
    say "step 1/2: run stages [$TYPES] per business"
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

# ---- 2. diff (SQL-direct, atomic) ----
say "step 2/2: diff"
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

say "done. rc=0 report=$REPORT_DIR"
exit 0
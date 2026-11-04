#!/usr/bin/env bash
# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/i18n.sh"
# run_one_business.sh - run selected stages for a single business.
#
# Usage: run_one_business.sh -type <stages> <business_name>
#
# Stages is a single comma-separated value matching the IDs used by
# daily_monitor.sh and install_cron.sh:
#   pdtm     — ../pdtm/pipeline.sh
#   icp      — ../ymicp/icp_mapp_query.py
#   enscan   — ../db_align/bin/db_align
#   daily-url— ../pdtm/scan_urls.py (it follows web_subdomain_scan_schedule)
#
# Default if no -type given: pdtm.
#
# Order matters and is fixed: enscan first (writes scopes), then pdtm
# (consumes scopes), then icp (only needs companies), then daily-url
# (only reads web_subdomain_scan_schedule - it does not depend on the other
#  stages, but it goes last
#  because it depends on the reactivation semantics of the run_markers (written
#  by the diff stage)).
# Caller's -type ordering is ignored - stages always run in this internal order.
#
# Onboarding guard: if the business exists in `businesses` but has zero
# 'measurable asset' scopes, pdtm is skipped and a "needs_onboarding" line is
# appended to WARNINGS_FILE (set by daily_monitor.sh). enscan / icp
# still run, since neither needs scopes to function.
#
# Exit code is a 4-bit mask (the user added daily-url on 2026-08-26):
#   bit 0 (1) — pdtm failed
#   bit 1 (2) — enscan (db_align) failed
#   bit 2 (4) — icp (ymicp) failed
#   bit 3 (8) — daily-url failed
#   0 — all the requested stages succeeded / were skipped (no error)
#   15 — all four failed
# needs_onboarding exits 0 (no pdtm run = no pdtm error bit).
# A business with no daily-url schedule row = "empty schedule, no sub" -> it is
# treated as skipped and bit3 is not set.
# The orchestrator (daily_monitor.sh) decodes the bits per-stage.
#
# The TYPES env var (a space-joined string) is honored when the orchestrator
# sets it; the CLI -type wins if TYPES is unset. TYPES is a string
# (not a bash array) so the env export survives - bash arrays do not
# inherit properly across subshells.

set -u

# Parse single -type with comma-separated value; reject multiple -type.
# -onesite is a mutually exclusive light subcommand: it skips the enscan/pdtm/icp
# stages and calls httpx directly through a subprocess to write web_hashes +
# web_subdomains, without clearing the other is_active=1 rows.
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

# -onesite and -type are mutually exclusive
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
RECON_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DAILY="$RECON_ROOT/main/daily"
PDTM="$RECON_ROOT/main/pdtm"
DBALIGN="$RECON_ROOT/public/db_align"
YMICP="$RECON_ROOT/public/ymicp"
# DB: the env var > RECON_DB > the default RECON_ROOT/main/db/recon.sqlite3
DB="${DB:-${RECON_DB:-$RECON_ROOT/main/db/recon.sqlite3}}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] [run_one] $*" >&2; }

# ---- the -onesite early return: skip the whole enscan/pdtm/icp section and call
# scan-onesite directly through a subprocess ----
# the bit-mask exit code logic is left alone; the ONESITE path is independent and
# exits with ONESITE_RC (0 = ok, anything else = an error).
# The business name is the same as in the original script: the 1st positional arg.
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

# ---- the daily-url stage (the user decided on 2026-08-26) ----
# Read the web_subdomain_scan_schedule table and run scan_urls.py for every
# subdomain with enabled=1
# - every batch holds <= DAILY_URL_BATCH_MAX subdomains (the user decided on 50k,
#   the documented constant)
# - a failure keeps the old last_run_at; only a success UPDATEs it (the next run
#   uses "the last success" as the baseline)
# - no schedule row = "empty schedule" -> one log line + return 0, it is not a
#   failure
# - it differs from scan-onesite: scan_urls.py takes several subdomains + it
#   batches automatically (a limit of 50 hosts
#   per batch; DAILY_URL_BATCH_MAX should be far below that here, so that a single
#   subprocess does not time out)
DAILY_URL_BATCH_MAX=50000  # 用户拍板(2026-08-26):见 README"daily-url batch 上限"
run_stage_daily_url() {
    log "daily-url begin: biz=$BIZ batch_max=$DAILY_URL_BATCH_MAX"
    # 1) read the schedule (enabled=1)
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

    # 2) split into batches
    n_subs=${#SCHED_LINES[@]}
    n_batches=$(( (n_subs + DAILY_URL_BATCH_MAX - 1) / DAILY_URL_BATCH_MAX ))
    log "daily-url: $n_subs sub(s), $n_batches batch(es)"

    # 3) the function that writes the last_run_at marker (only called on a success)
    _mark_run_at() {
        # the success time of the whole batch is written in one go (it is written by
        # the orchestrator
        # at the end, so that a sub that fails does not leave the subs that
        # succeeded before it unrecorded)
        :
    }

    # 4) run the batches
    local batch_idx=0
    local batch_failed=0
    while [ $batch_idx -lt $n_subs ]; do
        local end=$(( batch_idx + DAILY_URL_BATCH_MAX ))
        [ $end -gt $n_subs ] && end=$n_subs

        # take the subdomains + the sources out of the batch
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
                # the sources differ inside the batch -> for now the first one wins
                # (a separate source set per sub
                # is a later extension; the first version simplifies it)
                :
            fi
            i=$((i + 1))
        done

        # write the tmpfile for scan_urls.py
        local tmp_hosts
        tmp_hosts="$(mktemp)"
        printf '%s\n' "$batch_hosts" > "$tmp_hosts"

        log "daily-url batch: $((batch_idx+1))-$end / $n_subs, sources=$batch_sources"

        # the engine routing: DAILY_URL_ENGINE = katana (default) | urlfinder | auto
        #   - katana  : goes through modules/public/katana/katana_enrich.sh (the
        #               fields are aligned with httpx, the body is stored)
        #   - urlfinder: goes through the old scan_urls.py behaviour (URLFinder +
        #               ffuf + gau, the one with the Chinese sensitive information)
        #   - auto    : falls back to urlfinder when katana fails (best-effort)
        # the katana module does not exist / the binary is not installed / install.sh
        # never ran -> the batch is skipped (best-effort)
        local engine="${DAILY_URL_ENGINE:-katana}"
        local batch_rc=0
        local KATANA_ENRICH="$RECON_ROOT/public/katana/katana_enrich.sh"
        case "$engine" in
            katana)
                log "daily-url engine=katana (default)"
                if [ -x "$KATANA_ENRICH" ]; then
                    bash "$KATANA_ENRICH" \
                        --db "$DB" \
                        --business "$BIZ" \
                        --hosts-file "$tmp_hosts" 2>&1 \
                        || batch_rc=$?
                else
                    log "daily-url: katana_enrich.sh not found ($KATANA_ENRICH); skipping batch"
                    batch_rc=0
                fi
                rm -f "$tmp_hosts"
                ;;
            urlfinder)
                log "daily-url engine=urlfinder (legacy)"
                (cd "$PDTM" && python3 scan_urls.py scan-urls \
                    --business "$BIZ" \
                    --hosts-file "$tmp_hosts" \
                    --db "$DB" \
                    --sources "$batch_sources") 2>&1 \
                    || batch_rc=$?
                rm -f "$tmp_hosts"
                ;;
            auto)
                if [ -x "$KATANA_ENRICH" ]; then
                    log "daily-url engine=auto -> katana first"
                    bash "$KATANA_ENRICH" \
                        --db "$DB" \
                        --business "$BIZ" \
                        --hosts-file "$tmp_hosts" 2>&1
                    batch_rc=$?
                fi
                if [ "$batch_rc" -ne 0 ] || [ ! -x "$KATANA_ENRICH" ]; then
                    log "daily-url engine=auto -> fallback urlfinder (katana rc=$batch_rc)"
                    (cd "$PDTM" && python3 scan_urls.py scan-urls \
                        --business "$BIZ" \
                        --hosts-file "$tmp_hosts" \
                        --db "$DB" \
                        --sources "$batch_sources") 2>&1 \
                        || batch_rc=$?
                fi
                rm -f "$tmp_hosts"
                ;;
            *)
                log "daily-url: unknown DAILY_URL_ENGINE='$engine'; falling back to katana"
                if [ -x "$KATANA_ENRICH" ]; then
                    bash "$KATANA_ENRICH" \
                        --db "$DB" \
                        --business "$BIZ" \
                        --hosts-file "$tmp_hosts" 2>&1 \
                        || batch_rc=$?
                fi
                rm -f "$tmp_hosts"
                ;;
        esac

        if [ "$batch_rc" -ne 0 ]; then
            log "daily-url batch $((batch_idx+1))-$end FAILED (rc=$batch_rc)"
            batch_failed=$((batch_failed + 1))
        else
            # after the success: write last_run_at for every sub this batch touched
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

    # The URL-grain diff stage that used to run here has been removed
    # together with the module it invoked. It was best-effort by design:
    # a failure logged a warning and the surrounding pipeline carried on.

    return 0
}

# ---- the onboarding guard (pdtm only) ----
# If the business exists in `businesses` but has zero 'measurable asset' scopes,
# pdtm is skipped with a "needs_onboarding" warning. enscan and icp
# do not depend on the scopes, so they still run.
#
# It also fetches recon_business_config.tcp, so that --tcp can be passed to
# pipeline.sh
# when the operator opted this business into the TCP port scanning. The
# orchestrator (daily_monitor.sh) may pre-set ENABLE_TCP; the DB lookup
# here is the fallback for the direct invocations.
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
# shellcheck disable=SC2034
    ENS_SKIP=1
fi

if has_stage pdtm; then
    if [ -n "${GUARD_BIZ_ID:-}" ] && [ "${GUARD_SCOPE_COUNT:-0}" = "0" ]; then
        log "$(t main.run_one_business.needs_onboarding BIZ=$BIZ GUARD_BIZ_ID=$GUARD_BIZ_ID)"
        if [ -n "${WARNINGS_FILE:-}" ]; then
            printf '%s\tneeds_onboarding (no 可测资产 scope; run pipeline.sh -b %s -i <input_dir>)\n' \
                "$BIZ" "$BIZ" >> "$WARNINGS_FILE"
        fi
    # shellcheck disable=SC2034
    PDTM_SKIP=1
    elif run_stage_pdtm; then
        log "pdtm ok for biz=$BIZ"
    else
        rc=$?
        log "pdtm FAILED (rc=$rc) for biz=$BIZ"
        PDTM_RC=$rc
    fi
else
    # shellcheck disable=SC2034
    PDTM_SKIP=1
fi

if has_stage icp; then
    # the icp guard: the business exists in the DB but no companies are registered ->
    # icp_mapp_query.py would print "no subsidiaries yet" and exit 1. It is
    # treated as
    # needs_onboarding (skip, do not fail) — the same convention as the pdtm
    # scope guard above. pdtm still runs; icp genuinely has nothing to do
    # without the companies to iterate.
    if [ -n "${GUARD_BIZ_ID:-}" ] && [ "${GUARD_COMPANY_COUNT:-0}" = "0" ]; then
        log "biz='$BIZ' exists (id=$GUARD_BIZ_ID) but has 0 companies → needs_onboarding_companies; skipping icp"
        if [ -n "${WARNINGS_FILE:-}" ]; then
            printf '%s\tneeds_onboarding_companies (no companies; run db_align -n %s -all)\n' \
                "$BIZ" "$BIZ" >> "$WARNINGS_FILE"
        fi
    # shellcheck disable=SC2034
    ICP_SKIP=1
    elif run_stage_icp; then
        log "icp ok for biz=$BIZ"
    else
        rc=$?
        log "icp FAILED (rc=$rc) for biz=$BIZ"
        ICP_RC=$rc
    fi
else
    # shellcheck disable=SC2034
    ICP_SKIP=1
fi

# the daily-url stage: read the web_subdomain_scan_schedule and run the URL asset
# scan.
# The order: it goes last (enscan->pdtm->icp->daily-url). No schedule row =
# "empty schedule, no sub"
# -> it is treated as skipped and bit3 is not set. A failing batch -> bit3 is set.
if has_stage daily-url; then
    if run_stage_daily_url; then
        log "daily-url ok for biz=$BIZ"
    else
        rc=$?
        log "daily-url FAILED (rc=$rc) for biz=$BIZ"
        URL_RC=$rc
    fi
else
# shellcheck disable=SC2034
    URL_SKIP=1
fi

# Bit-mask: bit0=pdtm, bit1=enscan, bit2=icp, bit3=daily-url.
# Skipped stages don't set the bit.
RC=0
[ "$PDTM_RC" -ne 0 ] && RC=$((RC | 1))
[ "$ENS_RC"  -ne 0 ] && RC=$((RC | 2))
[ "$ICP_RC"  -ne 0 ] && RC=$((RC | 4))
[ "${URL_RC:-0}" -ne 0 ] && RC=$((RC | 8))

# 2026-10-01: scan_count +1 (the cold start guard).
# It is only +1 when every stage succeeded (RC=0). A failure keeps the old value
# and the next cron retry counts it then.
# When it runs: the business pipeline has finished and the BIZ is already in the DB.
# daily_monitor.sh does not
# +1 any more (it was withdrawn back to this single point), so an operator running
# ./srcradar daily run_one_business by hand
# also counts as "one scan" — the same as the design goal.
if [ "$RC" -eq 0 ] && [ -n "${BIZ:-}" ] && [ -n "${DB:-}" ] && [ -f "$DB" ]; then
    python3 - "$DB" "$BIZ" >>"${LOG_FILE:-/dev/null}" 2>&1 <<'PYINCR' || log "scan_count +1 失败 (非致命)"
import sqlite3, sys
db, biz = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
row = con.execute("SELECT id FROM businesses WHERE business_name = ?", (biz,)).fetchone()
if row is None:
    print("[scan_count] biz not found:", biz)
else:
    bid = row[0]
    cur = con.execute("UPDATE businesses SET scan_count = scan_count + 1 WHERE id = ?", (bid,))
    con.commit()
    print("[scan_count] +1 for", biz, "(id={})".format(bid), "new count =", cur.rowcount and (con.execute("SELECT scan_count FROM businesses WHERE id=?", (bid,)).fetchone()[0]))
PYINCR
fi

exit "$RC"

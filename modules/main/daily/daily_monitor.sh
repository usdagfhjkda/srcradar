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
#   pdtm   — ../pdtm/pipeline.sh             (subdomain / port / fingerprint scan)
#   icp    — ../ymicp/icp_mapp_query.py      (mini program / WeChat filing refresh)
#   enscan — ../db_align/bin/db_align        (pull the entity / the subsidiaries / the new assets)
#
# Manual:
#   ./daily_monitor.sh                       # -type pdtm (default)
#   ./daily_monitor.sh dryrun                # skip all stages; just run diff
#   ./daily_monitor.sh -type pdtm,icp        # two stages per business
#   ./daily_monitor.sh -type pdtm,icp,enscan # three stages
#   ./daily_monitor.sh -type pdtm,icp,daily-url   # + the daily URL scan (user 2026-08-26)
#
# daily-url is not in the cron (the user decided so). enscan is not in the cron
# either (manual only).

set -u
set -o pipefail
# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/i18n.sh"

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

# ---- 0-pre. the final_url schema migration (2026-10-28) ----
# Adds the final_url column to web_subdomains / web_subdomains_pre: where
# httpx lands after following the redirects. The STEP 1 fast field comparison of
# T4 uses it instead of the unstable url column (the form of url
# flips day to day between a domain and an IP, and adding it would misread the
# form jitter as a change).
#
# It must run before the step 0 snapshot: the INSERT/SELECT of the snapshot
# name the final_url column explicitly, and without the column sqlite3 reports
# "no such column" and the whole snapshot fails.
# Idempotent: the table is only altered when the column is missing.
if [ -f "$(dirname "$DB")/migrate_final_url.py" ]; then
    if python3 "$(dirname "$DB")/migrate_final_url.py" --db "$DB" --apply \
            2>>"$LOG_FILE"; then
        say "step 0-pre: final_url schema migration ok (idempotent)"
    else
        say "step 0-pre: final_url migration apply failed (rc=$?), continuing"
    fi
fi

# ---- 0-pre. the technologies form backfill (2026-10-29) ----
# Backfills the technologies of the existing rows that still hold the Python
# list literal form ("['X']") into the canonical form ("X"): the import side
# already writes the canonical form, but while the old rows are not backfilled
# the old and the new rows differ in form, and
# the STEP 1 technologies fast field comparison reads that as a change (the body
# is in fact byte-for-byte identical).
#
# It must run before the step 0 snapshot: the snapshot copies the whole main
# table into the snapshot table, so the backfill has to
# finish before the copy, otherwise the snapshot table still holds the old form.
# Idempotent: a row that is already in the canonical form is not touched again.
if [ -f "$(dirname "$DB")/migrate_tech_normalize.py" ]; then
    if python3 "$(dirname "$DB")/migrate_tech_normalize.py" --db "$DB" --apply \
            2>>"$LOG_FILE"; then
        say "step 0-pre: technologies normalize ok (idempotent)"
    else
        say "step 0-pre: technologies normalize failed (rc=$?), continuing"
    fi
fi

# ---- 0. the web_subdomains_pre snapshot (2026-10-08) ----
# When it runs: at the start of daily_monitor, before step 1 (per-business).
# It truncates the current web_subdomains and INSERTs the whole table into
# web_subdomains_pre.
# _pre only keeps "the most recent snapshot", it does not pile up history.
# A failure is absorbed with || true, the snapshot does not block the main flow.
# The sqlite3 CLI is already a dependency of daily_monitor (the system init /
# the check path), so it is called directly.
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
        #   icp     → icp stage  (ymicp refreshes the mini program / WeChat filing)
        #   enscan  → ungated    (db_align is a data-refresh, not a scan)
        # A missing config row means opt-out (all-zero), so a newly added business
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

# ---- 1.5. the schema migration (idempotent, 2026-10-01) ----
# When it runs: after the scan + the import of each business (including
# _apply_reverify_hits),
# but before diff.py reads the ct. The schema has to be applied first, otherwise
# the guard in diff.py raises a SQL ERROR because the scan_count
# column does not exist.
# Bug fix (2026-10-02): it used $DB_DIR, which the script never defined, so
# set -u exited right away. The 03:00 cron run today died on line 228 for that
# reason, and diff/notify never ran.
# It now takes the directory of the script from dirname "$DB" (the same level as
# the DB file).

# ---- 1.3b. the 3-package schema migration (2026-10-25) ----
# It must run before step 1.4 (algo_diff_cpp): the T4 of algo_diff_cpp reads
# the web_subdomains.curr_body_1 / curr_body_2 columns (the 3-package sample).
# Without them it
# does fall back to the 2-package path, but then the 3-package scheme does not
# take effect at all.
# Idempotent: the table is only altered when the column is missing.
if [ -f "$(dirname "$DB")/migrate_3pkg_body.py" ]; then
    if python3 "$(dirname "$DB")/migrate_3pkg_body.py" --apply --db "$DB" \
            2>>"$LOG_FILE"; then
        say "step 1.3b: 3pkg schema migration ok (idempotent)"
    else
        say "step 1.3b: 3pkg migration apply failed (rc=$?), continuing"
    fi
fi

# ---- 1.4. algo_diff_cpp (the T1/T2/T3/T4 change_type decision) ----
# When it runs: after step 1 (the per-business scanner + import), before the
# schema migration.
# Key: it must run after import_scan_results.py resets change_type=0,
# so that algo_diff_cpp handles the rows that were just imported (ct=0 -> ct=10
# -> T4 -> the final 0/2/4/6/8).
# An early version called this step 0.5 (after the snapshot, before the scanner),
# but back then cur and pre.curr_body
# were both the round 1 body, so T4 computed ratio=1.0, misjudged similar -> 0
# and missed the real ct2 changes.
# algo_diff_cpp loops over every business internally, so 1 call = the whole table.
# A missing binary is skipped (the same fallback as the snapshot).
# A failure is absorbed with || true, it does not block the main flow.
ALGO_DIFF_CPP_BIN="$RECON_ROOT/public/algo_diff_cpp/bin/algo_diff_cpp"
if [ -x "$ALGO_DIFF_CPP_BIN" ]; then
    # The default has to be resolved into a plain variable first: a prefix
    # assignment (VAR=x cmd) is only visible to the forked child process, so
    # when the shell itself expands "${HTTPX_BIN:-}" in the same command it
    # sees nothing and passes an empty string
    # (shellcheck SC2097/SC2098). The 3-package sampling gets its path through
    # --httpx-path, so an empty value fails silently.
    # The default stays empty: let the C++ read httpx_bin from pdtm.conf (the
    # single source of truth).
    # The old default "/usr/local/bin/httpx" does not exist on this machine, and
    # this command passes
    # --httpx-path explicitly — so the C++ sees a non-empty path, does not fall
    # back to pdtm.conf any more, and the 3-package sampling
    # tries to start a binary that does not exist and fails silently.
    # PATCH-3PKG-HTTPX-2026-10-27
    # The httpx path is resolved from config/pdtm.conf (the single source of truth),
    # with the same resolution chain as scanner.sh:
    #   env HTTPX_BIN > conf httpx_bin > the default ~/.pdtm/go/bin/httpx
    # It uses the load_config.sh (parses the conf) + resolve_tools.sh (the
    # priority) that already exist in lib, instead of
    # hand-rolling the conf parsing.
    # When no executable can be resolved it stays empty: the C++ side keeps its
    # own pdtm.conf fallback, and only when
    # neither side finds one does it degrade to 2 packages (and it logs that),
    # so it never fails silently.
    HTTPX_BIN_EFF="${HTTPX_BIN:-}"
    PDTMCONF_EFF="${PDTMCONF:-$RECON_ROOT/../config/pdtm.conf}"
    RESOLVERS_EFF="${RESOLVERS_FILE:-}"
    if [ -f "$RECON_ROOT/main/lib/load_config.sh" ] &&
       [ -f "$RECON_ROOT/main/pdtm/lib/resolve_tools.sh" ]; then
        # shellcheck disable=SC1090
        source "$RECON_ROOT/main/lib/load_config.sh"
        # shellcheck disable=SC1090
        source "$RECON_ROOT/main/pdtm/lib/resolve_tools.sh"
        # inject the httpx_bin / resolvers_file from the conf into the scope of this script
        load_config_set "$PDTMCONF_EFF" "$RECON_ROOT/main/lib"
        if [ -z "$HTTPX_BIN_EFF" ]; then
            HTTPX_BIN_EFF="$(resolve_tool httpx "$HOME/.pdtm/go/bin/httpx")"
        fi
        if [ -z "$RESOLVERS_EFF" ]; then
            RESOLVERS_EFF="${resolvers_file:-}"
        fi
    fi
    if [ -n "$HTTPX_BIN_EFF" ] && [ ! -x "$HTTPX_BIN_EFF" ]; then
        say "$(t main.daily_monitor.httpx_not_executable HTTPX_BIN_EFF=$HTTPX_BIN_EFF)"
        HTTPX_BIN_EFF=""
    fi
    # The 3-package scheme: on by default (2 = 2 extra samples, so 3 packages
    # together with the original scan sample).
    # Dynamic noise is judged by the "positional coverage rate", without relying
    # on the anchors — the number of anchors
    # fluctuates with an incidental change in the body, so the same asset could
    # flip between ct=0 and ct=2.
    # Setting FETCH_SAMPLES=0 turns it off for a while (the 2-package path).
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
# DEPRECATED 2026-10-08: the diff / algo_diff scheduling in the monitor is
# turned off for the time being.
# The scheduling logic (the compile part is off, the scripts it calls still exist
# and can be called directly by an external CLI / a test):
#   diff.py          modules/main/daily/lib/diff.py
#   algo_diff/run.py modules/main/daily/lib/algo_diff/run.py
#   algo_diff/rescan.py modules/main/daily/lib/algo_diff/rescan.py
# To restore it: remove the comments between the three if ! ... fi blocks.
# step 2 (diff.py):
# 2026-10-12: the temporary DEPRECATED of 10-08 is lifted and the automatic
# scheduling is back.
# The cold start guard (the ct is only written once scan_count > 3) stays, so
# there are no false reports.
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
# DEPRECATED 2026-10-08: the algo_diff main pass scheduling is turned off for
# the time being.
# say "step 3/4: algo_diff main pass"
# if ! python3 "$DAILY/lib/algo_diff/run.py" "$DB" 2>>"$LOG_FILE"; then
#     say "algo_diff main pass failed (rc=$?); non-fatal, continuing"
# fi

# ---- 4. the algo_diff async second pass ----
# DEPRECATED 2026-10-08: the algo_diff rescan scheduling is turned off for the
# time being.
# say "step 4/4: algo_diff async rescan"
# if ! python3 "$DAILY/lib/algo_diff/rescan.py" "$DB" --batch 200 2>>"$LOG_FILE"; then
#     say "algo_diff rescan failed (rc=$?); non-fatal, pending rows persist for next run"
# fi

# ---- 5. the optional notify push (only when the total change is > 0; a failure
# is absorbed with || true) ----
# modules/public/notify is an optional plugin; when the module does not exist /
# install.sh never ran / the config
# has no real values -> everything is skipped silently. notify_send.sh already
# handles the REPLACE_WITH_* refusal.
# Note: the hook comes after the algo_diff rescan, because diff.py:write_report is
# the aggregate report,
# while algo_diff handles a different table each, so the order does not change
# the semantics, but notify should run once every write is done.
NOTIFY_SEND="$RECON_ROOT/public/notify/notify_send.sh"
SUMMARY_MD="$REPORT_DIR/summary.md"
if [ -x "$NOTIFY_SEND" ] && [ -f "$SUMMARY_MD" ]; then
    # The total is read out of the summary in Python, so that it does not
    # depend on the wording of the line: the summary is written in the
    # configured language, and a grep on the Chinese label would find
    # nothing as soon as the language is not Chinese.
    DIFF_TOTAL="$(python3 - "$SUMMARY_MD" <<'PYEOF'
import re, sys
with open(sys.argv[1], encoding="utf-8") as f:
    for line in f:
        m = re.match(r"^- .*?:\s*\*\*(\d+)\*\*\s*$", line)
        if m:
            print(m.group(1))
            break
PYEOF
)"
    DIFF_TOTAL="${DIFF_TOTAL:-0}"
    if [ "${DIFF_TOTAL:-0}" -gt 0 ]; then
        # User 2026-09-28: when there are only deactivated ↘ rows (no added + ↻ + ✎),
        # it is treated as noise (it all comes from the persist clearing the
        # stage), so no mail is sent.
        # The summary.md section header format: `## <table>  (+N / ↻N / ↘N / ✎N)`
        # 2026-10-02: the old grep '+[0-9]+' was too loose, it also counted the
        # numbers in the other places of the summary
        # (the response_hash / run_id / the timestamps), so ADDED was miscounted.
        # The sum is now taken strictly by anchoring on the section header format
        # in Python.
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

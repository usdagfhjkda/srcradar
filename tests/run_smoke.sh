#!/usr/bin/env bash
# tests/run_smoke.sh -- 11-port x 3-round fixture-driven smoke (local-only).
#
# local only: CI still runs 5 ports + tests/smoke_e2e.py 18 scenarios (see .github/workflows/smoke_e2e.yml).
# CI is not affected by this wrapper. modules/* and config/* are left untouched.
#
# 11 port setup (reuses one fixture server binary, multiple fixture JSONs).
#   ct0 ports (28800/28802/28801) -> spring_boot / aliyun_oss / (none initially)
#   ct2 302_200 ports (28804/28805/28810/28803/28808) -> 302_200/initial & changed
#   ct2 body_drift ports (28805/28806/28809) -> body_drift/initial & changed
#   ct2 body_growth ports (28807/28810) -> body_growth/initial & changed
#
# Round 1: cold-start 28804/28805/28810  (28804 = 302_200 R1, 28805 = body_drift R1, 28810 = body_growth R1)
#   daily_monitor once → 3 rows ct=1
#   SQL deactivate these 3 rows (is_active=0)
#   /stop these 3 servers
#
# Round 2: cold-start 28800/28802/28803/28806/28807/28808/28809 (7 ports)
#   daily_monitor once → 11 rows in db; the 7 new rows are ct=1, the 3 old rows are still ct=1
#   /stop these 7 servers
#
# Round 3 setup (SQL reactivate R1 ports + start/restart/change/dead):
#   - reactivate 28804/28805/28810 is_active=1
#   - RESTART 28804 (same R1 fixture) → expect ct=4 (reactivated)
#   - RESTART 28805 (same R1 fixture) + /change → expect ct=2 (reactivated+body)
#   - leave 28810 alone (dead, no server) → expect ct=8 (deactivated)
#   - START 28801 cold-start (302_200/initial) → expect ct=1
#   - keep 28800/28802/28806/28808 (same fixture, R2) → expect ct=0
#   - /change 28803/28807/28809 → expect ct=2
#   - daily_monitor once
#
# Usage:
#   ./tests/run_smoke.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT" || exit

# port matrix ===========================================================
declare -A R1_FIX   # Round 1 fixture
declare -A R2_FIX   # Round 2 fixture (initial)
declare -A R3_FIX   # Round 3 fixture (cold-start 28801)
declare -A R3_ACTION

R1_FIX[28804]="tests/fixtures/ct2/302_200/initial.json"
R1_FIX[28805]="tests/fixtures/ct2/body_drift/initial.json"

R2_FIX[28800]="tests/fixtures/ct0/spring_boot.json"
R2_FIX[28802]="tests/fixtures/ct0/aliyun_oss.json"
R2_FIX[28803]="tests/fixtures/ct2/302_200/initial.json"
R2_FIX[28806]="tests/fixtures/ct2/body_drift/initial.json"
R2_FIX[28807]="tests/fixtures/ct2/body_growth/initial.json"
R2_FIX[28810]="tests/fixtures/ct2/body_growth/initial.json"
R2_FIX[28808]="tests/fixtures/ct2/302_200/initial.json"
R2_FIX[28809]="tests/fixtures/ct2/body_drift/initial.json"

R3_FIX[28801]="tests/fixtures/ct2/302_200/initial.json"

R3_ACTION[28800]="hold"
R3_ACTION[28802]="hold"
R3_ACTION[28803]="change"
R3_ACTION[28806]="hold"
R3_ACTION[28807]="change"
R3_ACTION[28808]="hold"
R3_ACTION[28809]="change"
R3_ACTION[28801]="start"
R3_ACTION[28804]="restart"
R3_ACTION[28805]="restart_change"
R3_ACTION[28810]="dead"

# expected ct matrix (after round 3 has run)
declare -A EXPECT_CT
EXPECT_CT[28800]=0
EXPECT_CT[28801]=1
EXPECT_CT[28802]=0
EXPECT_CT[28803]=2
EXPECT_CT[28804]=4
EXPECT_CT[28805]=6
EXPECT_CT[28806]=0
EXPECT_CT[28807]=2
EXPECT_CT[28808]=0
EXPECT_CT[28809]=2
EXPECT_CT[28810]=8


ALL_PORTS=(28800 28801 28802 28803 28804 28805 28806 28807 28808 28809 28810)
R1_PORTS=(28804 28805)
R2_PORTS=(28800 28802 28803 28806 28807 28808 28809 28810)

DB="modules/main/db/recon.sqlite3"
SUBDOMAIN="www.localtest.me"
SRCDAR_CMD="./srcradar"
PIDS_FILE="/tmp/smoke_e2e_pids.txt"
: > "$PIDS_FILE"

log()  { printf "[smoke][%s] %s\n" "$(date +%H:%M:%S)" "$*"; }
err()  { printf "[smoke][err] %s\n" "$*" >&2; }

# === Helpers ============================================================

# kill the process on a given port (lsof + kill -9)
kill_port() {
    local port="$1"
    local PIDS
    PIDS="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
    if [ -n "$PIDS" ]; then
        # shellcheck disable=SC2086
        kill -9 $PIDS 2>/dev/null || true
    fi
}

# start smoke_fixture_server.py on port + file, wait for /health
start_server() {
    local port="$1" file="$2"
    local attempt=0
    while [ "$attempt" -lt 3 ]; do
        local existing
        existing="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
        if [ -n "$existing" ]; then
            log "  pre-clean :${port} attempt=$attempt killing pids $existing"
            # shellcheck disable=SC2086
            kill -9 $existing 2>/dev/null || true
            sleep 1
        fi
        [ -z "$existing" ] && break
        attempt=$((attempt+1))
    done
    [ -f "/tmp/srv_${port}.pid" ] && rm -f "/tmp/srv_${port}.pid"
    sleep 0.3
    nohup python3 modules/main/lib/smoke_fixture_server.py "$port" "$file" \
        --log "/tmp/srv_${port}.log" --pid-file "/tmp/srv_${port}.pid" \
        > "/tmp/srv_${port}.out" 2>&1 &
    local parent_pid=$!
    echo "$parent_pid" >> "$PIDS_FILE"
    sleep 0.5
    local i=0
    while [ "$i" -lt 50 ]; do
        if curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
            log "  server up on :${port} (file=$file)"
            return 0
        fi
        i=$((i+1))
        sleep 0.2
    done
    err "server :${port} failed to start"
    cat "/tmp/srv_${port}.out" || true
    cat "/tmp/srv_${port}.log" 2>/dev/null | tail -10 || true
    return 1
}

# /stop a server (via the curl /stop route, smoke_fixture_server.py handles SIGTERM)
stop_server() {
    local port="$1"
    if ! curl -sf --max-time 3 "http://127.0.0.1:${port}/stop" >/dev/null 2>&1; then
        log "  /stop on :${port} curl failed (server already gone?)"
        return 0
    fi
    local i=0
    while [ "$i" -lt 30 ]; do
        if ! curl -sf --max-time 1 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
            return 0
        fi
        i=$((i+1))
        sleep 0.2
    done
    err "server :${port} still alive after /stop"
    return 1
}

# /change a server (switch to the changed fixture)
change_fixture() {
    local port="$1"
    if ! curl -sf "http://127.0.0.1:${port}/change" >/dev/null 2>&1; then
        err "/change failed on :${port}"
        return 1
    fi
    log "  /change on :${port}"
}

cleanup_all() {
    log "cleanup: kill remaining servers"
    if [ -s "$PIDS_FILE" ]; then
        while read -r pid; do
            [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true
        done < "$PIDS_FILE" 2>/dev/null || true
    fi
    for p in "${ALL_PORTS[@]}"; do
        kill_port "$p"
    done
}
trap cleanup_all EXIT

# === -1: install srcradar modules ========================================
echo "===================================================="
echo "[-1/8] Install srcradar modules (db + pdtm + manage)"
echo "===================================================="
$SRCDAR_CMD db install --yes || true
$SRCDAR_CMD pdtm install --yes || true
$SRCDAR_CMD manage install --yes || true

# === 0: seed scope + add_business =========================================
echo
echo "===================================================="
echo "[0/8] Seed scope + add_business (smoketest)"
echo "===================================================="
mkdir -p ~/scope
echo "$SUBDOMAIN" > ~/scope/target.txt
$SRCDAR_CMD manage add_business -n smoketest -i ~/scope || true

# === 1: bypass cold-start gate + cleanup ================================
echo
echo "===================================================="
echo "[1/8] Bypass cold-start gate + cleanup ports"
echo "===================================================="
if [ -f "$DB" ]; then
    sqlite3 "$DB" "UPDATE businesses SET scan_count=10 WHERE business_name='smoketest'"
    log "scan_count set to 10 for smoketest"
else
    err "$DB not found"
fi

# shellcheck disable=SC2086
pkill -9 -f smoke_fixture_server 2>/dev/null || true
sleep 1
for p in "${ALL_PORTS[@]}"; do
    kill_port "$p"
done
sleep 1

# === 2: round 1 - cold start 3 ports (0B4/0B6/0CD8) ======================
echo
echo "===================================================="
echo "[2/8] Round 1: cold-start 28804/28805/28810"
echo "===================================================="
export RUN_ID="smoke-e2e-r1"
for port in "${R1_PORTS[@]}"; do
    start_server "$port" "${R1_FIX[$port]}"
done
log "daily_monitor round 1"
export SRCDAR_PORTS="28804,28805,28810"
$SRCDAR_CMD daily daily_monitor

# SQL deactivate these 3 rows
for port in "${R1_PORTS[@]}"; do
    stop_server "$port"
done

# === 3: round 2 - cold start 7 ports ====================================
echo
echo "===================================================="
echo "[3/8] Round 2: cold-start 7 ports"
echo "===================================================="
export RUN_ID="smoke-e2e-r2"
for port in "${R2_PORTS[@]}"; do
    start_server "$port" "${R2_FIX[$port]}"
done
log "daily_monitor round 2"
export SRCDAR_PORTS="28800,28802,28803,28806,28807,28808,28809,28810"
$SRCDAR_CMD daily daily_monitor

for port in "${R2_PORTS[@]}"; do
    stop_server "$port"
done

# === 4: round 3 - SQL reactivate + restart + /change + dead =============
echo
echo "===================================================="
echo "[4/8] Round 3: reactivate + restart + /change + dead + cold-start"
echo "===================================================="
export RUN_ID="smoke-e2e-r3"
for port in "${ALL_PORTS[@]}"; do
    action="${R3_ACTION[$port]:-}"
    case "$action" in
        hold)
            # same fixture as R2
            start_server "$port" "${R2_FIX[$port]}"
            ;;
        start)
            # cold start new port
            start_server "$port" "${R3_FIX[$port]:-${R2_FIX[$port]}}"
            ;;
        restart)
            # same fixture as R1, server was stopped in R1 cleanup
            start_server "$port" "${R1_FIX[$port]:-${R2_FIX[$port]}}"
            ;;
        restart_change)
            # restart with R1 fixture, then /change
            start_server "$port" "${R1_FIX[$port]:-${R2_FIX[$port]}}" \
                && change_fixture "$port"
            ;;
        change)
            # start R2 fixture then /change to switched fixture
            start_server "$port" "${R2_FIX[$port]}" \
                && change_fixture "$port"
            ;;
        dead)
            log "  :${port} stays dead (no server)"
            ;;
        *)
            err "  unknown action '$action' for :${port}"
            ;;
    esac
done


log "daily_monitor round 3"
export SRCDAR_PORTS="28800,28801,28802,28803,28804,28805,28806,28807,28808,28809,28810"
$SRCDAR_CMD daily daily_monitor

# === 5: verify 11 ports all configured ==================================
echo
echo "===================================================="
echo "[5/8] Verify 11 ports all configured"
echo "===================================================="
for port in "${ALL_PORTS[@]}"; do
    log "  port ${port}: action=${R3_ACTION[$port]:-unknown}"
done

# === 6: assert change_type verdicts (11 paths) ==========================
echo
echo "===================================================="
echo "[6/8] Assert change_type verdicts (11 ports)"
echo "===================================================="

# Pull port / ct / is_active from SQLite, compare against EXPECT_CT
ROWS=$(sqlite3 -separator ' ' "$DB" \
    "SELECT port, change_type, COALESCE(is_active,0) FROM web_subdomains WHERE subdomain='$SUBDOMAIN' ORDER BY port")

FAIL=0
echo "--- rows ---"
declare -A ACTUAL
while read -r port ct active; do
    [ -z "$port" ] && continue
    printf "  port=%s ct=%s is_active=%s\n" "$port" "$ct" "$active"
    ACTUAL[$port]="$ct"
done <<< "$ROWS"

for port in "${ALL_PORTS[@]}"; do
    exp="${EXPECT_CT[$port]:-}"
    got="${ACTUAL[$port]:-MISSING}"
    if [ "$got" != "$exp" ]; then
        echo "  [FAIL] port=$port expected ct=$exp, got ct=$got"
        FAIL=1
    fi
done

if [ "$FAIL" -ne 0 ]; then
    err "stage 1 (11-port) assertions FAILED"
    exit 1
fi
echo "[OK] all 11 ports match expected change_type"
log "stage 1 (11-port) done"

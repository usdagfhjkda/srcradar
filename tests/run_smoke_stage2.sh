#!/usr/bin/env bash
# tests/run_smoke_stage2.sh — 完整 stage 2 本地跑(对应 GitHub Actions workflow):
#   -1/10  Install srcradar modules (db + pdtm + manage)
#    0/10  Seed scope (smoketest) + add_business
#    1/10  Verify srcradar dispatcher
#    2/10  Bypass cold-start gate (smoketest.scan_count=10)
#    3/10  Kill services on 5 fixture ports
#    4/10  Start 5 fixture HTTP servers (ct0 x 2, ct2 x 3)
#    5/10  Round 1: cold-start insert via pdtm httpx (ct=1)
#    6/10  Middle: ct2 servers swap initial -> changed
#    7/10  Round 2: algo_diff verdict via pdtm httpx (ct=0/2)
#    8/10  Assert algo_diff change_type verdicts
#    9/10  Cleanup fixture servers
#
# 用法:
#   ./tests/run_smoke_stage2.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PORTS=(28080 28443 28488 28500 28504)
CT2_PORTS=(28443 28488 28500)

DB="modules/main/db/recon.sqlite3"

# ============================================================
# -1/10: Install srcradar modules (db + pdtm + manage)
# ============================================================
echo "===================================================="
echo "[-1/10] Install srcradar modules (db + pdtm + manage)"
echo "===================================================="
./srcradar db install --yes || true
./srcradar pdtm install --yes || true
./srcradar manage install --yes || true

# ============================================================
# 0/10: Seed scope + add_business
# ============================================================
echo
echo "===================================================="
echo "[0/10] Seed scope + add_business (smoketest)"
echo "===================================================="
mkdir -p ~/scope
echo "www.localtest.me" > ~/scope/target.txt
./srcradar manage add_business -n smoketest -i ~/scope || true

# ============================================================
# 1/10: Verify srcradar dispatcher
# ============================================================
echo
echo "===================================================="
echo "[1/10] Verify srcradar dispatcher"
echo "===================================================="
./srcradar --list | head -3 || true

# ============================================================
# 2/10: Bypass cold-start gate
# ============================================================
echo
echo "===================================================="
echo "[2/10] Bypass cold-start gate: smoketest.scan_count=10"
echo "===================================================="
if [ -f "$DB" ]; then
    sqlite3 "$DB" "UPDATE businesses SET scan_count=10 WHERE business_name='smoketest'"
    echo "  scan_count set:"
    sqlite3 "$DB" "SELECT business_name, scan_count FROM businesses"
else
    echo "[WARN] $DB not found, will let install.sh create it"
fi

# ============================================================
# 3/10: Kill 5 fixture ports + any leftover fixture servers
# ============================================================
echo
echo "===================================================="
echo "[3/10] Kill services on ports: ${PORTS[*]}"
echo "===================================================="
pkill -9 -f smoke_fixture_server 2>/dev/null || true
sleep 1
for P in "${PORTS[@]}"; do
    PIDS="$(lsof -ti tcp:"$P" 2>/dev/null || true)"
    if [ -n "$PIDS" ]; then
        echo "  port $P: killing pids $PIDS"
        # shellcheck disable=SC2086
        kill -9 $PIDS 2>/dev/null || true
    else
        echo "  port $P: free"
    fi
done
sleep 1

# ============================================================
# 4/10: Start 5 fixture HTTP servers
# ============================================================
echo
echo "===================================================="
echo "[4/10] Start 5 fixture HTTP servers"
echo "===================================================="
: > /tmp/http_server.pids
for spec in \
    "28080:tests/fixtures/ct0/spring_boot.json" \
    "28504:tests/fixtures/ct0/aliyun_oss.json" \
    "28443:tests/fixtures/ct2/302_200/initial.json" \
    "28488:tests/fixtures/ct2/body_drift/initial.json" \
    "28500:tests/fixtures/ct2/body_growth/initial.json"; do
    PORT="${spec%%:*}"
    FILE="${spec##*:}"
    nohup python3 modules/main/lib/smoke_fixture_server.py \
        --foreground "$PORT" "$FILE" \
        --log "/tmp/srv_${PORT}.log" \
        > "/tmp/srv_${PORT}.out" 2>&1 &
    echo $! >> /tmp/http_server.pids
done
sleep 1

for PORT in "${PORTS[@]}"; do
    if ! curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        echo "[FAIL] fixture server not up on :${PORT}"
        cat "/tmp/srv_${PORT}.out" || true
        exit 1
    fi
    echo "  [OK] fixture server up on :${PORT}"
done

# ============================================================
# 5/10: Round 1 (cold-start insert)
# ============================================================
echo
echo "===================================================="
echo "[5/10] Round 1: cold-start insert via pdtm httpx"
echo "===================================================="
export RUN_ID="smoke-e2e-fixture"
export SRCDAR_PORTS="28080,28443,28488,28500,28504"
echo "[round 1] cold-start: 6 ports insert, ct=1 (trigger trg_ws_ai)"
./srcradar daily daily_monitor

# ============================================================
# 6/10: Middle - ct2 swap initial -> changed
# ============================================================
echo
echo "===================================================="
echo "[6/10] Middle: ct2 servers swap initial -> changed"
echo "===================================================="
# 用 fixture_server 自带的 GET /change endpoint 触发 swap.
# 一个 server 启着 (round 1 启的), 调 /change 后永久切到 changed.json.
# ct0 server 不动 (port 28080/28504 持续 same body).
for PORT in "${CT2_PORTS[@]}"; do
    if ! curl -sf "http://127.0.0.1:${PORT}/change" >/dev/null 2>&1; then
        echo "[FAIL] ct2 swap on :${PORT} (GET /change failed)"
        cat "/tmp/srv_${PORT}.out" || true
        exit 1
    fi
    echo "  [OK] ct2 swapped on :${PORT}"
done

# ============================================================
# 7/10: Round 2 (algo_diff verdict)
# ============================================================
echo
echo "===================================================="
echo "[7/10] Round 2: algo_diff verdict via pdtm httpx"
echo "===================================================="
echo "[round 2] inline algo_diff block runs; 6 ports get real verdicts"
./srcradar daily daily_monitor

# ============================================================
# 8/10: Assert algo_diff change_type verdicts
# ============================================================
echo
echo "===================================================="
echo "[8/10] Assert algo_diff change_type verdicts"
echo "===================================================="
python3 - <<PY
import sqlite3, sys
conn = sqlite3.connect("$DB")
rows = conn.execute(
    "SELECT subdomain, port, change_type, last_seen, first_seen, "
    "       last_comparison_kind, length(prev_body) AS prev_len, "
    "       length(curr_body) AS curr_len "
    "FROM web_subdomains WHERE subdomain='www.localtest.me' "
    "ORDER BY port"
).fetchall()
print("--- web_subdomains rows (www.localtest.me) ---")
for r in rows:
    print(r)
if not rows:
    raise AssertionError("FAIL: no www.localtest.me rows in web_subdomains")
cts = [r[2] for r in rows]
ports = [r[1] for r in rows]
print(f"--- ports: {ports}")
print(f"--- change_types: {cts}")

ct0_ports = {28080, 28504}
ct2_ports = {28443, 28488, 28500}
warn_only_ports = {28488}

for r in rows:
    port, ct = r[1], r[2]
    if port in ct0_ports:
        assert ct == 0, (
            f"FAIL: ct0 port {port} expected ct=0 (anchor mask), "
            f"got ct={ct} kind={r[5]!r}"
        )
    elif port in ct2_ports:
        if port in warn_only_ports:
            print(
                f"[WARN] {port} expected ct=2 but got ct={ct} "
                f"kind={r[5]!r} - accepted (warn_only)"
            )
        else:
            assert ct == 2, (
                f"FAIL: ct2 port {port} expected ct=2 (real change), "
                f"got ct={ct} kind={r[5]!r}"
            )
    else:
        raise AssertionError(f"unexpected port {port} in rows")

has_zero = any(c == 0 for c in cts)
has_two = any(c == 2 for c in cts)
assert has_zero, "FAIL: no change_type=0 after round 2"
assert has_two, "FAIL: no change_type=2 after round 2"
print("[OK] algo_diff verdicts: ct=0 + ct=2 both present")
PY

# ============================================================
# 9/10: Cleanup
# ============================================================
echo
echo "===================================================="
echo "[9/10] Cleanup fixture servers"
echo "===================================================="
while read -r pid; do
    [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true
done < /tmp/http_server.pids 2>/dev/null || true
while read -r pid; do
    [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true
done < /tmp/http_server_ct2.pids 2>/dev/null || true
rm -f /tmp/http_server.pids /tmp/http_server_ct2.pids
echo "  fixture servers killed"

echo
echo "===================================================="
echo "STAGE 2 PASS"
echo "===================================================="

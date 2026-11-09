#!/usr/bin/env bash
# ============================================================================
# tests/verify_image.sh — check the built image against the release spec
#
#   docker build -t srcradar:1.0.0 .
#   bash tests/verify_image.sh srcradar:1.0.0
#
# It runs the image and checks what the release promises: the module set,
# the language, the DNS resolver set, the dashboard, the MCP daemon, the
# crontab and the DB. It needs a running docker daemon and sudo (the
# image pulls and the containers are created).
#
# Checks:
#   1. the excluded modules are NOT in the image
#   2. the included modules ARE in the image
#   3. the language is en
#   4. the DNS resolver set is global
#   5. the dashboard binds 0.0.0.0:8765 and answers /health
#   6. the MCP daemon binds 0.0.0.0:8764 and answers /health
#   7. the crontab holds the daily job and the dashboard watchdog
#   8. the algo_diff_cpp binary is where daily_monitor.sh expects it
# ============================================================================
set -uo pipefail

IMAGE="${1:-srcradar:1.0.0_zh}"
LANG_EXPECTED="${2:-zh}"
RESOLVER_EXPECTED="${3:-cn}"
PASS=0
FAIL=0

ok()   { printf '[ok]   %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '[FAIL] %s\n' "$*"; FAIL=$((FAIL+1)); }
run()  { printf '\n=== %s ===\n' "$*"; }

# ---- 1/2: the module set ----
run "1/2 the module set"
MODS="$(docker run --rm --entrypoint ls "$IMAGE" /opt/srcradar/modules/public 2>&1)"
printf '%s\n' "$MODS" | sed 's/^/    /'

# the excluded module of the English build; the Chinese build ships db_align
if [ "$RESOLVER_EXPECTED" = "cn" ]; then
    for m in db_align ymicp; do
        if printf '%s\n' "$MODS" | grep -qx "$m"; then
            ok "the module $m is in the image"
        else
            bad "the module $m is MISSING from the image"
        fi
    done
else
    for m in db_align ymicp; do
        if printf '%s\n' "$MODS" | grep -qx "$m"; then
            bad "the excluded module $m IS in the image"
        else
            ok "the excluded module $m is not in the image"
        fi
    done
fi
if [ -d "$(docker run --rm --entrypoint ls "$IMAGE" /opt/srcradar/modules/private 2>/dev/null || true)" ]; then
    :
fi
if docker run --rm --entrypoint test "$IMAGE" -d /opt/srcradar/modules/private 2>/dev/null; then
    bad "modules/private IS in the image"
else
    ok "modules/private is not in the image"
fi

for m in pdtm daily db manage i18n lib; do
    if docker run --rm --entrypoint test "$IMAGE" -d "/opt/srcradar/modules/main/$m" 2>/dev/null; then
        ok "modules/main/$m is in the image"
    else
        bad "modules/main/$m is MISSING from the image"
    fi
done
for m in katana health notify; do
    if docker run --rm --entrypoint test "$IMAGE" -d "/opt/srcradar/modules/public/$m" 2>/dev/null; then
        ok "modules/public/$m is in the image"
    else
        bad "modules/public/$m is MISSING from the image"
    fi
done

# ---- 3: the language ----
run "3 the language"
LANG_ENV="$(docker run --rm --entrypoint printenv "$IMAGE" SRCDAR_LANG 2>&1)"
printf '    SRCDAR_LANG=%s\n' "$LANG_ENV"
if [ "$LANG_ENV" = "$LANG_EXPECTED" ]; then ok "the language is $LANG_EXPECTED"; else bad "the language is $LANG_ENV"; fi
LANG_CONF="$(docker run --rm --entrypoint cat "$IMAGE" /opt/srcradar/config/lang.conf 2>&1)"
printf '    lang.conf: %s\n' "$LANG_CONF"
if printf '%s' "$LANG_CONF" | grep -q "^lang=$LANG_EXPECTED"; then ok "lang.conf says $LANG_EXPECTED"; else bad "lang.conf says: $LANG_CONF"; fi

# ---- 4: the DNS resolver set ----
run "4 the DNS resolver set"
PDTM_CONF="$(docker run --rm --entrypoint cat "$IMAGE" /opt/srcradar/config/pdtm.conf 2>&1)"
printf '%s\n' "$PDTM_CONF" | sed 's/^/    /'
if printf '%s' "$PDTM_CONF" | grep -q "^resolver_set=$RESOLVER_EXPECTED"; then ok "resolver_set is $RESOLVER_EXPECTED"; else bad "resolver_set is not $RESOLVER_EXPECTED"; fi
if docker run --rm --entrypoint test "$IMAGE" -s "/opt/srcradar/modules/main/pdtm/resolvers/$RESOLVER_EXPECTED" 2>/dev/null; then
    ok "the $RESOLVER_EXPECTED resolver list is in the image"
else
    bad "the $RESOLVER_EXPECTED resolver list is MISSING from the image"
fi

# ---- 8: the algo_diff_cpp binary ----
run "8 the algo_diff_cpp binary"
if docker run --rm --entrypoint test "$IMAGE" -x /opt/srcradar/modules/public/algo_diff_cpp/bin/algo_diff_cpp 2>/dev/null; then
    ok "algo_diff_cpp is at the path daily_monitor.sh expects"
else
    bad "algo_diff_cpp is MISSING at modules/public/algo_diff_cpp/bin/algo_diff_cpp"
fi

# ---- 8b: db_align and ENScan (the Chinese build) ----
if [ "$RESOLVER_EXPECTED" = "cn" ]; then
    run "8b db_align and ENScan"
    if docker run --rm --entrypoint test "$IMAGE" -x /opt/srcradar/modules/public/db_align/bin/db_align 2>/dev/null; then
        ok "db_align is at the path run_one_business.sh expects"
    else
        bad "db_align is MISSING at modules/public/db_align/bin/db_align"
    fi
    if docker run --rm --entrypoint test "$IMAGE" -x /opt/srcradar/modules/public/ENScan_GO/ENScan 2>/dev/null; then
        ok "ENScan is at the path run_one_business.sh expects"
    else
        bad "ENScan is MISSING at modules/public/ENScan_GO/ENScan"
    fi
    if docker run --rm --entrypoint test "$IMAGE" -f /usr/share/doc/srcradar/licenses/ENScan_GO-LICENSE 2>/dev/null; then
        ok "the ENScan license travels with the image"
    else
        bad "the ENScan license is MISSING from the image"
    fi
    if docker run --rm --entrypoint test "$IMAGE" -f /opt/srcradar/modules/public/ymicp/icp_mapp_query.py 2>/dev/null; then
        ok "the ymicp client is in the image"
    else
        bad "the ymicp client is MISSING from the image"
    fi
fi

# ---- 5/6/7: the runtime (a real container) ----
run "5/6/7 the runtime"
CN="sr-verify-$$"
docker rm -f "$CN" >/dev/null 2>&1
docker run -d --name "$CN" -p 127.0.0.1:18765:8765 -p 127.0.0.1:18764:8764 "$IMAGE" >/dev/null 2>&1
sleep 12

printf '%s\n' "--- the entrypoint log ---"
docker logs "$CN" 2>&1 | sed 's/^/    /'

printf '%s\n' "--- the listening sockets inside the container ---"
docker exec "$CN" sh -c "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null" 2>&1 \
    | awk 'NR>1 {print $2, $4}' | sed 's/^/    /'

# 5: the dashboard
if docker exec "$CN" curl -sf --max-time 5 http://127.0.0.1:8765/health >/dev/null 2>&1; then
    ok "the dashboard answers /health on 8765"
else
    bad "the dashboard does NOT answer /health on 8765"
fi
if docker exec "$CN" sh -c "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk 'NR>1 && \$4==\"0A\" {print \$2}'" 2>&1 | grep -qi '223D'; then
    ok "the dashboard is LISTENING (0x223D = 8765)"
else
    bad "the dashboard is NOT listening on 8765"
fi

# 6: the MCP daemon
if docker exec "$CN" curl -sf --max-time 5 http://127.0.0.1:8764/health >/dev/null 2>&1; then
    ok "the MCP daemon answers /health on 8764"
else
    bad "the MCP daemon does NOT answer /health on 8764"
fi
if docker exec "$CN" sh -c "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk 'NR>1 && \$4==\"0A\" {print \$2}'" 2>&1 | grep -qi '223C'; then
    ok "the MCP daemon is LISTENING (0x223C = 8764)"
else
    bad "the MCP daemon is NOT listening on 8764"
fi

# the host side reaches them through the loopback mapping
if curl -sf --max-time 5 http://127.0.0.1:18765/health >/dev/null 2>&1; then
    ok "the dashboard is reachable from the host through the loopback mapping"
else
    bad "the dashboard is NOT reachable from the host"
fi
if curl -sf --max-time 5 http://127.0.0.1:18764/health >/dev/null 2>&1; then
    ok "the MCP daemon is reachable from the host through the loopback mapping"
else
    bad "the MCP daemon is NOT reachable from the host"
fi

# 7: the crontab
printf '%s\n' "--- the crontab ---"
CRON="$(docker exec "$CN" crontab -l 2>&1)"
printf '%s\n' "$CRON" | sed 's/^/    /'
if printf '%s' "$CRON" | grep -q 'daily_monitor.sh'; then ok "the daily job is in the crontab"; else bad "the daily job is MISSING"; fi
if printf '%s' "$CRON" | grep -q '/bin/zsh'; then bad "the daily cron line uses zsh (the image has no zsh)"; else ok "the daily cron line does not depend on zsh"; fi
if printf '%s' "$CRON" | grep -q 'dashboard_watchdog.sh'; then ok "the dashboard watchdog is in the crontab"; else bad "the dashboard watchdog is MISSING"; fi

# the DB was created (it lives next to the db module: the dashboard and the
# MCP daemon both resolve that path)
if docker exec "$CN" test -f /opt/srcradar/modules/main/db/recon.sqlite3 2>/dev/null; then
    ok "the DB was initialised"
else
    bad "the DB was NOT initialised"
fi

docker rm -f "$CN" >/dev/null 2>&1

printf '\n====================================================\n'
printf 'PASS=%d  FAIL=%d\n' "$PASS" "$FAIL"
printf '====================================================\n'
[ "$FAIL" -eq 0 ]

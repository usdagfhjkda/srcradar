#!/bin/bash
# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/i18n.sh"
# ==============================================================================
# The script name: RedTeam_Asset_Scanner_v10.sh
# The script description: an industrial grade red team asset mapping pipeline
#
# What changed (v10):
#   1. the CDN judgement of stage 2 drops the IP expansion (it prevents the real
#      business from being filtered out by mistake when the cloud IP is shared)
#   2. the naabu passive scan of stage 4 excludes the LB domains (the LB is
#      already handled on its own in stage 3.5)
#   3. the httpx of stage 5 adds -rate-limit 50
#   4. the file references of stage 6 are unified to $FILE_IP
#   5. dnsx adds -t 50 to limit the concurrency and avoid the DNS ban
#   ────── the stage 1+2 rework of 2026-08-01 ──────
#   6. the dnsx of stage 1 adds -cname -j, it produces a JSONL (for cdnmatch to
#      consume) and it resolves the CNAME as well
#   7. the stage 2 uses ./bin/cdnmatch offline, it takes over the blocking
#      cdncheck call
#      (it settles the 7h45m hang of 2026-07-31)
#   8. the RESOLVERS are declared as a CSV in one place, which replaces the
#      `-r resolvers` file path
# ==============================================================================

# the pdtm tool paths are hardcoded (pdtm owns them; the head of the Ubuntu .bashrc
#  returns early in the `case $-` branch, so sourcing ~/.bashrc never reaches the export later on, and hardcoding is the most reliable)
export PATH="$PATH:$HOME/.pdtm/go/bin"
source "$(cd "$(dirname "$0")" && pwd)/../lib/load_config.sh"
load_config_set "$(cd "$(dirname "$0")" && pwd)/../../../config/pdtm.conf" "$(cd "$(dirname "$0")" && pwd)/../lib"
# shellcheck source=modules/main/lib/resolve_resolvers.sh
source "$(cd "$(dirname "$0")" && pwd)/../lib/resolve_resolvers.sh"
# 2026-10-03: source resolve_tools.sh - the httpx/dnsx paths are read from
# config/pdtm.conf automatically (env override > the conf injection > the default
# fallback), the hardcoded ~/.pdtm/go/bin/ is gone.
source "$(cd "$(dirname "$0")" && pwd)/lib/resolve_tools.sh"



set -e

# the TCP switch; it is flipped to true with -all, see line 84 (case) + line 379 (the consume).
ENABLE_TCP=false
# shellcheck disable=SC2034  # ENABLE_TCP consumed at line 381 (if block)
: "${ENABLE_TCP:="\${ENABLE_TCP}"}"

# ==============================================================================
# The unified resolver - see README section 6, section 8-10, section 8-11
#
# The single source of truth = the `./resolvers` file. The -r tools that this
# script calls:
#   dnsx (stage 1) / naabu (stage 4.5) accept both forms, so it uses the CSV form
#   uniformly (the file
#   form is safe as well, but the CSV comes first).
#
# subfinder goes through the file form, and it is only called by scan.sh /
# pipeline.sh in this repo,
# and those two scripts derive their own resolvers_file / RESOLVERS_CSV.
#
# The traps:
#   - the -r of cdncheck takes the file name as a host name -> 0 output. This script
#     does not call the cdncheck binary any more
#     (it goes through ./bin/cdnmatch), and it keeps the CSV so that a future
#     cdncheck fallback does not hit the trap.
#   - subfinder is the other way round: it accepts the file but the CSV silently
#     gives 0 hits.
#   - the resolvers file is sorted by the latency (fast -> slow), see the pdtm
#     resolvers file of the repo itself.
# ==============================================================================
# the list in effect: the helper resolves the field of the conf, the set
# that the operator picked and the language (the auto mode)
resolvers_file="$(resolve_resolvers_file)" || {
    echo "$(t main.scanner.resolvers_unreadable file="$resolvers_file")" >&2; exit 1; }
# the CSV is derived from the file (change the resolvers file and the CSV here follows)
RESOLVERS="$(tr '\n' ',' < "$resolvers_file" | sed 's/,$//')"
[ -z "$RESOLVERS" ] && { echo "[-] $(t main.scanner.resolvers_unreadable file="$resolvers_file")" >&2; exit 1; }

OUTPUT_DIR="./scan_results"
DNSX_INPUT_RAW="./dnsx_output.txt"

# 2026-10-07: read from env SRCDAR_PORTS (smoke fixture uses 28080/28443/28488/28500/28504).
#   -p flag is not viable because scanner is wrapped under pipeline.sh -> run_one_business
#   -> daily_monitor, and the CLI args do not propagate.
#   Fallback to legacy common ports if SRCDAR_PORTS is unset.
COMMON_WEB_PORTS="${SRCDAR_PORTS:-80,443,8000,8080,8081,8443,8888,9000,9090}"
# shellcheck disable=SC2034  # COMMON_WEB_PORTS consumed by awk (line 487)
: "${COMMON_WEB_PORTS:="$COMMON_WEB_PORTS"}"

mkdir -p "$OUTPUT_DIR"

log() {
    echo -e "$(date '+%Y-%m-%d %H:%M:%S') $1"
}

# ==============================================================================
# The argument parsing
#   the default:  only the Web asset discovery (dnsx + cdncheck + httpx), the TCP port scan is skipped
#   -all:    enables the TCP port information collection (naabu passive + active)
# ==============================================================================
ENABLE_TCP=false

usage() {
    cat <<EOF
$(t main.scanner.usage_header script="$0")
  $(t main.scanner.usage_default)
  -all                $(t main.scanner.usage_all)
  -p PORTS            $(t main.scanner.usage_ports)
                      $(t main.scanner.usage_ports_default)
                      $(t main.scanner.usage_ports_note)
  -h                  $(t main.scanner.usage_help)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -all)      ENABLE_TCP=true; shift ;;
        -p)        COMMON_WEB_PORTS="$2"; shift 2 ;;
        -p=*)      COMMON_WEB_PORTS="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *)         usage; log "[-] $(t main.scanner.unknown_arg arg="$1")"; exit 1 ;;
    esac
done

# the TCP statistic placeholder variables (they are 0 when it is off by default)
NAABU_COUNT=0
NAABU_ACTIVE_COUNT=0
ACTIVE_MAPPED_COUNT=0

# ==============================================================================
# the environment dependency check
# ==============================================================================
log "$(t main.scanner.check_deps)"

for cmd in dnsx naabu httpx cdncheck awk sed grep sort tr python3; do

    if ! command -v "$cmd" >/dev/null 2>&1; then
        log "$(t main.scanner.missing_dep cmd=$cmd)"
        exit 1
    fi
done

log "$(t main.scanner.deps_ok)"

# ==============================================================================
# the detection of the naabu SYN scan permission
# the priority: setcap cap_net_raw > root > the degraded CONNECT
# ==============================================================================
log "$(t main.scanner.detect_naabu_perm)"

NAABU_BIN=$(which naabu)

if getcap "$NAABU_BIN" 2>/dev/null | grep -q cap_net_raw; then
    SCAN_MODE="-s s"
    log "$(t main.scanner.cap_net_raw)"
elif [ "$(id -u)" -eq 0 ]; then
    SCAN_MODE="-s s"
    log "$(t main.scanner.root_syn_mode)"
else
    SCAN_MODE="-s c"
    log "$(t main.scanner.degraded_connect)"
    log "$(t main.scanner.howto_setcap)"
fi

log "[+] ======================================================================"
log "$(t main.scanner.started)"
log "[+] ======================================================================"

# ==============================================================================
# the input check
# ==============================================================================
if [ ! -s "$DNSX_INPUT_RAW" ]; then
    log "$(t main.scanner.input_missing)"
    log "$(t main.scanner.input_path DNSX_INPUT_RAW=$DNSX_INPUT_RAW)"
    exit 1
fi

# ==============================================================================
# stage 0: the data cleaning
# ==============================================================================
log "$(t main.scanner.stage0_clean)"

awk '{print $1}' "$DNSX_INPUT_RAW" \
| tr -d '\r' \
| sed -e 's|https\?://||g' -e 's|/.*||g' \
| grep -E '^([a-zA-Z0-9][-a-zA-Z0-9]*\.)+[a-zA-Z]{2,}$' \
| sort -u -o "$OUTPUT_DIR/pure_domains.txt"

DOMAIN_COUNT=$(grep -c . "$OUTPUT_DIR/pure_domains.txt" || true)

log "$(t main.scanner.stage0_done DOMAIN_COUNT=$DOMAIN_COUNT)"

if [ "$DOMAIN_COUNT" -eq 0 ]; then
    log "$(t main.scanner.stage0_empty)"
    exit 1
fi

# ==============================================================================
# stage 1 + stage 2: the DNS resolution + the offline CDN/WAF judgement
#
# The rework (2026-08-01):
#   - the dnsx adds -cname -j: it produces a JSONL (for cdnmatch to consume) and it
#     resolves the CNAME as well
#   - cdnmatch (./bin/cdnmatch) takes over the old stage 1+2 offline:
#       * FILE_IP/FILE_IPV6/FILE_CNAME/FILE_NS/all_unique_ips.txt
#       * cdn_{ips,domains}.txt / waf_{ips,domains}.txt / cloud_{ips,domains}.txt
#       * non_cdn_list.txt / non_cdn_ips.txt
#       * it holds the host<->IP cross mapping + the non_cdn fallback that keeps
#         the NXDOMAIN from being lost
#   - it replaces the incident chain of 2026-07-31: `cdncheck -i <domains>` hung on
#     stdin with
#     7h45m of 0 progress; and it had no -timeout/-rate-limit, so the ~0.9s/host
#     was not adjustable.
#
# The downstream contract is kept: FILE_IP is still `<ip> <domain>`, so the stages
# 3-9 do not change.
# ==============================================================================
log "$(t main.scanner.stage1_dns)"

# 2026-10-03: the dnsx path is resolved through resolve_tools.sh (env override > the conf injection > the default)
DNSX_BIN=$(resolve_tool dnsx "$HOME/.pdtm/go/bin/dnsx")
[ -x "$DNSX_BIN" ] || { log "[-] $(t main.scanner.dnsx_missing bin="$DNSX_BIN")"; exit 1; }

DNSX_RAW_FILE="$OUTPUT_DIR/dnsx_raw_output.txt"
FILE_IP="$OUTPUT_DIR/tmp_domain_ip_pairs.txt"
FILE_IPV6="$OUTPUT_DIR/tmp_domain_ipv6_pairs.txt"
FILE_CNAME="$OUTPUT_DIR/tmp_domain_cname_pairs.txt"
# shellcheck disable=SC2034
FILE_NS="$OUTPUT_DIR/tmp_domain_ns_pairs.txt"
CDNMATCH_STATS="$OUTPUT_DIR/cdnmatch_stats.json"

"$DNSX_BIN" -l "$OUTPUT_DIR/pure_domains.txt" \
     -cname -a -resp \
     -t 50 \
     -rl 100 \
     -r "$RESOLVERS" \
     -j \
     -no-color \
     < /dev/null \
     -o "$DNSX_RAW_FILE" \
     || log "[!] $(t main.scanner.dnsx_nonzero)"

DNSX_OUTPUT_COUNT=$(wc -l < "$DNSX_RAW_FILE" | tr -d ' ')
log "$(t main.scanner.stage1_dnsx_rows DNSX_OUTPUT_COUNT=$DNSX_OUTPUT_COUNT)"
# ==============================================================================
# stage 2: the CDN/WAF judgement (offline - ./bin/cdnmatch)
# It replaces the original blocking `cdncheck -i <hosts>` call. cdnmatch imports
# the sources_data.json table of cdncheck directly,
# does an in-memory lookup on the dnsx JSONL, and there is no network overhead.
# ==============================================================================
log "$(t main.scanner.stage2_cdnmatch)"

CDN_DOMAIN_FILE="$OUTPUT_DIR/cdn_domains.txt"
CDN_IP_FILE="$OUTPUT_DIR/cdn_ips.txt"
WAF_DOMAIN_FILE="$OUTPUT_DIR/waf_domains.txt"
WAF_IP_FILE="$OUTPUT_DIR/waf_ips.txt"
NON_CDN_DOMAIN_FILE="$OUTPUT_DIR/non_cdn_list.txt"
NON_CDN_IP_FILE="$OUTPUT_DIR/non_cdn_ips.txt"

if [ ! -x "./bin/cdnmatch" ]; then
    log "$(t main.scanner.cdnmatch_missing)"
    exit 1
fi
./bin/cdnmatch \
    -in "$DNSX_RAW_FILE" \
    -domains "$OUTPUT_DIR/pure_domains.txt" \
    -out "$OUTPUT_DIR" \
    -stats "$CDNMATCH_STATS" \
    || { log "[-] $(t main.scanner.cdnmatch_failed)"; exit 1; }

AWK_EXTRACT_COUNT=$(cat "$FILE_IP" "$FILE_IPV6" 2>/dev/null | wc -l || echo 0)
UNIQUE_IP_COUNT=$(wc -l < "$OUTPUT_DIR/all_unique_ips.txt" | tr -d ' ')
CDN_IP_COUNT=$(cat "$CDN_IP_FILE" "$WAF_IP_FILE" 2>/dev/null | wc -l | tr -d ' ')
CDN_DOMAIN_COUNT=$(cat "$CDN_DOMAIN_FILE" "$WAF_DOMAIN_FILE" 2>/dev/null | wc -l | tr -d ' ')
NON_CDN_IP_COUNT=$(wc -l < "$NON_CDN_IP_FILE" | tr -d ' ')
NON_CDN_DOMAIN_COUNT=$(wc -l < "$NON_CDN_DOMAIN_FILE" | tr -d ' ')
# the field names of the old report (in the old implementation the cdncheck hit IP and the CDN/WAF total were the same thing)
CDNCHECK_HIT_COUNT=$CDN_IP_COUNT
CNAME_NS_CDN_COUNT=$(wc -l < "$FILE_CNAME" | tr -d ' ')

log "$(t main.scanner.dns_unique_ips UNIQUE_IP_COUNT=$UNIQUE_IP_COUNT)"
log "$(t main.scanner.judgement_done CDN_IP_COUNT=$CDN_IP_COUNT CDN_DOMAIN_COUNT=$CDN_DOMAIN_COUNT)"
log "$(t main.scanner.scan_targets NON_CDN_IP_COUNT=$NON_CDN_IP_COUNT NON_CDN_DOMAIN_COUNT=$NON_CDN_DOMAIN_COUNT)"

# stage 3: the IP weight filter (the LB filter)
# ==============================================================================
log "$(t main.scanner.stage3_filter_lb)"

: > "$OUTPUT_DIR/pure_domains.txt"
: > "$OUTPUT_DIR/pure_domains.txt"

FILTERED_LB=0

while read -r ip; do
    [ -z "$ip" ] && continue

    DOMAIN_BIND_COUNT=$(grep -c "^$ip " "$FILE_IP" || echo 0)

    if [ "$DOMAIN_BIND_COUNT" -gt 15 ]; then
        log "$(t main.scanner.skip_lb_core ip=$ip DOMAIN_BIND_COUNT=$DOMAIN_BIND_COUNT)"
        echo "$ip" >> "$OUTPUT_DIR/lb_ips.txt"
        FILTERED_LB=$((FILTERED_LB + 1))
    else
        echo "$ip" >> "$OUTPUT_DIR/pure_ips_to_scan.txt"
    fi

done < "$OUTPUT_DIR/non_cdn_ips.txt"

SCAN_IP_COUNT=$(wc -l < "$OUTPUT_DIR/pure_ips_to_scan.txt" | tr -d ' ')
LB_IP_COUNT=$(wc -l < "$OUTPUT_DIR/lb_ips.txt" | tr -d ' ')

log "$(t main.scanner.filtered_lb FILTERED_LB=$FILTERED_LB)"
log "$(t main.scanner.remaining_ips SCAN_IP_COUNT=$SCAN_IP_COUNT)"

# ==============================================================================
# stage 3.5: the CDN IP + the LB core IP are merged -> the httpx 80/443 probe with
# the Host header
# ==============================================================================
log "$(t main.scanner.stage35_merge)"

# the stages 3.5/5/8 all use it, so it is checked and assigned once at the entry of
# stage 3.5
# 2026-10-03: the httpx path is resolved through resolve_tools.sh (env override > the conf injection > the default)
HTTPX_BIN=$(resolve_tool httpx "$HOME/.pdtm/go/bin/httpx")
[ -x "$HTTPX_BIN" ] || { log "[-] $(t main.scanner.httpx_missing bin="$HTTPX_BIN")"; exit 1; }

cat "$OUTPUT_DIR/cdn_ips.txt" \
    "$OUTPUT_DIR/waf_ips.txt" \
    "$OUTPUT_DIR/cloud_ips.txt" \
    "$OUTPUT_DIR/lb_ips.txt" \
    2>/dev/null \
| sort -u -o "$OUTPUT_DIR/cdn_lb_combined_ips.txt"

CDN_LB_TOTAL=$(wc -l < "$OUTPUT_DIR/cdn_lb_combined_ips.txt" | tr -d ' ')
log "$(t main.scanner.cdn_lb_total CDN_LB_TOTAL=$CDN_LB_TOTAL)"

: > "$OUTPUT_DIR/pure_domains.txt"
CDN_LB_WEB_COUNT=0

# === the probe logic of stage 3.5 was disabled (the follow-up of commit e8157ba) ===
# the whole if block (line 311..line 360) is commented out, and the variables that
# the stage 7 summary log uses are kept
# CDN_LB_TOTAL was already assigned before the if, CDN_LB_WEB_COUNT=0 (line 309), and cdn_lb_web_summary.json stays empty
# (CDN_LB_WEB_COUNT=0 makes the 'CDN+LB port count' of the stage 7 fusion = 0)
: > "$OUTPUT_DIR/cdn_lb_web_summary.json"
: > "$OUTPUT_DIR/cdn_lb_mapped_ports.txt"
# (to restore it: remove the # below)
# if [ "$CDN_LB_TOTAL" -gt 0 ]; then

#     # while read -r ip; do
        # [ -z "$ip" ] && continue
        # grep "^$ip " "$FILE_IP" \
        # | awk '{print $2":80"; print $2":443"}'
    # done < "$OUTPUT_DIR/cdn_lb_combined_ips.txt" \
    # | sort -u \
    # > "$OUTPUT_DIR/cdn_lb_domain_ports.txt"

    #     # CDN_LB_DOMAIN_PORT_COUNT=$(wc -l < "$OUTPUT_DIR/cdn_lb_domain_ports.txt" | tr -d ' ')
    #     log "[+] CDN+LB domain:port combination count: $CDN_LB_DOMAIN_PORT_COUNT"

    #     # if [ "$CDN_LB_DOMAIN_PORT_COUNT" -gt 0 ]; then
        # httpx -l "$OUTPUT_DIR/cdn_lb_domain_ports.txt" \
        #       -sc \                         # [status] shows the status code (200, 404, etc.)
        #       -cl \                         # [length] shows the response length, it tells the pages with the same status code apart
        #       -title \                      # [title] shows the page title, it makes the business function obvious
        #       -td \                         # [fingerprint] turns on the Wappalyzer technology detection, it probes Spring Boot, Vue, Nginx and the like
   #  -favicon \                    # [icon] probes the favicon and computes its mmh3 hash, it is used for the UI clustering
        #       -hash \                # [dedup] computes the hash of the page body, it identifies the repeated template pages
   #  -cdn \                        # [CDN] detects whether a CDN or a cloud WAF is used
        #       -random-agent \               # [UA] a random User-Agent, a standard httpx option
        #       -follow-redirects \           # [redirect] follows the redirects, so the real business page after the jump is seen
        #       -max-redirects 3 \            # [limit] limits the number of jumps to 3, so an infinite redirect loop does not waste the traffic
        #       -fd \ 
        #       -rate-limit 50 \
        #       -timeout 5 \
        #       -silent \
        #       -no-color \
        #       2>/dev/null \
        # > "$OUTPUT_DIR/cdn_lb_web_summary.txt"
        # FIX-2026-08-25: it adds -json so that httpx outputs a JSONL, and the suffix becomes .json.
        # The old .txt output went through parse_text_web of import_scan_results, where the
        # non-greedy URL_RE
        # regex cut http://host.tld into http://<first character>, so the
        # web_subdomains.subdomain
        # was truncated to 1 character. (DB business_id=1~7, first_seen=2026-07-26~08-21, 352 rows in total)
        # "$HTTPX_BIN" -l "$OUTPUT_DIR/cdn_lb_domain_ports.txt" -sc -cl -title -td -hash mmh3,simhash -random-agent -follow-redirects -max-redirects 3  -rate-limit 50 -timeout 5 -silent -no-color -json -irr < /dev/null 2>/dev/null > "$OUTPUT_DIR/cdn_lb_web_summary.json"

        #         # CDN_LB_WEB_COUNT=$(wc -l < "$OUTPUT_DIR/cdn_lb_web_summary.json" | tr -d ' ')
        # log "[+] the CDN+LB web survivor count: $CDN_LB_WEB_COUNT"

    #     # else
        # log "[!] no domain mapping, the probe is skipped"
        # : > "$OUTPUT_DIR/cdn_lb_web_summary.json"
    # fi

# # else
    # log "[!] no CDN/LB IP, the probe is skipped"
    # : > "$OUTPUT_DIR/cdn_lb_web_summary.json"
# fi
# === end disabled ===

# FIX-2026-08-25: the upstream switched to the -json JSONL output, so this has to be
# parsed with Python,
# otherwise the awk $1 would read "{" and the output would be garbled. It is enough to take the
# host:port of every JSONL line
# (the input here is cdn_lb_domain_ports.txt, that is domain:port, the host is the domain/IP and the port is 80/443).
# An empty file / 0 hits from the upstream: it writes an empty file directly, and the sort -u
# stage skips it.
python3 - "$OUTPUT_DIR/cdn_lb_web_summary.json" <<'PYEOF' \
    | sort -u > "$OUTPUT_DIR/cdn_lb_mapped_ports.txt"
import json, sys
path = sys.argv[1]
try:
    fh = open(path, encoding="utf-8", errors="replace")
except OSError:
    sys.exit(0)
with fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        host = d.get("host") or ""
        port = d.get("port")
        if host and port:
            print(f"{host}:{port}")
PYEOF

# ==============================================================================
# stage 4: the naabu passive discovery
# the fix: the LB domains are excluded (the LB is already handled on its own in stage 3.5, so it is not duplicated)
# ==============================================================================
# ==============================================================================
# stage 4 - 4.5: the TCP port information collection (off by default, it is enabled with -all)
# ==============================================================================
if [ "$ENABLE_TCP" = true ]; then

    log "$(t main.scanner.stage4_naabu_passive)"

# the passive scan sends no packet (it queries the public sources), so the non-CDN IPs
# (including the LB core IPs) are fed to it directly.
# The output is natively IP:port, the same as the active scan, so there is no need
# for an IP->domain reverse lookup mapping.
# The CDN IPs are not included: their ports belong to the shared infrastructure of the
# CDN vendor, and storing them would cause a wrong attribution.
naabu -l "$OUTPUT_DIR/non_cdn_ips.txt" \
      -passive \
      -timeout 5 \
      -silent \
      -no-color \
      2>/dev/null \
| sed 's|https\?://||g' \
| grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+$' \
| sort -u -o "$OUTPUT_DIR/raw_naabu_passive.txt"

NAABU_COUNT=$(grep -c . "$OUTPUT_DIR/raw_naabu_passive.txt" || true)

log "$(t main.scanner.naabu_passive_ports NAABU_COUNT=$NAABU_COUNT)"

# ==============================================================================
# stage 4.5: the naabu active scan
# ==============================================================================
log "$(t main.scanner.stage45_naabu_active SCAN_MODE=$SCAN_MODE)"

NAABU_ACTIVE_FILE="$OUTPUT_DIR/raw_naabu_active.txt"

naabu -l "$OUTPUT_DIR/pure_ips_to_scan.txt" \
     -top-ports 100 \
     $SCAN_MODE \
     -rate 50 \
     -c 25 \
     -timeout 2 \
     -retries 2 \
     -silent \
     -no-color \
     -r "$RESOLVERS" \
     2>/dev/null \
| sed 's|https\?://||g' \
| sort -u \
> "$NAABU_ACTIVE_FILE"
NAABU_ACTIVE_COUNT=$(grep -c . "$NAABU_ACTIVE_FILE" || true)

log "$(t main.scanner.naabu_active_ports NAABU_ACTIVE_COUNT=$NAABU_ACTIVE_COUNT)"

# ------------------------------------------------------------------------------
# the active result IP:Port -> domain:Port reverse lookup mapping
# the bare IP:Port is kept when there is no domain mapping
# ------------------------------------------------------------------------------
log "$(t main.scanner.stage45_mapping)"

: > "$OUTPUT_DIR/pure_domains.txt"

while read -r line; do

    [ -z "$line" ] && continue

    clean=$(echo "$line" | sed 's|https\?://||g' | sed 's|/.*||g' | tr -d '\r')
    ip=$(echo "$clean" | cut -d':' -f1)
    port=$(echo "$clean" | cut -d':' -f2)

    if ! echo "$ip" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
        continue
    fi

    mapped=$(grep "^$ip " "$FILE_IP" \
             | awk -v p="$port" '{print $2":"p}')

    if [ -n "$mapped" ]; then
        echo "$mapped" >> "$OUTPUT_DIR/raw_naabu_active_mapped.txt"
    else
        echo "$ip:$port" >> "$OUTPUT_DIR/raw_naabu_active_mapped.txt"
    fi

done < "$NAABU_ACTIVE_FILE"

sort -u "$OUTPUT_DIR/raw_naabu_active_mapped.txt" \
     -o "$OUTPUT_DIR/raw_naabu_active_mapped.txt"

ACTIVE_MAPPED_COUNT=$(grep -c . "$OUTPUT_DIR/raw_naabu_active_mapped.txt" || true)

log "$(t main.scanner.mapping_done ACTIVE_MAPPED_COUNT=$ACTIVE_MAPPED_COUNT)"

else
    log "$(t main.scanner.tcp_disabled)"
    : > "$OUTPUT_DIR/raw_naabu_passive.txt"
    : > "$OUTPUT_DIR/raw_naabu_active.txt"
    : > "$OUTPUT_DIR/raw_naabu_active_mapped.txt"
fi

# ==============================================================================
# stage 5: the httpx active probe
# It only keeps the port/rate-limit/timeout, and it drops -sc -cl -title -hash mmh3
# Those 4 flags make the output `URL [200] [2] [hash]`, which holds spaces and
# breaks the cut -d':' of the stage 6 mapper
# See appendix C of the README.
# The fingerprint is redone by the `-j -title -td -hash` of stage 8, so the final
# stored form does not change.
# ==============================================================================
log "$(t main.scanner.stage5_httpx)"

# build the <sub>,<ip>:<port> input (a direct connection to the cached IP + the per-target
# Host header)
# it uses the input format "<custom_host>,<target>" of httpx runner.go:1827-1829:
#   - <sub> comes from FILE_IP ($2), <ip> is the IP of the current loop
#   - inside httpx it writes <sub> as the CustomHost into req.Host and connects the
#     TCP directly to <ip>:<port>
#   - it does not depend on a httpx DNS lookup, 1 file 1 httpx call, no -H needed
# The risk: the cached IP may be stale (FILE_IP comes from the dnsx resolution of
# this round, so it is fresh for ~minutes)
# The stages 6/7 stay unchanged: the httpx -o output is still http://<ip>:<port>, and the
# stage 6 mapper
#   cuts the host:port with sed 's|https\?://||g', the same as before.
: > "$OUTPUT_DIR/non_cdn_domain_ports.txt"
while read -r ip; do
    [ -z "$ip" ] && continue
    grep "^$ip " "$FILE_IP" \
    | awk -v ports="$COMMON_WEB_PORTS" '
        BEGIN { n = split(ports, a, ","); for (i = 1; i <= n; i++) p[i] = a[i] }
        { for (i in p) print $2 "," $1 ":" p[i] }' \
    >> "$OUTPUT_DIR/non_cdn_domain_ports.txt"
done < "$OUTPUT_DIR/pure_ips_to_scan.txt"
sort -u "$OUTPUT_DIR/non_cdn_domain_ports.txt" -o "$OUTPUT_DIR/non_cdn_domain_ports.txt"

DOMAIN_PORT_COUNT=$(wc -l < "$OUTPUT_DIR/non_cdn_domain_ports.txt" | tr -d ' ')
log "$(t main.scanner.sub_ip_port_combos DOMAIN_PORT_COUNT=$DOMAIN_PORT_COUNT)"

# Note: -p is not passed - the input already holds the port, and -p would expand it
# again and overwrite the URL port (runner.go UpdatePort)
# < /dev/null is added: the behaviour of httpx depends on stdin (even when -l already
# points at the input file),
# and when pipeline.sh calls it the stdin may not point at a tty. The < /dev/null
# forces the stdin EOF.
"$HTTPX_BIN" -l "$OUTPUT_DIR/non_cdn_domain_ports.txt" \
      -rate-limit 50 \
      -timeout 5 \
      -no-color -silent \
      < /dev/null \
      -o "$OUTPUT_DIR/raw_httpx_active_ips.txt"

ACTIVE_COUNT=$(wc -l < "$OUTPUT_DIR/raw_httpx_active_ips.txt" | tr -d ' ')

log "$(t main.scanner.alive_ports ACTIVE_COUNT=$ACTIVE_COUNT)"

# ==============================================================================
# stage 6: the Host virtual host mapping
# the fix: the file references are unified to $FILE_IP
#
# Note: this mapper depends on the output format of the httpx of stage 5 = `http(s)://IP` or
#       `http(s)://IP:PORT`
#       (no bracketed fingerprint field). If stage 5 restores -sc/-cl/-title/-hash mmh3,
#       the output becomes `URL [200] [2] [hash]`, which holds spaces, so the whole
#       line is eaten as $ip and the hits are silently lost.
#       See appendix C of the README. Before you touch the stage 5/6, read the "todo /
#       future improvements" of appendix C.
# ==============================================================================
log "$(t main.scanner.stage6_clean)"

: > "$OUTPUT_DIR/pure_domains.txt"

while read -r line; do

    [ -z "$line" ] && continue

    clean=$(echo "$line" | sed 's|https\?://||g' | sed 's|/.*||g')

    if [[ "$clean" == *":"* ]]; then
        echo "$clean" >> "$OUTPUT_DIR/raw_mapped_ports.txt"
    else
        # the URL has no port: the default port is added from the scheme
        if [[ "$line" == https://* ]]; then
            echo "${clean}:443" >> "$OUTPUT_DIR/raw_mapped_ports.txt"
        else
            echo "${clean}:80" >> "$OUTPUT_DIR/raw_mapped_ports.txt"
        fi
    fi

done < "$OUTPUT_DIR/raw_httpx_active_ips.txt"

sort -u "$OUTPUT_DIR/raw_mapped_ports.txt" \
     -o "$OUTPUT_DIR/raw_mapped_ports.txt"

# ==============================================================================
# stage 7: the data fusion (the passive + the active + the httpx mapping + the CDN/LB)
# ==============================================================================
log "$(t main.scanner.stage7_fusion)"

cat "$OUTPUT_DIR/raw_naabu_passive.txt" \
    "$OUTPUT_DIR/raw_naabu_active_mapped.txt" \
    "$OUTPUT_DIR/raw_mapped_ports.txt" \
    "$OUTPUT_DIR/cdn_lb_mapped_ports.txt" \
    2>/dev/null \
| sed 's|https\?://||g' \
| grep -E '^.+:[0-9]+$' \
| sort -u -o "$OUTPUT_DIR/non_cdn_all_ports.txt"

ALL_COUNT=$(grep -c . "$OUTPUT_DIR/non_cdn_all_ports.txt" || true)

log "$(t main.scanner.fused_ports ALL_COUNT=$ALL_COUNT)"

# ==============================================================================
# stage 8: the Web survivor confirmation
# ==============================================================================
log "$(t main.scanner.stage8_web)"

# httpx -l "$OUTPUT_DIR/non_cdn_all_ports.txt" \
#       -sc \                         # [status] shows the status code (200, 404, etc.)
#       -cl \                         # [length] shows the response length, it tells the pages with the same status code apart
#       -title \                      # [title] shows the page title, it makes the business function obvious
#       -td \                         # [fingerprint] turns on the Wappalyzer technology detection, it probes Spring Boot, Vue, Nginx and the like
# #  -favicon \                    # [icon] probes the favicon and computes its mmh3 hash, it is used for the UI clustering
#       -hash \                # [dedup] computes the hash of the page body, it identifies the repeated template pages
# #  -cdn \                        # [CDN] detects whether a CDN or a cloud WAF is used
#       -random-agent \               # [UA] a random User-Agent, a standard httpx option
#       -follow-redirects \           # [redirect] follows the redirects, so the real business page after the jump is seen
#       -max-redirects 3 \            # [limit] limits the number of jumps to 3, so an infinite redirect loop does not waste the traffic
#       -fd \ 
#       -rate-limit 50 \
#       -timeout 5 \
#       -silent \
#       -no-color \
#       2>/dev/null \
# > "$OUTPUT_DIR/non_cdn_web_summary.txt"
# the stage 8 input: the <sub>,<ip>:<port> format (a direct connection to the cached IP + the per-target Host header)
# the <ip>:<port> of non_cdn_all_ports.txt is reverse looked up in FILE_IP to get the sub
# the ones that FILE_IP does not cover (the ones naabu found on its own) take the fallback <ip>:<port> (no CustomHost, Host=IP)
: > "$OUTPUT_DIR/non_cdn_web_inputs.txt"
while read -r hp; do
    [ -z "$hp" ] && continue
    ip="${hp%:*}"
    port="${hp#*:}"
    subs=$(grep "^$ip " "$FILE_IP" 2>/dev/null | awk "{print \$2}")
    if [ -n "$subs" ]; then
        while read -r sub; do
            [ -z "$sub" ] && continue
            echo "$sub,$ip:$port" >> "$OUTPUT_DIR/non_cdn_web_inputs.txt"
        done <<< "$subs"
    else
        # the fallback: there is no sub mapping, so it goes with the IP-only probe
        echo "$ip:$port" >> "$OUTPUT_DIR/non_cdn_web_inputs.txt"
    fi
done < "$OUTPUT_DIR/non_cdn_all_ports.txt"
sort -u "$OUTPUT_DIR/non_cdn_web_inputs.txt" -o "$OUTPUT_DIR/non_cdn_web_inputs.txt"

INPUT_COUNT=$(wc -l < "$OUTPUT_DIR/non_cdn_web_inputs.txt" | tr -d ' ')
log "$(t main.scanner.stage8_combos INPUT_COUNT=$INPUT_COUNT)"

"$HTTPX_BIN" -l "$OUTPUT_DIR/non_cdn_web_inputs.txt" -sc -cl -title -hash mmh3,simhash -random-agent -follow-redirects -max-redirects 3  -td -rate-limit 50 -timeout 5 -silent -no-color -json -irr < /dev/null 2>/dev/null > "$OUTPUT_DIR/non_cdn_web_summary.json"
WEB_COUNT=$(grep -c . "$OUTPUT_DIR/non_cdn_web_summary.json" || true)

log "$(t main.scanner.web_count WEB_COUNT=$WEB_COUNT)"

# ==============================================================================
# stage 9: the final format alignment and the separation of the TCP ports
# ==============================================================================
log "$(t main.scanner.stage9_split)"

# the IP:port of the web survivors - it is extracted from the host (the resolved IP) +
# the port field of the httpx json.
# There is no jq, so the JSONL is parsed with python3.
python3 - "$OUTPUT_DIR/non_cdn_web_summary.json" <<'PYEOF' \
    | sort -u > "$OUTPUT_DIR/tmp_web_ip_ports.txt"
import json, sys
path = sys.argv[1]
try:
    fh = open(path, encoding="utf-8", errors="replace")
except OSError:
    sys.exit(0)
with fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        # the host field can be a domain (when a domain is fed) or an IP (when an IP is
        # fed), so it is not reliable;
        # host_ip / a[] is the stable resolved IP, it is used to line up with the IP:port of naabu.
        ip = d.get("host_ip") or ""
        if not ip:
            a = d.get("a") or []
            if a:
                ip = a[0]
        if not ip:
            ip = d.get("host") or ""
        port = d.get("port")
        if ip and port:
            print(f"{ip}:{port}")
PYEOF

# the TCP full set = the raw IP:port of the naabu passive + the active scan (both now output the IP:port natively)
cat "$OUTPUT_DIR/raw_naabu_passive.txt" \
    "$OUTPUT_DIR/raw_naabu_active.txt" \
    2>/dev/null \
| sed 's|https\?://||g' \
| grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+$' \
| sort -u -o "$OUTPUT_DIR/tmp_all_ip_ports.txt"

log "$(t main.scanner.web_ip_port_list)"
cat "$OUTPUT_DIR/tmp_web_ip_ports.txt" | while read l; do log "    $l"; done
log "$(t main.scanner.all_ip_port_list)"
cat "$OUTPUT_DIR/tmp_all_ip_ports.txt" | while read l; do log "    $l"; done

# the non-web TCP ports = the full set - web
comm -23 \
    "$OUTPUT_DIR/tmp_all_ip_ports.txt" \
    "$OUTPUT_DIR/tmp_web_ip_ports.txt" \
> "$OUTPUT_DIR/non_cdn_tcp_ports.txt"

rm -f "$OUTPUT_DIR/tmp_web_ip_ports.txt" \
      "$OUTPUT_DIR/tmp_all_ip_ports.txt"

TCP_COUNT=$(wc -l < "$OUTPUT_DIR/non_cdn_tcp_ports.txt" | tr -d ' ')

log "$(t main.scanner.stage9_done TCP_COUNT=$TCP_COUNT)"

# ==============================================================================
# the cleanup of the temporary files
# ==============================================================================
rm -f \
    "$OUTPUT_DIR/raw_naabu_passive.txt" \
    "$OUTPUT_DIR/raw_naabu_active.txt" \
    "$OUTPUT_DIR/raw_naabu_active_mapped.txt" \
    "$OUTPUT_DIR/raw_httpx_active_ips.txt" \
    "$OUTPUT_DIR/raw_mapped_ports.txt" \
    "$OUTPUT_DIR/cdn_lb_domain_ports.txt" \
    "$OUTPUT_DIR/cdn_lb_mapped_ports.txt" \
    "$OUTPUT_DIR/remaining_ips_for_cdncheck.txt" \
    "$OUTPUT_DIR/cdncheck_cdn_ips.txt" \
    "$OUTPUT_DIR/lb_domains.txt" \
    "$OUTPUT_DIR/non_cdn_non_lb_list.txt" \
    "$OUTPUT_DIR/tmp_web_ports.txt"

# ==============================================================================
# the final report
# ==============================================================================
log "[+] ======================================================================"
log "$(t main.scanner.pipeline_done)"
log "[+] ======================================================================"

log "$(t main.scanner.sum_input_domains DOMAIN_COUNT=$DOMAIN_COUNT)"
log "$(t main.scanner.sum_dnsx_rows DNSX_OUTPUT_COUNT=$DNSX_OUTPUT_COUNT)"
log "$(t main.scanner.sum_awk_mappings AWK_EXTRACT_COUNT=$AWK_EXTRACT_COUNT)"
log "$(t main.scanner.sum_unique_ips UNIQUE_IP_COUNT=$UNIQUE_IP_COUNT)"
log "$(t main.scanner.sum_cname_ns_cdn CNAME_NS_CDN_COUNT=$CNAME_NS_CDN_COUNT)"
log "$(t main.scanner.sum_cdncheck_hits CDNCHECK_HIT_COUNT=$CDNCHECK_HIT_COUNT)"
log "$(t main.scanner.sum_final_cdn_ips CDN_IP_COUNT=$CDN_IP_COUNT)"
log "$(t main.scanner.sum_final_cdn_domains CDN_DOMAIN_COUNT=$CDN_DOMAIN_COUNT)"
log "$(t main.scanner.sum_noncdn_ips NON_CDN_IP_COUNT=$NON_CDN_IP_COUNT)"
log "$(t main.scanner.sum_noncdn_domains NON_CDN_DOMAIN_COUNT=$NON_CDN_DOMAIN_COUNT)"
log "$(t main.scanner.sum_lb_core_ips LB_IP_COUNT=$LB_IP_COUNT)"
log "$(t main.scanner.sum_filtered_lb FILTERED_LB=$FILTERED_LB)"
log "$(t main.scanner.sum_cdn_lb_merged CDN_LB_TOTAL=$CDN_LB_TOTAL)"
log "$(t main.scanner.sum_cdn_lb_web CDN_LB_WEB_COUNT=$CDN_LB_WEB_COUNT)"
log "$(t main.scanner.sum_scan_ips SCAN_IP_COUNT=$SCAN_IP_COUNT)"
log "$(t main.scanner.sum_naabu_passive NAABU_COUNT=$NAABU_COUNT)"
log "$(t main.scanner.sum_naabu_active NAABU_ACTIVE_COUNT=$NAABU_ACTIVE_COUNT)"
log "$(t main.scanner.sum_active_mapping ACTIVE_MAPPED_COUNT=$ACTIVE_MAPPED_COUNT)"
log "$(t main.scanner.sum_httpx_alive ACTIVE_COUNT=$ACTIVE_COUNT)"
log "$(t main.scanner.sum_fused_ports ALL_COUNT=$ALL_COUNT)"
log "$(t main.scanner.sum_web_assets WEB_COUNT=$WEB_COUNT)"
log "$(t main.scanner.sum_tcp_ports TCP_COUNT=$TCP_COUNT)"
log "$(t main.scanner.sum_naabu_mode SCAN_MODE=$SCAN_MODE)"

log "[+] ======================================================================"

log "$(t main.scanner.report_web)"
log "    $OUTPUT_DIR/non_cdn_web_summary.json"

log "$(t main.scanner.report_cdn_lb_web)"
log "    $OUTPUT_DIR/cdn_lb_web_summary.txt"

log "$(t main.scanner.report_tcp_brute)"
log "    $OUTPUT_DIR/non_cdn_tcp_ports.txt"

log "$(t main.scanner.report_ip_domain_map)"
log "    $OUTPUT_DIR/tmp_domain_ip_pairs.txt"

log "$(t main.scanner.report_noncdn_domains)"
log "    $OUTPUT_DIR/non_cdn_list.txt"

log "$(t main.scanner.report_cdn_lb_merged)"
log "    $OUTPUT_DIR/cdn_lb_combined_ips.txt"

log "[+] ======================================================================"


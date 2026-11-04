#!/usr/bin/env bash
# the subfinder -> dnsx -> alterx -> dnsx pipeline (the glob scope version)
#
# The 2026-07-30 glob refactor:
#   - target.txt / exclude.txt support the `*` wildcard, at any position
#     (aaa.*.bbb.com / cc-*.bbb.com / *.bbb.com / ccc.bbb.com)
#   - subfinder is called per base (the base is the rightmost run of labels
#     without a * that is extracted from the glob pattern)
#   - three grep target regex + three grep -v exclude regex (after subfinder /
#     after alterx / after dnsx)
#   - every exclude is compiled as a glob into an ERE, and the original keyword
#     substring matching branch is gone
#
# Note: subfinder -d takes the literal base; alterx derives from the known
# subdomains, so its output still has to pass the target grep gate.

# the pdtm tool paths are hardcoded (pdtm owns them; the head of the Ubuntu .bashrc
#  returns early in the `case $-` branch, so sourcing ~/.bashrc never reaches the export later on, and hardcoding is the most reliable)
export PATH="$PATH:$HOME/.pdtm/go/bin"
source "$(cd "$(dirname "$0")" && pwd)/../lib/load_config.sh"
load_config_set "$(cd "$(dirname "$0")" && pwd)/../../../config/pdtm.conf" "$(cd "$(dirname "$0")" && pwd)/../lib"
# 2026-10-03: source resolve_tools.sh - the httpx/dnsx paths are read from
# config/pdtm.conf automatically (env override > the conf injection > the default
# fallback), the hardcoded ~/.pdtm/go/bin/ is gone.
source "$(cd "$(dirname "$0")" && pwd)/lib/resolve_tools.sh"



set -euo pipefail
# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/i18n.sh"

# ===== the configuration =====
# The single source of truth: the ./resolvers file. subfinder goes through
# resolvers_file, the others through RESOLVERS_CSV.
# See README section 8-11, the cross-tool trap matrix (the cdncheck=-file trap, the
# subfinder=-csv trap).
resolvers_file="${resolvers_file:-resolvers}"
RESOLVERS_CSV="$(tr '\n' ',' < "$resolvers_file" | sed 's/,$//')"
[ -z "$RESOLVERS_CSV" ] && { echo "[-] 错误: $resolvers_file 文件为空或不可读" >&2; exit 1; }
TARGET="target.txt"
EXCLUDE="exclude.txt"
ALIVE="alive.txt"
ALTERX_OUT="alterx.txt"
DNSX_OUT="dnsx_output.txt"
DB="${DB:-${RECON_DB:-db/recon.sqlite3}}"
BUSINESS_ID="${BUSINESS_ID:-1}"
WORDLIST="${WORDLIST:-}"
if [ -n "$WORDLIST" ] && [ -f "$WORDLIST" ]; then
    WORDLIST_HASH=$(sha256sum "$WORDLIST" | cut -c1-12)
else
    WORDLIST_HASH="${WORDLIST_HASH:-alterx-default}"
fi

# the alterx cadence control: the global scan / the manual override.
# ALTERX_CADENCE_DAYS=30 by default -> the alterx derivation runs once every 30
# days, and in between only subfinder+dnsx runs.
#                              setting it to 0 means "always expired" = alterx runs
# every time (the same as turning the cadence off).
# FORCE_ALTERX=1            force it this round, ignoring the cadence (it wins over
# the cadence).
ALTERX_CADENCE_DAYS="${ALTERX_CADENCE_DAYS:-30}"
FORCE_ALTERX="${FORCE_ALTERX:-}"

# check python3 (target_glob.py + permutation_cache.py need it)
command -v python3 >/dev/null 2>&1 || {
    echo "$(t main.scan.missing_python3)" >&2
    exit 1
}
[ -f "target_glob.py" ] || {
    echo "$(t main.scan.missing_target_glob)" >&2
    exit 1
}

# ===== prepare: clear / create the artifacts =====
: > "$ALIVE"
: > "$ALTERX_OUT"
: > "$DNSX_OUT"

# ===== 0) split target.txt: the exact subdomains (no *) / the glob patterns (with *) =====
# The first thing after the target is read: the exact subdomains go to dnsx ->
# DNSX_OUT, without going through any other step before that
# (no ERE is compiled, HAS_GLOB is not computed, they do not enter
# subfinder/alterx/permutation/wildcard).
# The intent: since only the exact hosts were listed, there is no need to expand
# and look at the sibling subdomains. The downstream scanner.sh reads DNSX_OUT
# directly -> the stages 1+2+5+8 all run, and only the enumerate trio is left out.
[ -s "$TARGET" ] || { echo "[-] $TARGET 为空或不存在" >&2; exit 1; }
[ -f "$EXCLUDE" ] || : > "$EXCLUDE"

PRECISE_HOSTS="$(mktemp)"
GLOB_HOSTS="$(mktemp)"
WWW_COUNT=0
while IFS= read -r t || [ -n "$t" ]; do
    [ -z "$t" ] && continue
    case "$t" in
        *\** ) echo "$t" >> "$GLOB_HOSTS"  ;;
        # an exact single subdomain (no *): when the domain holds only one ".", the
        # "www." prefix is added automatically
        # example: scanme.sh -> www.scanme.sh; example.com -> www.example.com
        # 2+ dots (such as sub.example.com / a.b.c.example.com) and the multi-label
        # domains are left alone
        # 0 dots (such as localhost) is rare but it is let through as well
        *.*.* ) echo "$t"        >> "$PRECISE_HOSTS" ;;
        *.*   ) echo "www.$t"    >> "$PRECISE_HOSTS"; WWW_COUNT=$((WWW_COUNT + 1)) ;;
        *     ) echo "$t"        >> "$PRECISE_HOSTS" ;;
    esac
done < "$TARGET"

# ===== the exact subdomains go straight to cat -> DNSX_OUT (the first thing after the
# target is read, it goes through no step) =====
# No dnsx, no ERE compilation, no HAS_GLOB, they do not enter
# subfinder/alterx/permutation/wildcard.
# The stage 1 of the downstream scanner.sh runs dnsx once more.
if [ -s "$PRECISE_HOSTS" ]; then
    sort -u "$PRECISE_HOSTS" -o "$PRECISE_HOSTS"
    PRECISE_COUNT=$(wc -l < "$PRECISE_HOSTS")
    cat "$PRECISE_HOSTS" >> "$DNSX_OUT"
    if [ "$WWW_COUNT" -gt 0 ]; then
        echo "$(t main.scan.precise_direct PRECISE_COUNT=${PRECISE_COUNT} WWW_COUNT=${WWW_COUNT})"
    else
        echo "$(t main.scan.precise_direct_short PRECISE_COUNT=${PRECISE_COUNT})"
    fi
fi
rm -f "$PRECISE_HOSTS"

# ===== the early fast path: all the targets are exact subdomains (no *) -> the full
# flow still runs (dnsx + scanner) =====
# An exact subdomain cannot exit 0 directly - the downstream scanner.sh / import
# would not run,
# and then the exact subdomains would only produce DNSX_OUT, with no tcp_assets /
# web_hashes.
# Here dnsx resolves the exact subdomains, and then it goes on (the subfinder loop
# later runs on nothing, without a side effect).
if [ ! -s "$GLOB_HOSTS" ]; then
    # keep GLOB_HOSTS (mktemp) alive — line 144 target_glob.py all-bases
    # reads it; empty file is fine (returns no bases, falls into
    # the "no glob base" skip path below).
    [ -s "$DNSX_OUT" ] && sort -u "$DNSX_OUT" -o "$DNSX_OUT"
    _i18n_val=$(wc -l < "$DNSX_OUT")
    echo "$(t main.scan.precise_fast_path count="$_i18n_val")"
    # 2026-10-03: the dnsx path is resolved through resolve_tools.sh (env override > the conf injection > the default)
    DNSX_BIN=$(resolve_tool dnsx "$HOME/.pdtm/go/bin/dnsx")
    [ -x "$DNSX_BIN" ] || DNSX_BIN=$(command -v dnsx || echo /opt/srcradar/bin/dnsx)
    [ -x "$DNSX_BIN" ] || DNSX_BIN=$(command -v dnsx || true)
    if [ -x "$DNSX_BIN" ]; then
        echo "$(t main.scan.dnsx_precise)"
        "$DNSX_BIN" -rl 100 -t 150 -retry 2 -o "$ALIVE" -r "$RESOLVERS_CSV" < "$DNSX_OUT" || true
        LINES=$(wc -l < "$ALIVE")
        echo "$(t main.scan.alive_count LINES=$LINES)"
        # the alive ones are appended to DNSX_OUT (the stage 1 of scanner.sh reads DNSX_OUT)
        [ -s "$ALIVE" ] && cat "$ALIVE" >> "$DNSX_OUT" && sort -u "$DNSX_OUT" -o "$DNSX_OUT"
    else
        echo "$(t main.scan.skip_dnsx_no_binary)"
    fi
    # the rest of the exact fast path: it does not run alterx/permutation/wildcard
    # (those are for the glob patterns),
    # but it keeps the empty targets.regex + the skipped subfinder loop, so that
    # scanner.sh takes over.
    : > targets.regex; echo '^  * $' > targets.regex
    : > excludes.regex
    SUBS_TMP="$(mktemp)"   # fast-path 提前定义,避免 set -u unbound
fi

# ===== the rest: the glob flow (ERE compilation -> subfinder -> alterx -> permutation_cache -> wildcard) =====
echo "$(t main.scan.compile_globs)"

python3 target_glob.py targets-ere  --input "$TARGET"  > targets.regex
python3 target_glob.py excludes-ere --input "$EXCLUDE" > excludes.regex

# the empty file guard (it never matches, so that grep -f on an empty file does not fail)
[ -s targets.regex ]  || echo '^$)$' > targets.regex
[ -s excludes.regex ] || echo '^$)$' > excludes.regex

# BASES is only extracted from the glob patterns - an exact subdomain needs no subfinder enumeration (it is already in DNSX_OUT)
BASES=$(python3 target_glob.py all-bases --input "$GLOB_HOSTS" || true)
# The exact subdomain fast path (the targets have no *) went straight into DNSX_OUT
#   at stage 0, so the glob bases
#   are empty anyway, and it goes on so that subfinder/alterx run on nothing,
# without a side effect.
# It does not exit 1 here, otherwise scanner.sh / import would not run.
if [ -z "$BASES" ] && [ -s "$DNSX_OUT" ]; then
    : # precise fast-path; continue
elif [ -z "$BASES" ]; then
    echo "$(t main.scan.no_glob_base)" >&2; exit 1
fi
rm -f "$GLOB_HOSTS"

# drop the wildcard bases: they are already covered by the separate subfinder path
# of the wildcard in stage 6
# (subfinder -> the bare host goes to DNSX_OUT -> the downstream scanner.sh resolves
# it with dnsx).
# Letting alterx eat these bases as well would derive a lot of candidates of the
# *.vendor-base.com form from the subdomain word patterns (api/www/sale/v2...), and
# 99% of them hit the catch-all IP -> it pollutes the CDN judgement + it inflates the
# dnsx_output candidates + it drags the dnsx stage from minutes to hours. 920k
# candidates on one base with 880k of them
# coming from the wildcard is exactly this bug. The fix: take them out of BASES
# directly, stage 6 already covers them.
WILDCARD_BASES=""
if [ -s wildcard.txt ]; then
    WILDCARD_BASES=$(python3 target_glob.py all-bases --input wildcard.txt || true)
fi
if [ -n "$WILDCARD_BASES" ]; then
    EXCLUDED=$(echo "$WILDCARD_BASES" | wc -l)
    BASES=$(comm -23 <(echo "$BASES" | sort -u) <(echo "$WILDCARD_BASES" | sort -u))
    echo "$(t main.scan.excluded_wildcard_bases EXCLUDED=$EXCLUDED)"
fi

# ===== 1) the subdomain enumeration: grouped by base, one subfinder per base =====
echo "$(t main.scan.subfinder_by_base)"
# ALTERX_TMPDIR is created in stage 3, it is declared early so that the EXIT trap can refer to it (it starts empty -> it does not delete . by mistake)
ALTERX_TMPDIR=
SUBS_TMP="$(mktemp)"
trap '[ -n "$ALTERX_TMPDIR" ] && rm -rf "$ALTERX_TMPDIR"; rm -f "$SUBS_TMP" "$SUBS_TMP.f" targets.regex excludes.regex' EXIT

while IFS= read -r base; do
    [ -z "$base" ] && continue
    subfinder -d "$base" -silent -r "$resolvers_file" 2>/dev/null >> "$SUBS_TMP" || true
done <<< "$BASES"

# the known subdomains (the original target lines, the pattern lines that hold a *
# are skipped - they are glob patterns, not real hosts)
# Note: the pattern lines are found indirectly by subfinder in the base form; cat-ing
# the pattern lines directly makes the later grep match itself.
#
# The exact subdomains already went straight into DNSX_OUT through dnsx at an earlier
# step (the early exact subdomain -> DNSX_OUT block of stage 0 of scan.sh), so they
# are not pushed again here. The glob subdomains keep the original full flow
# (stage 1 -> 4).
sort -u "$SUBS_TMP" -o "$SUBS_TMP"

# ===== 1.5) the first filter: after subfinder, grep target + grep -v exclude =====
if [ -s "$SUBS_TMP" ]; then
    grep -E -f targets.regex "$SUBS_TMP" | sort -u > "$SUBS_TMP.f"
    mv "$SUBS_TMP.f" "$SUBS_TMP"
    if [ -s excludes.regex ]; then
        grep -v -E -f excludes.regex "$SUBS_TMP" | sort -u > "$SUBS_TMP.f"
        mv "$SUBS_TMP.f" "$SUBS_TMP"
    fi
fi

# ===== 2) dnsx resolves the survivors =====
# 2026-10-03: the dnsx path is resolved through resolve_tools.sh (env override > the conf injection > the default)
DNSX_BIN=$(resolve_tool dnsx "$HOME/.pdtm/go/bin/dnsx")
[ -x "$DNSX_BIN" ] || { echo "[-] 缺少 dnsx: $DNSX_BIN" >&2; exit 1; }
echo "[*] dnsx -> alive.txt ..."
if [ -s "$SUBS_TMP" ]; then
    "$DNSX_BIN" -rl 100 -t 150 -retry 2 -o "$ALIVE" -r "$RESOLVERS_CSV" < "$SUBS_TMP"
else
    : > "$ALIVE"
fi

LINES=$(wc -l < "$ALIVE")
echo "$(t main.scan.alive_filtered LINES=$LINES)"

# ===== the cadence gate: it decides whether the alterx derivation runs this round =====
# Inside the period (it should be skipped) -> ALTERX_OUT stays empty and stage 4
# automatically takes the elif "$ALIVE" >> "$DNSX_OUT" fallback,
# so the downstream scanner still sees the subfinder known subdomains, it just does
# not see the alterx variants.
ALTERX_RUN_THIS_TURN=0
if python3 alterx_runs.py should-run \
        --db "$DB" --business-id "$BUSINESS_ID" \
        --cadence-days "$ALTERX_CADENCE_DAYS" \
        ${FORCE_ALTERX:+--force}; then
    ALTERX_RUN_THIS_TURN=1
    echo "$(t main.scan.alterx_gate_run)"
else
    echo "$(t main.scan.alterx_gate_skip ALTERX_CADENCE_DAYS=${ALTERX_CADENCE_DAYS})"
fi

# ===== 3) alterx generates the candidates (split by base, to avoid the cross-base
# word pattern pollution + a per-base adaptive -limit) =====
# A base can be a subdomain (such as *.api.example.com -> api.example.com), and
# partition-alive buckets by the
# longest match, and each bucket runs two rounds of alterx on its own (with /
# without -enrich), and the output is appended to ALTERX_OUT.
# Without the bucketing: alterx -enrich learns the word patterns from the mixed
# bases, and the word patterns of base A such as api/dev/v2
# get applied to base B -> a lot of the out-of-scope candidates are caught by the
# grep on targets.regex, but dnsx
# has already wasted the effort; and the -limit budget cannot be split by size
# between the bases.
#
# The cap on the candidate count: alterx -limit only truncates the output, it does
# not stop the internal pattern generation. With a huge input the internal
# generation can still produce millions of candidates and hang, even with a small
# -limit. The defence: it estimates first with -es, and when it is over
# ALTERX_MAX_EST (1 million by default) it halves it at random with shuf and
# re-estimates, until it is <= the threshold. The -enrich and the
# non -enrich branch estimate separately (the word lists differ, so the candidate
# counts differ).
echo "$(t main.scan.alterx_enrich)"
if [ "$ALTERX_RUN_THIS_TURN" = "1" ] && [ "$LINES" -gt 0 ]; then
    ALTERX_TMPDIR=$(mktemp -d)
    BASES_CSV=$(printf '%s' "$BASES" | tr '\n' ',' | sed 's/,$//')
    python3 target_glob.py partition-alive \
        --input "$ALIVE" --bases "$BASES_CSV" --output-dir "$ALTERX_TMPDIR" \
        || { echo "[-] partition-alive 失败" >&2; exit 1; }

    # the helper that estimates the alterx candidate count and halves it. The -es
    # output goes to stderr:
    #   [INF] Estimated Payloads (including duplicates): <N>
    ALTERX_MAX_EST=${ALTERX_MAX_EST:-1000000}

    alterx_estimate() {
        # $1=the input file, $2=the enrich flag ("-enrich" or empty); it echoes
        # the candidate count and falls back to 0 on a failure.
        # Note: alterx -silent also silences the [INF] estimate line, so it must not
        # be used here.
        cat "$1" | alterx -es ${2:-} 2>&1 \
            | awk '/Estimated Payloads \(including duplicates\)/{print $NF; exit}'
    }

    alterx_prepare_input() {
        # $1=the input file, $2=the enrich flag; it echoes the input path whose
        # count is <= ALTERX_MAX_EST.
        # The returned path is either the original file or
        # ALTERX_TMPDIR/halved.<rand> (the trap removes the whole tmpdir).
        local orig="$1" enrich_flag="$2"
        local cur="$orig" attempts=0 est
        while :; do
            est=$(alterx_estimate "$cur" "$enrich_flag")
            est=${est:-0}
            if [ "$est" -le "$ALTERX_MAX_EST" ]; then break; fi
            # the halving: half < lines is what works (with lines=1, half=1, which equals lines, so there is no progress)
            local lines half halved
            lines=$(wc -l < "$cur")
            half=$(( (lines + 1) / 2 ))
            if [ "$half" -lt 1 ] || [ "$half" -ge "$lines" ]; then
                echo "$(t main.scan.alterx_est_still_high enrich_flag=${enrich_flag:-no-enrich} lines=$lines est=$est)" >&2
                break
            fi
            halved="$ALTERX_TMPDIR/halved.$$.$RANDOM"
            if ! shuf -n "$half" "$cur" > "$halved" 2>/dev/null; then
                echo "$(t main.scan.alterx_shuf_failed)" >&2
                rm -f "$halved"
                break
            fi
            attempts=$((attempts + 1))
            if [ "$cur" != "$orig" ]; then rm -f "$cur"; fi
            cur="$halved"
        done
        if [ "$attempts" -gt 0 ]; then
            echo "$(t main.scan.alterx_halved enrich_flag=${enrich_flag:-no-enrich} attempts=$attempts est=$est orig=$orig)" >&2
        fi
        echo "$cur"
    }

    while IFS= read -r base; do
        [ -z "$base" ] && continue
        partition="$ALTERX_TMPDIR/alive.base.$base"
        [ -s "$partition" ] || continue
        BLINES=$(wc -l < "$partition")
        [ "$BLINES" -gt 0 ] || continue
        # each branch prepares its own input (the estimate + the halving when it is
        # needed), the halved artifact is already removed inside the function,
        # and when the returned path equals the original partition there is nothing to clean up
        in_enrich=$(alterx_prepare_input "$partition" "-enrich")
        cat "$in_enrich" | alterx -silent -limit $(( BLINES * 20 )) -enrich -silent >> "$ALTERX_OUT"
        in_plain=$(alterx_prepare_input "$partition" "")
        cat "$in_plain" | alterx -silent -limit $(( BLINES * 10 ))            -silent >> "$ALTERX_OUT"
    done <<< "$BASES"
fi

# ===== 3.25) the deduplication: the exclusion of the alterx derivations that collide with the subfinder known subdomains =====
if [ "$ALTERX_RUN_THIS_TURN" = "1" ] && [ -s "$ALTERX_OUT" ] && [ -s "$ALIVE" ]; then
    comm -23 <(sort -u "$ALTERX_OUT") <(sort -u "$ALIVE") > "${ALTERX_OUT}.try"
    mv "${ALTERX_OUT}.try" "$ALTERX_OUT"
fi

# ===== 3.5) the second filter: after alterx, grep target + grep -v exclude (the important one) =====
if [ "$ALTERX_RUN_THIS_TURN" = "1" ] && [ -s "$ALTERX_OUT" ]; then
    grep -E -f targets.regex "$ALTERX_OUT" > "${ALTERX_OUT}.try"
    mv "${ALTERX_OUT}.try" "$ALTERX_OUT"
    if [ -s excludes.regex ]; then
        grep -v -E -f excludes.regex "$ALTERX_OUT" > "${ALTERX_OUT}.try"
        mv "${ALTERX_OUT}.try" "$ALTERX_OUT"
    fi
fi

# ===== 3.7) the permutation_state filter (skip the already resolved / the cache that has not expired / the wildcard hits) =====
echo "[*] permutation cache filter ..."
if [ "$ALTERX_RUN_THIS_TURN" = "1" ] && [ -s "$ALTERX_OUT" ]; then
    python3 permutation_cache.py filter \
        --db "$DB" --business-id "$BUSINESS_ID" --wordlist-hash "$WORDLIST_HASH" \
        < "$ALTERX_OUT" > "${ALTERX_OUT}.try"
    mv "${ALTERX_OUT}.try" "$ALTERX_OUT"
fi

# ===== 4) resolve the candidates (the alterx derivations + the subfinder known
# subdomains, they all go to dnsx) =====
# Note: ALIVE is the dnsx result of the SUBS_TMP that the glob patterns export; the
# exact subdomains already went through the early fast path
# ===== 3.8) the shuffledns brute (the dictionary brute force, with a business size aware sampling, in parallel with alterx) =====
# The minimal implementation: the Top + Rolling modes share the sample size, and the 30 day cadence lines up with alterx
# brute computes its own base: target_glob.py all-bases runs on $TARGET (both the exact and the glob ones are supported)
echo "$(t main.scan.brute_gate)"
BRUTE_RUN_THIS_TURN=0
if [ -n "${SKIP_BRUTE:-}" ]; then
    echo "$(t main.scan.brute_skipped_env)"
elif python3 brute_runs.py should-run \
        --db "$DB" --business-id "$BUSINESS_ID" \
        --cadence-days "${BRUTE_CADENCE_DAYS:-30}" \
        ${FORCE_BRUTE:+--force}; then
    BRUTE_RUN_THIS_TURN=1
    echo "$(t main.scan.brute_gate_run)"
else
    echo "$(t main.scan.brute_gate_skip BRUTE_CADENCE_DAYS=${BRUTE_CADENCE_DAYS:-30})"
fi

if [ "$BRUTE_RUN_THIS_TURN" = "1" ] && [ -s "$TARGET" ]; then
    SHUFFLEDNS_BIN="${SHUFFLEDNS_BIN:-$HOME/.pdtm/go/bin/shuffledns}"
    [ -x "$SHUFFLEDNS_BIN" ] || SHUFFLEDNS_BIN=$(command -v shuffledns || true)

    if [ ! -x "$SHUFFLEDNS_BIN" ]; then
        echo "$(t main.scan.shuffledns_missing)"
    else
        # 0) brute computes its own base (from $TARGET, both the exact and the glob ones are supported)
        # shuffledns -d does not accept a newline, so it is converted to CSV (the same handling as the dnsx RESOLVERS_CSV)
        BRUTE_BASES=$(python3 target_glob.py all-bases --input "$TARGET" 2>/dev/null | tr "\n" "," | sed "s/,$//" || true)
        if [ -z "$BRUTE_BASES" ]; then
            echo "$(t main.scan.brute_no_base TARGET=$TARGET)"
        else
            BRUTE_TMPDIR=$(mktemp -d)
            BRUTE_MODE="${BRUTE_MODE:-top}"
            # the chain that looks up the wordlist path (three segments, whoever exists wins):
            #   1. $wordlists_dir/<file>      <- config/pdtm.conf provides it (written by install_wordlists)
            #   2. <module dir>/wordlists/<file> <- the default of the source checkout (a normal user on this machine)
            #   3. /opt/srcradar/wordlists/<file> <- the docker / root global path
            #   4. /usr/share/srcradar/wordlists/<file> <- the fallback (the old docker image)
            BRUTE_MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
            BRUTE_WL_DIR="${wordlists_dir:-$BRUTE_MODULE_DIR/wordlists}"
            case "$BRUTE_MODE" in
                rolling)
                    BRUTE_WL_BASE="dns-Jhaddix-clean.txt"
                    ;;
                top|*)
                    BRUTE_WL_BASE="subdomains-top1million-20000.txt"
                    ;;
            esac
            BASE_WL=""
            for cand in "$BRUTE_WL_DIR/$BRUTE_WL_BASE" \
                        "/opt/srcradar/wordlists/$BRUTE_WL_BASE" \
                        "/usr/share/srcradar/wordlists/$BRUTE_WL_BASE"; do
                if [ -f "$cand" ]; then
                    BASE_WL="$cand"
                    break
                fi
            done

            if [ ! -f "$BASE_WL" ]; then
                echo "$(t main.scan.wordlist_missing BASE_WL=$BASE_WL)"
            else
                BRUTE_BASE_COUNT=$(echo "$BRUTE_BASES" | wc -l)
                echo "$(t main.scan.brute_base_count BRUTE_BASE_COUNT=$BRUTE_BASE_COUNT BRUTE_MODE=$BRUTE_MODE)"

                # the wordlist preparation
                BRUTE_WORDLIST="$BRUTE_TMPDIR/brute_wordlist.txt"
                awk -F. '{for(i=1;i<NF;i++) print $i}' "$ALTERX_OUT" 2>/dev/null \
                    | sort -u > "$BRUTE_TMPDIR/alterx_words.txt"
                cat "$BASE_WL" "$BRUTE_TMPDIR/alterx_words.txt" | sort -u > "$BRUTE_WORDLIST"

                # the business size aware sampling
                if [ "$LINES" -lt 20 ]; then
                    SAMPLE_SIZE=1000
                elif [ "$LINES" -lt 100 ]; then
                    SAMPLE_SIZE=5000
                elif [ "$LINES" -lt 500 ]; then
                    SAMPLE_SIZE=20000
                elif [ "$LINES" -lt 5000 ]; then
                    SAMPLE_SIZE=50000
                else
                    SAMPLE_SIZE=100000
                fi

                # the adaptive halving
                BASE_COUNT=$BRUTE_BASE_COUNT
                BRUTE_MAX_CANDIDATES="${BRUTE_MAX_CANDIDATES:-1000000}"
                attempts=0
                while [ $(( BASE_COUNT * SAMPLE_SIZE )) -gt "$BRUTE_MAX_CANDIDATES" ] && [ "$attempts" -lt 5 ]; do
                    SAMPLE_SIZE=$(( (SAMPLE_SIZE + 1) / 2 ))
                    BASE_COUNT_LIMIT=$(( BRUTE_MAX_CANDIDATES / SAMPLE_SIZE ))
                    if [ "$BASE_COUNT" -gt "$BASE_COUNT_LIMIT" ]; then
                        echo "$BRUTE_BASES" | head -n "$BASE_COUNT_LIMIT" > "$BRUTE_TMPDIR/bases.limited"
                        BRUTE_BASES=$(cat "$BRUTE_TMPDIR/bases.limited")
                        BASE_COUNT=$BASE_COUNT_LIMIT
                    fi
                    attempts=$((attempts + 1))
                done
                [ "$attempts" -gt 0 ] && echo "[brute] 自适应减半 $attempts 次, SAMPLE_SIZE=$SAMPLE_SIZE"

                # the deterministic sampling (it avoids picking all the numeric tokens
                # in the dictionary order)
                # the hash of the wordlist content is used as the -R seed, so the same wordlist -> the same sample
                WL_LINES=$(wc -l < "$BRUTE_WORDLIST")
                if [ "$WL_LINES" -gt "$SAMPLE_SIZE" ]; then
                    echo "$(t main.scan.wordlist_sampled WL_LINES=$WL_LINES SAMPLE_SIZE=$SAMPLE_SIZE)"
                    WL_SEED=$(sha256sum "$BRUTE_WORDLIST" | cut -c1-8)
                    # sort -R uses a stable seed; --random-source=/dev/urandom + head -n
                    # combined give a stable sample (the same WL -> the same sample, a different WL -> a different sample)
                    { sort -R --random-source=<(yes "$WL_SEED" | head -c 4096) "$BRUTE_WORDLIST" || true; } \
                        | head -n "$SAMPLE_SIZE" > "${BRUTE_WORDLIST}.sampled"
                    mv "${BRUTE_WORDLIST}.sampled" "$BRUTE_WORDLIST"
                fi

                BRUTE_WORDLIST_HASH=$(sha256sum "$BRUTE_WORDLIST" | cut -c1-12)

                BRUTE_OUT="brute.txt"
                : > "$BRUTE_OUT"
                "$SHUFFLEDNS_BIN" \
                    -mode bruteforce \
                    -d "$BRUTE_BASES" \
                    -ad \
                    -w "$BRUTE_WORDLIST" \
                    -r "$resolvers_file" \
                    -sw -wt 250 \
                    -t 100 \
                    -batch-size 200000 \
                    -silent \
                    -o "$BRUTE_OUT" || true

                # the gate + the cache filter
                if [ -s "$BRUTE_OUT" ]; then
                    grep -E -f targets.regex "$BRUTE_OUT" > "${BRUTE_OUT}.try" && mv "${BRUTE_OUT}.try" "$BRUTE_OUT"
                    [ -s excludes.regex ] && grep -v -E -f excludes.regex "$BRUTE_OUT" > "${BRUTE_OUT}.try" && mv "${BRUTE_OUT}.try" "$BRUTE_OUT"
                    python3 permutation_cache.py filter \
                        --db "$DB" --business-id "$BUSINESS_ID" --wordlist-hash "$BRUTE_WORDLIST_HASH" \
                        < "$BRUTE_OUT" > "${BRUTE_OUT}.try" && mv "${BRUTE_OUT}.try" "$BRUTE_OUT"
                fi
            fi
        fi
    fi
fi

# ===== 3.9) merge ALTERX_OUT + BRUTE_OUT (deduplicated) =====
BRUTE_OUT="${BRUTE_OUT:-brute.txt}"
if [ -s "$BRUTE_OUT" ]; then
    cat "$ALTERX_OUT" "$BRUTE_OUT" 2>/dev/null | sort -u > "${DNSX_OUT}.try"
    mv "${DNSX_OUT}.try" "$DNSX_OUT"
fi

# they are written into DNSX_OUT. Here -o becomes an append, to make sure the glob
# flow and the exact subdomains are merged (the deduplication is done by the sort -u
# before 4.5).
echo "[*] dnsx on candidates ..."
if [ -s "$ALTERX_OUT" ]; then
    sort -u "$ALTERX_OUT" "$ALIVE" \
        | "$DNSX_BIN" -rl 100 -t 150 -retry 2 -r "$RESOLVERS_CSV" >> "$DNSX_OUT" || true
elif [ -s "$ALIVE" ]; then
    cat "$ALIVE" >> "$DNSX_OUT" || true
fi
# merge and deduplicate (the duplicates from the same source + the entries of the exact subdomains that already exist)
if [ -s "$DNSX_OUT" ]; then
    sort -u "$DNSX_OUT" -o "$DNSX_OUT"
fi

# ===== 4.5) the third filter: after dnsx (the final gate) =====
if [ -s "$DNSX_OUT" ]; then
    grep -E -f targets.regex "$DNSX_OUT" > "${DNSX_OUT}.try"
    mv "${DNSX_OUT}.try" "$DNSX_OUT"
    if [ -s excludes.regex ]; then
        grep -v -E -f excludes.regex "$DNSX_OUT" > "${DNSX_OUT}.try"
        mv "${DNSX_OUT}.try" "$DNSX_OUT"
    fi
fi

# ===== 5) the permutation_state write back (the alterx candidates against the dnsx
# results; alterx_runs is marked in the same transaction) =====
# Even when ALTERX_OUT was emptied by the filter of stage 3.7 (every candidate is
# already cached), record still has to be called once so
# that alterx_runs is marked in the same transaction - otherwise FORCE_ALTERX does
# not reset the cadence clock.
echo "[*] permutation cache record ..."
if [ "$ALTERX_RUN_THIS_TURN" = "1" ]; then
    python3 permutation_cache.py record \
        --db "$DB" --business-id "$BUSINESS_ID" --wordlist-hash "$WORDLIST_HASH" \
        --candidates "$ALTERX_OUT" --resolved "$DNSX_OUT" || true
fi

# brute_runs is marked separately (it does not share the transaction with alterx_runs, because the brute uses its own wordlist_hash)
if [ "$BRUTE_RUN_THIS_TURN" = "1" ]; then
    BRUTE_OUT="${BRUTE_OUT:-brute.txt}"
    BRUTE_CANDIDATES=$( [ -s "$BRUTE_OUT" ] && wc -l < "$BRUTE_OUT" || echo 0 )
    python3 brute_runs.py mark \
        --db "$DB" --business-id "$BUSINESS_ID" \
        --wordlist-hash "${BRUTE_WORDLIST_HASH:-brute-default}" \
        --candidates "$BRUTE_CANDIDATES" --resolved 0 || true
fi

# ===== 6) the wildcard domains: only subfinder, deduplicated by base and appended =====
# The original form `cat wildcard.txt | subfinder` fed the literal domain `a-b.com`
# to subfinder for `a-*.b.com`, so the glob semantics were not applied at all.
# It now extracts the base first and then deduplicates, so `a-*.b.com` / `d-*.b.com`
# both extract `b.com` -> subfinder only queries it once; afterwards the grep on
# the target regex filters out the subdomains that match their own glob
# (a-foo.b.com / d-bar.b.com), and the semantics are exactly the ones of stage 1
# of scan.sh.
if [ -s wildcard.txt ]; then
    echo "$(t main.scan.wildcard_subfinder)"
    WILDCARD_BASES=$(python3 target_glob.py all-bases --input wildcard.txt || true)
    if [ -n "$WILDCARD_BASES" ]; then
        echo "$WILDCARD_BASES" | subfinder -silent -r "$resolvers_file" | \
            grep -E -f targets.regex | \
            grep -v -E -f excludes.regex \
            >> "$DNSX_OUT" || true
        echo "$(t main.scan.wildcard_appended)"
    else
        echo "$(t main.scan.wildcard_no_base)"
    fi
else
    echo "$(t main.scan.no_wildcard_file)"
fi

# deduplicate the final result
if [ -s "$DNSX_OUT" ]; then
    sort -u "$DNSX_OUT" -o "$DNSX_OUT"
fi

echo "[+] done -> $DNSX_OUT ($(wc -l < "$DNSX_OUT") 条)"

# the pipeline of the mapping of the assets of pdtm

> English | [中文](README_zh.md)

A system of the mapping of the assets that orchestrates the tool chain of ProjectDiscovery: the detection of the wildcard resolution → the enumeration and the derivation of the subdomains → the judgement of the CDN + the scan of the ports + the probe of the web → the result goes into the SQLite.

> **The dependencies**: the tools that this module calls (`dnsx` / `httpx` / `naabu` / `subfinder` / `alterx` and the like) **do not come with this repository**, the user installs them into the PATH on their own (see the §V The quick start of the main README).

## The content of the directory

- [The architecture](#the-architecture)
- [The quick start](#the-quick-start)
- [The description of the four stages](#the-description-of-the-four-stages)
- [The flow of the data and the artefacts](#the-flow-of-the-data-and-the-artefacts)
- [The DB schema](#the-db-schema)
- [The cleaning of the intermediate artefacts](#the-cleaning-of-the-intermediate-artefacts)
- [The appendix A: the bug of the cache of alterx and its fix](#the-appendix-a-the-bug-of-the-cache-of-alterx-and-its-fix)
- [The appendix B: the three levels of the solutions that "find a new asset almost every run"](#the-appendix-b-the-three-levels-of-the-solutions-that-find-a-new-asset-almost-every-run)
- [The appendix C: the bug of the chain of the fusion of the stages 5/6 of the scanner](#the-appendix-c-the-bug-of-the-chain-of-the-fusion-of-the-stages-56-of-the-scanner)
- [The appendix D: the leftovers of scan_results and the pollution between the businesses](#the-appendix-d-the-leftovers-of-scan_results-and-the-pollution-between-the-businesses)

## The architecture

```
target.txt ──► check_wildcard.sh ──► wildcard.txt / no_wildcard.txt
                                            │
no_wildcard.txt ──► scan.sh ──► dnsx_output.txt
                       │              (subfinder → dnsx → alterx → dnsx)
                       │              + the cache of permutation_state
                       │
dnsx_output.txt ──► scanner.sh ──► scan_results/
                                    (the judgement of the CDN → naabu → httpx)
                                    │
scan_results/ ──► import_scan_results.py ──► ../db/recon.sqlite3
                                                (web + tcp + scope)
```

`pipeline.sh` schedules the scripts above in series, it supports the input `-i`, the storage by the name of the business, the skip of any stage, the dry-run and the cleaning on its own.

## The quick start

```bash
# prepare the input directory
mkdir -p input
cat > input/target.txt <<'EOF'
*.scanme.sh
cc-*.scanme.sh
demo.scanme.sh
EOF
# optional: any glob form that holds a *, see the "The syntax of target.txt / exclude.txt"
cat > input/exclude.txt <<'EOF'
*.internal.scanme.sh
EOF

# run the pipeline
./pipeline.sh -b mybusiness -i ./input/

# or read the scopes from the DB that already exists (the business has to be in the table businesses)
./pipeline.sh -b mybusiness

# skip a stage
./pipeline.sh -b mybusiness -i ./input/ --skip-check-wildcard
./pipeline.sh -b mybusiness -i ./input/ --dry-run-import
./pipeline.sh -b mybusiness -i ./input/ --keep-on-fail   # it keeps the scene on a failure
```

## The description of the four stages

### The stage 1 — `check_wildcard.sh`

dnsx probes whether the glob domains have the wildcard resolution on. For a pattern that holds a `*` (such as `*.example.com` /
`cc-*.example.com` / `aaa.*.bbb.com`), it replaces the `*` with a random label and it probes 3 times;
as soon as one of them resolves an IP, it is judged to be a wildcard. An exact single subdomain without a `*` does not need the detection of the wildcard.

- it outputs `wildcard.txt` (the globs of the wildcard) / `no_wildcard.txt` (the globs that are not a wildcard + the exact single subdomains)
- only the domains that are not a wildcard go to the next stage

### The syntax of target.txt / exclude.txt

`target.txt` and `exclude.txt` share the same glob syntax. One rule per line, a line that starts with
`#` is a comment, and the case does not matter; `target` is a measurable asset, and `exclude` is an asset that is not measurable.

| The way of writing it | The semantics | An example of a match |
|---|---|---|
| `*.example.com` | the wildcard of a whole domain (any segment, the subdomains of several levels included) | it hits `a.example.com`, `a.b.example.com`; it does not hit `example.com` (the apex), `notexample.com` |
| `cc-*.example.com` | the wildcard of a prefix | it hits `cc-api.example.com`, `sub.cc-x.example.com`; it does not hit `cc.example.com`, `ccfoo.example.com` |
| `aaa.*.bbb.com` | the wildcard of a middle segment | it hits `aaa.x.bbb.com`, `aaa.x.y.bbb.com`; it does not hit `aaa.bbb.com` (the empty wildcard), `aaax.bbb.com` |
| `ccc.bbb.com` | an exact single subdomain (no `*`) | it hits `ccc.bbb.com` and its subdomains; it does not hit `notccc.bbb.com` |

**The rule of the anchoring**: every pattern is compiled into the ERE `(^|\.)pattern$`, and it avoids that `*.example.com` hits
`notexample.com` by mistake. Under the semantics of a glob, the `*` matches any character (the `.` included), and it is the ERE `.*` that is equivalent.

**The extraction of the base**: subfinder is called by base (it runs once per base), and the base is
the run of the labels of the pattern on the right that does not hold a `*`:

- `*.example.com` → the base `example.com`
- `cc-*.example.com` → the base `example.com`
- `aaa.*.bbb.com` → the base `bbb.com`
- `ccc.bbb.com` → the base `ccc.bbb.com` (itself)

**The three gates of the grep** (inside scan.sh, they make sure that an asset that is not measurable never reaches the database):

1. after the output of subfinder: first `grep -E -f targets.regex`, then `grep -v -E -f excludes.regex`
2. after the output of alterx: the same two greps
3. after the output of dnsx: the same two greps (the last gate)

The early version of `exclude.txt` had a branch of the "match of a substring of a keyword" (`honey` / `kw:uat` and the like),
and it was **completely deleted**, and every line of an exclude is handled as a glob. Write a complete pattern with the dots
(`test\.example\.com` and the like) when a semantics of a substring is needed.

### The migration of the scope glob

After the upgrade of 2026-07-30, every row of a scope (the column `scopes.asset`) has to hold the glob form.
The old data (a bare domain name such as `example.com`) gets the prefix `*.` in one go with `migrate_scope_glob.py`:

```bash
# the preview of the dry-run
python3 migrate_scope_glob.py --db ../db/recon.sqlite3
# it really writes to the database
python3 migrate_scope_glob.py --db ../db/recon.sqlite3 --apply
```

### The stage 2 — `scan.sh`

```
no_wildcard.txt
    └─► subfinder -d <root>  (the passive enumeration)
        └─► dnsx  (the resolution of the records A)
            └─► ALIVE_FILTERED  (it is filtered with exclude.txt)
                ├─► alterx -enrich -limit N×20  (the enriched derivation)
                └─► alterx       -limit N×10  (the basic derivation)
                └─► the deduplication: the items of ALTERX_OUT that have the same name as the ones of ALIVE_FILTERED are dropped (see the appendix A)
                └─► ALTERX_OUT ∪ ALIVE_FILTERED → dnsx
                                          └─► the write back of the cache of permutation_state (only ALTERX_OUT)
```

**The gate of the rhythm of alterx** (the top of the stage 3 of `scan.sh`, `alterx_runs.py should-run`):
- by default alterx runs **once every 30 days** (`ALTERX_CADENCE_DAYS=30`), and inside the period the stages 3/3.25/3.5/3.7/5 are skipped as a whole,
  the ALTERX_OUT stays empty, and the stage 4 automatically takes the fallback of `cat "$ALIVE" >> "$DNSX_OUT"`, so that the scanner downstream still sees
  the known subdomains of subfinder, it only does not see the variants that alterx derives. During the period `permutation_state` is neither read nor written (it is frozen).
- `ALTERX_CADENCE_DAYS=0` is the same as turning the rhythm off, and alterx runs every time.
- `FORCE_ALTERX=1` forces the run of this round and ignores the rhythm (it fits the moment when two businesses merge / when an acquisition happens).
- The point of the rhythm is tracked on its own by the table `alterx_runs` (one row of `last_ran_at` per business), it does not depend on `permutation_state`, and it removes completely
  the problem of "the old cache cuts the new candidates" that the §VIII #13 describes.



The policy of a single record of `permutation_state` (the TTL of a single record, it is orthogonal to the gate of the rhythm above):
- `resolved` / `wildcard_hit` — it is never tried again
- `nxdomain` — a cooling of 30 days, it is tried again when it expires
- the hash of the word list changes — it forces a retry

> **It is merged into this solution**: the TODO of the "wipe of the whole table by `PERM_CACHE_WIPE_DAYS`" that used to hang on the §VIII #13 is replaced by this gate of the rhythm —
> inside the days of the rhythm the whole chain of alterx neither reads nor writes `permutation_state`, and it is equivalent to "the whole table is neither read nor written"; the regeneration of the day of the rhythm
> is a whole new round, and it does not depend on the "stale filter" of the old cache.

### The stage 3 — `scanner.sh`

```
dnsx_output.txt
    └─► the stage 1: the cleaning + the resolution of the DNS (A/AAAA/CNAME/NS)
    └─► the stage 2: the double judgement of the domains and of the IPs of cdncheck → the split of the CDN / of the non CDN
    └─► the stage 3: the filtering of the LB (>15 domains/IPs)
    └─► the stage 3.5: the IPs of the CDN and of the LB go through the httpx (with the Host header)
    └─► the stage 4: the passive discovery of the ports with naabu
    └─► the stage 4.5: the active scan of naabu (-top-ports 100)
    └─► the stage 5: the active probe of the httpx + the fingerprints
    └─► the stage 6: the mapping of the Host header
    └─► the stage 7: the fusion of the ports (the passive + the active + the httpx + the CDN/LB)
    └─► the stage 8: the confirmation that the web is alive (json)
    └─► the stage 9: the split of the web ports against the pure TCP ports
```

It outputs `scan_results/`:
- `non_cdn_web_summary.json` — the details of the web
- `cdn_lb_web_summary.json` — the result of the probe of the CDN / of the LB
- `non_cdn_tcp_ports.txt` — the list of the brute force of the pure TCP
- `tmp_domain_ip_pairs.txt` — the mapping of the IP → the domain
- `non_cdn_list.txt` — the domains that are not a CDN

### The stage 4 — `import_scan_results.py` + `finalize_scope`

`import_scan_results.py` parses `scan_results/` and it stores it:
- `web_subdomains` (deduplicated by response_hash) + `web_hashes` (the library of the fingerprints)
- `tcp_assets` (host:port + hosts)
- `scopes` (the measurable / the assets that are not measurable, with the mark of is_wildcard)
- a record that already exists with `is_active=0` is inserted again, and a new discovery is activated by itself

The `finalize_scope` (at the end of the pipeline) upserts the scopes once more explicitly, so that **the scope is already in the database before the input files are deleted**.

## The flow of the data and the artefacts

| The file | The source | The persistence |
|------|------|--------|
| `target.txt`, `exclude.txt` | the input `-i` / the DB | it is deleted after the pipeline succeeds (when this script generated it) |
| `wildcard.txt`, `no_wildcard.txt` | check_wildcard | it is deleted |
| `alive.txt`, `alterx.txt`, `dnsx_output.txt` | scan | it is deleted |
| `filter.regex`, `*.tmp`, `*.bak` | the temporary intermediates | it is deleted |
| `scan_results/` | scanner | it is deleted after the import succeeds |
| `../db/recon.sqlite3` | import + finalize_scope | it is kept (the shared DB) |

## DB Schema

The place of the database: `../db/recon.sqlite3` (the `db/` next to this directory, the table `businesses` is shared with the other projects)

| The table | What it holds | The key fields |
|----|------|---------|
| `businesses` *(shared)* | the dictionary of the businesses | `id`, `business_name` (this project does not CREATE / ALTER it on its own) |
| `scopes` | the measurable / the assets that are not measurable | `business_id`, `scope_name`, `asset`, `is_wildcard` |
| `web_hashes` | the library of the web fingerprints | `business_id`, `response_hash`, `subdomain_count` |
| `web_subdomains` | the web assets | `hash_id`, `subdomain`, `port`, `url`, `status_code`, `title`, `technologies` |
| `tcp_assets` | the assets of the TCP ports | `business_id`, `host`, `port`, `hosts`, `is_active` |
| `permutation_state` | the cache of the state of the derivation of alterx | the composite primary key `business_id+base_domain+permutation`, `status`, `wordlist_hash`, `next_attempt_at`, `attempts` |
| `alterx_runs` | the gate of the rhythm of alterx (one row per business) | the PK `business_id`, `last_ran_at` (the ISO of the UTC), `wordlist_hash`, `candidates`, `resolved` |

The `bootstrap_db` of the `pipeline.sh` only checks that the table `businesses` exists and it does an `INSERT OR IGNORE` of the row of the business; it does not create or change the shared tables on its own.

## The cleaning of the intermediate artefacts

The `trap cleanup_input EXIT` cleans when the pipeline exits:

- by default: **it cleans on the success and on the failure as well** (apart from `--keep-on-fail`)
- it cleans as well with `--skip-import` (the user explicitly does not want the data)
- what it cleans: the `target.txt`/`exclude.txt` (only when this script generated them) + every intermediate file of `*.txt`/`*.tmp`/`*.bak` + the `scan_results/`

The order of the cleaning guarantees: **import → finalize_scope → the deletion of the files**. The scope is necessarily in the database before the input files are deleted.

### The detection of the leftovers before the run (`pipeline.sh:94-106`)

To prevent the `scan_results/` of the last run from being read wrongly by the import of this one (see the appendix D), the pipeline **refuses a non empty `scan_results/` on its own** after the parsing of the arguments and before any stage starts:

```bash
if [ -d "$SCAN_DIR" ] && [ -n "$(ls -A "$SCAN_DIR" 2>/dev/null)" ]; then
    echo "[-] $(t main.pipeline.leftover_detected SCAN_DIR=$SCAN_DIR)" >&2
    ls -la "$SCAN_DIR/" >&2
    ...
    exit 2
fi
```

The case that triggers it (the typical one): the last run used `--debug 1` (the default) or a manual `./pipeline.sh` did not pass `--debug 0`, and the process exited abnormally so that the `trap cleanup_input EXIT` did not run → the leftovers reach the next cron.

What to do when this exit code (`2`) shows up:

```bash
# look at what the leftovers are
ls -la scan_results/
# they are the artefacts of this business of the last time, clean them right away
rm -rf scan_results/ && mkdir -p scan_results/
# run it again
./pipeline.sh -b <business name>
```

## The appendix A: the bug of the cache of alterx and its fix

### The symptom

The `dnsx_output.txt` of the second run is incomplete, and all the subdomains that subfinder had found are lost.

### The root cause

The stage 3 of `scan.sh` merges the 3 outputs into the `ALTERX_OUT`:
1. `alterx -enrich -limit N×20`
2. `alterx -limit N×10`
3. `cat ALIVE_FILTERED >> ALTERX_OUT` ← **the known subdomains**

The 3rd way mixes the real subdomains that subfinder found into the candidates of alterx → the `permutation_cache.record` writes them into the `permutation_state` (`status=resolved`, `next_at=NULL`) → the filter of the second run hits them all and skips them → the known subdomains disappear for good.

### The fix

```diff
 # the stage 3 of scan.sh: it only generates the derivation of alterx, it does not mix the known subdomains any more
 cat "$ALIVE_FILTERED" | alterx ... >> "$ALTERX_OUT"   # -enrich N×20
 cat "$ALIVE_FILTERED" | alterx ... >> "$ALTERX_OUT"   # the basic N×10
-cat "$ALIVE_FILTERED" >> "$ALTERX_OUT"                 # it is deleted

 # the stage 3.25 of scan.sh (it is new): the items of the derivation of alterx that have the same name as the known subdomains are dropped
+if [ -s "$ALTERX_OUT" ] && [ -s "$ALIVE_FILTERED" ]; then
+  comm -23 <(sort -u "$ALTERX_OUT") <(sort -u "$ALIVE_FILTERED") > "${ALTERX_OUT}.try"
+  mv "${ALTERX_OUT}.try" "$ALTERX_OUT"
+fi

 # the stage 4 of scan.sh: the derivation + the known subdomains go to dnsx together
-sort -u "$ALTERX_OUT" | dnsx ... -o "$DNSX_OUT"
+sort -u "$ALTERX_OUT" "$ALIVE_FILTERED" | dnsx ... -o "$DNSX_OUT"
```

The `permutation_cache.py` does not change — the table `permutation_state` lives up to its name, it only stores the state of the "derivations"; the real subdomains are fed again to dnsx by subfinder on every run.

#### Why a deduplication is needed

alterx is a tool that **generates by rules**: the root domain + a word list → it builds the prefixes / the suffixes / the insertions out of the tokens of the word list. The default word list holds the usual tokens such as `demo`/`dev`/`test`/`admin`, so the derivation of `scanme.sh` **already** produces `demo.scanme.sh` with the algorithm alone — and it happens to have the same name as a subdomain that subfinder had found.

- before the fix (`cat ALIVE_FILTERED >> ALTERX_OUT`): the 5 known subdomains of 5 go into the cache, and the second run loses them all
- only the explicit cat is deleted and there is no deduplication: the 4 known subdomains of 5 do not go into the cache any more, but `demo.scanme.sh` (the collision of the algorithm of alterx) still goes into the cache, and it is still frozen by the logic of "resolved is never tried again"
- after the deduplication: the 0 known subdomains of 5 go into the cache, and the `permutation_state` is clean

These items of the "collision" still reach the dnsx through the `sort -u ALTERX_OUT ALIVE_FILTERED | dnsx` of the stage 4 (the real subdomains are not cached but they are resolved as usual), so **the business side loses nothing**.

### The verification

After the `permutation_state` was emptied, 2 runs in a row (with the 5 known subdomains of scanme.sh):

| | Run 1 | Run 2 |
|---|---|---|
| the total of the `permutation_state` | 149 | 287 (an increment of 138, the second run has new candidates) |
| the hits of the known subdomains | **0** ✓ | **0** ✓ |
| `dnsx_output.txt` | 5 rows (the real subdomains) | 5 rows |
| the leftovers of the working directory | none | none |

- The growth of the number of the `permutation_state` shows that the capacity of the derivation of alterx is not affected by the deduplication
- The known subdomains are fed into the `dnsx_output.txt` by subfinder every time, and the scanner downstream always sees them
- The number of the queries of dnsx does not change (the set is the same before and after the merge)

## The appendix B: the three levels of the solutions that "find a new asset almost every run"

**The root problem**: the word list of alterx is fixed → the output is determined → the hash of the word list is stable → the cache hits everything → the second run has no new candidate to try.

The three independent solutions stack the best:

### A. The word list of the timestamps (it has to be done, it is the most important)

The word list of alterx is generated dynamically on every run, and the hash of the word list follows by itself → the cache expires naturally with the hash of the word list.

```bash
gen_wordlist() {
    cat "${WORDLIST:-wordlists/alterx-base.txt}" 2>/dev/null
    date +%Y           # 2026
    date +%V           # 26 (the ISO week)
    date +%Y%V         # 202626
    date +%Y-%m        # 2026-07
    echo "q$(($(date +%m)/4+1))"   # the quarter q3
    printf 'canary\nblue\ngreen\nv2\nv3\nstaging\nshadow\n'
}
```

The candidates hold the subdomains that change every week, such as `prod-2026.example.com` / `w26.example.com` / `green.example.com`. The `wordlist_hash` is different on every run, and the logic of the expiry of `permutation_cache.py` lets the stale items be tried again by itself.

**The cost of the change: 1 function + one line in scan.sh, `alterx -w <(gen_wordlist)`. The effect is immediate.**

### B. The sampling of the probabilistic revival (it catches the subdomains that "die and come back")

The DNS records change often: today it is an nxdomain, and next week it is suddenly registered. The current cooling of 30 days misses these revivals directly.

The `permutation_cache.filter_permutations` gains a branch of the sampling of the revival:

```python
# in the filter, add
stale = [p for p, (s, n, h) in cached.items()
         if s == "nxdomain" and n and parse_iso(n) < now_ts]
import random
sample = set(random.sample(stale, max(1, len(stale) * 7 // 100)))
for perm in sample:
    kept.append(perm)
```

Every run resolves ~7% more of the "dead" items, and a subdomain that came back is caught in 1-2 weeks. The cost of the change: ~5 lines.

### C. Several passive sources (the simplest but it works)

The stage 1 of `scan.sh` only uses subfinder today. There are new registrations on the internet every day, and the subdomains show up by themselves:

```bash
{
  for root in $ROOTS; do
    subfinder -d "$root" -silent
    amass enum -passive -d "$root" 2>/dev/null
    curl -s "https://crt.sh/?q=%25.${root}&output=json" 2>/dev/null \
        | python3 -c 'import json,sys
                      [print(n) for x in json.load(sys.stdin) if x.get("name_value") for n in x["name_value"].split("\n")]' 2>/dev/null
  done
} | sort -u > "$SUBS_TMP"
```

The new seeds reach the ALIVE_FILTERED → alterx derives around the new seeds again → a second discovery. The cost of the change: medium (a tool / the network is added).

### The recommended combination

| The solution | The cost of the change | The gain of the new assets | What it fits |
|-----|---------|----------|------|
| **A. The word list of the timestamps** | small | ★★★★★ | it is general, it has to be done |

## The appendix C: the bug of the chain of the fusion of the stages 5/6 of the scanner

### The symptom

The `scanner.sh` ran with its default arguments (without `-all`, that is with the collection of the information of the TCP ports turned off), and in the `scan_results/`:
- the `raw_httpx_active_ips.txt` holds the hits of httpx
- the `raw_mapped_ports.txt` is **empty**
- the `non_cdn_all_ports.txt` is **empty**
- the `non_cdn_web_summary.json` is **empty**

The `import_scan_results.py` reports `没有找到可入库的 Web 或 TCP 结果` and it exits.

With `-all` (the TCP on) every value is normal, because the IP:port of naabu goes straight into the fusion, and it bypasses the chain that has the problem.

### The root cause

The stage 5 httpx collects the fingerprints with `-sc -cl -title -hash mmh3`, and the format of the output is:
```
http://128.199.158.128 [200] [2] [-2088429648]
```
—— the URL is followed by 4 bracket fields that are separated by spaces.

The logic of the parsing of the stage 6 mapper (`scanner.sh:463-489`):
```bash
clean=$(echo "$line" | sed 's|https\?://||g' | sed 's|/.*||g')   # → "IP [200] [2] [hash]"
ip=$(echo "$clean" | cut -d':' -f1)                               # ← the error is here
```

The `cut -d':' -f1` needs the line to hold no colon so that it can extract the host part of the `host:port` correctly. A line with a bracket holds no colon, and **the whole line is eaten as the $ip**.

Then the `grep "^<the dirty string> " FILE_IP` does not find anything in the `tmp_domain_ip_pairs.txt` (the format is `IP domain`), and **the whole match is silently dropped**.

That is a **potential bug** — when the TCP was on by default, the naabu of the stages 4/4.5 produced the `IP:port` and it went into the fusion through its own mapping of the IP → the domain of `raw_naabu_active_mapped.txt`, and it bypassed the stage 6 mapper completely; the fallback of naabu masked the fragility of the stage 6 mapper all along. Only when the TCP was turned off by default did the chain of the fusion depend on the stages 5 → 6 → 7 for the first time at 100%, and the bug showed up.

### The fix

The call of httpx of the stage 5 drops the 4 fields of the fingerprint that turn the output into `URL [bracketed fields]`. The fingerprint is still filled in by the `-j -title -td -hash` of the stage 8 at the stage of the confirmation that the web is alive, and the content that goes into the database does not change.

```diff
  log "[*] the stage 5: the active probe of the httpx..."

 httpx -l "$OUTPUT_DIR/pure_ips_to_scan.txt" \
-      -p "$COMMON_WEB_PORTS" \
-      -rate-limit 50 \
-      -timeout 5 \
-      -sc -cl -title \
-      -hash mmh3 \
+      -p "$COMMON_WEB_PORTS" \
+      -rate-limit 50 \
+      -timeout 5 \
+      -no-color -silent \
       -o "$OUTPUT_DIR/raw_httpx_active_ips.txt"
```

The output becomes a clean `http://IP` / `https://IP` (the default port is left out, and there is no bracketed field). The `cut -d':' -f1` of the stage 6 mapper extracts the host right away.

The stage 6 mapper is still fragile on its own (it depends on the prefix `https?://` + on a line without a space). If the stage 5 ever gets the fields `-title -td` back without losing the data, the stage 6 has to become a JSON parsing (see the section of the "to do" of the appendix C).

### The verification

The run of `./scanner.sh` with its default arguments and a `dnsx_output.txt = scanme.sh [A] [128.199.158.128]`:

| | Before the change | After the change |
|---|---|---|
| the hits of the httpx of the stage 5 | 2 | 2 |
| the number of the ports of the fusion of the stage 7 | **0** | **2** |
| the number of the web assets of the stage 8 | **0** | **2** |
| the number of the pure TCP ports of the stage 9 | 0 | 0 (the TCP is off by default, it is expected) |
| the content of `non_cdn_all_ports.txt` | _empty_ | `scanme.sh:80\nscanme.sh:443` |
| `non_cdn_web_summary.json` | _empty_ | 2 rows of `host=scanme.sh` `status_code=200` |

The run of `./scanner.sh -all` gives the same numbers as the default arguments (the TCP is on → the fusion also gets the ports of naabu), and it proves that the path with the TCP was not hurt.

### The to do / the future improvements

- **The fault tolerance of the stage 6 mapper**: it parses the output of httpx with the bracketed fields, and the stage 5 gets the fields of the fingerprint such as `-title -td -hash` back. The raw file of the stage 5 does not hold a fingerprint today, and the stage 8 is the fallback; if the format of the stage 8 ever changes, the gap of the fingerprint shows up.
- **The stages 5/6 through the JSON**: the stage 5 gets `-j` and it outputs the JSONL, and the stage 6 reads the field `port` and it produces the `domain:port`. The port is a structured field, and the format of the output of httpx can change again without breaking it.



| B. The sampling of the revival | small | ★★ | the business has a lot of temporary subdomains |
| C. Several sources of seeds | medium | ★★★★ | the continuous discovery of the assets |

**The advice: A has to be done, B is optional, C depends on the time**. A takes effect at once with a single call, and together with the 2 lines of the modification of the appendix A ("the real ones do not go into permutation_state") it makes the whole solution of this pipeline that "finds a new asset almost every run".

## The appendix D: the leftovers of scan_results and the pollution between the businesses

### The symptom

After the run of the business A, the `web_subdomains` of the business A in the DB hold the subdomains of the business B (such as ExampleCo that holds `*.vendor-cdn-app.com`).

The signs to watch:
- The polluted subdomains all hold a `host_ip` that points to a shared IP of a CDN / of a CloudFront
- Their `response_hash` (such as the one of a generic 404 page, `mmh3 = -1840324437`) exists on its own in the `web_hashes` of the business A and of the business B (the `UNIQUE (business_id, response_hash)` allows the same hash to exist across the businesses)
- The polluted rows have `is_active = 0` (they detach by themselves after the next normal run), but **the DB does not delete them**, and they pile up
- The timestamps of the `first_seen` are concentrated on the import stage of one run (the `fetched_at` as well)

### The root cause

The list of the files that the `import_scan_results.py` reads (`import_scan_results.py:277-282`):

```python
paths = [
    scan_dir / "non_cdn_web_summary.json",
    scan_dir / "cdn_lb_web_summary.json",   # ← the current scanner.sh does not write it
    scan_dir / "non_cdn_web_summary.txt",   # ← the current scanner.sh does not write it
    scan_dir / "cdn_lb_web_summary.txt",
    scan_dir / "raw_httpx_active_ips.json", # ← the current scanner.sh writes the .txt
]
```

What the current version of `scanner.sh` really produces:

| The file | Where it is written |
|---|---|
| `non_cdn_web_summary.json` | the truncation `>` of the httpx at `scanner.sh:558` |
| `cdn_lb_web_summary.txt` | the truncation `>` of the httpx at `scanner.sh:322/329/334` |
| `non_cdn_web_summary.txt` | **it is not written** (only the .json is) |
| `cdn_lb_web_summary.json` | **it is not written** (only the .txt is) |
| `raw_httpx_active_ips.txt` | the `-o` of the httpx at `scanner.sh:466` |
| `raw_httpx_active_ips.json` | **it is not written** (the .txt is written) |

**The 3 files that the import reads and that the current scanner.sh does not write**. When those files stay in the `scan_results/` because the last run used `--debug 1` and did not clean them, the next import takes them as new results — the `business_id` comes from the argument `pipeline.sh -b` of the moment, but **the hosts inside them are the ones that another business scanned the last time**.

An old version of scanner.sh (`_deprecated_scanners/scanner.sh.bak`) did write `raw_httpx_active_ips.json`; the list of the `paths` of the old import kept the name of that file, and the import did not follow when the scanner renamed it to `.txt`. **The two sides that are not strictly aligned** are the source of the pollution.

### The chain of the reproduction

1. Somebody runs the pipeline of the business B by hand (the `KEEP_OUTPUT=1` by default / or the old version of scanner.sh wrote the `.json`)
2. the `scan_results/` keeps the `cdn_lb_web_summary.json` with the result of the probe of the `*.vendor-cdn-app.com`
3. the cron of the business A starts the pipeline.sh: the scanner.sh only does a `mkdir -p` and not a `rm -rf`, and the current version does not write the 3 files of the `.json`
4. the import sees that the 3 files are still there → it parses them → it inserts them with `business_id=ExampleCo` → several hundreds of `*.vendor-cdn-app.com` reach the `web_subdomains` of the business A
5. every `first_seen` gets the same timestamp, and the pollution takes shape

### The fix

**The cure (the detection before the run of the pipeline.sh)**: see the section of the "detection of the leftovers before the run" above. The `pipeline.sh:94-106` checks right after the parsing of the arguments whether the `scan_results/` is empty, and when it is not it quits with `exit 2` and it refuses to run, and it forces a manual confirmation / a cleaning.

**The aid (the alignment of the import and of the scanner)**: either the scanner.sh explicitly truncates those 3 files (`: > "$OUTPUT_DIR/cdn_lb_web_summary.json"` and the like), or the names of the files that are not produced any more are removed from the list of the `paths` of the `import_scan_results.py` (the `.backup/20260803_123155/import_scan_results.py:279` still holds `cdn_lb_web_summary.json`, and it is the same hazard).

**The cleaning of the rows that are already polluted** (a one time SQL):

```sql
BEGIN;
-- the deletion of the polluted rows of web_subdomain
DELETE FROM web_subdomains
WHERE hash_id IN (SELECT id FROM web_hashes WHERE business_id = :victim_biz_id)
  AND subdomain LIKE '%the polluted keyword%';
-- the deletion of the rows of hash that became empty
DELETE FROM web_hashes
WHERE business_id = :victim_biz_id
  AND NOT EXISTS (SELECT 1 FROM web_subdomains WHERE hash_id = web_hashes.id);
COMMIT;
```

### The verification

After the fix, the run of two neighbouring businesses (such as `TestBiz` that leaves the KEEP_OUTPUT, and then `ExampleCo`):

| | Before the fix | After the fix |
|---|---|---|
| the start of `pipeline.sh -b ExampleCo` | it continues silently | `exit 2` + the hint `rm -rf scan_results/ && mkdir -p scan_results/` |
| the `*.vendor-cdn-app.com` of the `web_subdomains` of the business A | several hundreds (the pollution) | 0 |

### The to do / the future improvements

- **the scanner.sh explicitly truncates every file that is read in the list of the `paths`**: even when the scanner is replaced in the future, it does not depend on the "natural overwriting by the `>`". Add after the `mkdir -p "$OUTPUT_DIR"`:
  ```bash
  : > "$OUTPUT_DIR/cdn_lb_web_summary.json"
  : > "$OUTPUT_DIR/non_cdn_web_summary.txt"
  : > "$OUTPUT_DIR/raw_httpx_active_ips.json"
  ```
- **the list of the files of the import and of the scanner is strictly the same**: remove the names of the files that are not produced any more from the list of the `paths` of the import (`cdn_lb_web_summary.json` / `non_cdn_web_summary.txt` / `raw_httpx_active_ips.json`), or the other way round — make the scanner write the `.json` again. One of the two sides has to be decided once and for all.
- **the entry of the scanner.sh gets the same detection of the leftovers**: today only the entry of the `pipeline.sh` stops them. A direct `cd pdtm && ./scanner.sh` bypasses that gate, and it hits the same pollution.

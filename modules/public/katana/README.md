# modules/public/katana — the integration of katana (an optional plugin)

> English | [中文](README_zh.md)

> **The position of the module**: the plugin of the scan of the URL assets `katana`, **it is optional**.
> Besides [`modules/main/pdtm/scan_urls.py`](../../main/pdtm/scan_urls.py) (URLFinder / gau / ffuf),
> it offers a scan at the granularity of the URL whose fields are aligned with the ones of httpx, and it is the **default engine** of the `daily-url` stage (since 2026-09-22).

## The navigation

- **This document**: §the boundary of the module / §the safety of the dangerous routes / §the alignment of the fields / §the deployment / §the use by the client / §the operation / §the description of the files
- **The relation with scan_urls.py** (they coexist, they do not exclude each other):
  - URLFinder is still the engine that can be scheduled by hand (the extraction of the sensitive information of the Chinese / the adaptation to the Chinese sites)
  - katana takes the `daily-url` cron stage over (the fields are rich, they go straight into the pipeline of algo_diff)
  - Both of them write the table `web_hash_urls` (it already exists, `modules/main/db/schema.sql:233-262`), and they do not collide
- **The case where you skip it**: you do not need the richness of the fields of katana / you do not want to change the default engine → do not install it;
  `pdtm` / `daily` / `db_align` / `ymicp` still work on their own.

> **⚠️ Important statement**: this directory only holds the Python adaptation code that srcradar wrote, **it does not redistribute the katana binary**.
> katana is maintained on its own by [ProjectDiscovery](https://github.com/projectdiscovery/katana), under the MIT license.
> This module calls katana through a `subprocess`, it **does not link the Go source directly**, and it respects the boundary of the MIT.

---

## I. The boundary of the module

| The item | The scope | The license |
|---|---|---|
| the 7 files of this directory | the adaptation code that srcrador wrote | Apache-2.0 |
| the katana CLI binary | it is maintained on its own by ProjectDiscovery, self deployed | MIT |
| the URLFinder CLI (it already exists) | it is kept for the manual use / the fallback / the sensitive information of the Chinese | GPL (the NOTICE is there) |

**The duties of srcradar**:
- it calls katana with `-jsonl` → it parses → it stores into the table `web_hash_urls`
- **the double insurance of the interception of the dangerous routes** (the blacklist of the 5 words of URLFinder is kept, see §II)
- it aligns the fields with the ones of httpx (`urllib.parse` fills the host/port/path, `mmh3` computes the response_hash)

**What srcradar does not do**:
- it does not package the katana binary (`install.sh` fetches it with `go install`)
- it does not take the upgrades of katana over (the user upgrades it with `go install`)
- it does not maintain the issues of the upstream of katana

---

## II. The safety of the dangerous routes (a must read)

**The blacklist of the 5 words of URLFinder is kept** (`~/tools/URLFinder/config/config.go:19`):

```python
RISKS = ("remove", "delete", "insert", "update", "logout")
```

**The double insurance**:

1. **The interception at the source** (when `katana_enrich.sh` builds the command): `-crawl-out-scope` excludes every URL that holds one of the 5 words with a single regex
   ```bash
   katana ... -crawl-out-scope "/(remove|delete|insert|update|logout)/i"
   ```
2. **The gate before the storage** (`lib/risks.py:is_dangerous_url`): the JSONL output of katana goes through it once more, and the ones that hit it are marked
   `is_dangerous=1, danger_reason="疑似危险路由,已跳过验证"`, and they are **written** into the `web_hash_urls` instead of being dropped
   (it is aligned with the behaviour of URLFinder, so that the report of the SRC sees "which URLs were skipped").

> **The known flaw**: the match of a substring has false positives (a URL that holds a read-only query path such as `delete-check` is blocked too).
> That is the native flaw of URLFinder, this module inherits it; it does not introduce a smarter check here (the cost / the complexity is not worth it).

---

## III. The alignment of the fields (httpx ↔ katana)

| The field of httpx | The source | The field of katana | The strategy of this module |
|---|---|---|---|
| `host` | it comes with httpx | ❌ it is in `request.endpoint` | `urllib.parse.urlparse(endpoint).hostname` |
| `port` | it comes with httpx | ❌ | `urlparse().port` or 80/443 |
| `scheme` | it comes with httpx | ❌ | `urlparse().scheme` |
| `path` | it comes with httpx | ❌ | `urlparse().path` |
| `status_code` | ✅ | ✅ `response.status_code` | it is taken directly |
| `content_type` | ✅ | ⚠️ `response.headers["content-type"]` | the headers are the fallback |
| `content_length` | ✅ | ✅ | it is taken directly |
| `title` | ✅ | ⚠️ it is in the body | the regex `<title>(.*?)</title>` (the same as URLFinder) |
| `technologies` | httpx calls it `tech` | ✅ | the key becomes `tech` |
| `header` | ✅ | ✅ `response.headers` | it is taken directly |
| `body` | ✅ `-sr` | ✅ | it is taken directly (it can be turned off with `KATANA_OMIT_BODY=1`) |
| `web_hash_urls.body` (BLOB) | ❌ the old version has none | ✅ it is stored by default | the default `KATANA_OMIT_BODY=0` → it is stored with zlib(level=6); with `KATANA_OMIT_BODY=1` it stores NULL; it feeds the 8 steps of the algorithm of `url_diff` |
| `location` | ✅ | ⚠️ `headers["location"]` | the headers are the fallback |
| `response_hash` | ✅ mmh3 | ❌ | it **has to be computed**: `mmh3.hash128(body)` (algo_diff depends on it) |
| `time` | ✅ | ✅ the `timestamp` of the top level | it is taken directly |
| `input` | ✅ | ❌ | the `request.endpoint` of katana is used as the input |

**The key constraint**: the `response_hash` **has to be computed** — the 8 steps of the algorithm of algo_diff depend on it for the masking of the simhash / of the MMH3 field,
and without that field the record is dead for algo_diff.

---

## IV. The deployment

```bash
cd modules/public/katana
bash install.sh              # go install katana + write the config example + the self check
bash install.sh --check      # it only checks the dependencies
bash install.sh --uninstall  # it does not delete the binary (the go environment takes care of it), it only deletes the config + the cache
```

It needs: Go ≥ 1.22 (`go version` checks itself), Python ≥ 3.10, `bash` ≥ 4.

---

## V. The use by the client

### Calling the katana enrich by hand

```bash
# the equivalent of the srcradar dispatcher
./srcradar katana enrich -b ExampleCo

# or directly
python3 modules/public/katana/lib/katana_runner.py \
    --business ExampleCo \
    --db db/recon.sqlite3 \
    --hosts-file <(echo "example.com")
```

### The daily-url stage (the default engine)

```bash
# the default behaviour: it goes through katana
./srcradar daily daily_monitor -type daily-url

# it forces the URLFinder (the old behaviour, the fallback / the sensitive information of the Chinese)
DAILY_URL_ENGINE=urlfinder ./srcradar daily daily_monitor -type daily-url

# auto: when katana fails it falls back on URLFinder (best-effort)
DAILY_URL_ENGINE=auto ./srcradar daily daily_monitor -type daily-url
```

### The manual button of the dashboard

`127.0.0.1:8765/<biz>/katana-enrich` — it is at the same level as the existing `scan-urls` button.

---

## VI. The operation

| The symptom | The troubleshooting |
|---|---|
| `install.sh --check` reports `缺 katana` | `go install github.com/projectdiscovery/katana/v2/cmd/katana@latest` |
| the daily-url ran but the web_hash_urls did not grow | check whether the table `web_subdomain_scan_schedule` has a row with `enabled=1` |
| an `is_dangerous=1` hits a read-only URL | tighten the `lib/risks.py:RISKS` (5 words by default) |
| katana starts slowly | put a limit on it with `-crawl-duration 300 -timeout 10` |
| the response_hash is empty everywhere | the body was turned off by `-ob`, make sure that `KATANA_OMIT_BODY=1` is not set |
| the `web_hash_urls.body` is NULL everywhere | it is the same root cause as the empty response_hash; the stage of `url_diff` skips the rows without a body (the old paths of ffuf / urlfinder / gau keep the body=NULL, as the user decided) |

---

## VII. The description of the files

| The file | The lines | The duty |
|---|---|---|
| `install.sh` | ~120 | it installs the katana binary + it writes the example conf + it checks itself |
| `katana_enrich.sh` | ~80 | the entry of the CLI (the stage of the daily-url calls it) |
| `config/katana.conf.example` | ~30 | a template of the placeholders (the operator fills the real values and saves it as `katana.conf`, the `.gitignore` hides it) |
| `lib/__init__.py` | ~1 | it is empty, it makes `lib/` a package |
| `lib/risks.py` | ~40 | the blacklist of the 5 words + `is_dangerous_url()` + the entry of the unit tests |
| `lib/katana_runner.py` | ~250 | the subprocess that calls katana + the parse of the JSONL + the gate of the risk |
| `lib/schema_map.py` | ~120 | the parameters of the INSERT into web_hash_urls from the JSON dict of katana (the alignment of the fields + the computation of the missing ones) |

# srcradar — the pipeline of the mapping and of the monitoring of the SRC assets

> English | [中文](README_zh.md)

> the tool chain of SRC that goes from the collection of the assets of a company to the active mapping, to the daily incremental monitoring and to the local visualisation

<p align="left">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <a href="./NOTICE"><img src="https://img.shields.io/badge/upstream-12%20projects-success.svg" alt="Upstream"></a>
  <img src="https://img.shields.io/badge/go-%3E=1.25-00ADD8.svg" alt="Go">
  <img src="https://img.shields.io/badge/python-%3E=3.12-3776AB.svg" alt="Python">
  <img src="https://img.shields.io/badge/information--collection-only-important.svg" alt="Info Collection Only">
</p>

<br>

<p align="center">
  <img src="./assets/dashboard-overview.png" alt="the overview of the srcradar dashboard" width="900">
</p>

<p align="center">
  <em>The overview of the dashboard (127.0.0.1:8765, the 7 core tabs: Task Status / Site Details / Port Services / Risk Level / Subsidiaries / Mini Programs / WeChat Accounts)</em>
</p>

<br>

**srcradar** is the pipeline of the mapping and of the monitoring of the SRC assets, all in one place: **the name of a business → the graph of its legal entities → the active mapping → the daily incremental diff → a local dashboard**. Its core parts:

| The role | The module | The nature |
|---|---|---|
| **The backbone** | `pdtm` (the orchestration of the 4 stages of the active mapping) + `daily` (the cron + the diff of the snapshots + the local dashboard) + `manage` (the entry of the businesses) | installed by default, it runs on its own |
| **An optional plugin** | `db_align` (enscan, the graph of the companies, it marks the LOCKED tag) + `ymicp` (the reverse lookup of the filing of the mini programs / of the WeChat accounts) | self deployed, self enabled |
| **The shared data** | a single SQLite (`db/recon.sqlite3`) connects every output |

The installation / the use / the operation of the optional plugins are in the README of each of them: [`db_align/README.md`](./modules/public/db_align/README.md) · [`ymicp/README.md`](./modules/public/ymicp/README.md). **It is for the authorized scenarios only** (see the prerequisites and the compliance below). This tool **only collects information**, it does not exploit anything.

---

## The prerequisites and the compliance

This tool is **for the users that hold a legal written authorization** only, and a compliant authorization includes, without being limited to:

- **An agreement of a SRC (a security response center)**
- **An authorization of a penetration test** (the contract / the PDF is kept)
- **A whitelist of the assets** (the assets of the company itself / a托管 agreement that was signed / the domains of a test)

srcradar only provides the **technical implementation**, it **does not take part in, does not endorse and does not know** any concrete use case.

**The operator carries the whole responsibility of the compliance.** It includes, without being limited to: the authorization of the target, the compliance of the cross border transfer of the data (the PIPL / the security assessment of the data that leaves the country), and the respect of the ToS of the data sources such as `爱企查/天眼查/七麦`.

The whole legal terms are in [`LICENSE`](./LICENSE) (Apache-2.0) + [`TERMS_ADDENDUM.md`](./TERMS_ADDENDUM.md) (the additional restrictions of the use and the disclaimer) + [`NOTICE`](./NOTICE) (the thanks to the upstream) + the section of the statement of [`ymicp/README.md`](./modules/public/ymicp/README.md) (the notice of the module ymicp alone).

> **The position of the tool**: srcradar is a tool of **information collection** (the enumeration of the subdomains / the resolution of the DNS / the probe of the ports / the probe of the HTTP / the scan of the URL assets), and it **does not exploit anything and does not trigger any PoC**. Use a dedicated vulnerability scanner when a vulnerability has to be verified.

---

## What it is

For the operation of a SRC (a security response center), it turns "the name of a business → the graph of its legal entities → the active mapping of the assets → the daily incremental monitoring" into a single pipeline:

- **The contract of the data**: every output settles in one shared SQLite (`db/recon.sqlite3`), isolated by the name of the business
- **The contract of the operation**: one round runs on its own every day at 03:00 Beijing time, it produces the incremental report + the local dashboard of the visualisation
- **The contract of the collaboration**: it does not take the place of the upstream (`ENScan_GO`), it does not take the place of the downstream (the other asset mapping tools), it only connects and orchestrates

---

## The quick start

```bash
# 0. install the system dependencies (Ubuntu/Debian): sqlite3 for the daily DB / cron for the scheduled runs;
#    go/python3/git and the like are guided by ./check.sh
sudo apt update
sudo apt install sqlite3 cron -y

# 1. pull the code (the public mirror)
git clone https://github.com/usdagfhjkda/srcradar.git
cd srcradar

# 2. check the environment (it only checks, it does not install): it needs go>=1.25, python3>=3.12, git>=2.0
./check.sh

# 3. install all the upstream dependencies (pdtm installs dnsx/httpx/subfinder/alterx/naabu/cdncheck
#    -> cdnmatch -> the init-db on its own;
#    main/{db,lib,manage,pdtm} are ticked by default, daily is not (it avoids the automatic registration of the cron);
#    db_align (enscan) and ymicp are guided by install.sh on their own, see their README):
./install.sh                       # the interactive checklist (enter switches / 0 confirms / q quits)
./install.sh --skip-check          # the environment is known to be good, it skips check.sh and goes straight to the checklist

#    The priority of the path of the DB (it is handled at the top of ./srcradar):
#      1) CLI --db / $RECON_DB env
#      2) the recon_db_path of config/db.conf (it is generated at the end of ./install.sh)
#      3) /opt/srcradar/db/recon.sqlite3 (it is fixed inside the docker image)
#      4) /data/recon.sqlite3 (it is compatible with the old mount)
#      5) the default of the modules (<repo>/modules/main/db/recon.sqlite3)

# 4. (optional) for the plugin of enscan, see [db_align/README.md](./modules/public/db_align/README.md) §installation
#    (it marks the LOCKED tag, it needs a manual authorization; the run half automatically steps in — the cookie heals itself / the cache is cleared,
#     see [db_align/README.md](./modules/public/db_align/README.md) §operation)

# 5. (optional) for the plugin of the reverse lookup of the filing of the mini programs, see [ymicp/README.md](./modules/public/ymicp/README.md) §deployment

# once it is installed, look at what the dispatcher can do (it lists every pair of module/script, it is the cheat sheet of the usual ones)
./srcradar --list

# 6. feed an exact single subdomain + register the business
#    add_business -i <dir> needs a target.txt in the directory;
#    it cleans the target.txt on its own after the success (see the known problem §14)
mkdir -p ~/scope
echo www.scanme.sh > ~/scope/target.txt
./srcradar manage add_business -n test -i ~/scope

# 7. run the pipeline of pdtm of that business (dnsx + scanner.sh + import)
#    the stages are gated by the bits of the table recon_business_config (see the daily operation):
#    the business of the test uses the default values web=1 / tcp=0 / icp=1, it really runs pdtm + icp
./srcradar daily run_one_business test

# 8. start the dashboard (127.0.0.1:8765 by default, only this machine can reach it; see §The startup with Docker for the docker / the remote access)
./srcradar daily dashboard
```

> **The convention of the entry points**: `check.sh` and `install.sh` are the entry points of the refactoring of 2026-09; the old `init.sh` only keeps the two standalone tool entries `--init-db` / `--check-schema` (see [the daily operation](#the-daily-operation)).

> **A new shell has to load the PATH**: `pdtm` and the tools of PD are installed into `~/go/bin/` and `~/.pdtm/go/bin/`, and they **do not** enter the PATH of the current shell on their own; a new shell has to `source ~/.bashrc` or `source ~/.zshrc` by hand (pick the one of your shell), or the script has to `export PATH="$PATH:$HOME/go/bin:$HOME/.pdtm/go/bin"` before `pdtm` / `dnsx` stop reporting that they are not found.

> **Where it is installed**: pdtm is installed into `~/go/bin/`; the tool chain of PD (dnsx/httpx/subfinder/alterx/naabu/cdncheck) is managed by pdtm in `~/.pdtm/go/bin/`; cdnmatch is in `pdtm/bin/`; the empty database `db/recon.sqlite3` is created at the end of install.sh. See the comment at the top of [`install.sh`](install.sh).

> **The requirements of the run**: the active scan of srcradar depends on the external tools that install.sh installs (`httpx`, `dnsx`, `naabu`, `subfinder`, `alterx`, `cdncheck`). These tools do not come with the repository, `./check.sh` + `./install.sh` have to run first. See the thanks to the upstream.

### Quick reference (`./srcradar` dispatcher)

There is no need to `cd modules/<x>/<y>` — the `./srcradar` of the root finds and runs the scripts of every module on its own:

```bash
./srcradar --list                    # it lists every pair of (module, script)
./srcradar manage add_business -n exampleCo    # the entry of a business
./srcradar manage set_config  -n exampleCo    # the change of the configuration
./srcradar db init_db                          # it creates an empty SQLite
./srcradar db check_schema                    # it checks the drift of the schema
./srcradar pdtm scan -b exampleCo             # the active scan of a single business
./srcradar daily daily_monitor                 # it runs the daily pipeline by hand
./srcradar ymicp icp_mapp_query -b exampleCo  # the reverse lookup of the filing
```

The order of the resolution of the modules is `modules/private` → `modules/main` → `modules/public` (an internal module overrides a public one); the type of the script `.sh` comes before `.py`, and a `.sh` file has to carry the executable bit. `./srcradar --list` prints the whole list of the scripts.

---

## The startup with Docker

> A single container runs the whole stack of srcradar: the database is persisted in a named volume, the scopes of the businesses are fed from a bind mount directory, and the dashboard is exposed on the loopback only by default (a command with `--host 0.0.0.0` is given below for an access from the same host).

```bash
# 1. load the local image (from srcradar-0.1.0.tar)
sudo docker load -i ~/srcradar-0.1.0.tar

# 2. start the container
#    -v ~/scope:/scope             the input directory of target.txt / exclude.txt
#    -v srcradar-db:/opt/srcradar/db   the persistence of the DB (a named volume, the DB stays when the container is removed)
#    -p 127.0.0.1:8765:8765        the dashboard is only reachable from this machine (it avoids an exposure to the public network)
sudo docker run -d --name srcradar \
  -v ~/scope:/scope \
  -v srcradar-db:/opt/srcradar/db \
  -p 127.0.0.1:8765:8765 \
  srcradar:0.1.0

# 3. enter the interactive shell of the container
sudo docker exec -it srcradar /bin/bash

# 4. list the scripts (it verifies the dispatcher)
srcradar --list

# 5. feed an exact single subdomain + register the business
echo www.scanme.sh > /scope/target.txt
srcradar manage add_business -n test -i /scope

# 6. run the pipeline of pdtm of that business (dnsx + scanner.sh + import)
srcradar daily run_one_business test

# 7. start the dashboard (use --host 0.0.0.0 for an access from the same host, you can leave it out if only you reach it)
srcradar daily dashboard --host 0.0.0.0
```

> **The surface of the exposed ports**: `-p 127.0.0.1:8765:8765` binds the loopback only, neither the public network nor the other machines of the intranet can reach it; to see the dashboard, `curl http://127.0.0.1:8765/` from the same host is enough, or go through a tunnel of the SSH from a remote host (`ssh -L 8765:127.0.0.1:8765 user@<srcradar-host>`).
>
> **The persistence of the database**: `srcradar-db` is a docker named volume, and after the container was removed with `docker rm` the next `docker run -v srcradar-db:/opt/srcradar/db` mounts the volume of the same name again, so the data of the businesses is not lost. To see the content of the database: `sudo docker exec srcradar python3 -c "import sqlite3; c=sqlite3.connect('/opt/srcradar/db/recon.sqlite3'); print(c.execute('SELECT * FROM web_subdomains').fetchall())"`.
>
> **The one time consumption of target.txt**: after the success of `add_business`, `/scope/target.txt` is cleaned up on its own (the known problem §14 of the README); to feed the scopes again, write `/scope/target.txt` again and run `add_business` again.

---

## The architecture

```
   ┌─────────────────┐
   │   business name │       e.g. "ExampleCo"
   └────────┬────────┘
            ▼
   ┌──────────────────────┐         ┌──────────────────────────┐
    │   db_align (Go)      │────────►│  ENScan_GO (the subprocess of the upstream) │
    │ resolver→crawler     │         │  爱企查/天眼查/七麦              │
   │       →store         │         └──────────────────────────┘
   └──────────┬───────────┘
              ▼
   ┌──────────────────────────────────────┐
    │  db/recon.sqlite3  (WAL, shared)        │
   │  businesses · companies · mapp_records│
   │  scopes · web_subdomains · tcp_assets│
   └──────────────┬───────────────────────┘
                  ▼
   ┌───────────────────────┐    ┌──────────────────────────┐
   │  pdtm (shell+py)      │    │  ymicp (Python)          │
    │  the 4 stages of the     │    │  the reverse lookup of the filing  │
    │  active mapping:         │    │  of the mini programs / of the    │
   │  dnsx→naabu→httpx     │    │                          │
    │  →import_scan_results │    │  ⭐ the user deploys it, srcradar │
    │                       │    │    only provides the client         │
   └──────────┬────────────┘    └──────────┬───────────────┘
              ▼                            ▼
   ┌─────────────────────────────────────────────────────────┐
   │                daily/  (cron + Python)                   │
   │  snapshot.py → diff.py → reports/<run-id>/ + dashboard  │
   └─────────────────────────────────────────────────────────┘
```

<br>

**The key invariants**

- The modules are decoupled **by the name of the business**, the 1:N of `businesses.id` → `companies.business_id` is the boundary of the isolation
- `db_align` does not write `web_subdomains` / `tcp_assets` / `permutation_state`, that is the ground of pdtm
- pdtm does not write `mapp_records`, that is the ground of `db_align` + ymicp
- `daily/` only reads the database and takes a snapshot, it never touches a table of a business

---

## The modules

| The module | The role | The details |
|---|---|---|
| `db_align/` | **the plugin of enscan (optional)**: the name of the business → the legal graph + the reverse lookup of the assets (a Go orchestrator) | [README](./modules/public/db_align/README.md) · [CLAUDE.md](./modules/public/db_align/CLAUDE.md) |
| `internal/resolver` | the scoring of the several candidates of AQC, the strict mode refuses a weak match | `MinAcceptScore=80`, a weak match needs the bypass of `-broad` or `-pid` |
| `internal/crawler` | the traversal of the tree of the holdings + the reverse lookup of the section of the assets | the guard of the cycles of `seen[pid]` in the process, the threshold of a holding is 51% by default |
| `internal/store` | the upsert of the SQLite + the incremental migration of the schema | it only adds `service_type_map` / 2 indexes / `companies.group` |
| `internal/permute` | the generation of the variants of the keywords + the conversion of the numbers ↔ the Chinese characters | it is covered by the unit tests |
| `pdtm/` | the orchestration of the 4 stages of the active mapping | [README](./modules/main/pdtm/README.md) |
| `pipeline.sh` | the orchestration of the top + the cleaning on its own | the mutual exclusion of `flock`, the scene of a failure can be kept on purpose |
| `scan.sh` | the derivation of the subdomains + the two ways of the exact search / of the glob | without a `*` it goes through the fast path straight to dnsx, with a `*` it goes through subfinder → alterx → permutation |
| `scanner.sh` | the resolution of the DNS + the CDN judgement + the scan of the ports | `cdnmatch` judges it offline and it takes the place of the blocking call of the old cdncheck; `httpx` / `dnsx` get `< /dev/null` so that they do not hang on the stdin |
| `cdnmatch/` | the Go wrapper (optional): the classification of the CDN / of the WAF / of the cloud | the match of the ranges offline, it has to be built, see the known problem §10 |
| the external dependencies | `httpx` / `dnsx` / `naabu` / `subfinder` / `alterx` and the like | the user installs them into the PATH (see the daily operation), srcradar only calls them |
| `bin/` | the directory of the build artefacts (cdnmatch is optional) | it does not come with the repository, the user builds it |
| `target_glob.py` | `target.txt` → ERE + the extraction of the base | `(^&#124;\.)` is compliant with the POSIX ERE (the silent 0 match of the original PCRE is fixed) |
| `import_scan_results.py` | `scan_results/` → the database | the deduplication by `response_hash` |
| `ymicp/` | **the plugin of the integration (optional)**: the batch reverse lookup of the filing of the mini programs / of the WeChat accounts | [README](./modules/public/ymicp/README.md) |
| `daily/` | the cron + the diff of the snapshots + the dashboard | [README](./modules/main/daily/README.md) |
| `daily_monitor.sh` | the entry point of the cron + the orchestration of the several stages + the filter by the config | the mutual exclusion of `flock`; the fixed order of the stages: `enscan → pdtm → icp`; every business is filtered by `recon_business_config` |
| `lib/snapshot.py` | the full snapshot of the 6 tables into JSON | it derives the `host_ip_map` that fills the IP column of the dashboard back |
| `lib/diff.py` | the comparison of the snapshots → the incremental report | added/reactivated/deactivated/changed/deleted |
| `lib/dashboard.py` | the read only web of 127.0.0.1, 4 tabs | Task Status/Site Details/Port Services/Risk Level |
| `ENScan_GO/` | the client of 爱企查 of a third party, it runs as a subprocess | the source is vendored + the xlsx of the `outs/` lands on the disk |
| `i18n/` | the language of the user-visible output (the zh and en tables) | [README](./modules/main/i18n/README.md) |
| `db/recon.sqlite3` | the shared data, every module writes to it | WAL + a busy_timeout of 5s, the concurrent reads are allowed |

---

## The model of the shared data

The database `db/recon.sqlite3`, the mode WAL + a busy_timeout of 5s.

| The table | What it holds | Who writes it |
|---|---|---|
| `businesses` | the dictionary of the SRC businesses (`id`, `business_name`) | anybody, it is unique by `business_name` |
| `recon_business_config` | the switches of the stages of a business (`business_id`, `enabled`, `web`, `tcp`, `icp`) | a manual SQL; `daily_monitor.sh` reads it and filters by business |
| `companies` | the legal entities (`business_id`, `unit_name`, `nature_name`, `main_licence`, `group`) | `db_align` + `ymicp` |
| `mapp_records` | the filings and the light assets such as the ICP / the mini programs / the App / the WeChat accounts (`company_id`, `service_licence`, `service_type`) | `db_align` + `ymicp` |
| `scopes` | the whitelist of the measurable and of the assets that are not measurable | **`db_align -scope`** / **`pdtm finalize_scope`** / **`manage/scope_import.sh`** (3 independent paths, any of them is enough) |
| `web_subdomains` / `web_hashes` | the web assets + the library of the fingerprints | `pdtm/import_scan_results.py` |
| `tcp_assets` | the assets of the TCP ports | `pdtm/import_scan_results.py` |
| `permutation_state` | the cache of the state of the derivation of alterx (a cooling of 30 days per entry for the moment; **it has to become a wipe of the whole table per period — see the known problem §13**) | `pdtm/permutation_cache.py` |
| `service_type_map` | the integer of `service_type` → the name that a human can read (it accumulates on its own) | an `INSERT OR IGNORE` when `db_align` upserts |

The contract of the collaboration of the fields is in the README of each module — `db_align/README.md` §Caveats / `pdtm/README.md` §DB Schema.

---

## The daily operation

> This section answers the question "what do I do once it is installed". The runbook of the entry point of the scripts is in the README of each module, here are only the most used ones.

The detailed documentation sits next to the scripts:

- **add a business / change the switch of a business / the seed TSV** → [`manage/README.md`](./modules/main/manage/README.md)
- **install the cron / remove the cron / run a single business by hand / take a snapshot / the dashboard** → [`daily/README.md`](./modules/main/daily/README.md)
- **enter the scopes / run the whole pipeline** → [`pdtm/README.md`](./modules/main/pdtm/README.md)
- **the flags of the reverse lookup of the legal entities (db_align)** → [`db_align/README.md`](./modules/public/db_align/README.md)

### The language of the output

The user-visible output (the `--help` of the tools, the runtime messages,
the pages of the dashboard, the daily report and the body of the mail)
follows one setting. The resolution order is:

1. the `SRCDAR_LANG` environment variable (for the CI and the temporary
   overrides)
2. the `lang` field of `config/lang.conf` (the persistent choice of the
   operator)
3. `en` as the default

`config/lang.conf` is written by the installer of the i18n module, and
it is never overwritten afterwards:

```bash
bash modules/main/i18n/install.sh --yes --lang zh   # Chinese
bash modules/main/i18n/install.sh --yes --lang en   # English
```

The dashboard also takes the language per request: `?lang=zh` or
`?lang=en` overrides the setting for that one page.

### The commands of the common tasks

> Every command runs from the root of the repository; `./srcradar --list` prints the whole list of the scripts.
> The stages (`pdtm` / `icp` / `enscan` / `daily-url`) are configured with a 0/1 per business in the table `recon_business_config`
> (web/tcp drive pdtm, icp drives ymicp, enscan is not gated). See the top of [`daily/run_one_business.sh`](./modules/main/daily/run_one_business.sh).

| The task | The command |
|---|---|
| create a business + fill the scopes | `./srcradar manage add_business -n <business name> -i <input_dir>` |
| only store the scopes without scanning (to fill the old scopes back) | `./srcradar pdtm scope_import -b <business> -i <input_dir>` |
| look at / change the switch of a business | `./srcradar manage set_config -n <business name> [--enable/--disable/--web 0\|1 --tcp 0\|1 --icp 0\|1]` |
| run the whole flow of a single business (once the scopes are in, it is gated by the table of the configuration) | `./srcradar daily run_one_business <business name>` |
| pull the tree of the holdings / the ICP / the mini programs by hand (the plugin of enscan) | see [`modules/public/db_align/README.md`](./modules/public/db_align/README.md) |
| run the reverse lookup of the filing of the mini programs (the plugin of ymicp) | see [`modules/public/ymicp/README.md`](./modules/public/ymicp/README.md) |
| install the cron (every day at 03:00 Beijing time, the cron line hardcodes `-type pdtm,icp`) | `./srcradar daily install_cron` |
| remove the cron | `./srcradar daily install_cron uninstall` |
| see whether the cron is installed | `crontab -l \| grep daily_monitor` |
| start the dashboard (127.0.0.1:8765 by default) | `./srcradar daily dashboard` |

### The order of the three usual stages (it is fixed inside the pipeline / inside run_one_business)

```
enscan(db_align)  →  pdtm  →  icp(ymicp)  →  daily-url
   ↓                  ↓         ↓                ↓
  writes companies /    writes web_sub /  writes mapp_records /   writes web_hash_urls
scopes            tcp_assets
```

A failure of a stage does not block the stages that come after — `run_one_business.sh` returns an **exit code of a bit mask**:

| The bit | What it means |
|---|---|
| 1 (1) | pdtm failed |
| 2 (2) | enscan failed |
| 4 (4) | icp failed |
| 8 (8) | daily-url failed |
| 0 | everything is OK or it was skipped |

### A business can skip the cron (the operator controls it in the background)

Every business has the switch `recon_business_config.enabled`. After a `--disable`, **every stage** of that business is skipped by the cron and by `run_one_business.sh`, but **the snapshot is still taken** (it watches the additions of the period when it did not run).

```bash
./srcradar manage set_config -n <business name> --disable   # pause it
./srcradar manage set_config -n <business name> --enable    # restore it
./srcradar manage set_config -n <business name>             # show the current configuration
```

The detailed semantics are in `daily/README.md`, in the sections "Skipping a business that should not run" and "Installing / removing the cron".

---

## The thanks to the upstream

The executable capacity of this project rests on the following upstream projects, see the `LICENSE` (Apache-2.0) at the top and the file [`NOTICE`](./NOTICE).

### The upstream of the code (srcradar calls it directly or depends on its source)

| The upstream project | The role | The license | The maintenance relation |
|---|---|---|---|
| **[mssky9527/ENScan_GO](https://github.com/mssky9527/ENScan_GO)** (the original repository [wgpsec/ENScan_GO](https://github.com/wgpsec/ENScan_GO) moved) | the collection of the information of the companies (爱企查/天眼查/七麦/风鸟) | **Apache-2.0** | © 2023-2026 keac @ wgpsec |
| **[projectdiscovery/httpx](https://github.com/projectdiscovery/httpx)** | the active probe of the web | **MIT** | © 2021-2025 ProjectDiscovery, Inc. |
| **[projectdiscovery/dnsx](https://github.com/projectdiscovery/dnsx)** | the batch resolution of the DNS | **MIT** | © 2021-2025 ProjectDiscovery, Inc. |
| **projectdiscovery/cdncheck** (it is imported by [`modules/main/pdtm/cdnmatch`](./modules/main/pdtm/cdnmatch/), the source is not modified) | the offline classification of the CDN / of the WAF | **MIT** | © 2021-2025 ProjectDiscovery, Inc. |
| **modernc.org/sqlite** (a dependency of `db_align`) | the driver of a pure Go SQLite | **BSD-3-Clause** | © modernc.org/sqlite authors |

### The management of the packages and the external dependencies (the set of the tools that pdtm installs + the tools that are recommended)

| The tool | The upstream | The license | The maintenance relation |
|---|---|---|---|
| **[pdtm](https://github.com/projectdiscovery/pdtm)** | the package manager (it installs the tool chain of PD) | **MIT** | © 2021-2025 ProjectDiscovery, Inc. |
| **[subfinder](https://github.com/projectdiscovery/subfinder)** | the enumeration of the subdomains | **MIT** | © 2021-2025 ProjectDiscovery, Inc. |
| **[alterx](https://github.com/projectdiscovery/alterx)** | the derivation of the keywords | **MIT** | © 2021-2025 ProjectDiscovery, Inc. |
| **[naabu](https://github.com/projectdiscovery/naabu)** | the scan of the TCP ports | **MIT** | © 2021-2025 ProjectDiscovery, Inc. |
| **[ffuf](https://github.com/ffuf/ffuf)** | the brute force of the URLs with a dictionary (`pdtm/scan_urls.py`) | **MIT** | © 2021 Joona Hoikkala |
| **[gau](https://github.com/lc/gau)** | the wayback / the historical URLs (`pdtm/scan_urls.py`) | **MIT** | © 2025 Corben Leo |
| **[URLFinder](https://github.com/pingc0y/URLFinder)** (the Chinese community edition by pingc0y) | the active crawler (`pdtm/scan_urls.py`) | **MIT** | © 2022 pingc0y |
| **[massdns](https://github.com/blechschmidt/massdns)** | the batch resolution of the DNS at a high performance (the stage of the brute force, a hard dependency of shuffledns) | ⚠️ **GPL-3.0** | © 2017 Marc Blechschmidt |
| **[shuffledns](https://github.com/projectdiscovery/shuffledns)** | the resolution of the DNS (the stage of the brute force) | ⚠️ **GPL-3.0** | © 2022 ProjectDiscovery, Inc. |

> **The mode of the integration**: every tool of this section is called through a `subprocess.run()` by a script such as `pdtm/scan_urls.py`, it is **not a fork of the source / an import**.
> `massdns` and `shuffledns` are the only two tools of **GPL-3.0** here: they run as separate processes and they are not linked with the code of this repository; the whole text of their licenses comes with the mirror in `/usr/share/doc/srcradar/licenses/`, and the whole statement is in [`NOTICE`](./NOTICE). `pdtm` is the package manager, it is **not** the upstream of the code of these tools.
>
> **The versions of the upstream**: ENScan_GO_TAG=v1.4.0 (see `install.sh`). The tools of ProjectDiscovery are installed into `~/.pdtm/go/bin/` by `pdtm -duc -i dnsx,httpx,subfinder,alterx,naabu,cdncheck`, there is no lock (the user manages the upgrades).

### The data that comes with the repository (the word lists)

| The data | The upstream | The license | The maintenance relation |
|---|---|---|---|
| **[danielmiessler/SecLists](https://github.com/danielmiessler/SecLists)** `Discovery/DNS/` (the original of `subdomains-top1million-20000.txt` + the cleaned product of `dns-Jhaddix.txt`) | the word list of the brute force of the DNS subdomains (the brute stage of `pdtm/scan.sh`) | **MIT** | © 2018 Daniel Miessler |

The word lists under `docker/wordlists/` **come with the repository**, they are different from the tools of the table above that "pdtm installs at the time of the run". The `dns-Jhaddix-clean.txt` is a cleaned derivative of the file of the upstream (the filtering + the deduplication + the sorting, the exact rules are in [`NOTICE`](./NOTICE)), and it is redistributed under the terms of the MIT. The whole statement is in [`NOTICE`](./NOTICE).

### The statement of the third party services (the user deploys them)

| The service | The provider | The license | The maintenance relation |
|---|---|---|---|
| **ymicp / ICP_Query** | [HG-ha / 一铭](https://github.com/HG-ha/ICP_Query) | ⚠️ **not declared** (on GitHub it is All rights reserved by default) | **not maintained by srcradar**, the README of the original project declares that it is for the learning and the exchange only |

ymicp is the third party service that the `ymicp/` module of srcradar depends on, srcradar **does not** redistribute the server, **does not** pull the image on its own and **does not** endorse its compliance. See the section of the statement of [`ymicp/README.md`](./modules/public/ymicp/README.md).

---

## The hard conventions of the operation

They come from [`modules/public/db_align/CLAUDE.md`](./modules/public/db_align/CLAUDE.md), and every module shares them:

1. **`ENScan_GO/config.yaml` must never be read** — it is the file of the credentials of the upstream, not even the name of one field may be echoed; the `.claudeignore` + `settings.json` intercept it as a fallback. Claude must not infer its content from the name of the file / from the list either
2. **When the cookie of a data source expires**:
   - the keywords: `未登录` / `登录已过期` / `cookie` / `Cookie expired` / `aqc|tyc|rb|qimai 401/403/empty result`
   - a single decision needs at least 2 signals that agree before it is accepted
   - a line is appended to `cookie.log`: `<timestamp> [<source>] <reason>; the last successful run: <the name of the log file>`
   - the later runs exclude the source on their own through the `-type`; **when every source expires → FATAL, it stops**
3. **The cache is cleared when ENScan fails**: `rm -f ../ENScan_GO/enscan.gob`, and only when it goes from "the last success → this failure" and there is no signal of a cookie
4. **The path of the database**: every `-db` / `--db` points to `../db/recon.sqlite3` by default, confirm it before writing; do not put `cookie.log` / `logs/*.log` under the version control
5. **The defence of the stdin of the tools of ProjectDiscovery**: any shell call of `dnsx / httpx / naabu / subfinder / cdncheck`, as soon as the upstream is a script of the kind of `pipeline.sh` / `scan.sh` / `scanner.sh` that is called from the outside by `bash script.sh`, has to get `< /dev/null`. **The tools of ProjectDiscovery close the stdin in a best-effort way, but it was measured that they park for good in a sub shell** (see the known problem §12.2). cdncheck still needs the `< /dev/null` as a fallback, even though `pdtm/scanner.sh` now goes through the offline path of `./bin/cdnmatch` and does not call the cdncheck binary any more.

---

## The summary of the project

After the several rounds of dry-run and of the smoke test that started on 2026-07-24, this pipeline reached a usable state on the **ExampleCo** business.

### The state of the data (2026-08-02)

| The dimension | The number | The remark |
|---|---|---|
| `businesses` | 2 | `ExampleCo` / `DemoCorp` (both of them have scopes) |
| `companies` | 31 | the manual grouping of ExampleCo (2/4/10/5) + the 10 new ones of DemoCorp |
| `mapp_records` | 95 | there are still 2 `service_type` (4/7), (the follow up work is on the list of the priorities) |
| `scopes` | 8 | 6 measurable / 2 that are not measurable, all of them with `is_wildcard=1` (they hit a wildcard resolution) |
| `web_subdomains` | 74,168 | it keeps growing, the ~215 rows of the noise of the single characters (the known problem §1) are still to be fixed |
| `tcp_assets` | 488 | |
| `permutation_state` | 40,592 | the cache of the derivation of alterx, it exploded from 172 after 2026-07-28 (the data is normal) |
| the size of `recon.sqlite3` | 87 MB | the mode WAL |

### The highlights of the design

1. **The resolver refuses the silent errors** — `MinAcceptScore=80`, a weak match (50/60 points) errors out right away and asks for the bypass of `-broad` or `-pid`, so that the SRC does not bind to the parent company of the group
2. **The restraint of the collaboration of the schema** — it only adds `service_type_map` / 2 indexes / `companies.group`; the other tables are read only, so that the merge into a bigger platform does not collide
3. **The patch of the synthetic licence of mapp_records** — the sections of the APP / of WeChat / of Weibo have no number of the ICP filing, so `synth:<company_id>:<service_type>:<service_name>` stands in; it is deterministic → an upsert that runs again does not duplicate it; the logical identity stays `(company_id, service_name, service_type)`
4. **The fix of "the real subdomain does not go into the permutation_state" of the cache of the derivation of alterx** — after the deduplication of `comm -23`, the real subdomains that subfinder found are fed in again on every run, and the cache only keeps the real derived candidates (it was verified with 5 known subdomains + an increment of 149→287)
5. **The orchestration of several stages that fails soft** — `run_one_business.sh` reports the result with a 3 bit bit-mask; the mutual exclusion of `flock`; when the before or the after of the snapshot fails → the `previous.json` is not rotated, and the next run still has a baseline
6. **The "local heuristics and no call out" of the dashboard** — the dictionary of the risk of the ports + the buckets of the density of the fingerprints, a red mark at ≥51 + the reordering that puts the Chinese first; it strictly binds 127.0.0.1 only, and it warns at the startup when it is changed to 0.0.0.0
7. **The clear definition of the fields of the snapshot / of the diff** — the timestamps (`fetched_at` / `last_seen` / `updated_at` and the like) do not count as a change; the logical key of `web_subdomains` is `(business_id, subdomain, port)` and not the `hash_id` that drifts
8. **The contract of the operation goes down to `CLAUDE.md`** — the rules of the expiry of the cookie / of the proxy / of the cache do not depend on the memory of the README, and every "it has to be done" is in one hard statement; when the `README.md` and the `CLAUDE.md` disagree, the CLAUDE.md wins

### The limits of the capacity (what is deliberately not done)

- **AQC does not go up** — `branch` is the only reliable signal that goes down; `invest` / `holds` are null most of the time; `partner` is a natural person. The run of `db_align -n DemoCorp` does not reach the sister subsidiaries of the group of DemoCorp on its own. That is a limit of AQC, not a bug of the tool (see `db_align/README.md` §Design notes)
- **The UNIQUE and the not null constraint of mapp_records.service_licence** — they are defined by the schema of ymicp, and db_align copes with them through the synthetic licence; in the end it is an upstream contract that cannot be changed
- **The `seen` of the tree of the crawler lives in the process** — restarting the runner starts again from the seeds, and it is expensive for the long batch tasks; as the README says, the seen should be persisted in the database in the future

---

## The known problems

They are sorted in three levels: "located / mitigated / not cured".

### 1. The subdomain of one character / the noise of the wildcard DNS ✅ cured (2026-08-25)

**The symptom of the past**: the tab "Site Details" of `daily/lib/dashboard.py` held N rows of the shape `http://a/` / `http://j/` / `http://0/`, and the URL could not be reached. The early root cause (the text output of httpx + the cut of the columns by awk + no check before the storage) piled three layers up:

1. The candidates of a permutation of one character were not dropped before the probe of the HTTP
2. During the probe of httpx the DNS was closed on the IP of the CDN by the wildcard, and the HTTP request landed on the server of the real domain
3. At the import only the prefix of the input `a` was kept as the `subdomain`, and the domain that was really hit was not written into the canonical field

**The early measurement**: 215 rows → 72 hashes → 6 IPs (all of them the closure of a wildcard CDN).

**The cure (2026-08-25)**:

- The calls of httpx of the stage 3.5 / of the stage 8 of `pdtm/scanner.sh` now use `-json` and they output JSONL (scanner.sh:328 / 583; the comment `FIX-2026-08-25`)
- `pdtm/import_scan_results.py::parse_json_web` takes the real host out of the field `url` of the JSON with `urlparse(...).hostname`, and it does not go through the cut of the columns of the text by awk any more
- `import_scan_results.py::HOSTNAME_RE = re.compile(r"^([a-zA-Z0-9][-a-zA-Z0-9]*\.)+[a-zA-Z]{2,}$")` (the line 29) checks the value before it is stored, and **a host of one character is refused right away by `not HOSTNAME_RE.match(h)`** (the line 806)

In the root cause of the three layers, the root cause 2 (the JSON output of httpx) is the key fix — the path of the JSON does not generate the fake subdomain of the kind of "the prefix of the input `a`" any more, and the entries of one character disappear by themselves. The root cause 1 (the stage of the permutation drops the single characters) and the root cause 3 (the wildcard redirect changes the canonical) still exist but **the root cause 2 covers them and they have no real impact**, and they are kept as the optional optimisations, see the appendix of `pdtm/README.md`.

**It is still there**: the `if "." in subdomain` of the display layer of `daily/lib/dashboard.py::_build_sites` (the line 831) stays as a defensive fallback; once the new data went through the check of HOSTNAME_RE, that filter does not trigger any more in practice.

**The measurement in production (2026-09)**: there is no leftover entry of one character in the `web_subdomains` of the production database (0 rows of `subdomain` without a `.`), and the filter of HOSTNAME_RE before the storage is in place.

### 2. The tidiness of the directory of ENScan_GO ✅ the public repository is cleaned (2026-09-06); the production deployment has to be verified by hand

After the layout migration of 2026-09-06 (the commit `2c13c60`), the **public repository** does not have `code.bak.*` nor the directory `outs/` under `modules/public/db_align/` any more, and only `cmd/ internal/ install.sh/ CLAUDE.md/ README.md/ go.mod/ go.sum` are left. **The surface of the risk of the public repository is gone**; if the output of enscan has to be generated again in the future, a `.gitignore` has to be built by hand (see the rules `outs/*.xlsx` and `*.bak.*` that `.gitignore` already has).

**The hint for the production deployment**: in a srcradar instance that was deployed before the layout migration, the old output of `outs/` (the xlsx, whose name holds the name of a company, the risk of a leak of the credentials) and the backup directories `*.bak.*` may **still be there** under `db_align/`. After the upgrade to v0.1.0 it is advised to run by hand:

```bash
rm -rf db_align/outs db_align/*.bak.*
```

**The posture of the risk**: this one is cured in the public repository; the production side has to be verified by hand when the instances are upgraded one by one.

### 3. service_type_map is incomplete 🔴 the comparison between the businesses is limited

The `service_type_map` only has 2 rows (`type_4`, `type_7`), so the comparison of the `mapp_records` between the businesses has no name that a human can read. The README admits that "the placeholder waits for the confirmation of the real `icpinfoAjax` mapping of AQC", and the whole mapping has to be completed before a production use (the typical values: 4=ICP, 7=the mini programs, 5=the App, 8=the WeChat accounts and the like).

### 4. The verification of a single business on a single point ⚠️ the scale is not verified

The table `businesses` only holds the row of a single business, and every design was only verified on a few dozens of companies / a few hundreds of filings:

- The mutual exclusion of `flock` when several businesses run at the same time, the concurrency of the ENScan subprocesses and the contention of the quota of AQC did not go through a stress test
- The time and the peak of the memory of the snapshot of the 6 tables and of the hundreds of thousands of rows of the whole database were not measured
- **The advice**: add a 2nd business (the simplest kind, the one of `scanme`) for a parallel verification

### 5. The fixes that pdtm already has are not merged 🟡 partly cured (2026-09)

The appendix A/B/C of `pdtm/README.md` gives three levels of the solutions that "find a new asset almost every run" + the fix of the bug of the chain of the fusion of the stages 5/6, the state of the implementation:

- **The appendix A**: the bug of the cache of alterx — **the filter was added** (the commit `ba581e6` / `1803252` of 2026-09, `scan.sh:309-326` drops the candidates that are already ALIVE with `comm -23 <(sort -u ALTERX_OUT) <(sort -u ALIVE)`). But alterx still does `cat in_enrich | alterx >> ALTERX_OUT` (`scan.sh:302-304`), so every run still feeds the known ALIVE candidates to alterx to generate them again, and it is **not cured** (the output is filtered, the generation is not stopped).
- **The option A of the appendix B**: the word list of the timestamps — not implemented
- **The option B of the appendix B**: the sampling of the probabilistic revival — not implemented
- **The appendix C**: the stage 6 mapper goes through the JSONL — not implemented (the stage 6 of `scan.sh` still parses the text of the mapper)

### 6. daily/reports has no automatic rotation 🟡 it accumulates over the time

The `daily/reports/` keeps everything by default, the README gives the commands of a manual cleaning but **it is not in the cron**. A run over the long term accumulates without a limit (the production already holds a few dozens of them).

**The advice**: add a line at the entry of `daily_monitor.sh`:

```bash
find "$REPORTS_DIR" -maxdepth 1 -mindepth 1 -mtime +30 -exec rm -rf {} +
```

### 7. The coverage of the integration tests is incomplete 🟡 it is known

- `go test ./...` covers `db_align/internal/{resolver,permute,scope}` (the 3 `_test.go`, they survived the layout migration of 2026-09)
- The path of the upsert of the `crawler` and of the `store` only ran in the smoke test (`-n ExampleCo -all`), and it depends on the credentials of AQC
- `pdtm` has no automated test, and every fix is told in the appendix of the README
- **The advice**: write the integration tests of the store / of the crawler on a sqlite in memory, no real AQC is needed; the fixes of pdtm get a regression case when they are merged

### 8. The sensitive data of the database is not encrypted ⚠️ the surface of a leak of the credentials

The `recon.sqlite3` weighs a few dozens of MB, and the fields `mapp_records.raw_json` and `web_subdomains.raw_json` were **desensitized or stripped** in the public version (the measurement in production of 2026-09: the field mapp_records.raw_json is empty); the fields of the filings that land on the disk without being encrypted + the entities of the companies are still sensitive data.

**The advice**: assess the need of a static encryption of `recon.sqlite3` (the SEE of sqlite / sqlcipher), or strip the fields of `raw_json` explicitly / only keep a sample in the tables of the archive.

### 9. The entry points of the documentation are scattered 🟢 it is a problem of the readability

- The content of `db_align/README.md` and of `db_align/CLAUDE.md` overlaps partly (the expiry of the cookie, the proxy)
- `daily/README.md` is 500+ lines and `pdtm/README.md` is 300+ lines, and a new reader gets confused by the appendix A/B/C at the first sight
- **The advice**: split `daily/README.md` into a user guide and the design notes; the appendix of `pdtm` should go into a `KNOWN_ISSUES.md` that holds everything, and this file becomes the index

### 10. The blocking read of the stdin of cdncheck hangs the pipeline for good ✅ cured (2026-08-01)

**The symptom**: the run of the cron of 2026-07-31 (`20260731-030002`) hung from 03:00 until 14:34 when a human stepped in, **11.5 hours** in total. The `ExampleCo` finished normally from 03:00 to 06:32 and it stored its data; the `DemoCorp` got stuck at 06:32 in the probe of the domains of cdncheck at `pdtm/scanner.sh:209`, **7h45m with no progress at all**. The 6 threads of the process were all parked in `futex_wait_queue_me` / `ep_poll`, and the CPU added up to 22 seconds only.

**The root cause (two independent problems piled up)**:

1. **cdncheck blocks while it reads the stdin** (the main cause). Even when the input is already given with `-i <file>`, it still reads the stdin when it is not a TTY and it waits for the EOF; when it never comes, it parks for good. **The result has already been computed and written, the process simply does not quit.**
2. **The mode of the domains is slow and it cannot be tuned** (the secondary cause). It was measured at ~0.9 seconds per domain, and the CONFIG section of `cdncheck -help` only has `-resolver` / `-retry` / `-exclude`, **there is no `-timeout` / `-concurrency` / `-rate-limit`** — it cannot go faster and it cannot be contained. The mode of the IP is a match of the ranges offline, it is fast and it is not affected.

**The measurement** (v1.2.44):

| The size of the input | With `< /dev/null` | The result |
|---|---|---|
| 1 / 5 / 20 / 400 domains | ❌ | all of them hang, the timeout of 60s+, 0 output |
| 4 domains | ✅ | **exit=0, 2 seconds**, 3 hits (exactly the same result as the one that was computed before the hang) |
| 50 domains | ✅ + the form `-r` with commas | exit=0, **44 seconds**, 0 parse errors |

The test of the gradient is decisive: **1 domain and 27958 domains hang exactly the same way**, so it has nothing to do with the scale / with the concurrency / with the rate limit. And the result of the 4 domains was all written within 5 seconds, and then it waited for 85 seconds until the timeout killed it — which proves that it "computes and does not quit".

**The fix (merged on 2026-08-01)**:

A new `pdtm/cdnmatch/` — a Go wrapper that calls the internal `Check()` (the trie of the ranges of the IPs) and the `CheckSuffix()` (the lookup of the suffix of the publicsuffix) directly with `import "github.com/projectdiscovery/cdncheck"`, **purely offline, it does not touch the `retryabledns`**. The stages 1+2 of `scanner.sh` were rewritten:

- `dnsx -cname -a -resp -j` outputs the JSONL, it was already running, and the `-cname` comes for free (the cost is almost 0, because dnsx resolves the whole set of the domains anyway).
- `cdnmatch -in DNSX_RAW_FILE -domains pure_domains.txt` produces in one go `tmp_domain_{ip,ipv6,cname,ns}_pairs.txt` + `all_unique_ips.txt` + `{cdn,waf,cloud}_{ips,domains}.txt` + `non_cdn_{list,ips}.txt` + `cdnmatch_stats.json`, and the name of the files and the format of the lines are **literally the same** as the ones of the original pipeline of awk, so the stages 3-9 do not change.
- 0 DNS query (the 27958 queries of the side of the domains of the original cdncheck are all gone), and the stages 1+2 of a cron that runs 27958 domains went from 7h+ to ~5 seconds.

**The verification by comparison** (the smoke test of `pdtm/cdnmatch`): the old `cdncheck -i ... -cdn -waf` against the new `cdnmatch`, with the same input of 12 IPs both output the 4 WAF IPs `{104.16.132.229, 104.16.133.229, 104.17.207.5, 104.17.208.5}`; the 4 CNAMEs hit the 2 suffixes of Cloudflare (`*.cdn.cloudflare.net`) with the old and with the new one.

**For the users that upgrade from v0.0.x to v0.1.0**: if you paused the cron because of the hang of cdncheck, you can now install the cron again with the "hard conventions of the operation §5" (`./install_cron.sh` without arguments, it runs pdtm+icp, the switch of a business is in the table `recon_business_config`). Watch the report of the next 03:00: the stages 1+2 should be done in ~10 seconds (the line `dnsx JSONL: <N> rows` is followed right away by the line `[cdnmatch] records=...`), and the text `[+] cdncheck` is gone. **The users that install v0.1.0 fresh do not need this step** — `./install_cron.sh` is enough.

**The changes that go with it**:

- The `RESOLVERS` are declared at the top of `scanner.sh` in one place, and every call uses `-r "$RESOLVERS"`. See the known problem §11.
- `scanner.sh` drops the reference to `tmp_cdn_ips_detected.txt` (cdnmatch does not produce it any more).

**The related risk**: the cron uses `flock -n`, and if the new version triggers a hang again it is the same way.
**The rollback** (if there is a problem):
1. `cd pdtm/cdnmatch && rm -rf ../bin/cdnmatch`
2. `git checkout scanner.sh` (it goes back to the old version with the awk + the cdncheck binary)
3. The old "the solution" section of the known problem §10 goes back into the hardening layer (`< /dev/null` + `-r` in CSV + `timeout 600`)

### 11. The form of the `-r` resolvers is a trap matrix across the tools 🟡 some of them fail silently

**The trap**: not every PD tool that reads `-r file` or `-r csv` behaves the way you expect. **With `-silent` there is no error at all**, and it looks like "it finishes in 1 second with 0 hits" or a similar false positive.

**The measured matrix** (v1.2.44):

| The tool | `-r ./resolvers` (a file) | `-r 1.2.3.4,5.6.7.8` (CSV) | Where it is called | Which one to take |
|---|---|---|---|---|
| cdncheck | ❌ it resolves the name of the file as a host → 0 output, exit=0 | ✅ it works | it is now `cdnmatch` and the binary is not used any more | CSV |
| dnsx | ✅ it works | ✅ it works | `scanner.sh`, `scan.sh`, `check_wildcard.sh` | either one, scanner.sh takes the CSV |
| httpx | ✅ it works | ✅ it works | the stages 3.5 / 8 of `scanner.sh` | either one |
| naabu | ✅ it works | ✅ it works | the stage 4.5 of `scanner.sh` | either one |
| subfinder | ✅ it works | ❌ it fails silently with 0 hits | the derivation of `scan.sh` | **the file** |
| alterx | n/a (it has no `-r` flag) | n/a | the derivation of `scan.sh` | the default resolution |
| asnmap | ✅ | ✅ (there is no warning about the file, but it was measured) | it is not in the main pipeline | the same as dnsx |

**The conclusion**:

- cdncheck is the only tool of the main pipeline that is trapped the other way round (it was solved indirectly through cdnmatch). **The new code must not call the `cdncheck` binary any more**; for a fallback, see the three pieces `cdncheck -cdn -waf` + the CSV + `< /dev/null` of the known problem §10.
- subfinder is trapped the other way round — its documentation says that it accepts a CSV, but **the CSV path was measured with 0 hits**. So the only call with a file is the one of subfinder at `scan.sh:182/378`; the dnsx of the same file uses the CSV.
- scanner.sh / check_wildcard.sh do not call subfinder, they use the form of the CSV everywhere, and it is derived from the file `pdtm/resolvers`.

**The unified convention (since 2026-08-01)**:

- **The single source of truth = the file `pdtm/resolvers`**. Change that file and the CSV and the file view of every script follow by themselves.
- **The derivation** (each of `scanner.sh` / `scan.sh` / `check_wildcard.sh` takes what it needs):
  ```bash
  RESOLVERS_FILE="${RESOLVERS_FILE:-resolvers}"   # subfinder goes through this one
  RESOLVERS="$(tr '\n' ',' < "$RESOLVERS_FILE" | sed 's/,$//')"   # dnsx/httpx/naabu
  ```
- **What every tool takes**:
  - subfinder → `-r "$RESOLVERS_FILE"` (the file, it is mandatory)
  - dnsx / httpx / naabu / cdncheck (the fallback of the future) → `-r "$RESOLVERS"` or `-r "$RESOLVERS_CSV"` (the CSV)
- **The override of the environment**: `export RESOLVERS_FILE=/path/to/file ./scanner.sh` switches all of them with one line.
- **The rule of the choice of the DNS**: the order of the file `pdtm/resolvers` = **the fast latency → the slow one** (the user sorts it by the measurement / by the business case, the exact list is in the repository).

**The file `pdtm/resolvers`**: the list that is in the repository at the moment is maintained by install.sh automatically, so look at the file `pdtm/resolvers` itself.

### 12. The fixes and the new behaviours that were merged on the 2nd of August (the second check up after the cdncheck)

#### 12.1 The fast path of the exact subdomains of scan.sh (2026-08-02)

The top of `scan.sh` (after the `[ -s "$TARGET" ]`, before the compile of the glob) gained an early detection: it scans the `target.txt` once, and if **there is no line with a `*`**:

```
PRECISE_HOSTS (mktemp) → dnsx → DNSX_OUT → sort -u → exit 0
```

It skips the whole flow of the subfinder / of the alterx / of the permutation_cache. The next step of `pipeline.sh` calls `scanner.sh`, and scanner.sh reads the hosts that are already resolved from `DNSX_OUT` and it runs the stage 1 (the JSONL of dnsx, it does a dnsx again) + 2 (cdnmatch) + 5/8 (httpx).

The behaviour splits:

| The target.txt | The behaviour | The measured time |
|---|---|---|
| `www.scanme.sh` (pure exact) | only one dnsx and then `exit 0` | < 1s |
| `ww*.scanme.sh` (pure glob) | the whole flow of the subfinder → alterx → permutation | 45s (2 IP / 2 web) |
| `*.scanme.sh` (pure glob) | the same | 57s (4 IP / 4 web) |
| `www.scanme.sh\n*.scanme.sh` | it goes through the flow of the glob; the exact subdomain goes through the two ways of PRECISE_HOSTS | the same as the glob |

**The convention of the business**: the list of the exact subdomains means "do not widen the scope to look for the sister subdomains". If only `www.scanme.sh` matters, do not list `*.scanme.sh`, otherwise the cron goes looking for the 5+ sister domains findme/demo/honey/ssl-*.

#### 12.2 The hang of the stdin of httpx in the stage 5 of scanner.sh

**The root cause**: `httpx -l <file>` already has its input, but it still tries to read the stdin. When the stdin is not a TTY (under the call of `pipeline.sh`) and it is not `/dev/null`, **a path of the GNU httpx 1.10 parks for good**.

**The reproduction** (an isolated directory, the 4 IPs of scanme.sh):

```
scanner.sh standalone        it finishes in 11 seconds
scanner.sh inside pipeline  the timeout of 600 seconds, 0 output
```

**The fix**: the three `httpx -l ...` of `scanner.sh` get a `< /dev/null` (the stage 3.5 / the stage 5 / the stage 8). The call of dnsx inside `scan.sh` got the `< /dev/null` defence as well.

**The theoretical root cause**: the tools of ProjectDiscovery (when they write to the stdin) close the stdin in a best-effort way, and in some cases they need an explicit `< /dev/null`. When the upstream pipeline calls `bash scanner.sh` under `set -euo pipefail`, **the sub shell does not close the stdin by itself**.

#### 12.3 The silent refusal of the PCRE `(?:...)` of target_glob.py

The output of `target_glob.py:49` was `(?:^|\.){escaped}$` — the syntax of a non capturing group of the PCRE. But `grep -E` is the POSIX ERE, and **the GNU grep 3.7 accepts it silently and returns 0 hits as a whole** (it does not error out).

**The fix**: `(?:^|\.)` → `(^|\.)` (a capturing group, compliant with the POSIX ERE).

**The impact**: every glob path that the cron of the production ran before, the `grep -E -f targets.regex SUBS_TMP` of the stages 1.5 / 4.5 lost 0 rows silently, and then because of the `set -euo pipefail` it did not trigger inside the body of the `if [...] fi`, and the empty SUBS_TMP flowed into ALIVE. The scan.sh printed "done -> dnsx_output.txt (0 rows)", which **is the same as skipping the filter of the glob** (when the upstream has a `*.scanme.sh`, the candidates that alterx derives are not filtered by the regex of the targets and they land on the disk as they are).

**The scope of the impact of this bug**: the cron went from 46k to 74k `web_subdomains`, and **a few subdomains that are "outside the pattern of the targets" may have been stored wrongly**. The reference is the `re.match(targets.regex)` of the field `subdomain` of `web_subdomains` in `daily/lib/diff.py` for a one time cleaning — but after the new version was merged, the new entries are strictly filtered, and only the old ones have the problem.

**The reproduction**: feed the `targets.regex` to itself with `grep -E -f` — the old regex `(?:^|\.)` hits 0 everywhere; the new one `(^|\.)` hits 5/5.

#### 12.4 The order of the resolvers file

The file `pdtm/resolvers` is sorted by the latency (the fast one → the slow one). The exact list is maintained by install.sh automatically, and the detailed values are in the file of the repository itself.

#### 12.5 What is left: the exact subdomain still triggers a wasted call of subfinder in the case of a glob

**Not fixed**. When the `target.txt` is `www.scanme.sh
*.example.com` (a mix of exact and glob), the output of `all-bases` is `["www.scanme.sh", "example.com"]` and subfinder runs one empty query for `www.scanme.sh`. There is no impact on the business (the empty SUBS_TMP still goes through the grep and it is still empty, the final result is not affected), it only wastes one round trip of an external API.

The path of the fix: add a mode `all-glob-bases` to `target_glob.py` that only extracts the base of the lines of the glob, and let scan.sh call it. It is not destructive, the diff is ~15 lines, **it is not implemented** (the priority 🟢).

### 13. permutation_state should be wiped as a whole table per period and not cooled down for 30 days per entry ⏳ not cured

**The background**: `pdtm/permutation_cache.py` maintains a `next_attempt_at` for every row of a permutation, the state of nxdomain is retried after a cooling of 30 days, and `resolved` / `wildcard_hit` is never retried. Semantically it assumes that "the space of the candidates of alterx is stable, and the conclusion of NXDOMAIN of the same permutation holds for 30 days".

**The conflict**: on 2026-08-03 a random halving of alterx was added to the stage 3 of `scan.sh` (the logic of `ALTERX_MAX_EST` that is merged this time — when the estimate of the number of the candidates is above 1 million, `shuf -n` halves the number of the lines of the input until the estimate is under the threshold). The halving changes the subset of the subdomains that alterx is fed with on every run, and the patterns of the words that alterx learns follow.

**The problem**: the same string of a permutation may be generated by **different contexts of the words**:

| The run | The input | The bias of the words of alterx | The candidates it generates | The state of the cache |
|---|---|---|---|---|
| N | the subset of 50% | the direction X | {A, B, C, D}, all of them go to dnsx → nxdomain | {A, B, C, D} is stored for 30 days |
| N+1 | the subset of 100% | the whole | {A, B, E, F} | A and B are filtered out, **E and F** are handled as new entries |
| N+2 | the subset of 30% (at random) | the direction Y | {E, F, A} | A is still cut by the cache; E and F pass |

While the conclusion of nxdomain of A is **factually right** (we confirmed that it does not resolve), the cache cuts the new candidates hard **under different contexts of the words** — it is like "the conclusion of the small sample of the last round blocks the discovery of the whole set of this round". **The coverage of the halving cannot be guaranteed**: some of the candidates that alterx wants to generate are skipped because their string collides with an old entry of the cache, even though those candidates are **new discoveries** in the new (whole) context of the words.

**The proposed solution**: **a wipe of the whole table by business + by time window**, and not a per entry TTL.

- the implementation: in the stage `permutation_cache.py filter`, it queries `SELECT MAX(last_attempt_at) FROM permutation_state WHERE business_id=?` first, and when `now - MAX > WIPE_DAYS` days have passed (7 by default, the env `PERM_CACHE_WIPE_DAYS` tunes it), it runs `DELETE FROM permutation_state WHERE business_id=?`, and then it filters as usual (the cache is empty at that point, and every candidate is handled as a new entry)
- the trigger: every call of the filter queries it once, there is no need for an external cron
- it does not collide with the per entry TTL: after the wipe, the 7 next days still go through the per entry TTL, until the next wipe that expired

**The trade-off**:

| The dimension | The per entry TTL of the moment | The wipe that is proposed |
|---|---|---|
| the throttling of dnsx (the retest of a nxdomain) | ✅ it is not retested within 30 days | ❌ after a wipe every 7 days the whole batch is retested |
| the coverage after the halving of alterx | ❌ the old cache cuts the new candidates | ✅ after the wipe alterx generates the whole set again, with no historical bias |
| the persistence of the resolved subdomains | ✅ the cache never retries | ⚠️ the persistence of the cache is lost, but the dnsx_output.txt + web_subdomains still hold the real subdomains, nothing is lost |
| the complexity of the implementation | the logic of the moment | a diff of ~30 lines in `permutation_cache.py` (the block of the wipe in the filter + `--wipe-days` in the argparse); `scan.sh` passes the wipe_days or reads the env |

**The cost of the retest of dnsx**: the candidates that alterx derives are usually a few thousands to a few tens of thousands for a single base; with 5 businesses × 3 bases each × 10k candidates on average, the retest after a wipe of 7 days is ~150k dnsx queries, far below a single full query of subfinder (~145s × a few hundreds of thousands of API).

**The estimate of the change**: 30 lines in `permutation_cache.py`; 1 line of the env in `scan.sh`; the section of the cache strategy of `pdtm/README.md` is updated. **This time it is only documented, it is not implemented** — it waits for the decision of the user.

---

### 14. `-i <dir>` deletes the target.txt / exclude.txt of the external directory by default after a successful import ✅ since v0.1.0

**The symptom**: `add_business.sh -i <dir>` / `pdtm scope_import.sh -i <dir>` /
`pdtm pipeline.sh -i <dir>` delete the `<dir>/target.txt`
and the `<dir>/exclude.txt` (and the directory `<dir>/` when it becomes empty) once the scopes are in the database. They delete them on the path of a failure as well (the unconditional trap semantics that pipeline.sh already had).

**The root cause**: when the `<dir>/` is shared as a pool of the scopes of several businesses (the typical SRC case: all the candidate domains are collected in a single `target.txt` first, and then it is split per business), keeping the original file makes the next business read the stale entries of the last round. If the `target.txt` is appended to with `>>`, the scopes of the businesses pollute each other.

**The mitigation of the moment**: pass `--keep-files` (scope_import.sh) or `--keep-input`
(add_business.sh) to keep the original files explicitly.

**The real fix**: it already is the behaviour that v0.1.0 expects, there is no plan. **The posture of the risk**: it is destructive — the user has to treat the `<dir>/` as a temporary input directory (like `mktemp -d`). If the `<dir>/` holds other files besides the `target.txt` / `exclude.txt` (notes / a configuration / a backup), the `rmdir` fails but the `rm -f` does not touch them, so **only the two names target.txt / exclude.txt are deleted**.

**The advice of the use**:

| The case | The command |
|---|---|
| a one time batch (the default) | `mktemp -d && cp seed target.txt $d/ && ./srcradar manage add_business -n biz -i $d/` |
| to keep the data of the scopes | `./srcradar manage add_business -n biz -i /scope-pool/ --keep-input` |
| several businesses that share the scopes | give every business a `--keep-input` to keep the data shared |

**The direction of the evolution of the API** (for v0.2.0+): the names that state the semantics. `-i` is renamed to `--tmp-dir`, or an explicit flag `--consume-input` / `--no-consume-input` is added, so that a `cd` into the `<dir>/` and then a `./srcradar ... -i ./` does not delete the files of the current directory by mistake.

### 15. The data source of the `-type` of `daily/install_cron.sh` is out of sync with the comment at the top ⏳ partly fixed

**The symptom**: the comment of the lines 9-15 at the top of `modules/main/daily/install_cron.sh` claims that "The installed entry
always runs `-type pdtm,icp`. ... the config table is the single source of truth for what
each business runs. This script no longer takes -type", but:

1. The real cron line (`install_cron.sh:42`) passes the types through the env variable `TYPES`:
   `exec $SCRIPT -type $(echo $TYPES | tr ' ',',')` (it is parameterised, after the commit of 2026-09);
   but the **default behaviour** of the script still hardcodes `TYPES="pdtm icp"`, so "always runs `-type pdtm,icp`"
   holds under the default call, and the wording of the top is ambiguous
2. The table `recon_business_config` holds the 4 switches of 0/1 `enabled/web/tcp/icp`,
   and `daily_monitor.sh:141-180` uses those bits to **filter** the declared stages (pdtm / icp),
   it is **not** the source
3. The consequence: a new stage (such as the `daily-url2` of the future) has to change the default `TYPES` of install_cron.sh,
   and a new column of the table of the configuration does not carry it either; the comment at the top does not agree with the fact that "the parameterisation is implemented"



**The mitigation (it is partly fixed)**: `install_cron.sh:42` was changed to pass the `TYPES` through the env variable (the commit of 2026-09),
and the README of the daily operation already mentions that "the cron line hardcodes `-type pdtm,icp`, and the stages are gated by the table of the configuration", but
**the comment at the top + the default TYPES are still not aligned with the "the table of the configuration is the single source of truth"**.

**The real fix**:

- the option 1: the comment at the top becomes "the default cron line hardcodes `-type pdtm,icp` (the env `TYPES` can overwrite it),
  and the table `recon_business_config` filters the declared stages by bit", aligned with the behaviour of the code
- the option 2: the default `TYPES` reads the stages from the table of the configuration (a new column `stages TEXT`), for a real
  single source of truth; the scope of the change is bigger and it touches the parsing of the arguments of `daily_monitor.sh`



**Whether it is worth it**:

| The use | The value |
|---|---|
| the 2 stages of the moment (pdtm+icp) | 🟡 the comment and the code are partly out of sync, the default call still works |
| to hook a new stage into the cron | 🔴 without the fix the default TYPES of install_cron.sh has to change, and it does not agree with the promise of the documentation |
| the operator adds a stage | 🔴 the same, a new column of the table of the configuration does not take effect by itself |

---

## The follow up work

| The priority | The item | The remark |
|---|---|---|
| 🔴 high | merge the fixes of the appendix A/B/C of `pdtm` | the deduplication of alterx, the word list of the timestamps, the filter of the permutations of one character, the stages 5/6 through the JSONL |
| 🔴 high | complete the `service_type_map` | the prerequisite of the comparison between the businesses |
| 🟡 medium | clean the directory `ENScan_GO/` | delete the bak, add a gitignore for the outs, archive the xlsx |
| 🟡 medium | the automatic rotation of the `daily/reports` | add a line `find -mtime +30 -delete` at the entry of the cron |
| 🟡 medium | the integration tests of the crawler / of the store | a sqlite in memory, no dependency on AQC |
| 🟡 medium | add a 3rd business for a parallel verification | the verification of the mutual exclusion of the `flock` and of the contention of the quota of AQC |
| 🟡 medium | clean the old entries of `web_subdomains` that are "outside the pattern of the targets" that the known problem §12.3 mentions | add a one time `re.match(targets.regex)` in `daily/lib/diff.py` |
| 🟡 medium | add `< /dev/null` to the dnsx of the stage of check_wildcard.sh to avoid the hang | see the analysis A of the known problem §11 |
| 🟢 low | the self test script of cdnmatch goes into the repository | the smoke test runs cdnmatch once and compares it with the old cdncheck, it runs once before it hangs on the cron |
| 🟢 low | the split of `daily/README.md` into a user guide / the design notes | it lowers the barrier for a new reader |
| 🟢 low | the move of the section of the "known problems" into Linear / the GitHub Issues | everything is tracked in one place, this file becomes the index |
| 🟢 low | assess the need of a static encryption of `recon.sqlite3` | sqlcipher / SEE |
| 🟢 low | the mode `all-glob-bases` in target_glob.py | the fix of the wasted subfinder of the known problem §12.5 |
| 🟢 low | the merge of the stage 6 of scope_import.sh and of scan.sh | the duplication T+U across the files of the known problem §11 |

---

## License

srcradar is distributed under the Apache License 2.0, the whole terms are in [`LICENSE`](./LICENSE).
The upstream projects and their own licenses are in `## The thanks to the upstream` and in [`NOTICE`](./NOTICE).
The prerequisites of the use of the tools and the disclaimer are in [`TERMS_ADDENDUM.md`](./TERMS_ADDENDUM.md).




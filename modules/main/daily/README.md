# daily/ — the daily report of the monitoring of the SRC assets

> English | [中文](README_zh.md)

Every day at 03:00 it runs `pdtm/pipeline.sh` on its own (with the optional `db_align` / `ymicp`), compares the result with the snapshot of the last run and writes the incremental report to the disk (it does not send a mail or a webhook).

## The three projects (the choice of `-type`)

The single parameter `-type <pdtm[,icp[,enscan]]>` selects the stages of this run, separated by commas (the default is `pdtm`):

| Value of `-type` | Project | Path | What it does |
|---|---|---|---|
| `pdtm`   | pdtm  | `../pdtm/pipeline.sh`        | the scan of the subdomains / of the ports / of the fingerprints |
| `icp`    | ymicp | `../ymicp/icp_mapp_query.py` | the refresh of the filing of the mini programs / of the WeChat accounts |
| `enscan` | db_align | `../db_align/bin/db_align`  | the pull of the entities / of the subsidiaries / of the assets (with `-scope`) |

**`-type` takes one value only**, several `-type` are refused (the guard against a typo). `-type pdtm -type icp` reports "only takes one value; use comma-separated form like -type pdtm,icp".

The stages run in a **fixed order** (the order of the `-type` you write does not matter):
`enscan` → `pdtm` → `icp`.
**The order is fixed (when enscan is enabled)**: enscan writes the `scopes` first, and pdtm can only read a scope as a target afterwards.
When there is no scope, pdtm quits with 1 right away (pipeline.sh:91-95), so turning the order around makes the first run of pdtm fail.
`icp` runs last — it only reads the `companies` table and it does not depend on the output of the other stages.

## The structure of the directory

```
daily/
├── daily_monitor.sh            # the entry point of the cron (the orchestration of the top)
├── run_one_business.sh         # one business: enscan → pdtm → icp (in the fixed order)
├── install_cron.sh             # install (without arguments) / uninstall
├── lib/
│   ├── snapshot.py             # the full snapshot of the 6 tables into JSON
│   ├── diff.py                 # the comparison + the CSV / summary.md
│   ├── dashboard.py            # the local visualisation of the 4 tabs of ARL (it binds 127.0.0.1 only)
│   └── log.py                  # the stderr and the log file, both written
├── snapshots/                  # previous.json + the snapshot of this round
├── reports/<run-id>/           # the report of this round
└── logs/<run-id>.log           # the log of this round
```

## What a complete run leaves behind

`reports/20260725-030000/`：

```
summary.md                       # the overview (this is the one to open)
added_scopes.csv                 # the added scopes
added_mapp_records.csv           # the added ICP / APP / mini programs
added_web_subdomains.csv         # the added web assets
added_tcp_assets.csv             # the added TCP ports
added_companies.csv              # the added entities
added_web_hashes.csv             # the added fingerprints
reactivated_web_subdomains.csv   # the ones that came back
reactivated_tcp_assets.csv
deactivated_web_subdomains.csv   # they were online in the last round and they are gone now
deactivated_tcp_assets.csv
changed_companies.csv            # a field of the entity changed (main_licence / nature_name)
changed_mapp_records.csv         # a field of the asset changed
changed_web_hashes.csv           # subdomain_count and the like changed
changed_web_subdomains.csv       # status / title / tech / response_hash changed
changed_tcp_assets.csv
deleted_<table>.csv              # a physical deletion (it hardly ever happens)
full_snapshot.json               # a copy of after.json, it is easier to read it offline
```

The CSV of a category is only written when that category holds data; an empty category leaves no empty file behind.

## Installing / removing the cron

```bash
# install it (without arguments, the cron runs pdtm+icp, the switch of a business is in the table `recon_business_config`)
./install_cron.sh

# the verification
crontab -l | grep daily_monitor

# remove it
./install_cron.sh uninstall
```

The cron line hardcodes `-type pdtm,icp` — the stages that a business really triggers are decided per business by the `recon_business_config` table:

| Field | The stage it controls | Default |
|---|---|---|
| `enabled` | the business as a whole (0 = the cron skips the business) | 1 |
| `web` | the pdtm stage (the scan of the subdomains / of the domains) | 1 |
| `tcp` | the pdtm stage (the scan of the ports, pdtm shares the same stage) | 0 |
| `icp` | the ymicp stage (the refresh of the filing of the mini programs / of the WeChat accounts) | 1 |

The pdtm stage is triggered when `web OR tcp` is 1; a business whose config row is missing is treated as if every field were 0 (opt-in).

**enscan is not part of the cron** — it is the stage of `db_align` that pulls the data again (it is triggered by hand with `run_one_business.sh -type enscan <business name>`), it does not go through the gate of the config, and the cron never runs it on its own.

**daily-url is not part of the cron line either** (the user decided on 2026-08-26) — it reads `web_subdomain_scan_schedule` and runs the subdomains that were marked by hand,
the default cron does not run it. Run `./daily_monitor.sh -type daily-url` by hand when it is needed, or add a line of its own with `crontab -e`.
See the section on the daily URL scan.

The `flock -n` lock makes sure that a manual trigger and the cron do not run at the same time; the path of the `.lock` file is fixed.
When it is installed again, `install` replaces the old cron line that carries the same marker (idempotent).

## Running it by hand

```bash
# the default (pdtm only)
./daily_monitor.sh

# several stages (separated by commas)
./daily_monitor.sh -type pdtm,icp
./daily_monitor.sh -type pdtm,icp,enscan

# no stage at all (only the snapshot + the diff, it fits the troubleshooting)
./daily_monitor.sh dryrun

# run a single business
./run_one_business.sh ExampleCo
./run_one_business.sh -type icp ExampleCo
./run_one_business.sh -type pdtm,enscan ExampleCo

# the scan of a single host with httpx + the storage (it skips dnsx/cdnmatch/naabu/icp and it leaves the is_active=1 rows of the same business alone)
cat > /tmp/api_hosts.txt <<EOF
api.example.com
api2.example.com
EOF
./run_one_business.sh -onesite /tmp/api_hosts.txt ExampleCo
```

### The mode of a single site (`-onesite`)

It skips the whole set of prerequisites of `pdtm/pipeline.sh` (dnsx + cdnmatch + naabu + scanner) and runs `httpx` as a subprocess to write `web_hashes + web_subdomains`:

- It **does not** trigger the `UPDATE ... SET is_active=0` of `persist()`, the one that clears the floor; it only adds / refreshes the rows of the hosts it was given, and the other `is_active=1` rows of the same business stay untouched
- It **does not** write the other tables, such as `scopes`, `tcp_assets`, `companies` and `mapp_records`
- It fits the cases of a point: "add a new domain and check it / scan a few of them again and see how httpx answers"
- It excludes `-type`; it does not affect the full baseline of the cron

`hosts.txt` holds one domain per line, a line that starts with `#` is a comment; the limit is 200 lines; only the FQDN form is accepted (no `http://`).

### The form of the dashboard

The page of `/<business name>` (the top of the "Task Status" tab that opens by default) holds a folding panel called **"Scan and store"**:

- A click unfolds it → a text box + the "Scan and store" button
- The browser POSTs to `/<business name>/scan` (`application/x-www-form-urlencoded`, the field `hosts=<newline-separated>`)
- The dashboard waits for it (5-30s is typical); it returns a small result page with a link "back to /<business name>"
- When it succeeds the dashboard reloads itself, and the next visit of the page of the business shows the new data
- It goes through the same backend as the `-onesite` CLI above (the subprocess of the dashboard calls `import_scan_results.py scan-onesite`), and the semantics, the exit codes and the promise of leaving the `is_active=1` rows alone are exactly the same

The same request can be reproduced by hand with `curl` (for the debugging / for the automation):

```bash
curl -si -X POST -d $'hosts=baidu.com\nqq.com' \
  http://127.0.0.1:8765/ExampleCo/scan
# 200 OK, the HTML holds "the scan is done · 2 rows were written"
```

### The exit codes (a bit mask)

`run_one_business.sh` reports the result of every stage with a 4 bit bit-mask (the user added daily-url on 2026-08-26):

| Bit | Value | What it means |
|---|---|---|
| bit0 | 1 | pdtm failed |
| bit1 | 2 | enscan (db_align) failed |
| bit2 | 4 | icp (ymicp) failed |
| bit3 | 8 | daily-url failed |

Put together: `0` = everything is ok, `3` = pdtm+enscan failed, `5` = pdtm+icp failed, `7` = the three of them failed, `15` = the four of them failed.
A stage that was not asked for does not show up in the mask. `daily_monitor.sh` writes every stage that failed into the "Business run warnings" section of `summary.md`.

## The scope of the monitoring

Every business of the `businesses` table whose `TRIM(business_name) != ''`. At the moment:
- `ExampleCo`

The run of `enscan` (db_align) calls AQC on its own, and it may need a cookie / a proxy; see `../db_align/README.md`.

## The tables that are monitored

| Table | The logical key (the identity) | The content fields (what decides a change) |
|---|---|---|
| scopes | (business_id, scope_name, asset) | is_wildcard |
| companies | (business_id, unit_name) | nature_name, main_licence |
| mapp_records | (company_id, service_licence) or (company_id, service_name, service_type) | domain, content_type_name, record_updated_at |
| web_hashes | (business_id, response_hash) | subdomain_count |
| web_subdomains | **(business_id, subdomain, port)** ← the logical key, not the hash_id | status_code, title, technologies, response_hash |
| tcp_assets | (business_id, host, port) | hosts, raw_value |
| **web_hash_urls** | **(business_id, subdomain, url, source)** | status_code, content_length, word_count, title, redirect, link_source, risk_flag, is_dangerous, content_type |

`web_subdomains` uses the logical key `(business_id, subdomain, port)` and not the natural key
`(hash_id, subdomain, port)`: `import_scan_results.py` groups by the fingerprint of the response,
and the hash_id can drift on every run.
`web_hash_urls` uses the logical key `(business_id, subdomain, url, source)` as well — it agrees with
the `UNIQUE` constraint, and it makes it easy to split the history by source.

Every table also tells the flip of is_active apart:

- `added`      it showed up and active=1
- `reactivated` it was active=0 before and it is active=1 now
- `deactivated` it was active=1 before and it is active=0 now
- `changed`    it stayed active, but a content field changed
- `deleted`    it disappeared (very rare)

The timestamp fields (`fetched_at` / `last_seen` / `updated_at` / `first_seen` /
`created_at` / `raw_json`) move on every run, they do not count as a change.

The decision of a change of `web_hash_urls` has one special case: **only the rows of
`is_static=0` (the last `.` of the path is not followed by `js` / `css`) take part in the
marking of the change_type**. The trigger `trg_whu_au` carries `AND NEW.is_static = 0` in its
WHEN clause, so that a static row is never marked changed / reactivated.
See the section on the scan of the URL assets → "the semantics of is_static".

## The dashboard

`lib/dashboard.py` copies the layout of the 4 tabs of the ARL beacon, it **reads the SQLite database directly** and it renders a single file of HTML. **It is purely local**, **it binds 127.0.0.1 only**, and it makes no call out.

### The management of the service (systemd --user, the user decided on 2026-08-28)

`systemd --user` takes the dashboard over, it **does not** fall back on the cron watchdog of `*/5 * * * * dashboard_watchdog.sh` any more
(the cron line is gone). The file of the systemd unit:

```
~/.config/systemd/user/dashboard.service    (the file of the source)
~/.config/systemd/user/default.target.wants/dashboard.service  (it is enabled)
```

The key configuration of the unit:

| Field | Value | What it does |
|---|---|---|
| `Type` | `simple` | it runs `python3 lib/dashboard.py` in the foreground |
| `Restart` | `on-failure` | it starts it again when it quits with an error |
| `RestartSec` | `30` | it avoids the race of the bind on a socket that is in TIME_WAIT after an OOM kill (errno 98) |
| `MemoryMax` | `3G` | the steady state is ~800M and the peak of a reload is 1.5G; more than 3G is a leak → restart |
| `DASHBOARD_RELOAD` | `300` | the env is injected into dashboard.py, it reloads every 5 minutes |
| `StandardOutput/Error` | `append:~/.../logs/dashboard_service.log` | both the stdout and the stderr go to the log of the service |

The daily operations:

```bash
systemctl --user status dashboard        # the current PID / the state / the memory
systemctl --user restart dashboard       # restart it by hand after a change of lib/*.py
systemctl --user stop dashboard          # it stops the service for the maintenance
journalctl --user -u dashboard -n 50    # the log (it takes the place of tailing the log file)
```

The first time it is enabled (run it once when the environment is rebuilt):

```bash
# 1. check that the file of the unit + the symlink of the enable are there
ls ~/.config/systemd/user/dashboard.service ~/.config/systemd/user/default.target.wants/dashboard.service

# 2. reload + enable + start
systemctl --user daemon-reload
systemctl --user enable dashboard
systemctl --user start dashboard

# 3. the verification
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/health   # it has to be 200
```

> ⚠ **the real name of the unit is `dashboard.service`**, not `recon-dashboard.service`.
> `systemctl --user status recon-dashboard` answers "Unit could not be found".

### The quick start (starting the server by hand, for the debugging / without systemd)

```bash
# start the server (127.0.0.1:8765 by default, it reads the SQLite database directly)
python3 lib/dashboard.py
```

To reach it from this machine:

```bash
ssh -L 8765:127.0.0.1:8765 user@recon-host
# open http://localhost:8765 in the browser
```

In the background (it survives the closing of the SSH session):

```bash
ssh -fN -L 8765:127.0.0.1:8765 user@recon-host
```

Every user opens a tunnel of his own, the port of the machine can be different (you take 8765, a colleague takes 8766, and they all map to the 8765 of the recon).

### The parameters of the CLI

| Parameter | Default | What it does |
|---|---|---|
| `--port N` | `8765` | the port it listens on |
| `--host ADDR` | `127.0.0.1` | **keep 127.0.0.1**; changing it to `0.0.0.0` lets the recon data run free, and the startup warns about it |

### The endpoints

| Path | What it does |
|---|---|
| `GET /` | the HTML home page (it renders itself again every `reload_interval` seconds, or `GET /refresh` refreshes it at once) |
| `GET /health` | `200 ok` |
| `GET /api/snapshot` | the JSON snapshot as it is (for the scripts / for curl to read the data) |

There is no POST/PUT/DELETE, it is read-only.

### The 4 tabs in detail

#### Task Status

```
┌── the card ─────────────────────────────────────┐
│ business · scope · ICP/APP · web subdomains · TCP ports │
└─────────────────────────────────────────────┘
┌── on the left: the analysis of the web assets ────┐ ┌── on the right: the classes of the TCP ports ──┐
│ · the distribution of the status codes (green)   │ │ ⚠ the 80/443 are really web │
│ · the top 10 of the technologies (purple)        │ │ the table of the class + of the share │
│ · the registered root domains (blue)             │ │                     │
│ · the buckets of the density of the fingerprints  │ │                     │
└─────────────────────────┘ └─────────────────────┘
```

**The colours of the buckets of the density of the fingerprints**: 🟢 unique (1 subdomain) / 🔵 2-10 / 🟡 11-50 / 🔴 ≥51.
**The rule**: the wider the coverage, the lower the value — a `≥51` is most of the time a wildcard resolution, a default page or a wildcard certificate.

The ⚠ next to the **classification of the TCP ports**: the category `web` of the table (80/443 and the like) is already stored in `web_subdomains`, this table keeps it for the cross checking, and it is better left out when the analysis is purely about TCP.

#### Site Details

Every `web_subdomains` row is shown flat, 2269 rows × 8 columns.

The **URL column** joins the scheme to the port:

| Port | The format of the URL |
|---|---|
| 80 | `http://subdomain/` |
| 443 | `https://subdomain/` |
| the others | `http://subdomain:port/` |

The URL of a row is an `<a target="_blank">`, a click opens it in a new window.

The **Hash column** is folded: `<a class="hash-link">hash</a><span>(count)</span>`, the count is outside the `<a>` tag;
when several subdomains share the same hash, every row shows that hash and the count tells how many were merged.
**A click on the hash** opens a window (it closes with Esc / with a click on × / with a click on the mask), it lists every subdomain of that hash.

The **colour of the background of a row** follows the `status_code`:

| Status code | The colour |
|---|---|
| 5xx | 🔴 red (high) |
| 4xx | 🟡 yellow (med) |
| 3xx | ⚪ grey (info) |
| 2xx | 🟢 green (low) |
| None | ⚪ grey (info) |

**The 3 switches of the filter** (on the right of the search box, they stack):

| Switch | What it does |
|---|---|
| hide http | it hides the rows of `port=80` (only https stays) |
| only unique | it only shows the rows of `hash_count=1` (every fingerprint belongs to one single subdomain) |
| Chinese first | it reorders the visible rows, the rows whose title holds a Chinese character come first |

**The search**: it matches a substring of the URL / of the title / of the tech / of the hash (it does not tell the case apart).

**A click on the head of a column**: it toggles the ascending and the descending order (▲ / ▼), the numeric columns go by their value and the text columns by `localeCompare('zh-Hans-CN')`.

#### Port Services

Every `tcp_assets` row is shown flat, 2797 rows × 7 columns. The order of the columns:

```
business · port · host · ip · category · service · risk
```

**The `port` is tagged on its own**:

| Category | The typical ports | The risk |
|---|---|---|
| web | 80/443/8080/8443/8000-8999 | 🟢 low |
| db | 3306/5432/27017/6379/1433/9200/11211/... | 🔴 high |
| remote | 22 (SSH) / 23 (Telnet) / 3389 (RDP) / 5900-5901 (VNC) | SSH🟡 medium, the others🔴 high |
| mail | 25/110/143/465/587/993/995 | 🟡 medium |
| snmp | 161/162 | 🟡 medium |
| tftp | 69 | 🟡 medium |
| high-port | > 10000 and not recognised | 🟡 medium |
| other | the others that are not recognised | ⚪ a hint |

The colour bar on the left of a row follows the risk. **The search**: it matches a substring of the host / of the ip / of the service / of the category.
**A click on the head of a column**: it toggles the order (the port by its value, the other columns as text; it shares the logic of the sorting with the tab of the site details).

**The source of the `ip` column**: the table `tcp_assets` has no IP column of its own (only the host name), and the dashboard fills it back from the derived field `snapshot.host_ip_map` — that map is built in `snapshot.py` with
`json_extract(web_subdomains.raw_json, '$.host_ip')`, and the **first-seen wins**
(the IP that was seen first is kept, and the IP really changes very seldom). When a host never showed up in web_subdomains, the IP column
shows `—` (of the 2797 rows only 2 are empty).

#### Risk Level

Three independent lists, sorted by the severity:

1. **The exposed high risk ports** — every tcp_assets whose risk=high (DB / RDP / Telnet / VNC / the services without an authentication), listed by host
2. **The alerts of the mass fingerprints** — the fingerprints whose `response_hash` covers ≥50 subdomains (they are a wildcard resolution, a default page or a site that is not configured on its own, not an asset of its own)
3. **The hosts whose ports are spread too wide** — a single host that opens ≥50 ports (it looks like the product of a scan of a range of ports, and it can be left out of the web view)

### The limitations

- **Every decision is a local heuristic**, there is no probe from the outside:
  - the risk score is based on a hardcoded dictionary of the ports, it does not know the real version of the service (the 22 is not necessarily an OpenSSH 9.x with the passwords disabled and fail2ban)
  - "exposed" only means `tcp_assets.is_active=1`, and it **does not mean that it is really open to the world** — it can be inside the intranet / behind the isolation of a VPC / the domain can resolve to a placeholder of 0.0.0.0
  - the detection of a Chinese title only looks at the CJK range U+4E00-U+9FFF
- **The derived fields**: the snapshot holds a new `host_ip_map` at the top level (subdomain→IP), and the dashboard uses it to fill the IP column of the tcp_assets back.
  The source of the derivation is `web_subdomains.raw_json.host_ip`; **the first-seen wins**, so the old value is shown when the domain changes its IP (it really changes very seldom).
  If the structure of the `raw_json` ever changes (when `host_ip` is renamed for instance), this SQL of the snapshot has to change with it.
- **There is no real screenshot** — the "Site Details" is a view of the metadata; the tab of the screenshots of the sites was replaced in the early v1, and a real screenshot needs the output of an external `gowitness` / `aquatone` scan in the table `web_screenshots`
- **The page embeds everything** — the tab of the site details renders 2269 rows × 8 columns ≈ 1MB of HTML; the data of the window of the hash→subdomains is ≈ 50KB of embedded JSON; the page as a whole is ~1.8MB, the filtering happens on the client with no round trip over the network

### The relation with the snapshot

The dashboard **reads the SQLite database directly** when it starts and it caches the result of the rendering. **The refresh of the cache is triggered by the time**, it is not triggered by the mtime of a file any more:

| The way it is triggered | What it does | The frequency / the moment |
|---|---|---|
| by the time | every request checks whether the `reload_interval` seconds have passed since the last reload, and it renders again when they have | 30s by default; the env `DASHBOARD_RELOAD=N` overrides it |
| on purpose | `GET /refresh` renders again at once and answers `reloaded` | it is not rate limited |

So:

```bash
# 1) refresh the browser → in most of the cases you see the new data within 30s (the reload_interval decides)
# 2) to refresh it at once:
curl http://127.0.0.1:8765/refresh
# refresh the browser once more

# 3) to change the frequency of the refresh (to make it 10s for instance):
DASHBOARD_RELOAD=10 python3 lib/dashboard.py
```

The dashboard does not have to be restarted.

### The troubleshooting

```bash
# it does not start? it is usually the port that is taken
python3 lib/dashboard.py
# the log writes "dashboard listening on http://127.0.0.1:8765"

# the conflict of the port
ss -tlnp | grep 8765         # to see who holds it
python3 lib/dashboard.py --port 9999   # take another port, -L changes with it

# the tunnel of the SSH works but the page does not open
curl -I http://localhost:8765/health   # on this machine
curl -I http://127.0.0.1:8765/health   # after you went into the remote over the SSH
# the step that answers 200 and the step that fails is where the problem is

# the dashboard does not answer once systemd took it over (the user, after 2026-08-28)
systemctl --user status dashboard     # to see whether it is active / the memory / the last errors
journalctl --user -u dashboard -n 100 --no-pager   # to see the error of the startup
systemctl --user restart dashboard    # restart it by hand after a change of the code / when it hangs
# the process is there but the health does not answer → look at the state of the fd
lsof -p $(pgrep -f 'lib/dashboard.py' | head -1) | grep TCP
# a lot of CLOSE_WAIT piles up → the socket was not reclaimed, kill it and restart it
pkill -9 -f 'lib/dashboard.py'    # systemd brings it back on its own after 30s, it is Restart=on-failure
```

## The onboarding of a new business

When `run_one_business.sh` finds a business name in the `businesses` table but **no scope at all** (scope_name='可测资产'), it **skips pdtm** and marks `needs_onboarding` in the summary.md, instead of quitting with "the business X of the database has no measurable asset". That way the run of the cron does not fall over because of a new business.

### The first onboarding of a new business (by hand)

```bash
# 1. prepare the input/ directory: the target.txt holds the measurable domains, the exclude.txt the ones that are not measurable
mkdir -p input/业务名
cat > input/业务名/target.txt <<EOF
main.example.com
api.example.com
EOF
cat > input/业务名/exclude.txt <<EOF
mail.example.com
EOF

# 2. use the entry -i of pipeline.sh: it INSERTs the row of the business + it writes the target/exclude into the scopes
cd /opt/srcradar/pdtm
./pipeline.sh -b 业务名 -i /opt/srcradar/input/业务名

# 3. the verification
sqlite3 ../db/recon.sqlite3 "SELECT asset FROM scopes WHERE business_id=(SELECT id FROM businesses WHERE business_name='

# 4. the daily cron takes it from there: run_one_business.sh 业务名 goes through the path of the DB
/opt/srcradar/daily/run_one_business.sh 业务名
```

### The daily cron that follows

The business has a row in the `businesses` table + at least one scope of `可测资产` → the cron runs it on its own, following the `recon_business_config` (the default is `enabled=1, web=1, tcp=0, icp=1`, and enscan is the stage that is run by hand).
The parameter `-type` is the override of a single run of the **manual** run of `daily_monitor.sh / run_one_business.sh`, it has no effect on the cron (the cron hardcodes `-type pdtm,icp`, and enscan is not part of the cron).

### Skipping a business that should not run

Set the `enabled` of the matching row of `recon_business_config` to 0, and the cron skips that business (the other fields stay, and to restore it you only put the 1 back):

```sql
-- pause it
UPDATE recon_business_config SET enabled=0 WHERE business_id=(SELECT id FROM businesses WHERE business_name='业务名');
-- restore it
UPDATE recon_business_config SET enabled=1 WHERE business_id=(SELECT id FROM businesses WHERE business_name='业务名');
-- turn the refresh of the filing of the icp of a business off (web/tcp stay)
UPDATE recon_business_config SET icp=0 WHERE business_id=(SELECT id FROM businesses WHERE business_name='业务名');
```

Calling `run_one_business.sh <business name>` directly (without the scheduler) bypasses the config — a manual call is an explicit action, it does not go through the gate.

## The known problems

### The subdomain of one character / the noise of the wildcard DNS (the kind of `http://j/`)

**The symptom**: the tab "Site Details" of the dashboard holds N rows of the shape `http://a/` / `http://j/` / `http://0/` — the URL cannot be reached and it is of no value for the inventory of the assets.

**The evidence** (looked up in the database, the batch of 2026-07-26):

```sql
SELECT subdomain, port, url, raw_json FROM web_subdomains
WHERE instr(subdomain, '.') = 0 AND subdomain='a' AND port=80 LIMIT 3;
```

| subdomain | url | raw_json |
|---|---|---|
| `'a'` | `'http://a'` | `"http://aliondemandfiles.example.com [403] [238] ..."` |
| `'a'` | `'http://a'` | `"http://al.example.com [200] [50331] ..."` |

The fields do not match: `subdomain='a'` is the prefix of the input that the scanner fed in, but the HTTP really hit `aliondemandfiles.example.com` in `raw_json` (the target of the redirection of the wildcard DNS). The `url='http://a'` misses the `.example.com` suffix, it is a bug of the concatenation of the scanner.

**The density of the information** (a typical batch):

```
215 rows → 72 hashes → 6 IPs (all of them the closure of a wildcard CDN)
every first_seen/fetched_at comes from the same batch of the scan (less than a minute)
all of them are is_active=1
the response_hash is the placeholder of the string '<status_code>|<content_length>', it is not a real fingerprint of the content
```

**The root cause** (it involves three layers):

1. **`pdtm/check_wildcard.sh`** detected that `*.example.com` has a wildcard resolution, but the candidates of a permutation of one character were not dropped before the probe of the HTTP
2. **The probe of httpx**: the DNS is closed on the IP of the CDN by the wildcard, so the HTTP request lands on the server of the real domain (such as `aliondemandfiles.example.com`)
3. **`pdtm/import_scan_results.py`** keeps only the prefix of the input `a` as the `subdomain` when it stores the row, it does not write the domain that was really hit into the canonical field

**The mitigation of the moment** (it is in place):

- The first line of `lib/dashboard.py:_build_sites` filters with `if '.' in subdomain` — the 215 rows without a dot all leave the display
- The deduplication by hash + the count column: the sites-table goes from 46,911 rows → 179 rows; the page weighs 45.8 KB once gzipped (it was 25 MB before)

**The real fix (to do, it is better done on the side of pdtm)**:

Two options, pick one:

1. **`pdtm/permutation_cache.py` drops the permutations of one character in its filter stage** (the one that is recommended):
   ```python
   parts = permutation.lower().rstrip(".").split(".")
   if len(parts[0]) <= 2:
        continue   # the permutations of one / two characters are skipped
   ```
   They are not probed at all, and every day 200+ httpx requests are saved (the scan of the single characters takes a few minutes to run).

2. **`pdtm/import_scan_results.py` writes the domain that was really hit when it detects a wildcard redirection**:
   ```python
   if item.get("wildcard_redirect"):
       record["subdomain"] = item["resolved_domain"]
   ```
   The data is kept but it is replaced by the canonical name, and the filter of the dashboard can be relaxed at the same time.

**Whether it is worth it**:

| The use | The value |
|---|---|
| the inventory of the real assets | ❌ 215 rows ≠ 215 sites, the truth is 6 CDN IPs |
| the monitoring of a change of the content | ❌ the hash of a placeholder, no value for a diff |
| the detection of the state of the wildcard DNS | ✅ these rows are the proof that the wildcard resolution is on |
| the history of the trend | ❌ they come out of the same batch, a rerun gives the same 215 rows |

## The handling of a failure

| The point of the failure | What it does |
|---|---|
| the before of `snapshot.py` fails | it quits right away, the previous.json is not rotated |
| a stage of a business fails | the matching bit is set to 1 and it goes on with the next business; the summary.md marks it with ⚠ (by stage) |
| the after of the snapshot fails | it quits, the previous.json is not rotated (the before is kept as well) |
| diff.py fails | it reports the failure, but the after.json is kept, and the diff can be done again by hand |
| there is no previous.json | it writes the baseline + a summary.md of the "first run" |

When the whole round fails, **the previous.json is not rotated** — the next run still has a baseline to compare with.

## The cleaning of the history

The reports are all kept by default. When they have to be cleaned regularly:

```bash
# keep the reports of the last 30 days
find /opt/srcradar/daily/reports -maxdepth 1 -mindepth 1 -mtime +30 -exec rm -rf {} +
# keep the snapshots of the last 30 days
find /opt/srcradar/daily/snapshots -maxdepth 1 -name '2*_after.json' -mtime +30 -delete
# the logs as well
find /opt/srcradar/daily/logs -name '2*.log' -mtime +30 -delete
```

## The troubleshooting

```bash
# look at the log of the last run
tail -f /opt/srcradar/daily/logs/$(ls -t /opt/srcradar/daily/logs/ | grep -v cron | head -1)

# the timestamp of the current baseline
python3 -c "import json; d=json.load(open('/opt/srcradar/daily/snapshots/previous.json')); print('captured_at:', d['captured_at']); print('rows:', sum(d['row_counts'].values()))"

# is the lock taken?
ls -la /opt/srcradar/daily/.lock
fuser /opt/srcradar/daily/.lock 2>/dev/null   # the PID that holds the lock
```

## The scoring of the web hashes

The table `web_hashes` has three fields that belong to the scoring:

| Field | Type | Who writes it | What it is |
|---|---|---|---|
| `score` | INTEGER NULL | the cron / by hand | a manual or automatic score between 0 and 100; NULL = it was not scored |
| `description` | TEXT NOT NULL DEFAULT '' | **by hand only** | a note of the operator; the cron neither reads nor writes it |
| `score_initialized_at` | TEXT NULL | the cron | NULL = it was not scored; not NULL = that hash was already scored by the cron (**it is never scored again automatically**)|

### The rules of the scoring (the baseline is 50; the INT goes from 0 to 100)

```
score = 50
  + 20  if any active web_subdomain has CJK chars in title (U+4E00-U+9FA5)
  + 20  if subdomain_count == 1
         OR COUNT(DISTINCT subdomain) over active web_subdomains == 1
          (that is, an HTTP and an HTTPS of the same site count as one site)
  - 20  if ALL active web_subdomains have empty/null title
```

### The behaviour of the cron

The cron (the daily run of `daily_monitor.sh` → it triggers `pdtm/import_scan_results.py`):

- **It only gives the initial score to the new hashes**: `pdtm/import_scan_results.py:_upsert_web_records`
  tells an "INSERT" from an "UPDATE", and it calls
  `daily/lib/score.py score-new --ids <ids>` for the **newly inserted** hash ids to write the score and the `score_initialized_at`
- **It does not touch the description**: the cron neither reads nor writes that field
- **It does not touch the score of a hash that exists already**: the SQL has the guard `score_initialized_at IS NULL`,
  a hash that has a score is not overwritten (not even when the content of the hash changed on the next scan)
- **It scores the hashes that went inactive as well**: the algorithm uses every ws (the historical ws of the `is_active=0` ones included),
  and an inactive hash still gets a score (30 or 50 usually, as the signal of "it went inactive")
- **A failed subprocess is swallowed** (best-effort): the write of the database is committed already, and a failed scoring is not retried;
  the hashes that are still unscored when the next run of the cron happens are scored then

### The initialization of everything at once

The init was already run on every hash that existed after the change of the schema. To run it again:

```bash
python3 lib/score.py init --db /opt/srcradar/db/recon.sqlite3
# {"mode": "init", "updated": N}
```

It is idempotent: it only touches the rows whose `score_initialized_at IS NULL`.

### Changing a score by hand (the REST endpoint of the dashboard)

```
POST /api/hash/<id>/edit
Content-Type: application/x-www-form-urlencoded

score=<int 0..100>          # required
description=<str>           # optional, '' by default
```

The JSON of the answer:
```json
{"ok": true, "id": 2, "score": 85, "description": "..."}
```

The errors: `400 bad params` / `404 hash not found` / `500 db error`.

Once the write succeeded, the dashboard updates `_State.cached_snap` + `_State.cached_snap_json` right away,
**there is no need to wait for the reload**. The length of the `description` is limited to 500 characters.

An example of `curl`:
```bash
curl -si -X POST -d "score=85&description=核心业务网关" \
  http://127.0.0.1:8765/api/hash/42/edit
```

### The dependency between the projects (pdtm → daily)

`pdtm/import_scan_results.py:persist()` calls
`daily/lib/score.py score-new --db <db> --ids <id1,id>` through a **subprocess** once the
database is written, to score the new hashes.

**It is a one way subprocess call from pdtm to daily**:

- a failure of daily does not affect pdtm (the write of the database is committed)
- pdtm does not have to change when daily upgrades score.py
- the resolution of the path: `Path(import_scan_results.py).parent.parent / "daily" / "lib" / "score.py"`
  it assumes that the two of them sit next to each other under `~/tools/<repo>/`

`cmd_scan_onesite()` (the light path of the dashboard, "add one row and see") **does not** call score.py:
a hash that was added by hand is left to the operator to score by hand in the dashboard.

## The scan of the URL assets (ffuf / URLFinder / gau) → `web_hash_urls`

An inventory of the assets at the granularity of the URL. **Triggered by hand from the dashboard** (`POST /<business>/scan-urls`)
**+ an optional daily stage** (`daily-url`, the user decided on 2026-08-26; see the section on the daily URL scan).
Before 2026-08-26 it was only by hand; after that it **is hooked to diff.py**, and the result of the scan is compared with the one of the last run, which produces the CSV of `added/changed/deactivated`.

### The table `web_hash_urls`

It hangs under `web_hashes`, it is an extension of N:1 (the same hash can share a set of URLs across several subdomains).

```
field                type               what it is
hash_id             INTEGER NOT NULL  → web_hashes.id
business_id         INTEGER NOT NULL  → businesses.id
subdomain           TEXT NOT NULL     the subdomain of the source at the time of the scan (Q4: a redundant field that makes the analysis easier)
source              TEXT NOT NULL     'ffuf' | 'urlfinder' | 'gau'
scheme/host/port    nullable           it comes from the split of urlparse
url                 TEXT NOT NULL     the whole URL
path                TEXT              the path only (it makes the aggregation easier)
status_code         INTEGER           measured by ffuf; NULL for urlfinder+gau
content_type/length INTEGER           measured by ffuf; NULL for urlfinder+gau
word_count          INTEGER           the number of the words that ffuf hit
first_seen/last_seen/fetched_at  TEXT NOT NULL
is_active           INTEGER NOT NULL DEFAULT 1
is_static           INTEGER NULL      the suffix of the path decides it: .js / .css (the user on 2026-08-26)
change_type         INTEGER NOT NULL DEFAULT 0  it is hooked to the diff (the user on 2026-08-26)
UNIQUE (hash_id, subdomain, url, source)   ← the logical key, there is no raw_json
```

The logical key: `(hash_id, subdomain, url, source)` — the same URL that was scanned three times (three sources) counts as three rows of their own,
so that the history can be split by source. `web_hashes.url_count` is the cache of the COUNT of the rows of `web_hash_urls.is_active=1`,
and it is written by an explicit UPDATE at the end of `scan_urls.py:persist()` (no trigger maintains it).

The migration: `python3 lib/migrate_urls.py /path/to/db` (it is idempotent).

#### The semantics of `is_static` (the user decided on 2026-08-26)

`scan_urls.py:_detect_is_static()` computes it for every path:
- it takes the lowercase suffix after the last `.` of the path
- there is no `.` → `0` (not static)
- the suffix is `js` or `css` → 1 (static)
- the other suffixes (`.png` / `.jpg` / `.svg` / `.woff` and the like) → `0` (not static, only js/css count)
- the suffix is empty (the path ends with a `.`, such as `/v1/`) → `0` (not static)

When it is written: `persist()` computes it on every INSERT / ON CONFLICT UPDATE (it does not drift).
The old rows (`is_static IS NULL`) are **ignored** by the colouring logic of the dashboard, but the string of the suffix of the path stays
compatible with the old logic (`p.endswith('.js') || p.endswith('.css')`).

The trigger `trg_whu_au` carries `AND NEW.is_static = 0` in its WHEN clause, so **a static row never takes part in the marking of the change_type**,
and it satisfies strictly that "only a subdomain that holds now && a path that is not js/css can change the change_type". See the section on the daily URL scan.

#### The bitmask of the `change_type` (the user decided on 2026-08-26, it is hooked to diff.py)

It is aligned with web_subdomains:
| Value | What it means |
|---|---|
| 0 | clean |
| 1 | inserted (the AI triggers it) |
| 2 | the content changed |
| 4 | reactivated (is_active 0→1 and OLD.last_seen < the last successful scan) |
| 6 | reactivated + the content changed |

The triggers: `trg_whu_ai` (the INSERT sets 1) + `trg_whu_au` (the UPDATE goes through the logic of the bitmask).
At the end of every `daily_monitor.sh` the diff.py reads the rows whose `change_type > 0`, classifies them and resets them to 0 atomically,
and the report produces the `added_web_hash_urls.csv` / `changed_web_hash_urls.csv` /
`deactivated_web_hash_urls.csv` (three new CSV, with the same format as the six tables that already exist).

### The three scanners (`pdtm/scan_urls.py`)

| Tool | Path | Command |
|---|---|---|
| ffuf | `~/go/bin/ffuf` | `ffuf -u https://host/FUZZ -w wordlist ...` |
| URLFinder (the Chinese edition by pingc0y) | `~/tools/URLFinder/URLFinder` | `URLFinder -m 3 -s all -o <dir> -u <url>` |
| gau | `~/go/bin/gau` | `gau --threads 5 --timeout 30 <domain>` |

**Note that the `--timeout` of gau is a number of seconds** (it is not `"30s"`).

The paths of the tools are hardcoded in the constants at the top of `scan_urls.py`, and moving the directory means editing the constants by hand.
URLFinder is a personal tool and **its output format is not fixed**; `run_urlfinder()` copes with a JSON dict, a JSON list and plain text.

### The entry point of the dashboard

| Path | What it does |
|---|---|
| `POST /<business>/scan-urls` | the form is submitted → a subprocess calls `pdtm/scan_urls.py scan-urls` |
| `GET /<business>/urls/<hash_id>` | the page of the details of the URLs, **it queries the SQL in real time by hash_id** (strictly lazy, it does not go into the snapshot) |
| a new column at the end of the tab "Site Details" of the page of the business | a button `URL details` at the end of every row → it goes to the GET page above (in a new window) |

A new folding panel "URL asset scan" at the top of the tab "Task Status" of the page of the business, the fields:
- `hosts` (required, ≤ 50 subdomains, one per line)
- `sources` (the checkboxes of a multiple choice: ffuf / urlfinder / gau, at least one)
- `wordlist` (only for ffuf, the default is `~/tools/wordlists/SecLists-master/Discovery/Web-Content/common.txt`)

The overall timeout is 600s (the user decided: a big wordlist can take 5min+ for a single host with ffuf). A timeout of the process / an
exception / a failure all come back through the unified `_render_scan_result` with a green/red panel, and it **does not reload on its own** (it puts
`_State.last_reload_ts = 0`, and the next GET reloads by itself).

### The page of the details of the URLs (`/<business>/urls/<hash_id>`)

`lib/dashboard.py:_serve_urls_detail()` renders it and it queries the `web_hash_urls` in real time (a separate
SQLite connection, a timeout of 5s, read-only). Every table corresponds to one `source` and **the order of the display is hardcoded**:
**urlfinder → ffuf → gau** (the decision of the user), and an empty source is skipped by itself.

#### The visual style (ant-table)

It is taken from `~/tools/URLFinder/demo.html` (~3KB of inline CSS, the whole Ant Design
library is not pulled in). Its features: the head of the table is a light grey `#fafafa`, the separator is a light grey of 1px, the numeric columns use tabular-nums, and the
background of a risky row is close to the token of ant-design (red `#fff1f0` / yellow `#fffbe6` / green `#fcffe6`).
See the `<style>` block at the end of `_serve_urls_detail`.

**The width of the columns adapts** (`table-layout: auto` + `word-break: break-all`), and it is the longest content of the column that decides it.
A long URL / path / title **is shown on several lines** and it does not scroll sideways; the head of the table keeps `white-space: nowrap`
so that it stays neat; a numeric column has `min-width: 48px` so that an empty string does not squash it to a width of 0.

#### The difference between the columns of the sources

| Column | urlfinder | ffuf | gau |
|---|---|---|---|
| URL / path / status / bytes / host:port / risk / danger / last_seen | ✓ | ✓ | ✓ |
| **title** | ✓ | — | — |
| **words** | ✓ (the place is kept) | ✓ | shown when there is data, an empty value — |
| **redirect** | ✓ | — | — |
| **link_source** | ✓ | — | — |

ffuf and gau have no data for the field urlfinder_extra (title/the fields / redirect / link_source),
so it is hidden; the column `words` is kept for every source so that the view stays aligned.

#### The 4 checkboxes of the filter (at the top of the page of the details of the URLs)

| The checkbox | The default | The field it filters | The remark |
|---|---|---|---|
| only the current subdomain | **not ticked** | `tr.data-host === sample_subdomain` | the early version compared with `data-subdomain`, and because the field `subdomain` is the same for every URL of a hash, it **always matched everything** and looked broken. With the real host of the URL it filters the hosts that do not belong (such as the test-internal.example that was scanned from the subdomain large-assets) |
| only status=200 | not ticked | `tr.data-status === '200'` | — |
| remove .js / .css | not ticked | `tr.data-static === '1'` | the backend decides by the suffix of the `path`; the `?query` and the `#fragment` are already stripped at the stage of `scan_urls._parse_url()` |
| **only the new ones or the ones that changed** (the user on 2026-08-26) | not ticked | `ct & 1` or `ct & 2` and `host == sample_subdomain` and `is_static=0` | it is tied to the **current subdomain && not static** (hardcoded, the user decided that it is **separate** from the toggle of removing the .js / .css). The semantics: it only looks at the rows of the "new / the content changed" that the stage of the scan-urls / daily-url of this round produced |

The 4 checkboxes **stack**; `applyFilters()` runs on every change event and it walks through the state of
all of them in order. The filter does not touch the DOM, it only sets `tr.style.display = 'none'`.

The `<h3 class="src-heading">` of every table shows `· visible N/M rows` (`count-chip`) in real time,
so that the reader sees right away that a checkbox triggered the filter.

#### The colouring of the default (the user on 2026-08-26)

The `<tr>` of the page of the details of the URLs **already get** the CSS class of the colouring
of the rows of "the current subdomain && not js/css && change_type > 0", and they are visible **without ticking any toggle**:

| class | The condition | The colour |
|---|---|---|
| `row-new` | `change_type & 1` (the INSERT, it is new) | a light green `#f6ffed` |
| `row-changed` | `change_type & 2` (the content changed) | a light blue `#e6f7ff` |
| `row-reactivated` | `change_type & 4` or `6` (it came back) | a light orange `#fff7e6` |

The class of the colouring stacks with the existing `.risk-high/.risk-med/.risk-low/.risk-info` (by status_code):
the CSS is `tr.risk-X.row-Y`, and they do not collide.
See the section `.urls-table tbody > tr.row-*` of the `<style>` block at the end of `_serve_urls_detail`.

#### The sorting of the head of the columns (only status / bytes)

Only the two columns `status` and `bytes` can be clicked to sort, the other ones cannot. The cycle of three states:

```
none → click → asc  → click → desc → click → none
```

- `status` and `bytes` are both numeric columns, they are compared by their value directly
- **The empty values go to the end** (the same in asc and in desc): the rows of `status_code IS NULL` / `content_length IS NULL` are pushed to the bottom of the table
- Every row is tagged with `data-_idx` before the sorting, and a third click restores the **original order of the insertion**
- The sorting does not reset the state of the filter (they are two independent dimensions, they stack)
- The head of the table: `⇅` grey by default, `▲` / `▼` blue when it is active

#### The source of the `sample_subdomain`

The top of the page of the details of the URLs shows the "subdomain of the source" of the current hash, the priority of the source of the value:

1. the subdomain of that hash in the table `web_subdomains` (`ORDER BY is_active DESC, subdomain`)
2. the fallback: the `subdomain` of the URL of the first row of that hash in `web_hash_urls` (all the URLs of the same hash usually come from the same subdomain)

It is also the base of the filter of the checkbox "only the current subdomain".

### The trade-off of not hooking it to the cron / to diff.py

~~The old version (before 2026-08-26)~~: the scan of the URL assets was only by hand from the dashboard, it was not hooked to the cron / to diff.py.
**The state of the moment**: it is hooked to diff.py (it produces the `added/changed/deactivated_web_hash_urls.csv`),
and it offers a `daily-url` stage so that the cron runs it on its own — see the section on the daily URL scan.

## The daily URL scan (the daily-url stage, the user decided on 2026-08-26)

The manual scan-urls of the dashboard fits the verification of a single point; the tracking of the assets
**in bulk and every day** is the job of the `daily-url` stage. It reads the table `web_subdomain_scan_schedule`, it runs on its own
the subdomains whose `enabled=1`, the result is written to the `change_type` by a trigger, and then the diff.py produces the CSV.

### The configuration: the table `web_subdomain_scan_schedule`

```sql
CREATE TABLE web_subdomain_scan_schedule (
    id, business_id, subdomain,
     sources     TEXT DEFAULT 'urlfinder',  -- separated by commas, urlfinder by default
     last_run_at TEXT,                      -- the time of the last successful scan
    enabled     INTEGER DEFAULT 1,
    created_at, updated_at,
    UNIQUE (business_id, subdomain)
);
```

**1 subdomain = 1 row**. A row of `enabled=0` is not run by the cron (the row stays).
The migration: `python3 lib/migrate_schedule.py /path/to/db` (it is idempotent).

### Adding a schedule to the dashboard / removing it

`POST /<business>/schedule/toggle`, the body:
```
subdomain=<FQDN>&action=add|remove
```
It answers a plain JSON: `{ok, action, created|removed, schedule_state}`.
The **add** is an `INSERT OR IGNORE` (the same (biz, sub) again → created=0, it is idempotent).
The **remove** is a `DELETE` (it is ok when the row does not exist).
When the operation succeeded the front end calls `location.reload()` to show the new state and the new wording of the button.

The place of the button: the top of the page of the details of the URLs (`/<business>/urls/<hash_id>`), the chip shows the state of the moment:

| The state | The wording of the button | data-action |
|---|---|---|
| `enabled` (it is in the schedule) | "Remove the daily scan" | `remove` |
| `absent` (it is not in the schedule) | "Add the daily scan" | `add` |
| `disabled` (it is paused) | "Remove the daily scan" | `remove` |

### `daily-url` stage

It goes at the very end of the fixed order of `run_one_business.sh`: `enscan → pdtm → icp → daily-url`.

```bash
./daily_monitor.sh -type pdtm,icp,daily-url   # run everything by hand
./daily_monitor.sh -type daily-url             # run the daily-url only
```

> ⚠ **the cron line does not change** (it hardcodes `-type pdtm,icp`). The daily-url does **not** go into the cron by default, the user triggers it
> by hand or hangs it on a cron of its own. When it is needed: `install_cron.sh` with `-type pdtm,icp,daily-url`, or
> `crontab -e` with a line of its own.

Inside `run_stage_daily_url()`:
1. it reads the rows of the schedule whose `enabled=1` → the list of the subdomains + the sources
2. it splits them into batches (every batch ≤ `DAILY_URL_BATCH_MAX`, **the user decided: 50000**, a constant of the documentation)
3. every batch calls `pdtm/scan_urls.py scan-urls --sources <the sources of the batch>` as a subprocess
4. **only when the exit code of the batch is 0** it UPDATEs the `last_run_at` of all the subs of that batch (= the current time)
5. a failure keeps the old `last_run_at` (the next run that catches up uses the "last success" as its baseline)
6. a failure of any batch → it sets bit3 (the exit code 8)

**The limit of 50000 of a batch is a constant of the documentation** (it is written in the `DAILY_URL_BATCH_MAX` at the top of
`run_one_business.sh`, and it is changed in one place when it has to be adjusted). **The real limit is `scan_urls.py:MAX_HOSTS = 50`** — the limit of the
subdomains of a single call of the subprocess is 50; `DAILY_URL_BATCH_MAX` is the limit of a batch of the wrapper layer, and it should in theory be far below that (to avoid a timeout of a single subprocess). The relation between the two:
- `DAILY_URL_BATCH_MAX=50000` is the limit of the "how many batches before it quits" of the wrapper layer (it keeps the run from running away)
- `scan_urls.MAX_HOSTS=50` is the "how many hosts at most in one process" inside scan_urls
- therefore the daily-url usually sends only one batch (≤ 50 subs), and the rest runs the next day

**The typical flow** (for instance: the schedule of the business `ExampleCo` holds 5 enabled subdomains):
```
run_stage_daily_url: 5 sub(s), 1 batch(es)
daily-url batch: 1-5 / 5, sources=urlfinder
[scan_urls] .../urlfinder: start
[scan_urls] .../urlfinder: got 304 urls
[scan_urls] .../urlfinder: persisted (new=304)
run_one] daily-url ok: 5 sub(s) scanned
```

When there is no row of the schedule it answers 0 (rc=0), it does not set bit3, and it writes one line "no schedule rows" in the log.

### The semantics of the revival against the bulk-deactivate-then-UPSERT (aligned with web_subdomains)

The `web_hash_urls.is_active` is currently **only flipped to 1 by the ON CONFLICT UPDATE** (it does not come back to life by itself).
The trigger `trg_whu_au` tells the two cases apart:
- **a real revival** (`OLD.last_seen < COALESCE(schedule.last_run_at, OLD.last_seen)`) → 4 / 6
- **a bulk-deactivate-then-UPSERT in the same run**: there is no row of the schedule → it is equivalent to `last_seen < last_seen` = FALSE → it is set to 0 (it does not change)

It means: when the daily scan is turned on for the first time, **none of the results of the first run is marked as a revival** (because there is no baseline of last_run_at).
That is on purpose — the first run is the baseline, and only after that can there be a diff.

### The handling of a failure

| The point of the failure | What it does |
|---|---|
| the exit code of `scan_urls.py` is not 0 | the counter of the failed batches grows by 1; it **does not** write the `last_run_at` of that batch |
| a batch failed | exit 8; the `summary.md` marks "daily-url FAILED" |
| every batch failed | the same; the next day it catches up with the "last success" |
| the business has no enabled schedule | it is skipped, exit 0 (it does not set bit3) |
| the `-type` of `daily_monitor.sh` does not hold `daily-url` | that stage does not run; the other stages are not affected |

### The known problems (Q7)

| The symptom | The state |
|---|---|
| when the scanner is given a subdomain that is not in `web_subdomains.is_active=1`, it writes the "orphan URLs" to `web_hash_urls` | the front end of the dashboard checks the subdomain, but a bypass pollutes the table. When a soft FK check is added later, it needs an automatic deactivate |
| the output format of `URLFinder` is not stable (a personal tool, the version is not pinned) | the parser copes with it + three ways out: a JSON dict / a list / plain text |
| gau still outputs 0 URLs when wayback answers 429 | a failure of gau is a problem of the environment, the code catches it and logs it, and the write of the other sources is not affected |
| ffuf scans the outside of the network / the businesses of the others | it is **triggered by hand**, the risk is far lower than with the cron; but it is better to keep ffuf unticked by default on the form of the dashboard (urlfinder is ticked by default) |
| the daily-url stage is not part of the cron line (the decision of the user on 2026-08-26) | it is triggered by hand or it hangs on a cron of its own. When it is needed, `crontab -e` with a `-type daily-url` line; the default of `install_cron.sh` must not be edited |
| `web_hash_urls` grows fast (about 3000 rows per subdomain and per day, 5w subs a day = 15w rows) | there is **no cleaning** at the moment; the table will burst in the long run. It is better that the operator cleans the old data when it is needed (to do: a retention stage that archives what is older than 30 days) |
| the toggle "only the new ones or the ones that changed" only works once a trigger has written a change_type | the change_type is reset to 0 every time the diff ran at the end of the daily_monitor.sh; **the toggle is always empty before the next run of the cron**. To "keep seeing the diff of the last round", the state of the run_marker has to be kept (or the trigger has to stop resetting it — the same road as the one of web_subdomains) |
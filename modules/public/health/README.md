# the health module of the operation and of the monitoring (an optional plugin)

> English | [中文](README_zh.md)

> **The position of the module**: the plugin of the operation and of the monitoring, **it is completely optional**. The daily cron runs it once, it does
> 5 health checks (**the disk / the CPU / the memory / the cron tasks / the dashboard process**),
> it sends no mail when everything is OK; as soon as one of them is abnormal → it calls `modules/public/notify/notify_send.sh` directly
> and it reuses the GPG+Resend channel of the push (the body is desensitized + the attachment holds the whole values and it is encrypted).
> The goal of the design: **the capture of the anomalies + a silent success**, it does not compete with `daily_monitor` for the resources, and it does not pollute the DB.

## The navigation

- **This document**: §the boundary of the module / §the checks / §the installation / §the configuration / §the format of the mail / §the relation with notify / §the safety / §the operation / §the points of integration / §the description of the files
- **The upstream it depends on**: none — the health module is a leaf, **it does not depend on daily / pdtm / db**, it only reads the information of the OS such as `df` / `uptime` / `top` / `/proc/meminfo` / `crontab -l` / `pgrep`
- **The sibling it depends on**: `modules/public/notify/` (it reuses its `notify_send.sh` + the credentials of its `notify.conf`)
- **The dependencies of the binaries**: `python3` + `bash ≥4` + `pgrep` + `df` + `uptime` + `nproc` + `awk` + `crontab` + (on the side of the notification) `gpg` + `curl`
- **The relation with daily_monitor**: a **completely independent** cron entry, it is not hooked into `daily_monitor.sh`, and it does not write the DB

## I. The boundary of the module

| The item | The scope | The license |
|---|---|---|
| `health_check.sh` / `install.sh` / `install_cron.sh` | the integration layer that srcradar wrote | Apache-2.0 |
| `lib/health_check.py` / `lib/health_config.py` / `lib/thresholds.py` | written by srcradar | Apache-2.0 |
| `notify_send.sh` / `lib/notify_config.py` (reused) | the sibling module of srcradar | Apache-2.0 |
| `gpg` (GnuPG) | the package of the OS (apt: gnupg), the OpenPGP of the RFC 4880 | GPL-3.0 |
| `df` / `uptime` / `nproc` / `pgrep` / `awk` | coreutils / procps | GPL / of the MIT style |
| `crontab` | the package of the OS (apt: cron) | the license of cron (of the BSD style) |
| the HTTP API of Resend | the delivery of the transactional mail of a third party | the ToS of Resend.com |

**What srcradar does not maintain**: GnuPG / coreutils / the daemon of cron / the API of Resend.
This module does only four things: **the 5 checks + the decision against the thresholds + the rendering of the report + the reuse of the push of notify**.
**It does not** do: the cleaning of the disk / the starting of a process / an automatic repair — it only detects and it alarms.

## II. The checks (5 of them)

### 1. The disk (disk)
- **The input**: `df -P` (the POSIX format, it avoids the GNU extension of the name of the columns that differs from one distribution to another)
- **The parsing**: the field `Capacity` of the 5th column, the regex `^(\d+)%$`, and from the 6th column the `Mounted on`
- **The decision**: the `used_pct` of every mount point is compared with the table of the thresholds
  - `used_pct >= DISK_ALERT_PCT` → alert
  - `DISK_WARN_PCT <= used_pct < DISK_ALERT_PCT` → warn
  - otherwise ok
- **The example** (the output of v1 at the moment):
  ```
  /dev/vda2   61806672 45674456  13494684     78% /
  tmpfs       374360   16304     358056       5% /run
  ```
  The module picks the mount points of `>= 80%` (here the vda2 is at 78% < the threshold of 80% → it does not alarm).
- **The platforms**: `df -P` is the same on Linux/macOS/AIX/Solaris, and it is not supported on Windows (the goal of the design of this module = a Linux server)

### 2. The CPU (cpu)
- **The load average**: `uptime` parses `load average: x, y, z` (1m / 5m / 15m)
  - the threshold: `load5 / nproc >= CPU_LOAD5_PER_NPROC` (2.0 by default)
  - the command `nproc` reads `/proc/cpuinfo` as a fallback
  - the decision: above the threshold → warn (it does not tell an alert from a warn; a high load usually means that there is a load, but not necessarily a failure)
- **The instantaneous usage**: `top -bn1 | grep '^%Cpu'`, the regex takes the `id` (the idle), `used = 100 - idle`
  - the threshold: `used >= CPU_PCT_ALERT` (95 by default)
  - the decision: above the threshold → warn
- **The trade-off of the design**: the load of 5min is smoother than the one of 1min, and it avoids the false positives of the occasional spike of the cron; the instantaneous usage only
  reflects the moment of the sample, and it is an auxiliary information

### 3. The memory (memory)
- **The input**: `/proc/meminfo`, it takes `MemTotal` + `MemAvailable`
- **The formula**: `used_pct = (MemTotal - MemAvailable) / MemTotal * 100`
- **The decision**: `used_pct >= MEM_ALERT_PCT` (90 by default) → alert
- **Why MemAvailable and not MemFree**: the `MemAvailable` of the Linux kernel 3.14+
  is the estimated memory that the applications can really use, the reclaimable cache is deducted, and it is more accurate than `MemFree`

### 4. The cron tasks (cron)
- **The parsing**: the output of `crontab -l`, a simplified parser:
  - it skips the empty lines, the `#` comments and the `KEY=VAL` lines of the environment variables
  - it recognizes the 5 fields of the time (a number / `*` / `,` / `-` / `/`)
  - everything after the fields of the time is the command
- **The logs that are watched by default**: the `WATCHED_LOGS` of `config/health.conf`, by default:
  ```
  modules/main/daily/logs/dashboard_watchdog.log
  ```
  (a relative path is based on the root of the srcradar project; an absolute path is accepted as well)
- **The decision**: the `stat` of every path of `WATCHED_LOGS` gives the mtime, and `age_hours > CRON_MAX_AGE_HOURS` (25h by default) → warn
- **The trade-off of the design**: 25h and not 24h, so that the drift of a cron that crosses the midnight does not give a false positive

### 5. The dashboard process (dashboard)
- **The detection of the process**: `pgrep -f 'python3 lib/dashboard.py'`
  - at least 1 PID → ok
  - 0 PID → alert (the process died)
- **The mtime of the log of the watchdog**: the mtime of `modules/main/daily/logs/dashboard_watchdog.log`
  - it is compared with the same threshold as `CRON_MAX_AGE_HOURS` (25h)
  - out of time → alert (it means that the cron of the dashboard_watchdog died too, and it alarms together with it)
- **The trade-off of the design**: it detects the process and the mtime of the log at the same time, and it avoids the blind spot of a dead watchdog while the dashboard still runs

### The design of the fault tolerance

Every check function has its own try/except (and `run_all` wraps them once more), so **any exception of a check
only produces one alert Finding, and it does not bring the other checks down**. For instance, when the command `top` is
blocked by a seccomp, it only makes the cpu check report "the command top does not exist", and the checks of the disk / of the memory are not affected.

## III. The installation

```bash
# 1. install the dependencies of the OS
sudo apt-get install -y python3 coreutils procps cron gnupg curl
# python3 / df / uptime / nproc / awk / pgrep come with most of the distributions

# 2. write the example + the self check
bash modules/public/health/install.sh --check
# the expectation: every "OK ..."; there is no "缺失"/"FAIL"

# 3. the operator fills the real thresholds (it is optional; the defaults of the example are enough)
cp modules/public/health/config/health.conf.example \
   modules/public/health/config/health.conf
chmod 600 modules/public/health/config/health.conf
$EDITOR modules/public/health/config/health.conf

# 4. register the cron (every day at 03:15 Beijing)
bash modules/public/health/install_cron.sh install
crontab -l | grep health   # it verifies that it is registered

# 5. run a dryrun by hand (it sends no mail)
bash modules/public/health/health_check.sh --dryrun
cat modules/public/health/logs/health_report.md
```

> **The key point**: `config/health.conf` is already inside the `.gitignore`
> (`modules/public/health/config/health.conf`), and the real thresholds never reach a commit.

## IV. The configuration (config/health.conf)

The description of the fields (they are all optional; when it is empty → the `DEFAULT_THRESHOLDS` of `lib/thresholds.py` is used):

| The field | The type | The default | What it means |
|---|---|---|---|
| `DISK_WARN_PCT` | int | 80 | the usage of the disk >= this value is a warn |
| `DISK_ALERT_PCT` | int | 90 | the usage of the disk >= this value is an alert |
| `CPU_LOAD5_PER_NPROC` | float | 2.0 | the limit of the load of 5min / nproc |
| `CPU_PCT_ALERT` | int | 95 | the alarm line of the instantaneous usage of top -bn1 |
| `MEM_ALERT_PCT` | int | 90 | the alarm line of the usage of the memory |
| `CRON_MAX_AGE_HOURS` | float | 25 | the threshold of the timeout of the mtime of the cron log |
| `WATCHED_LOGS` | a list separated by spaces | dashboard_watchdog.log | the list of the paths of the logs that are watched |

**The credentials are not in this file**: `RESEND_API_KEY` / `FROM_ADDR` / `TO_ADDR` / `SUBJECT` /
`GPG_RECIPIENT` are **not** in this file, they are all in `../notify/config/notify.conf`,
and the health module reuses them directly, **it does not make the operator fill them a second time**.

## V. The format of the mail

**The condition of the trigger**: one of the 5 checks produces a warn / an alert Finding.
**No trigger**: everything is OK (it only writes an INFO to `logs/health.log`, and it sends no mail).

### The body (the text field, the desensitized version)

```
[srcradar] health 2026-05-15

overall: WARN (alert=0 warn=2 ok=10)

- [WARN][disk] mount point /data uses 85% (>80%)
- [WARN][cron] the log dashboard_watchdog.log did not update for 26.5h (>25h)

Encrypted attachment: health_report.md.gpg (OpenPGP, requires OpenKeychain to decrypt)
```

**The body does not hold**:
- the detailed values (above `85%` it does not expose `used=450GB` / `avail=80GB`)
- the absolute paths (above `/data` it does not expose `/dev/sda1` / the UUID)
- the PID of the process
- the raw line of the device of a mountpoint (the whole line of `df`)

### The attachment (the health_report.md in plain text, encrypted with GPG)

```markdown
# health report — run_id=20260515-031500

- generated_at: 2026-05-15T03:15:01+08:00
- date_placeholder: 2026-05-15
- overall: **WARN**  (alert=0 warn=2 ok=10)

## Summary

- [WARN][disk] mount point /data uses 85% (>80%)
- [WARN][cron] the log dashboard_watchdog.log did not update for 26.5h (>25h)

## Detailed findings

| Severity | Check | Message | Detail |
|----------|-------|---------|--------|
| OK | disk | every mount point < 80% | there are 6 mount points, the highest usage is 85% |
| WARN | disk | mount point /data uses 85% (>80%) | <details>...</details> |
| WARN | cron | the log dashboard_watchdog.log did not update for 26.5h | <details>...</details> |
| OK | dashboard | the dashboard is running (pid=12345) | <details>...</details> |
...
```

Before the push, `notify_send.sh` encrypts the attachment with `gpg -r <recipient> -e health_report.md`
into `health_report.md.gpg`, and the receiver decrypts it with OpenKeychain / gpg.

## VI. The relation with the notify module

```
modules/public/health/health_check.sh
    │
    ├─ it calls lib/health_check.py
    │   ├─ the 5 checks → list[Finding]
    │   ├─ render_report(findings)  → logs/health_report.md   (the attachment in plain text)
    │   └─ render_body(findings)    → logs/health_report.body.md  (the desensitized body)
    │
    └─ when there is an anomaly → it calls ../notify/notify_send.sh:
           bash notify_send.sh health_report.md health <run_id> <date_ph>
                                       │
                                       ├─ it reads ../notify/config/notify.conf
                                       │   (the operator already filled it in the notify module,
                                       │    the health module does not make the operator fill it again)
                                       ├─ gpg -r <GPG_RECIPIENT> -e health_report.md
                                       │   → health_report.md.gpg
                                       ├─ it writes ~/.cache/notify/payload.json
                                       └─ curl POST https://api.resend.com/emails
```

**The boundary of the reuse**:
- the health module **does not** read the content of `notify.conf` (the real credentials never reach the memory of the health module)
- the health module **does not** write `notify.conf` (the notify module takes care of itself)
- the health module only **calls** the command `notify_send.sh` (the arguments: `report.md`, `business_name='health'`, `run_id`, `date_ph`), and it is exactly the way `daily_monitor` calls it

**The fallback of a failure**: `health_check.sh` wraps the call of notify with `|| true`,
and a failed push does not affect the conclusion of the health itself (the log holds a WARN, and the exit code is still 0).

## VII. The safety

- **The isolation of the credentials**: `config/health.conf` goes through `chmod 600`, and it is inside the `.gitignore` (the wildcard `config/*` + the explicit `modules/public/health/config/health.conf`)
- **The reuse of notify.conf**: the real credentials show up exactly once, in `modules/public/notify/config/notify.conf`, and the health module calls it directly, so that there is no double maintenance of the real values
- **The desensitization of the body**: the field `text` of the mail only holds a summary such as `[WARN][disk] mount point /data uses 85%`, and it holds neither `used=450GB` / `avail=80GB` / the PID / the path of the device
- **The encryption of the attachment**: the whole `health_report.md` → `gpg --batch --yes --trust-model always -r <recipient> -e` → `health_report.md.gpg`, and the receiver decrypts it with OpenKeychain / gpg
- **The private key never leaves the receiver**: the side of the push only imports the public key, it never imports the private key (the same as the notify module)
- **The fallback of a failure**: a failed push of notify → health_check.sh still exits with 0 (so that the cron does not judge the health wrongly because of a failed push)
- **No cookie / token is persisted**: it does not write `~/.bashrc` / `~/.netrc` / `~/.config/httpie*`
- **The hygiene of the commit message**: no later commit exposes the internal paths / the tokens / the real names of the businesses; this README already uses the placeholder `your-domain.example` instead of a real domain

## VIII. The operation

| The symptom | The possible cause | What to do |
|---|---|---|
| `bash install.sh --check` reports "缺 pgrep" | procps is not installed | `sudo apt install -y procps` |
| `bash install.sh --check` reports "依赖缺失: ../notify/notify_send.sh" | the notify module is not installed | run `bash modules/public/notify/install.sh --check` first |
| `config/health.conf 不存在,用 example 作占位` | the operator did not copy the example | it is generally OK (the defaults are enough); copy it to change the thresholds |
| `health_check.py 失败,详见 health.log` | an exception of the Python | look at the stack trace of `modules/public/health/logs/health.log` |
| no mail arrives, and `logs/health.log` holds `notify 失败 rc=4` | the HTTP of Resend answers ≥400 | look at `~/.cache/notify/payload.json` and at the back office of Resend; the usual cause: the `attachments[].content` is not base64 (this module goes through `base64 -w 0`, without a line break) |
| no mail arrives but the report is generated OK | the fields of `notify.conf` are `REPLACE_WITH_*` | fill the real values in `modules/public/notify/config/notify.conf` |
| a false positive: dashboard_alert but the dashboard really runs | the `pgrep -f` did not match | look at the real command line of `ps aux | grep dashboard`; the threshold `pattern='python3 lib/dashboard.py'` and the command that starts `bin/dashboard_watchdog.sh` have to be the same |
| a false positive: cron_alert but the dashboard_watchdog runs | the watchdog did not write a new line (it was not restarted) | look at the last line of `modules/main/daily/logs/dashboard_watchdog.log`; if it runs every 5min and the mtime is new → the threshold or the path is wrong |
| a false positive across the midnight (00:30 cron_alert) | `CRON_MAX_AGE_HOURS=24` is too tight | use the default of 25h, or make it bigger |
| the parsing of the mount points of the df output fails when it holds a space | it is rare (a mount of Windows) | the target platform of this module = Linux, Windows is not supported |

## IX. The points of integration

The design of the `health` module is an **independent cron entry**, it is **not** embedded in `daily_monitor.sh`.
The reason:

1. **The decoupling**: the health check belongs to the layer of the operation, and it is a different domain of failure from the daily diff (the failure of the daily
   does not affect the health, and the other way round). When one of them dies, the other one still alarms
2. **The staggering of the schedule**: daily_monitor runs at 03:00 (it can last 30min+), the health runs at 03:15
   and they are 15min apart, so that they do not compete for the disk IO at the same moment
3. **The independent strategy of the receivers**: the health can later take a prefix of its own for the subject (`[srcradar] health`),
   apart from the daily diff (`[srcradar] daily diff`)

If the health really has to be embedded in the daily_monitor (it is not recommended):

```bash
# modules/main/daily/daily_monitor.sh (an example, it does not go into the repository)
HEALTH="${RECON_ROOT}/public/health/health_check.sh"
if [ -x "$HEALTH" ]; then
    bash "$HEALTH" --dryrun 2>>"$LOG_FILE" || true
fi
```

## X. The description of the files

| The file | The description |
|---|---|
| `health_check.sh` | the main entry (it calls lib/health_check.py → it calls notify_send.sh) |
| `install.sh` | it writes the example + it checks the dependencies (python3 / pgrep / df / uptime / nproc / awk / crontab / gpg / curl) |
| `install_cron.sh` | it registers the daily cron of 03:15 (it is independent of daily_monitor) |
| `lib/health_check.py` | the 5 check functions + the dataclass Finding + the rendering of the report |
| `lib/health_config.py` | the reader of config/health.conf (the same model as notify_config.py) |
| `lib/thresholds.py` | the constant `DEFAULT_THRESHOLDS` + a merge that is safe about the types |
| `config/health.conf.example` | the template of the placeholders, every field of the thresholds gets a reasonable default (it goes into the repository) |
| `config/health.conf` | the real thresholds that the operator fills, `chmod 600`, the `.gitignore` hides it (it does not go into the repository) |
| `logs/health.log` | the stdout / the stderr of every run are appended (a local cache, it does not go into git) |
| `logs/health_report.md` | the whole report of every run (the next run overwrites it, it does not go into git) |
| `logs/health_report.body.md` | the desensitized body of every run (the next run overwrites it, it does not go into git) |
| `logs/health_cron.log` | the stdout / the stderr of the cron (a local cache, it does not go into git) |
| `logs/.gitkeep` | a placeholder, it makes sure that the directory logs/ exists in the repository |
| `README.md` | this document |
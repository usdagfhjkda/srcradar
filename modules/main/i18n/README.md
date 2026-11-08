# modules/main/i18n — language module

> **Module role**: i18n infrastructure, **always installed**. It provides
> two message tables and a single language-resolution entry point, so the
> user-visible output of the CLI, the dashboard, the reports and the
> notification emails can be rendered in Chinese or English.
>
> This module carries **no business copy** — the message tables are data,
> and every caller looks its own strings up.

## Contents

- **This document**: §Module boundary / §Language resolution / §Usage / §Message table format / §The page level language of the dashboard / §Configuration / §File reference
- **Upstream dependencies**: none — no dependency on daily / pdtm / db, no DB writes, no network access
- **Binary dependencies**: `python3` (>= 3.12, same baseline as the other python modules in the repo) + `bash` >= 4 (associative arrays)
- **Depended on by**: the `srcradar` dispatcher, `daily` (dashboard / diff / reports), `notify` (email body), `health` (check reports), and every shell script with user-visible output

---

## 1. Module boundary

| Item | Scope | License |
|---|---|---|
| `i18n.py` | python side: locate the repository root / resolve the language / look up the table | Apache-2.0 |
| `i18n.sh` | shell side: the same, loading every key into an associative array in one step | Apache-2.0 |
| `bootstrap.py` | repository-root lookup shared by both sides | Apache-2.0 |
| `../lib/i18n.sh`, `../lib/i18n.py` | thin forwarders for callers under `modules/main/` | Apache-2.0 |
| `locales/zh.json` / `locales/en.json` | message table data | Apache-2.0 |
| `install.sh` | write `config/lang.conf` and validate the message tables | Apache-2.0 |
| `python3` | OS / language runtime | PSF |
| `bash` | OS / shell | GPL-3.0 |

**srcradar does not maintain**: python or bash itself. The message table
content is maintained by srcradar.

---

## 2. Language resolution

Every tool follows this one chain:

```
1. SRCDAR_LANG environment variable   highest priority, for CI and
                                     temporary overrides
2. the lang field in config/lang.conf  the operator's persistent choice
3. en as the default                   the fallback when no config exists
```

Language aliases accepted by `SRCDAR_LANG` and by `lang.conf`:

| Input | Normalised to |
|---|---|
| `zh` / `zh-CN` / `zh_CN` / `zh-Hans` / `cn` / `chinese` | `zh` |
| `en` / `en-US` / `en_US` / `english` | `en` |
| anything else | treated as unset, resolution continues |

**The default is `en`** — it matches the language of the documentation, and
a deployment that carries a `config/lang.conf` keeps the language of it.

---

## 3. Usage

### python side

```python
import sys
from pathlib import Path

# Put the i18n directory on sys.path (each caller computes its own depth)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "i18n"))
from i18n import t

print(t("dashboard.tab.status"))
print(t("install.module_ok", {"area": "main", "name": "pdtm"}))
```

`t()` resolves a missing key by trying the active language, then the
default language (`en`), and finally the key name itself.

### The language of a thread

The python side keeps the active language in a `threading.local`, so a
program that serves several requests at the same time can give each
request its own language:

```python
from i18n import set_lang, get_lang, reset_lang

set_lang("en")       # this thread renders in English
get_lang()           # "en"
reset_lang()         # drop it, the next lookup resolves it again
```

`set_lang()` takes a `thread_local` flag: with it (the default) the
language belongs to the calling thread, without it the language is
process wide, which is what a command line tool wants.

**A program that serves requests has to release the language of a
request when it is done**, or the next request that lands on the same
thread inherits it.

### shell side

```bash
source /path/to/srcradar/modules/main/i18n/i18n.sh

t dashboard.tab.status
t install.module_ok area=main name=pdtm
traw dashboard.tab.status        # the same without a trailing newline
i18n_lang                         # print the active language
```

The shell side **loads the message table on the first `t` call** (one
python fork exports every key/value pair), so later calls only do bash
string substitution and never fork again.

---

## 4. Message table format

`locales/<lang>.json` is a **flat object** with string keys and string
values:

```json
{
  "dashboard.tab.status": "任务状态",
  "install.module_ok": "[{area}/{name}] OK"
}
```

### Placeholders

A value may contain `{name}`; the caller passes `name=value` on the shell
side and `{"name": value}` on the python side.

The python side also accepts the positional form `{0}` / `{1}` when
several non-dict arguments are given.

### Key naming

`<domain>.<subdomain>.<key>`, all lowercase, dot separated. Current
domains:

| Domain | Covers |
|---|---|
| `common.*` | shared words (OK / failed / none / unknown ...) |
| `install.*` | the checklist and install results of the top-level `install.sh` |
| `check.*` | the environment check output of `check.sh` |
| `i18n.*` | this module itself |
| `dashboard.common.*` | the words the dashboard shares: the business, the count, the share |
| `dashboard.tab.*` | the tabs of the dashboard |
| `dashboard.overview.*` | the overview: the cards, the sections and the tables of the assets |
| `dashboard.sites.*` | the site detail page: its filters, its notes and the score control |
| `dashboard.ports.*` / `dashboard.risk.*` / `dashboard.companies.*` / `dashboard.mapp.*` | the other tabs of the dashboard |
| `dashboard.scan_form.*` / `dashboard.scan_urls_form.*` / `dashboard.katana_form.*` | the forms that start a scan |
| `dashboard.scan_result.*` / `dashboard.urls.*` | the result of a scan and the URL detail page |
| `daily.summary.*` | the daily report `summary.md` |
| `notify.body.*` | the notification email body |
| `health.report.*` / `health.check.*` / `public.health_check.*` | the health check report |
| `main.<file>.*` | the output of the tools of `modules/main/` |

### Adding a new string

1. Add the same key to **both** `locales/zh.json` and `locales/en.json`
2. Replace the hardcoded string at the call site with `t("key", {...})`
3. Run `bash modules/main/i18n/install.sh --check` to confirm both tables
   are still valid JSON

**The two tables must stay in sync** — a missing key falls back to `zh`
without raising an error, which shows up as mixed Chinese and English
rather than as a failure.

---

## 5. The page level language of the dashboard

The dashboard is the one place where the language is a setting of a
single page rather than of the whole program: the operator picks a
language in `config/lang.conf`, and a visitor can switch the page to the
other language without touching the configuration.

| Where | What it does |
|---|---|
| `config/lang.conf` | the default language of every page |
| `?lang=zh` / `?lang=en` | overrides the default for that one request |
| the switcher in the header | links to the same page with the other language |

The rules:

- The language of a request comes from the `lang` query parameter; when
  it is missing or not a known language, the configured language is used.
- The pages are cached **per language**, so a page in one language is
  never served for a request in the other one.
- The `<html lang="...">` attribute follows the language of the page.
- The override is per request, not per session: it does not change the
  configuration and it does not survive a reload without the parameter.

---

## 6. Configuration

`config/lang.conf` is generated by `modules/main/i18n/install.sh` on the
first install:

```
lang=zh
```

The file lives under `.gitignore` (`config/*`) and is not tracked — the
language is a **deployment-side choice**, not part of the code. Delete it
and a later install regenerates it.

`write_db_conf()` in the top-level `install.sh` writes `config/db.conf`,
while this module's `install.sh` writes `config/lang.conf`; both follow
the same convention.

### Switching language

```bash
# persistent switch
sed -i 's/^lang=.*/lang=en/' config/lang.conf

# temporary switch (highest priority)
SRCDAR_LANG=en ./srcradar daily daily_monitor
```

The dashboard reads the same configuration, and a page can be switched
with the query parameter: `http://127.0.0.1:8765/?lang=en`.

---

## 7. File reference

| File | Purpose |
|---|---|
| `i18n.py` | python side: `t()` / `resolve_lang()` / `load_table()` / `set_lang()` / `get_lang()` / `reset_lang()` |
| `i18n.sh` | shell side: `t()` / `traw()` / `i18n_lang()` / `i18n_load()` |
| `bootstrap.py` | repository-root lookup, shared by the python side |
| `../lib/i18n.sh` | thin forwarder so `modules/main/` callers use the short `../lib/` path |
| `../lib/i18n.py` | thin forwarder for the python callers under `modules/main/` |
| `locales/zh.json` | Chinese message table |
| `locales/en.json` | English message table |
| `install.sh` | validate the message tables and write `config/lang.conf` |
| `README.md` | this document |

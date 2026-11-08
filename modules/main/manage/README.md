# manage/ — the entry point of the operation of a business

> English | [中文](README_zh.md)

It manages the "adding of a business" and the manual operations of a
business that follow. Every data that belongs to a business (the name
of the business, the switches of the stages, the seed companies, the
scopes) shows up exactly once, in the database or in `seeds/` — every
module (`db_align` / `pdtm` / `ymicp` / `daily`) reads it from the
database and never hardcodes the name of a business.

## The content of the directory

```
manage/
├── README.md                # this file
├── add_business.sh          # the main script that adds a business
├── set_config.sh            # shows / changes the recon_business_config of one business
├── set_lang.sh              # shows / changes the language of the user-visible output
└── set_resolvers.sh         # shows / changes the DNS resolver set that is in effect
├── set_lang.sh              # shows / changes the language of the user-visible output
└── seeds/                   # the seed data of the businesses (TSV, tracked by git)
```

## Adding a business: `add_business.sh`

```bash
./manage/add_business.sh -n <business name>
                         [--enabled 0|1] [--web 0|1] [--tcp 0|1] [--icp 0|1]
                         [-s <seed.tsv>] [-i <input_dir>] [-d <db.sqlite3>]
```

`--enabled/--web/--tcp/--icp` default to `1, 1, 0, 1` (leave them out
when the spec does not change). A value has to be `0` or `1`.

**Note**: the config row uses `INSERT OR IGNORE`, so a config row that
**already exists** is **not** overwritten by the flags — that protects
the manual tuning of the operator afterwards. Use `set_config.sh` to
change a config that exists.

The steps of the run (the order is fixed):

| Step | Table / action | Default |
|---|---|---|
| 1 | `INSERT OR IGNORE INTO businesses` | — |
| 2 | `INSERT OR IGNORE INTO recon_business_config` | enabled=1, web=1, tcp=0, icp=1 |
| 3 | with `-s`: it reads the company from the seed TSV → the `companies` table | the group column is passed through |
| 4 | with `-i`: it calls `pdtm/scope_import.sh` to fill the scopes | target.txt → measurable, exclude.txt → not measurable |

It **deliberately does not** run `db_align -n -all` on its own: that
stage takes a long time and may trigger the risk control of AQC, so the
operator has to trigger it explicitly. The script prints the command of
the next step at the end.

### The format of the seed TSV

- the columns are separated by `\t` (TSV), the first line holds the header
- the required column: `name` (→ `companies.unit_name`)
- the optional column: `group` (→ `companies.group`)
- the extra columns (such as `pid` / `legal_person` / `status` of the old format) are ignored
- a line that starts with `#` is a comment and is skipped

An example of `seeds/example.tsv` (in case it is rebuilt later):

```tsv
name\tgroup
ExampleCo子公司有限公司\t核心
ExampleCo(湖南)信息技术有限公司\t核心
某弱关联公司\tE组-弱关联
```

### The format of the input_dir

See the header comment of `../pdtm/README.md` and of `scope_import.sh`:

- required: `target.txt` (one domain / asset per line; the measurable assets)
- optional: `exclude.txt` (one per line; the assets that are not measurable)

`scope_import.sh` runs `check_wildcard.sh` first to probe the wildcard
resolution, and it sets `is_wildcard=1` when it hits one.

## The daily cron that follows

Once the row of the business is in the database:

- `enabled=1` by default, the cron (every day at 03:00, `daily/install_cron.sh`) runs pdtm + icp on its own, following the `recon_business_config` (see `../daily/README.md`, the section on installing and removing the cron)
- the enscan stage is **not** part of the cron, it has to be run by hand when the assets have to be pulled again:
  ```bash
  cd ../db_align && ./bin/db_align -n '<business name>' -all
  ```

## The language of the output: `set_lang.sh`

The command line, the dashboard, the daily report and the mail body all
follow one setting. The resolution order is:

1. the `SRCDAR_LANG` environment variable (temporary, for the CI)
2. the `lang` field of `config/lang.conf` (the persistent choice)
3. `en` as the default

`set_lang.sh` reads and writes the `lang` field, so it changes the
persistent choice:

```bash
./manage/set_lang.sh                    # shows the language in effect
./manage/set_lang.sh --lang zh          # switches to Chinese
./manage/set_lang.sh --lang en          # switches to English
```

The installer of the i18n module (`i18n/install.sh --lang`) never
overwrites an existing `config/lang.conf`, because the choice of the
operator wins there. `set_lang.sh` is the deliberate switch.

The aliases are normalised (`zh-CN` / `cn` / `chinese` → `zh`, `en-US` /
`english` → `en`), an unknown language is refused and the file is left
alone, and the file is written atomically.

`SRCDAR_CONFIG_DIR` overrides the directory of the config, exactly like the
lookup does, so a test or a container can keep its own setting.

## The DNS resolver set: `set_resolvers.sh`

The lists live in `modules/main/pdtm/resolvers/`, one file per set:

| The set | The content |
|---|---|
| `cn` | the resolvers of the domestic operators |
| `global` | the public resolvers of the world |

The domestic resolvers answer the foreign names differently from the
public ones, and the difference shows in the assets that are collected.

```bash
./manage/set_resolvers                  # shows the set in effect
./manage/set_resolvers --set cn         # the domestic set
./manage/set_resolvers --set global     # the public resolvers of the world
./manage/set_resolvers --set auto       # the language decides
```

The `auto` mode follows the language: the Chinese language means the
domestic set, every other language means the global set. The language is
the field that `set_lang.sh` writes, so the two settings stay in step.

The choice is the `resolver_set` field of `config/pdtm.conf`. The
installer writes the default only when the field is missing, so an install
does not reset the choice of the operator.

## The adjustment of the configuration of a business: `set_config.sh`

```bash
./manage/set_config.sh -n <business name>                            # only shows it
./manage/set_config.sh -n <business name> --disable                  # suspends the business
./manage/set_config.sh -n <business name> --enable                   # restores it
./manage/set_config.sh -n <business name> --web 0 --icp 1            # changes several fields
```

Only `-n` means read-only; the database is written when at least one
change flag is given; `--enable` and `--disable` exclude each other; the
values of `--web/--tcp/--icp` have to be `0` or `1`. Every change prints
the before and the after, and it marks the fields that really changed
apart from the ones that were already the same.

It errors out when the row of the business or the config row does not
exist (it tells the reader to run `add_business.sh` first), it never
creates one.

### The SQL directly (the escape hatch)

A few rare cases (adding a weakly related company and the like) are not
covered by `set_config.sh`, and they go through the SQL:

```sql
-- a weakly related company is added for the moment (it bypasses the noise filter)
INSERT OR IGNORE INTO companies (unit_name, business_id, nature_name, "group", created_at, updated_at)
VALUES ('某公司',
        (SELECT id FROM businesses WHERE business_name='业务名'),
        '企业', '临时', datetime('now'), datetime('now'));
```

## The known problems

### `companies.unit_name` is globally UNIQUE

The current schema is `UNIQUE(unit_name)` without `business_id`, so a
company name can only hang under one business. `add_business.sh -s` uses
`INSERT OR IGNORE` and **skips** a company of the same name that belongs
to another business (it does not rebind it).

The direction of the fix (not decided yet): turn the UNIQUE into
`UNIQUE(business_id, unit_name)` and add a one-off migration script for
the conflicts that already exist.

### The metadata of the seed (pid / legal_person / status) is not persisted yet

The multi column metadata TSV of the old `db_align/` (5 columns with
pid/legal_person/status and the like) passes only `name` and `group`
through this script; `pid` / `legal_person` / `status` are dropped (the
upstream command that consumed them is gone, and there is no consumer
downstream).

If these fields are ever needed again, a table
`biz_seeds(business_id, name, pid, group, legal_person, status)` is the
way to go, and `add_business.sh -s` would INSERT into it as well.

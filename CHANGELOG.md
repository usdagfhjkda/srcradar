# Changelog

All notable changes to srcradar will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-09-12

First public release of srcradar — a SRC (Security Response Center) asset
mapping and monitoring pipeline. This is an early-stage release: APIs are
not yet stable and breaking changes are expected in 0.2.x and 0.3.x before
1.0.0.

### Added

- Active recon pipeline (`modules/main/pdtm`): subfinder + dnsx + httpx +
  alterx + naabu orchestrated through the pdtm toolchain.
- Asset storage in a shared SQLite database (`modules/main/db/recon.sqlite3`)
  with schema covering `businesses`, `companies`, `mapp_records`, `scopes`,
  `web_subdomains`, `tcp_assets`, and snapshot history.
- Daily monitoring pipeline (`modules/main/daily`): snapshot diff +
  HTML dashboard at 03:00 local time via cron (`install_cron.sh`).
- Business management (`modules/main/manage`): `add_business.sh` to register
  new targets with optional auto-discovery via ymicp (`--auto`).
- Root-level dispatcher (`./srcradar <module> <script> [args]`): resolves
  `<module>/<script>.{sh,py}` under `modules/{private,main,public}/` and
  `exec`'s it, replacing the previous `cd modules/main/manage && ./...`
  invocation pattern. Use `./srcradar --list` to enumerate every dispatchable
  script; module resolution order is `private > main > public` so internal
  modules override shipped ones.
- Optional plugins:
  - `modules/public/db_align`: Go wrapper around ENScan_GO for legal-entity
    graph expansion (aqc / tyc / qimai). Marked **LOCKED** — requires
    manual auth gate per `modules/public/db_align/CLAUDE.md`.
  - `modules/public/ymicp`: Python client for the ymicp `/query/mapp` and
    `/query/web` endpoints (小程序 / 公众号 备案反查).

### CI / Quality

- GitHub Actions workflow (`.github/workflows/ci.yml`) covering five jobs:
  - `go (db_align)`: `go vet ./...` + `go test -race ./...`
  - `python (ruff)`: ruff 0.16 lint against `pyproject.toml`
  - `shell (shellcheck)`: shellcheck `-S warning` with `.shellcheckrc` `-x`
  - `secrets (gitleaks)`: fail-on-leak against full git history
  - `license-headers`: warn-only Apache-2.0 / SPDX header scan
- Dependabot config (`.github/dependabot.yml`) for Go modules under
  `modules/public/db_align` (weekly, patch + minor grouped) and GitHub
  Actions version bumps.
- CODEOWNERS scaffold (`.github/CODEOWNERS`) with explicit review paths
  for `db_align`, `pdtm`, `daily`, `manage`, and `ymicp`.
- Branch protection on `main` via repository ruleset (`protect-main`):
  requires pull request, blocks force-pushes, requires the four gating
  status checks (`go`, `python`, `shell`, `secrets`).

### Security

- `golang.org/x/net` upgraded from v0.48.0 to **v0.55.0** to address
  [CVE-2026-25680](https://github.com/advisories/GHSA-5cv4-jp36-h3mw)
  (DoS via excessive CPU time when parsing arbitrary HTML in the Go net
  HTML parser). srcradar's transitive use of this package flows through
  cdnmatch → projectdiscovery/cdncheck → x/net, and the vulnerable code
  path is not exercised by the `db_align` test suite, but the upgrade
  removes the advisory for users who exercise HTML parsing in downstream
  plugins.

### Known limitations

- **Test coverage is thin.** Only `db_align` has unit tests (3 of 13
  packages: `internal/permute`, `internal/resolver`, `internal/scope`).
  The daily / dashboard / ymicp Python modules have no automated tests.
  Total Go test code is ~200 lines against ~2,800 lines of Go (~7% Go
  coverage). The pipeline shell scripts are untested.
- **Two intentional ruff per-file-ignores** in
  `modules/main/daily/lib/dashboard.py`:
  - `RUF012` (mutable class default) — `cached_snap: dict = {}` is
    intentional singleton-scoped cache; only one Dashboard instance per
    process.
  - `BLE001` (blind `except Exception`) — at the request-handler
    boundary we want to log any failure and return 5xx rather than
    propagate.
- **cdnmatch is not built by CI.** It depends on the in-tree
  `projectdiscovery/cdncheck` vendor via `replace ../cdncheck`, which is
  `.gitignore`-d. Build path is `modules/main/pdtm/install.sh` on a
  real machine, not GitHub Actions.
- **CODEOWNERS contains `@YOUR_GITHUB_HANDLE` placeholders** (7
  occurrences). Resolve before inviting external collaborators.
- **Dependabot security alert `#1`** (CVE-2026-25680 / `x/net` DoS) was
  open at release time. It is addressed by this release's `x/net`
  upgrade but will only auto-close after the next Dependabot refresh
  cycle post-tag.
- **`db_align` is gated behind a manual auth step** (cookie acquisition
  for ENScan_GO data sources). First-run requires interactive setup; see
  `modules/public/db_align/README.md`.

### Compliance

- Apache-2.0 license (`LICENSE`).
- Upstream acknowledgments in `NOTICE` (12 upstream projects).
- Compliance addendum in `TERMS_ADDENDUM.md` (cookie-based auth limits,
  cross-border data transfer notes, operator liability).
- Information-collection only — no exploit code or PoC triggers. The
  pipeline does sub-domain enumeration, DNS resolution, port probing,
  HTTP probing, and URL asset scanning; nothing beyond.

### Migration notes

This is the first public release. No migration from a prior version is
required. Future versions will document migration steps here.

#!/usr/bin/env bash
#
# init.sh — the environment check + the db initialization.
#
# After the refactor (init.sh no longer clones or builds; that part moved to
# modules/*/install.sh):
#   - the environment check (go/python3/git + the full dependency list)
#   - --install-deps installs the missing dependencies (apt/dnf/brew)
#   - --init-db [PATH] calls modules/main/db/install.sh --path
#   - --check-schema checks db/schema.sql against the CREATE TABLE in the sources
#   - --offline      skips every network call (only build/check; if the git mv
#                    is already done it fast-passes)
#
# The clone/build of the modules is now scheduled by ./install.sh or
# ./install-internal.sh
# (they walk the install.sh of every module under modules/main, modules/public).
#
# Usage:
#   ./init.sh                  # only check + print the next-step hints
#   ./init.sh --check          # only check git/curl/docker
#   ./init.sh --check-deps     # the detailed environment check
#   ./init.sh --install-deps   # list the packages that would be installed (DRY-RUN, no sudo)
#   ./init.sh --install-deps --yes   # install them for real (sudo apt/dnf/brew)
#   ./init.sh --offline        # skip every network call
#   ./init.sh --init-db [PATH] # call modules/main/db/install.sh --path PATH
#   ./init.sh --check-schema   # check db/schema.sql against the sources
#
# Exit codes:
#   0   everything succeeded
#   1   bad arguments
#   2   a required command is missing (--check / --check-deps failed)
#   3   an upstream clone failed (this script does not own that)
#   4   the --install-deps auto install failed
#   5/6/7  init-db / sqlite / a missing schema.sql

set -euo pipefail

# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/modules/main/lib/i18n.sh"

# ---- the configuration (change it here to upgrade the upstream version) ----
# Note: the upstream vendors such as ENScan_GO / cdncheck are pulled by the
# install.sh of the matching module
# (modules/public/db_align/install.sh / modules/main/pdtm/install.sh)

# ---- the list of tools needed for the full feature set ----
# The categories:
#   - GO_REQUIRED  needed once at build time (Go >= 1.21)
#   - CORE         needed at runtime (in every mode)
#   - SCAN         recommended (without it some data cannot be collected, but
#                  nothing breaks)
# The fields:
#   cmd  the command name
#   min  the minimum version (empty = not checked)
#   pkg  the package name installed per OS by --install-deps (apt / dnf / brew)
# shellcheck disable=SC2034 # used by --check-deps / --install-deps sibling scripts
GO_MIN_VERSION="1.21"
# shellcheck disable=SC2034 # used by --check-deps / --install-deps sibling scripts
PY_MIN_VERSION="3.10"

# the list of tools needed for the full feature set
# the format: <cmd>|<min_version>|<apt_pkg>|<dnf_pkg>|<brew_pkg>|<category>|<note>
DEPS=(
    # --- build time (the Go toolchain) ---
    "go|1.21|golang-go|golang|go|go|$(t root.init.deps.go)|"
    # --- needed at runtime ---
    "bash|4.0|bash|bash|bash|core|$(t root.init.deps.bash)|"
    "python3|3.10|python3|python3|python3|core|$(t root.init.deps.python3)|"
    "docker|0.0|docker.io|podman|docker|core|$(t root.init.deps.docker)|"
    "flock|0.0|util-linux|util-linux|flock|core|$(t root.init.deps.flock)|"
    "sqlite3|3.0|sqlite3|sqlite|sqlite3|core|$(t root.init.deps.sqlite3)|"
    "git|0.0|git|git|git|core|$(t root.init.deps.git)|"
    # --- recommended (the scan / the URL assets) ---
    "subfinder|0.0|subfinder|subfinder|subfinder|scan|$(t root.init.deps.subfinder)|"
    "alterx|0.0|alterx|alterx|alterx|scan|$(t root.init.deps.alterx)|"
    "naabu|0.0|naabu|naabu|naabu|scan|$(t root.init.deps.naabu)|"
    "ffuf|0.0|ffuf|ffuf|ffuf|scan|$(t root.init.deps.ffuf)|"
    "gau|0.0|gau|gau|gau|scan|$(t root.init.deps.gau)|"
    "URLFinder|0.0||URLFinder||scan|$(t root.init.deps.urlfinder)|"
)

# ---- the paths ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DB_DIR="$SCRIPT_DIR/modules/main/db"
DB_PATH_DEFAULT="$DB_DIR/recon.sqlite3"

# ---- the argument parsing ----
MODE="all"
PASSTHROUGH_ARGS=()
INIT_DB_PATH=""
while [ $# -gt 0 ]; do
    case "$1" in
        --check)         MODE="check"; shift ;;
        --check-deps)    MODE="check-deps"; shift ;;
        --install-deps)  MODE="install-deps"; shift ;;
        --offline)       MODE="offline"; shift ;;
        --init-db)       MODE="init-db"; shift
                         [ $# -gt 0 ] && [[ "$1" != --* ]] && INIT_DB_PATH="$1" && shift ;;
        --check-schema)  MODE="check-schema"; shift ;;
        --yes|-y)        PASSTHROUGH_ARGS+=("$1"); shift ;;
        -h|--help)
            sed -n "2,32p" "$0"; exit 0 ;;
        *) echo "[init] unknown arg: $1" >&2; exit 1 ;;
    esac
done

log()  { printf "[init] %s\n" "$*"; }
warn() { printf "[init][warn] %s\n" "$*" >&2; }
err()  { printf "[init][err]  %s\n" "$*" >&2; }

# ---- the environment check ----
need_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        err "$(t root.init.missing_command cmd="$1")"
        return 1
    }
}

check_env() {
    local ok=1
    case "$MODE" in
        all|check)
            need_cmd git || ok=0
            need_cmd curl || ok=0
            ;;
    esac
    case "$MODE" in
        all|check)
            need_cmd docker || ok=0
            ;;
    esac
    [ "$ok" = 1 ] || return 1
    return 0
}

# ---- the OS detection ----
detect_os() {
    case "$(uname -s)" in
        Linux)
            if   command -v apt-get >/dev/null 2>&1; then echo "apt"
            elif command -v dnf      >/dev/null 2>&1; then echo "dnf"
            elif command -v yum      >/dev/null 2>&1; then echo "dnf"
            else echo "unknown"; fi ;;
        Darwin) command -v brew >/dev/null 2>&1 && echo "brew" || echo "unknown" ;;
        *) echo "unknown" ;;
    esac
}

# ---- the version comparison ----
ver_ge() {
    [ "$1" = "$2" ] && return 0
    local highest
    highest="$(printf "%s\n%s\n" "$1" "$2" | sort -V | tail -1)"
    [ "$highest" = "$1" ]
}

# ---- the tool detection + the version parsing ----
cmd_path() {
    local cmd="$1"
    command -v "$cmd" 2>/dev/null && return 0
    case "$cmd" in
        go)        [ -x /usr/local/go/bin/go ]    && echo /usr/local/go/bin/go    && return 0
                  [ -x /opt/homebrew/bin/go ]     && echo /opt/homebrew/bin/go     && return 0
                  [ -x /usr/lib/go/bin/go ]       && echo /usr/lib/go/bin/go       && return 0
                  ;;
        docker)    [ -x /usr/local/bin/docker ]    && echo /usr/local/bin/docker    && return 0 ;;
        python3)   [ -x /opt/homebrew/bin/python3 ] && echo /opt/homebrew/bin/python3 && return 0 ;;
        sqlite3)   [ -x /opt/homebrew/opt/sqlite/bin/sqlite3 ] && echo "$_" && return 0 ;;
    esac
    return 1
}

parse_version() {
    local cmd="$1" binp out ver
    binp="$(cmd_path "$cmd")" || return 1
    if [ "$cmd" = "go" ]; then
        out="$("$binp" version 2>/dev/null)"
    else
        out="$("$binp" --version 2>/dev/null | head -1)"
        if [ -z "$out" ]; then
            out="$("$binp" -version 2>/dev/null | head -1)"
        fi
    fi
    ver="$(printf "%s\n" "$out" | grep -oE "[0-9]+(\.[0-9]+){1,3}" | head -1)"
    printf "%s" "$ver"
}

check_one_dep() {
    local line="$1"
    local cmd min apt_pkg dnf_pkg brew_pkg category note
    IFS="|" read -r cmd min apt_pkg dnf_pkg brew_pkg category note <<<"$line"

    local status actual

    if cmd_path "$cmd" >/dev/null 2>&1; then
        actual="$(parse_version "$cmd")"
        if [ -n "$min" ] && [ "$min" != "0.0" ] && [ -n "$actual" ]; then
            if ver_ge "$actual" "$min"; then
                status="OK"
                printf "  %s %-10s %-10s %s\n" "$status" "$cmd" "$actual" "[$category] $note"
                return 0
            else
                status="X"
                printf "  %s %-10s %-10s %s\n" "$status" "$cmd" "$actual(<$min)" "[$category] $note"
                if [ "$category" = "go" ] || [ "$category" = "core" ]; then
                    MISSING_DEP=1
                else
                    MISSING_OPT=1
                fi
                return 1
            fi
        fi
        status="OK"
        printf "  %s %-10s %s\n" "$status" "$cmd" "[$category] $note"
        return 0
    fi
    status="X"
    printf "  %s %-10s %s\n" "$status" "$cmd(NOT FOUND)" "[$category] $note"
    if [ "$category" = "go" ] || [ "$category" = "core" ]; then
        MISSING_DEP=1
    else
        MISSING_OPT=1
    fi
    return 1
}

check_deps() {
    MISSING_DEP=0
    MISSING_OPT=0
    printf "%s\n" "$(t root.init.check_deps_header)"
    printf "%s\n\n" "$(t root.init.check_deps_legend)"
    for line in "${DEPS[@]}"; do
        check_one_dep "$line" || true
    done
    printf "\n"
    if [ "$MISSING_DEP" = 0 ] && [ "$MISSING_OPT" = 0 ]; then
        log "$(t root.init.deps_all_present)"
        return 0
    fi
    if [ "$MISSING_DEP" = 1 ]; then
        err "$(t root.init.missing_required_deps)"
        return 1
    fi
    warn "$(t root.init.missing_optional_deps)"
    return 2
}

install_deps() {
    local auto_apply=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --yes|-y) auto_apply=1; shift ;;
            *) shift ;;
        esac
    done

    local os missing_pkgs cmd min apt_pkg dnf_pkg brew_pkg category note pkg_name
    os="$(detect_os)"
    log "$(t root.init.os_detected os="$os")"

    if [ "$os" = "unknown" ]; then
        err "$(t root.init.unsupported_os)"
        return 4
    fi

    MISSING_DEP=0
    MISSING_OPT=0
    missing_pkgs=()

    for line in "${DEPS[@]}"; do
        IFS="|" read -r cmd min apt_pkg dnf_pkg brew_pkg category note <<<"$line"
        if cmd_path "$cmd" >/dev/null 2>&1; then
            continue
        fi
        case "$os" in
            apt)   pkg_name="$apt_pkg" ;;
            dnf)   pkg_name="$dnf_pkg" ;;
            brew)  pkg_name="$brew_pkg" ;;
        esac
        if [ -z "$pkg_name" ]; then
            warn "$(t root.init.no_package_for_cmd cmd="$cmd" os="$os")"
            MISSING_OPT=1
            continue
        fi
        missing_pkgs+=("$pkg_name")
        if [ "$category" = "go" ] || [ "$category" = "core" ]; then
            MISSING_DEP=1
        else
            MISSING_OPT=1
        fi
    done

    if [ "${#missing_pkgs[@]}" = 0 ] && [ "$MISSING_OPT" = 0 ]; then
        log "$(t root.init.nothing_to_install)"
        return 0
    fi

    if [ "$auto_apply" = 0 ]; then
        cat <<EOF
$(t root.init.dryrun_missing)

  $os: ${missing_pkgs[*]:-$(t root.init.no_auto_packages)}

$(t root.init.dryrun_confirm)
  ./init.sh --install-deps --yes
EOF
        return 0
    fi

    log "$(t root.init.will_install missing_pkgs="${missing_pkgs[*]:-$(t root.init.no_auto_packages)}")"

    case "$os" in
        apt)
            warn "$(t root.init.need_sudo_apt)"
            sudo apt-get update || { err "$(t root.init.apt_update_failed)"; return 4; }
            sudo apt-get install -y "${missing_pkgs[@]}" || {
                err "$(t root.init.apt_install_failed)"
                return 4
            }
            ;;
        dnf)
            warn "$(t root.init.need_sudo_install)"
            sudo dnf install -y "${missing_pkgs[@]}" || {
                err "$(t root.init.dnf_install_failed)"
                return 4
            }
            ;;
        brew)
            brew install "${missing_pkgs[@]}" || {
                err "$(t root.init.brew_install_failed)"
                return 4
            }
            ;;
    esac

    log "$(t root.init.deps_installed)"
    return 0
}

# ---- init-db: forwarded to modules/main/db/install.sh ----
init_db() {
    local db_path="${1:-$DB_PATH_DEFAULT}"
    local db_install="$DB_DIR/install.sh"

    if [ ! -x "$db_install" ]; then
        err "$(t root.init.db_installer_missing db_install="$db_install")"
        return 5
    fi

    log "init-db (via modules/main/db/install.sh): $db_path"
    if bash "$db_install" --path "$db_path"; then
        log "init-db OK: $db_path"
        return 0
    else
        err "$(t root.init.init_db_failed)"
        return 6
    fi
}

# ---- check-schema: forwarded to modules/main/db/install.sh --check-schema ----
check_schema() {
    local db_install="$DB_DIR/install.sh"
    if [ ! -x "$db_install" ]; then
        err "$(t root.init.db_installer_missing_check_schema db_install="$db_install")"
        return 7
    fi
    log "check-schema (via modules/main/db/install.sh)"
    if bash "$db_install" --check-schema; then
        return 0
    else
        return 7
    fi
}

# ---- the next-step hints ----
next_step_hint() {
    cat <<EOF

$(t root.init.next_steps)

  1. $(t root.init.step_main)
       ./install.sh
     $(t root.init.step_internal)
       ./install-internal.sh

  2. $(t root.init.step_db_align)
       modules/public/db_align/install.sh
     $(t root.init.step_db_align_detail)

  3. $(t root.init.step_ymicp)
       modules/public/ymicp/install.sh

$(t root.init.done)
EOF
}

# ---- the main flow ----
main() {
    case "$MODE" in
        check-deps)
            check_deps
            return $?
            ;;
        install-deps)
            install_deps "${PASSTHROUGH_ARGS[@]}"
            return $?
            ;;
        init-db)
            init_db "${INIT_DB_PATH:-}"
            return $?
            ;;
        check-schema)
            check_schema
            return $?
            ;;
        check)
            if check_env; then
                log "$(t root.init.env_ok)"
                return 0
            else
                err "$(t root.init.env_check_failed)"
                return 2
            fi
            ;;
        offline)
            log "$(t root.init.offline_skip_network)"
            next_step_hint
            return 0
            ;;
    esac

    log "$(t root.init.mode mode="$MODE")"
    check_env || { err "$(t root.init.env_check_failed_needs_deps)"; return 2; }

    if check_deps >/dev/null 2>&1; then
        :
    else
        rc=$?
        if [ "$rc" = 1 ]; then
            warn "$(t root.init.missing_core_deps)"
        elif [ "$rc" = 2 ]; then
            warn "$(t root.init.missing_optional_deps_for_install)"
        fi
    fi

    next_step_hint
    return 0
}

main "$@"
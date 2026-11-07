#!/usr/bin/env bash
#
# modules/main/pdtm/install.sh — install the pdtm module (the scan pipeline)
#
# The pdtm module holds:
#   - pdtm/         the PD toolchain orchestrator
#   - cdnmatch/     a Go module (a projectdiscovery/cdncheck fork + local changes)
#   - *.sh / *.py   the scan pipeline (scan.sh / scanner.sh / pipeline.sh ...)
#
# Usage:
#   bash install.sh                  # the default: install everything
#   bash install.sh --check          # only check the dependencies
#   bash install.sh --cdnmatch-only  # only install cdnmatch
#   bash install.sh --pdtm-only      # only install the pdtm orchestrator + the PD toolchain
#   bash install.sh --uninstall      # roll back
#
# Exit codes:
#   0   success
#   1   bad arguments
#   2   a dependency is missing
#   3   the build failed
#   4   an artifact is missing

set -euo pipefail
# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/i18n.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# the helper resolves the resolver list that is in effect
# shellcheck source=modules/main/lib/resolve_resolvers.sh
source "$SCRIPT_DIR/../lib/resolve_resolvers.sh"
cd "$SCRIPT_DIR"

log()  { printf '[pdtm] %s\n' "$*"; }
warn() { printf '[pdtm][warn] %s\n' "$*" >&2; }
err()  { printf '[pdtm][err]  %s\n' "$*" >&2; }

usage() {
    sed -n '2,15p' "${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
}

# ---- check that go is in PATH (and try the paths it is often missed at) ----
check_go() {
    local go_bin=""
    if command -v go >/dev/null 2>&1; then
        go_bin="$(command -v go)"
    elif [ -x /usr/local/go/bin/go ]; then
        go_bin=/usr/local/go/bin/go
    elif [ -x /opt/homebrew/bin/go ]; then
        go_bin=/opt/homebrew/bin/go
    fi
    if [ -z "$go_bin" ]; then
        err "$(t main.pdtm_install.missing_go)"
        return 2
    fi
    log "go: $($go_bin version)"
    return 0
}

# ---- install the pdtm orchestrator (go install into ~/go/bin/) ----
install_pdtm_bin() {
    if command -v pdtm >/dev/null 2>&1 || [ -x "$HOME/go/bin/pdtm" ]; then
        log "$(t main.pdtm_install.already_present HOME="$HOME")"
        return 0
    fi
    log "go install pdtm -> $HOME/go/bin/pdtm"
    (cd /tmp && go install -v github.com/projectdiscovery/pdtm/cmd/pdtm@latest)
    [ -x "$HOME/go/bin/pdtm" ] || { err "$(t main.pdtm_install.binary_missing path="$HOME/go/bin/pdtm")"; return 3; }
    log "$(t main.pdtm_install.installed)"
    return 0
}

# ---- run the pdtm installer for the PD toolchain (the 5 mainline tools + cdncheck) ----
# The 6 tools are listed explicitly, to avoid `pdtm -ia` installing the whole
# default PD toolset
# (the Dockerfile:48 comment says about 25). Same reading as Dockerfile:50.
# The `replace ../cdncheck` of cdnmatch is satisfied by install_cdnmatch()
# vendoring it with git clone.
install_pdtm_tools() {
    local pdtm_bin="$HOME/go/bin/pdtm"
    [ -x "$pdtm_bin" ] || pdtm_bin="$(command -v pdtm || true)"
    [ -n "$pdtm_bin" ] || { err "$(t main.pdtm_install.not_installed)"; return 3; }
    log "pdtm -duc -i dnsx,httpx,subfinder,alterx,naabu,cdncheck"
    "$pdtm_bin" -duc -i dnsx,httpx,subfinder,alterx,naabu,cdncheck
    # shellcheck disable=SC2034 # miss counts missing tools; used in summary below
    local miss=0
    for t in dnsx httpx subfinder alterx naabu; do
        if [ ! -x "$HOME/.pdtm/go/bin/$t" ]; then
            warn "$(t main.pdtm_install.tool_missing t="$t" HOME="$HOME")"
        else
            printf "  OK %s\n" "$t"
        fi
    done
    return 0
}

# ---- install cdnmatch (a local Go module, it needs cdncheck vendored) ----
install_cdnmatch() {
    local cdn_dir="$SCRIPT_DIR/cdnmatch"
    local vendor_dir="$SCRIPT_DIR/cdncheck"
    local bin="$SCRIPT_DIR/bin/cdnmatch"

    if [ ! -d "$cdn_dir" ]; then
        err "$(t main.pdtm_install.cdn_dir_missing cdn_dir="$cdn_dir")"
        return 3
    fi
    if [ -x "$bin" ]; then
        log "$(t main.pdtm_install.cdnmatch_exists bin="$bin")"
        return 0
    fi
    if [ ! -d "$vendor_dir" ]; then
        log "$(t main.pdtm_install.cloning_cdncheck vendor_dir="$vendor_dir")"
        git clone --depth 1 https://github.com/projectdiscovery/cdncheck.git "$vendor_dir"
    fi
    (
        cd "$cdn_dir"
        go mod tidy
        mkdir -p "$SCRIPT_DIR/bin"
        go build -o "$bin" .
    )
    [ -x "$bin" ] || { err "$(t main.pdtm_install.cdnmatch_missing path="$bin")"; return 4; }
    log "$(t main.pdtm_install.cdnmatch_installed bin="$bin")"
    return 0
}

# ---- verify: the key artifacts exist ----
verify() {
    local rc=0
    if [ -x "$SCRIPT_DIR/bin/cdnmatch" ]; then
        printf "  OK cdnmatch\n"
    else
        warn "$(t main.pdtm_install.cdnmatch_not_installed)"
        rc=1
    fi
    if command -v pdtm >/dev/null 2>&1 || [ -x "$HOME/go/bin/pdtm" ]; then
        printf "  OK pdtm\n"
    else
        warn "$(t main.pdtm_install.pdtm_not_installed)"
        rc=1
    fi
    return $rc
}

# ---- install massdns (a hard dependency of shuffledns, GPL-3.0) ----
install_massdns() {
    local dst="/usr/local/bin/massdns"
    if [ ! -w /usr/local/bin ] && [ ! -w "$(dirname "$dst")" ]; then
        # fall back to the user bin when there is no write permission (common on this machine / in a container)
        dst="$HOME/.local/bin/massdns"
        mkdir -p "$(dirname "$dst")"
    fi
    if [ -x "$dst" ] || command -v massdns >/dev/null 2>&1; then
        _i18n_val=$(command -v massdns || echo "$dst")
        echo "$(t main.pdtm_install.massdns_exists path="$_i18n_val")"
        return 0
    fi
    log "git clone massdns -> /tmp/massdns"
    (cd /tmp && git clone --depth 1 https://github.com/blechschmidt/massdns.git)
    (cd /tmp/massdns && make)
    cp /tmp/massdns/bin/massdns "$dst"
    rm -rf /tmp/massdns
    [ -x "$dst" ] || { err "massdns install failed: $dst"; return 3; }
    log "$(t main.pdtm_install.massdns_installed dst="$dst")"
    return 0
}

# ---- install the wordlists (the Top mode + the Rolling mode) ----
# The source: SecLists (MIT) + the public Jhaddix wordlist (cleaned of noise)
#
# The install path falls back (in the style of the write permission check of
# install_massdns):
#   /opt/srcradar/wordlists              <- docker / root (the default global path)
#   $SCRIPT_DIR/wordlists/               <- the source checkout (a normal user, under the module dir)
#
# The dst it really lands in is written as wordlists_dir=<abs_path> in
# config/pdtm.conf, so that scan.sh / scanner.sh / pipeline.sh prefer it on
# the next run.
install_wordlists() {
    local dst="/opt/srcradar/wordlists"
    # write permission check: docker/root uses /opt; a normal user (the usual source checkout) falls back to the module dir
    if [ ! -w /opt ] && [ ! -w "$(dirname "$dst")" ]; then
        dst="$SCRIPT_DIR/wordlists"
        mkdir -p "$dst"
    else
        mkdir -p "$dst"
    fi

    # the wordlists that ship with the repo (they go out with the release), used
    # first:
    #   docker/wordlists/subdomains-top1million-20000.txt  the upstream file, unchanged
    #   docker/wordlists/dns-Jhaddix-clean.txt              the cleaned product of the upstream file
    # Both are MIT licensed, see NOTICE. When they are missing it falls back to
    # the manually cloned SecLists copy on this machine (if there is one).
    local repo_root
    local repo_wl
    repo_root="$(cd "$SCRIPT_DIR/../../.." && pwd)"
    repo_wl="$repo_root/docker/wordlists"
    local src_top=""
    local src_rolling=""
    if [ -f "$repo_wl/subdomains-top1million-20000.txt" ]; then
        src_top="$repo_wl/subdomains-top1million-20000.txt"
    elif [ -f ~/tools/wordlists/SecLists-master/Discovery/DNS/subdomains-top1million-20000.txt ]; then
        src_top=~/tools/wordlists/SecLists-master/Discovery/DNS/subdomains-top1million-20000.txt
    fi
    if [ -f "$repo_wl/dns-Jhaddix-clean.txt" ]; then
        src_rolling="$repo_wl/dns-Jhaddix-clean.txt"
    elif [ -f ~/tools/wordlists/dns/dns-Jhaddix.txt ]; then
        # the upstream file is on this machine but the cleaned product is not in the repo: clean it on the spot
        mkdir -p "$dst"
        awk '
            length($0) > 0 &&
            $0 !~ /^[.@*]/ &&
            $0 !~ /\./ &&
            length($0) <= 32
        ' ~/tools/wordlists/dns/dns-Jhaddix.txt \
          | sort -u > "$dst/dns-Jhaddix-clean.txt"
        src_rolling="$dst/dns-Jhaddix-clean.txt"
    fi

    if [ -n "$src_top" ]; then
        cp "$src_top" "$dst/"
        _i18n_val=$(wc -l < "$dst/subdomains-top1million-20000.txt")
        echo "$(t main.pdtm_install.top_wordlist_installed count="$_i18n_val")"
    else
        warn "$(t main.pdtm_install.top_wordlist_missing)"
    fi

    if [ -n "$src_rolling" ] && [ "$src_rolling" != "$dst/dns-Jhaddix-clean.txt" ]; then
        cp "$src_rolling" "$dst/"
    fi
    if [ -f "$dst/dns-Jhaddix-clean.txt" ]; then
        _i18n_val=$(wc -l < "$dst/dns-Jhaddix-clean.txt")
        echo "$(t main.pdtm_install.rolling_wordlist_installed count="$_i18n_val")"
    else
        warn "$(t main.pdtm_install.rolling_wordlist_missing)"
    fi

    # write the dst it really landed in into the wordlists_dir= field of
    # config/pdtm.conf
    # (the incremental write mode of write_config in modules/private/patches/install.sh)
    write_wordlists_config "$dst"
    return 0
}

# ---- write the runtime path fields into config/pdtm.conf ----
# An existing field with the same name has its value replaced; a missing one is
# appended; chmod 600 (the same as the other modules).
#
# 4 fields are written:
#   wordlists_dir   the wordlist dir (just installed by this module)
#   httpx_bin       the httpx executable
#   dnsx_bin        the dnsx executable
#   resolvers_file  the DNS resolver list (it ships with the repo)
#
# The last 3 used to be written by the deployment side, which this module does
# not own, so the config was still incomplete after the install: the downstream
# consumers that depend on the tool paths (the T1/T2 reverify helper /
# check_wildcard) failed because they could not find httpx
# or the resolver list. The installer writes all of them now, so it works
# right after the install.
#
# The paths are always absolute: the consumer (check_wildcard.sh) reads the
# file by that value directly, and its
# cwd is the caller rather than the repo root, so a relative path would not be
# found there.
write_wordlists_config() {
    local wl_dir="$1"
    local cfg="$SCRIPT_DIR/../../../config/pdtm.conf"
    local tool_dir="$HOME/.pdtm/go/bin"
    local resolvers="$SCRIPT_DIR/resolvers"
    # the set that the operator picked. The field is never overwritten, so
    # an install does not reset the choice; the default is the global set.
    local resolver_set_val="global"
    if [ -f "$cfg" ]; then
        resolver_set_val="$(sed -n 's/^[[:space:]]*resolver_set[[:space:]]*=[[:space:]]*//p' \
                               "$cfg" | head -1 | tr -d '"\''[:space:]')"
        [ -n "$resolver_set_val" ] || resolver_set_val="global"
    fi
    mkdir -p "$(dirname "$cfg")"

    # the path of the list that the set resolves to. The field of the conf
    # stays in the scope of the helper, so the custom set still resolves to
    # the path that the operator wrote.
    local resolvers_file_val
    resolver_set="$resolver_set_val"
    resolvers_file="$(sed -n 's/^[[:space:]]*resolvers_file[[:space:]]*=[[:space:]]*//p' \
                         "$cfg" | head -1 | tr -d '"\''[:space:]')"
    resolvers_file_val="$(resolve_resolvers_file || true)"
    if [ -z "$resolvers_file_val" ]; then
        resolvers_file_val="$resolvers/global"
    fi

    local tmp
    tmp="$(mktemp)"
    if [ -f "$cfg" ]; then
        awk '
            /^wordlists_dir=/ { next }
            /^httpx_bin=/     { next }
            /^dnsx_bin=/      { next }
            /^resolver_set=/  { next }
            /^resolvers_file=/ { next }
            { print }
        ' "$cfg" > "$tmp" || cp /dev/null "$tmp"
    fi

    cat >> "$tmp" <<EOF
wordlists_dir=$wl_dir
httpx_bin=$tool_dir/httpx
dnsx_bin=$tool_dir/dnsx
resolver_set=$resolver_set_val
resolvers_file=$resolvers_file_val
EOF

    mv "$tmp" "$cfg"
    chmod 600 "$cfg"
    log "config written: $cfg"
    log "  wordlists_dir=$wl_dir"
    log "  httpx_bin=$tool_dir/httpx"
    log "  dnsx_bin=$tool_dir/dnsx"
    log "  resolver_set=$resolver_set_val"
    log "  resolvers_file=$resolvers_file_val"
}

# ---- uninstall ----
uninstall() {
    # shellcheck disable=SC2115
    rm -rf -- "$SCRIPT_DIR/bin" "$SCRIPT_DIR/cdncheck"
    log "$(t main.pdtm_install.cleaned)"
    log "$(t main.pdtm_install.orchestrator_stays HOME="$HOME")"
    return 0
}

main() {
    case "${1:-}" in
        --check)
            check_go || exit 2
            log "$(t main.pdtm_install.deps_ok)"
            exit 0
            ;;
        --cdnmatch-only) install_cdnmatch || exit $? ;;
        --pdtm-only)
            check_go || exit 2
            install_pdtm_bin || exit $?
            install_pdtm_tools || exit $?
            ;;
        --uninstall) uninstall ;;
        --yes|"")
            check_go || exit 2
            install_pdtm_bin || exit $?
            install_pdtm_tools || exit $?
            install_massdns || exit $?
            install_cdnmatch || exit $?
            install_wordlists
            verify
            ;;
        -h|--help) usage; exit 0 ;;
        *) err "unknown arg: $1"; usage; exit 1 ;;
    esac
    return 0
}

main "$@"

#!/bin/bash
# backup_file.sh — keep exactly one .latest backup per source path.
#
# Usage:
#   backup_file.sh <source-file> [<source-file> ...]
#
# Behavior:
#   For each <source-file>, compute a stable key from its absolute path
#   (sha1[:12] of the path) and keep the backup as:
#     $DAILY_ROOT/lib/.backup/<key>.latest
#
# This script is the gating step before ANY code edit. Invoke from the
# Hermes `patch` workflow by re-reading the file first; the caller can
# pipe it in. Only one backup per source survives — older backups are
# overwritten. The git history of the file is the canonical record.
#
# Idempotent: running twice on the same file is safe.

set -eu

# 默认推断 repo 根: backup_file.sh 在 <repo>/daily/bin/, 向上两级即 repo 根。
# 用户仍可 export DAILY_ROOT=<abs-path> 覆盖。
DAILY_ROOT="${DAILY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/daily}"
BACKUP_DIR="$DAILY_ROOT/lib/.backup"

mkdir -p "$BACKUP_DIR"

if [ "$#" -lt 1 ]; then
    echo "usage: backup_file.sh <file> [<file> ...]" >&2
    exit 2
fi

key_for() {
    # Stable, short key derived from absolute path.
    # sha1sum is in coreutils; cut to 12 chars (48 bits, plenty).
    local abs
    abs="$(readlink -f "$1" 2>/dev/null || realpath "$1" 2>/dev/null || echo "$1")"
    printf '%s' "$abs" | sha1sum | cut -c1-12
}

for src in "$@"; do
    if [ ! -f "$src" ]; then
        echo "backup_file.sh: skip (not a regular file): $src" >&2
        continue
    fi
    key="$(key_for "$src")"
    dst="$BACKUP_DIR/${key}.latest"
    # Use cat to preserve the original mtime/atime metadata; cp -p would
    # also work but cat is enough — the .pyc cache invalidation rules key
    # off mtime, so we want the BACKUP to have its own (fresh) mtime and
    # the source untouched.
    cat -- "$src" > "$dst"
    chmod 0644 "$dst" || true
    echo "backup: $src -> $dst ($(wc -c <"$dst") bytes)"
done
#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# modules/main/lib/i18n.sh — thin forwarder to the i18n module.
#
# The message tables and the lookup helpers live in
# modules/main/i18n/. This file exists so that callers can reach them
# with the same short relative path they already use for
# modules/main/lib/load_config.sh:
#
#   modules/main/X/*.sh       source "$SCRIPT_DIR/../lib/i18n.sh"
#   modules/main/X/lib/*.sh   source "$SCRIPT_DIR/../../lib/i18n.sh"
#
# The target is located by walking up to the repository root, so this
# forwarder does not depend on its own depth.

_i18n_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [ ! -f "$_i18n_lib_dir/i18n/i18n.sh" ] && [ "$_i18n_lib_dir" != "/" ]; do
    _i18n_lib_dir="$(dirname "$_i18n_lib_dir")"
done

if [ -f "$_i18n_lib_dir/i18n/i18n.sh" ]; then
    # shellcheck source=../i18n/i18n.sh
    source "$_i18n_lib_dir/i18n/i18n.sh"
else
    printf '[i18n] ERROR: cannot locate modules/main/i18n from %s\n' \
        "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" >&2
    return 1 2>/dev/null || exit 1
fi

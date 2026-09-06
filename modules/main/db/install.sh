#!/usr/bin/env bash
#
# modules/main/db/install.sh — 装 db 模块(数据库 schema + fixture + init)
#
# db 模块组成:
#   - schema.sql         14 表 + 索引 + 触发器(权威 schema)
#   - init_db.py         Python 初始化(支持 PATH 已存在 → 自动备份)
#   - check_schema.py    schema drift 校对
#   - _test_init.sqlite3 测试 fixture(进仓)
#
# 用法:
#   bash install.sh                              # 默认:init_db 到 ../db/recon.sqlite3
#   bash install.sh --path /custom/path.db       # 自定义 db 路径
#   bash install.sh --check-schema               # 只校对 schema drift
#   bash install.sh --check                      # 只查依赖
#   bash install.sh --uninstall
#
# 退出码:0/1/2/3/4 同 pdtm 模块

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 默认 db 路径:同级目录 db/recon.sqlite3
DEFAULT_DB="$(cd .. && pwd)/db/recon.sqlite3"

log()  { printf "[db] %s\n" "$*"; }
warn() { printf "[db][warn] %s\n" "$*" >&2; }
err()  { printf "[db][err]  %s\n" "$*" >&2; }

usage() {
    sed -n "2,17p" "$0"
}

check_deps() {
    local rc=0
    command -v python3 >/dev/null 2>&1 || { err "缺 python3"; rc=2; }
    command -v sqlite3 >/dev/null 2>&1 || { err "缺 sqlite3"; rc=2; }
    [ -f "$SCRIPT_DIR/init_db.py" ] || { err "init_db.py 缺失"; rc=3; }
    [ -f "$SCRIPT_DIR/schema.sql" ] || { err "schema.sql 缺失"; rc=3; }
    return $rc
}

# ---- init-db ----
init_db() {
    local db_path="$1"
    local init_py="$SCRIPT_DIR/init_db.py"
    local schema_sql="$SCRIPT_DIR/schema.sql"

    log "init-db: $db_path (schema=$schema_sql)"
    if python3 "$init_py" "$db_path" "$schema_sql"; then
        log "init-db OK: $db_path"
        return 0
    else
        err "init_db.py 失败(看上面 [err] 行)"
        return 3
    fi
}

# ---- check-schema ----
check_schema() {
    local checker="$SCRIPT_DIR/check_schema.py"
    [ -f "$checker" ] || { err "check_schema.py 缺失"; return 3; }
    log "check-schema:校对 $SCRIPT_DIR/schema.sql"
    if python3 "$checker" "$SCRIPT_DIR/schema.sql"; then
        return 0
    else
        err "schema drift(看上面)"
        return 1
    fi
}

# ---- verify:确认 14 张表存在 ----
verify_db() {
    local db_path="$1"
    [ -f "$db_path" ] || { err "db 不存在: $db_path"; return 1; }
    # init_db.py 完成后等 sqlite 落盘,避免 race
    sleep 0.2
    local n
    n=$(sqlite3 "$db_path" "SELECT count(*) FROM sqlite_master WHERE type='table'" 2>/dev/null || echo 0)
    if [ "$n" -ge 14 ]; then
        printf "  OK db 包含 %s 张表 (>= 14)\n" "$n"
    else
        err "db 仅含 $n 张表(期望 >= 14)"
        return 1
    fi
    return 0
}

verify() {
    local db_path="${DB_PATH:-$DEFAULT_DB}"
    if [ -f "$db_path" ]; then
        verify_db "$db_path" || return 1
    else
        warn "db 不存在(将自动 init): $db_path"
    fi
    return 0
}

uninstall() {
    log "db 模块反安装:删除 recon.sqlite3(只删自动建的)"
    local db_path="${DB_PATH:-$DEFAULT_DB}"
    if [ -f "$db_path" ]; then
        warn "确认要删 $db_path 吗?手动 rm -f $db_path"
    fi
    return 0
}

main() {
    DB_PATH="$DEFAULT_DB"
    case "${1:-}" in
        --check)
            check_deps || exit $?
            log "db 模块依赖 OK"
            exit 0
            ;;
        --check-schema)
            check_deps || exit $?
            check_schema || exit $?
            ;;
        --path)
            [ $# -ge 2 ] || { err "--path 需参数"; exit 1; }
            DB_PATH="$2"
            shift 2
            check_deps || exit $?
            init_db "$DB_PATH" || exit $?
            verify_db "$DB_PATH" || exit $?
            ;;
        --uninstall) uninstall ;;
        --yes|"")
            check_deps || exit $?
            init_db "$DB_PATH" || exit $?
            verify_db "$DB_PATH" || exit $?
            ;;
        -h|--help) usage; exit 0 ;;
        *) err "unknown arg: $1"; usage; exit 1 ;;
    esac
    return 0
}

main "$@"

#!/bin/bash
# Расчёт настроек PostgreSQL под RAM конкретного хоста.
#
# Дефолты образа postgres (shared_buffers=128MB) рассчитаны на игрушечную базу,
# а сюда каждые 10 секунд пишутся метрики со всех нод. Плоские константы не годятся:
# числа, нормальные для дедика на 256 ГБ, не дадут postgres стартовать на VPS с 2 ГБ.
#
# Подключается и установщиком (deploy.sh), и апдейтером (scripts/apply-update.sh) —
# двух копий формул быть не должно, иначе при правке одной из них значения на свежей
# установке и после обновления разъедутся.

pg_tune_host_ram_mb() {
    local kb
    kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null)
    case "$kb" in
        ''|*[!0-9]*) echo 0; return 0 ;;
    esac
    echo $((kb / 1024))
}

pg_tune_clamp_mb() {
    local value=$1 min=$2 max=$3
    if [ "$value" -lt "$min" ]; then value=$min; fi
    if [ "$value" -gt "$max" ]; then value=$max; fi
    printf '%s' "$value"
}

pg_tune_add_env_key() {
    local env_file=$1 key=$2 value=$3
    if grep -q "^${key}=" "$env_file" 2>/dev/null; then
        return 0
    fi
    printf '%s=%s\n' "$key" "$value" >> "$env_file"
}

pg_tune_notify() {
    if command -v log_success >/dev/null 2>&1; then
        log_success "$1"
    else
        printf '%s\n' "$1"
    fi
}

# Дописывает в .env только отсутствующие ключи: значения, выставленные оператором
# вручную, остаются как есть.
tune_postgres_env() {
    local env_file=$1
    [ -f "$env_file" ] || return 0

    local ram_mb
    ram_mb=$(pg_tune_host_ram_mb)
    [ "$ram_mb" -gt 0 ] || return 0

    local keys="POSTGRES_SHARED_BUFFERS POSTGRES_EFFECTIVE_CACHE_SIZE POSTGRES_WORK_MEM
                POSTGRES_MAINTENANCE_WORK_MEM POSTGRES_MAX_WAL_SIZE"
    local missing=0 key
    for key in $keys; do
        if ! grep -q "^${key}=" "$env_file" 2>/dev/null; then
            missing=1
        fi
    done
    [ "$missing" -eq 1 ] || return 0

    local shared_mb cache_mb work_mb maint_mb wal_mb
    # Выше 8 ГБ буферный кэш перестаёт окупаться: чекпоинты дорожают, а страницы
    # всё равно продублированы в page cache ОС
    shared_mb=$(pg_tune_clamp_mb $((ram_mb / 4)) 128 8192)
    cache_mb=$(pg_tune_clamp_mb $((ram_mb * 3 / 5)) 512 196608)
    maint_mb=$(pg_tune_clamp_mb $((ram_mb / 20)) 64 2048)
    # work_mem выделяется на каждую сортировку, а не на соединение, и потолок здесь
    # держится у дефолта postgres (4 МБ) намеренно: пул бэкенда — до 120 коннектов,
    # и щедрый work_mem на маленьком хосте сложился бы в кратный перерасход RAM
    work_mb=$(pg_tune_clamp_mb $((ram_mb / 512)) 4 64)
    wal_mb=$(pg_tune_clamp_mb $((ram_mb / 8)) 1024 4096)

    printf '\n# PostgreSQL tuning (auto-calculated for %s MB RAM)\n' "$ram_mb" >> "$env_file"
    pg_tune_add_env_key "$env_file" POSTGRES_SHARED_BUFFERS "${shared_mb}MB"
    pg_tune_add_env_key "$env_file" POSTGRES_EFFECTIVE_CACHE_SIZE "${cache_mb}MB"
    pg_tune_add_env_key "$env_file" POSTGRES_WORK_MEM "${work_mb}MB"
    pg_tune_add_env_key "$env_file" POSTGRES_MAINTENANCE_WORK_MEM "${maint_mb}MB"
    pg_tune_add_env_key "$env_file" POSTGRES_MAX_WAL_SIZE "${wal_mb}MB"

    pg_tune_notify "PostgreSQL tuning added to .env (host RAM: ${ram_mb} MB)"
}

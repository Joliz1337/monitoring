#!/bin/bash
# Часовой пояс и NTP на хосте. Ожидает переменную TZ_NAME — её подставляет строкой выше
# build_time_sync_command().
#
# Демон времени берётся тот, что уже стоит на хосте: chrony (Debian/Proxmox),
# systemd-timesyncd (Ubuntu), ntpsec/ntp. Нет ни одного — ставится chrony.
# Внутри контейнера (LXC) часы принадлежат хосту: демон не ставится и не трогается,
# выставляется только пояс.
#
# stdout: NTPService=, NTPInstalled=, NTPManagedByHost=, затем вывод timedatectl show.
# stderr: причины сбоев. Код выхода 1 — пояс или демон настроить не удалось.
#
# Файл существует в двух копиях — node/app/services/ и panel/backend/app/services/:
# образы собираются из разных контекстов, общего пути у них нет. Тест панели
# сверяет, что копии совпадают.
set -u

NTP_UNITS="chrony.service systemd-timesyncd.service ntpsec.service ntp.service"
NTP_PACKAGE="chrony"
SYNC_WAIT_SECONDS=15

find_ntp_unit() {
    local unit fallback=""
    for unit in $NTP_UNITS; do
        [ "$(systemctl show -p LoadState --value "$unit" 2>/dev/null)" = "loaded" ] || continue
        if systemctl is-active --quiet "$unit"; then
            echo "$unit"
            return 0
        fi
        [ -n "$fallback" ] || fallback="$unit"
    done
    [ -n "$fallback" ] || return 1
    echo "$fallback"
}

install_ntp_daemon() {
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "no NTP daemon on host and apt-get is unavailable to install $NTP_PACKAGE" >&2
        return 1
    fi
    export DEBIAN_FRONTEND=noninteractive
    local output
    # Сначала из кеша apt; пустой или устаревший кеш — обновить списки и повторить
    output=$(apt-get install -y -qq -o DPkg::Lock::Timeout=120 "$NTP_PACKAGE" 2>&1) && return 0
    apt-get update -qq >/dev/null 2>&1
    output=$(apt-get install -y -qq -o DPkg::Lock::Timeout=120 "$NTP_PACKAGE" 2>&1) && return 0
    echo "apt-get install $NTP_PACKAGE failed: $(printf '%s' "$output" | tail -n 3 | tr '\n' ' ')" >&2
    return 1
}

failed=0
installed=no
managed_by_host=no
unit=""

if systemd-detect-virt --container --quiet; then
    managed_by_host=yes
elif ! unit=$(find_ntp_unit); then
    if install_ntp_daemon; then
        installed=yes
        unit=$(find_ntp_unit) || unit=""
    fi
    if [ -z "$unit" ]; then
        echo "no NTP daemon available on host" >&2
        failed=1
    fi
fi

if ! timedatectl set-timezone "$TZ_NAME"; then
    echo "failed to set timezone $TZ_NAME" >&2
    failed=1
fi

if [ -n "$unit" ]; then
    # set-ntp включает юнит, зарегистрированный в ntp-units.d; демон без регистрации — напрямую
    timedatectl set-ntp true 2>/dev/null || systemctl enable "$unit" >/dev/null 2>&1
    if systemctl restart "$unit"; then
        for _ in $(seq "$SYNC_WAIT_SECONDS"); do
            [ "$(timedatectl show -p NTPSynchronized --value)" = "yes" ] && break
            sleep 1
        done
    else
        echo "failed to restart $unit" >&2
        failed=1
    fi
fi

echo "NTPService=${unit%.service}"
echo "NTPInstalled=$installed"
echo "NTPManagedByHost=$managed_by_host"
timedatectl show
exit "$failed"

#!/usr/bin/env bash
# Проверка прокси-конфигураций на ноде — исполнитель для панели.
#
# Панель присылает готовые конфиги ядра и ссылку на бинарник (нода в GitHub не
# ходит: под жёсткой блокировкой он ей недоступен). Скрипт поднимает ядро на
# фиксированном локальном порту, гоняет пробы через socks и печатает по строке
# JSON на каждую проверку — панель читает их потоком.
#
# Зависимости: bash, curl, sha256sum. Namespace ноды даёт nsenter со стороны
# агента, здесь ничего дополнительного не требуется.
#
# Запуск: xray-test-runner.sh <base64-payload>
#         xray-test-runner.sh version
set -uo pipefail

RUNNER_VERSION="1.1.0"

TOOLS_DIR="/opt/monitoring-node/tools"
CORES_DIR="$TOOLS_DIR/cores"
# Порт зарезервирован от эфемерной выдачи (tune-sysctl.sh): иначе исходящее
# соединение могло бы занять его ровно между проверками
SOCKS_PORT=7501
CORE_START_TIMEOUT=5
PROBE_TIMEOUT=10
SPEED_TIMEOUT=20
SPEED_BYTES=10000000
DEGRADED_RTT_MS=1500
STALE_CORE_SECONDS=120

GENERATE_204_URL="https://cp.cloudflare.com/generate_204"
TRACE_URL="https://cloudflare.com/cdn-cgi/trace"
SPEED_URL="https://speed.cloudflare.com/__down?bytes=${SPEED_BYTES}"

WORKDIR=""
CORE_PGID=""

umask 077

cleanup() {
    stop_core
    [ -n "$WORKDIR" ] && [ -d "$WORKDIR" ] && rm -rf "$WORKDIR"
}
trap cleanup EXIT INT TERM

log() { printf '{"type":"log","line":"%s"}\n' "$(escape "$1")"; }

escape() {
    printf '%s' "$1" | tr -d '\r' | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n' ' ' | cut -c1-400
}

now_ms() { date +%s%3N; }

# ── ядра ────────────────────────────────────────────────────────────────────

# Ядро от прошлого прогона, переживившее аварийный выход, займёт порт и будет
# жечь CPU — снимаем такие до начала работы.
kill_stale_cores() {
    local pid etime
    for pid in $(pgrep -f "$CORES_DIR/" 2>/dev/null); do
        etime=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
        [ -n "$etime" ] && [ "$etime" -gt "$STALE_CORE_SECONDS" ] && kill -9 "$pid" 2>/dev/null
    done
}

ensure_core() {
    local name="$1" version="$2" url="$3" want_sha="$4"
    local path="$CORES_DIR/${name}-${version}"

    if [ -x "$path" ] && [ "$(sha256sum "$path" | cut -d' ' -f1)" = "$want_sha" ]; then
        printf '%s' "$path"
        return 0
    fi

    mkdir -p "$CORES_DIR" || return 1
    local tmp="${path}.tmp.$$"
    # --insecure безопасен: подмену ловит сверка sha256 ниже, а сертификат
    # панели может быть самоподписанным
    if ! curl -fsSL --insecure --max-time 180 "$url" -o "$tmp"; then
        rm -f "$tmp"
        return 1
    fi

    if [ "$(sha256sum "$tmp" | cut -d' ' -f1)" != "$want_sha" ]; then
        rm -f "$tmp"
        return 1
    fi

    chmod 0755 "$tmp" && mv -f "$tmp" "$path" || { rm -f "$tmp"; return 1; }
    printf '%s' "$path"
}

start_core() {
    local binary="$1" config="$2"
    setsid "$binary" run -c "$config" >"$WORKDIR/core.log" 2>&1 &
    local pid=$!
    CORE_PGID=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
    [ -n "$CORE_PGID" ] || CORE_PGID="$pid"

    local deadline=$(( $(date +%s) + CORE_START_TIMEOUT ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        kill -0 "$pid" 2>/dev/null || return 1
        if (exec 3<>"/dev/tcp/127.0.0.1/$SOCKS_PORT") 2>/dev/null; then
            exec 3>&- 2>/dev/null
            return 0
        fi
        sleep 0.1
    done
    return 1
}

stop_core() {
    [ -n "$CORE_PGID" ] || return 0
    kill -TERM "-$CORE_PGID" 2>/dev/null
    local waited=0
    while kill -0 "-$CORE_PGID" 2>/dev/null && [ "$waited" -lt 20 ]; do
        sleep 0.1
        waited=$((waited + 1))
    done
    kill -KILL "-$CORE_PGID" 2>/dev/null
    CORE_PGID=""
}

# ── пробы ───────────────────────────────────────────────────────────────────

# Печатает «min avg jitter»; пусто, если сервер не ответил ни разу
tcp_ping_ms() {
    local host="$1" port="$2" i start elapsed
    local best="" worst="" total=0 count=0
    for i in 1 2 3; do
        start=$(now_ms)
        if timeout 3 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null; then
            elapsed=$(( $(now_ms) - start ))
            count=$((count + 1))
            total=$((total + elapsed))
            if [ -z "$best" ] || [ "$elapsed" -lt "$best" ]; then best="$elapsed"; fi
            if [ -z "$worst" ] || [ "$elapsed" -gt "$worst" ]; then worst="$elapsed"; fi
        fi
    done
    [ "$count" -eq 0 ] && return 0
    printf '%s %s %s' "$best" "$((total / count))" "$((worst - best))"
}

# Печатает «ip ms». Без резолва в подробностях строки стояли бы прочерки
resolve_host() {
    local host="$1" start ip
    case "$host" in
        *[!0-9.]*) ;;
        *) printf '%s 0' "$host"; return 0 ;;
    esac
    start=$(now_ms)
    ip=$(getent ahostsv4 "$host" 2>/dev/null | awk 'NR==1{print $1}')
    [ -n "$ip" ] || ip=$(getent hosts "$host" 2>/dev/null | awk 'NR==1{print $1}')
    [ -n "$ip" ] || return 0
    printf '%s %s' "$ip" "$(( $(now_ms) - start ))"
}

curl_socks() { curl -s --socks5-hostname "127.0.0.1:$SOCKS_PORT" "$@"; }

# Последняя строка лога ядра со следами ошибки. Просто tail отдавал бы рабочий
# вывод («accepted tcp:…», «dialing TCP to …»), который ничего не объясняет.
core_failure_line() {
    local log="$WORKDIR/core.log"
    [ -f "$log" ] || return 0
    grep -iE 'fail|error|refused|timeout|rejected' "$log" 2>/dev/null | tail -1 | cut -c1-400
}

# ── основной цикл ───────────────────────────────────────────────────────────

emit_cell() {
    printf '{"type":"cell","index":%s,"verdict":"%s","reason":%s,"detail":"%s",' \
        "$1" "$2" "$3" "$(escape "$4")"
    printf '"tcp_min_ms":%s,"handshake_ms":%s,"rtt_ms":%s,"http_status":%s,' \
        "${5:-null}" "${6:-null}" "${7:-null}" "${8:-null}"
    printf '"exit_ip":%s,"exit_country":%s,"speed_mbps":%s,' \
        "${9:-null}" "${10:-null}" "${11:-null}"
    printf '"resolved_ip":%s,"dns_ms":%s,"tcp_avg_ms":%s,"tcp_jitter_ms":%s}\n' \
        "${12:-null}" "${13:-null}" "${14:-null}" "${15:-null}"
}

json_str() { [ -n "${1:-}" ] && printf '"%s"' "$(escape "$1")" || printf 'null'; }

run_cell() {
    local index="$1" core_name="$2" address="$3" port="$4" udp="$5" config_b64="$6"
    local tcp_ms="" tcp_avg="" tcp_jitter="" handshake="" rtt="" status=""
    local exit_ip="" exit_country="" speed="" resolved_ip="" dns_ms=""

    local resolved
    resolved=$(resolve_host "$address")
    if [ -n "$resolved" ]; then
        resolved_ip=$(printf '%s' "$resolved" | cut -d' ' -f1)
        dns_ms=$(printf '%s' "$resolved" | cut -d' ' -f2)
    else
        emit_cell "$index" "fail" '"DNS_FAIL"' "домен $address не резолвится"
        return
    fi

    if [ "$OPT_TCP" = "1" ] && [ "$udp" != "1" ]; then
        local tcp
        tcp=$(tcp_ping_ms "$address" "$port")
        if [ -z "$tcp" ]; then
            emit_cell "$index" "fail" '"TCP_REFUSED"' "порт $port недоступен" \
                null null null null null null null "$(json_str "$resolved_ip")" "${dns_ms:-null}"
            return
        fi
        tcp_ms=$(printf '%s' "$tcp" | cut -d' ' -f1)
        tcp_avg=$(printf '%s' "$tcp" | cut -d' ' -f2)
        tcp_jitter=$(printf '%s' "$tcp" | cut -d' ' -f3)
    fi

    local binary="${CORE_PATHS[$core_name]:-}"
    if [ -z "$binary" ]; then
        emit_cell "$index" "fail" '"CORE_START_FAILED"' "ядро $core_name недоступно" \
            "${tcp_ms:-null}" null null null null null null \
            "$(json_str "$resolved_ip")" "${dns_ms:-null}" "${tcp_avg:-null}" "${tcp_jitter:-null}"
        return
    fi

    WORKDIR=$(mktemp -d /tmp/mon-xtest.XXXXXX) || return
    printf '%s' "$config_b64" | base64 -d > "$WORKDIR/config.json" 2>/dev/null

    if ! start_core "$binary" "$WORKDIR/config.json"; then
        local detail
        detail=$(core_failure_line)
        [ -n "$detail" ] || detail=$(tail -c 400 "$WORKDIR/core.log" 2>/dev/null)
        emit_cell "$index" "fail" '"CORE_START_FAILED"' "$detail"             "${tcp_ms:-null}" null null null null null null             "$(json_str "$resolved_ip")" "${dns_ms:-null}" "${tcp_avg:-null}" "${tcp_jitter:-null}"
        stop_core
        rm -rf "$WORKDIR"; WORKDIR=""
        return
    fi

    if [ "$OPT_HTTP" = "1" ]; then
        local first second
        first=$(curl_socks -o /dev/null --max-time "$PROBE_TIMEOUT" \
            -w '%{http_code} %{time_total}' "$GENERATE_204_URL" 2>/dev/null)
        status=$(printf '%s' "$first" | cut -d' ' -f1)
        handshake=$(printf '%s' "$first" | cut -d' ' -f2 | awk '{printf "%.0f", $1*1000}')

        if [ -z "$status" ] || [ "$status" = "000" ]; then
            emit_cell "$index" "fail" '"PROXY_HANDSHAKE_FAILED"' \
                "$(core_failure_line)" \
                "${tcp_ms:-null}" null null null null null null \
                "$(json_str "$resolved_ip")" "${dns_ms:-null}" "${tcp_avg:-null}" "${tcp_jitter:-null}"
            stop_core; rm -rf "$WORKDIR"; WORKDIR=""
            return
        fi

        second=$(curl_socks -o /dev/null --max-time "$PROBE_TIMEOUT" \
            -w '%{time_total}' "$GENERATE_204_URL" 2>/dev/null)
        rtt=$(printf '%s' "$second" | awk '{printf "%.0f", $1*1000}')

        if [ "$status" != "204" ] && [ "$status" != "200" ]; then
            emit_cell "$index" "fail" '"HTTP_BAD_STATUS"' "HTTP $status" \
                "${tcp_ms:-null}" "${handshake:-null}" "${rtt:-null}" "$status" null null null \
                "$(json_str "$resolved_ip")" "${dns_ms:-null}" "${tcp_avg:-null}" "${tcp_jitter:-null}"
            stop_core; rm -rf "$WORKDIR"; WORKDIR=""
            return
        fi

        if [ "$OPT_EXIT" = "1" ]; then
            local trace
            trace=$(curl_socks --max-time "$PROBE_TIMEOUT" "$TRACE_URL" 2>/dev/null)
            exit_ip=$(printf '%s' "$trace" | awk -F= '/^ip=/{print $2}')
            exit_country=$(printf '%s' "$trace" | awk -F= '/^loc=/{print $2}')
        fi

        if [ "$OPT_SPEED" = "1" ]; then
            local bps
            bps=$(curl_socks -o /dev/null --max-time "$SPEED_TIMEOUT" \
                -w '%{speed_download}' "$SPEED_URL" 2>/dev/null)
            speed=$(printf '%s' "$bps" | awk '{printf "%.2f", $1*8/1000000}')
        fi
    fi

    stop_core
    rm -rf "$WORKDIR"; WORKDIR=""

    local verdict="ok"
    if [ "$OPT_HTTP" = "1" ]; then
        { [ -n "$rtt" ] && [ "$rtt" -gt "$DEGRADED_RTT_MS" ]; } && verdict="degraded"
        [ "$OPT_EXIT" = "1" ] && [ -z "$exit_ip" ] && verdict="degraded"
    fi

    emit_cell "$index" "$verdict" 'null' "" "${tcp_ms:-null}" "${handshake:-null}" \
        "${rtt:-null}" "${status:-null}" "$(json_str "$exit_ip")" \
        "$(json_str "$exit_country")" "${speed:-null}" \
        "$(json_str "$resolved_ip")" "${dns_ms:-null}" "${tcp_avg:-null}" "${tcp_jitter:-null}"
}

main() {
    [ "${1:-}" = "version" ] && { printf '%s\n' "$RUNNER_VERSION"; return 0; }
    [ -n "${1:-}" ] || { log "нет полезной нагрузки"; return 2; }

    kill_stale_cores
    declare -A CORE_PATHS=()
    OPT_TCP=1; OPT_HTTP=1; OPT_EXIT=1; OPT_SPEED=0

    local payload
    payload=$(printf '%s' "$1" | base64 -d 2>/dev/null) || { log "не разобрать payload"; return 2; }

    while IFS=$'\t' read -r kind a b c d e f; do
        case "$kind" in
            CORE)
                local path
                if path=$(ensure_core "$a" "$b" "$c" "$d"); then
                    CORE_PATHS[$a]="$path"
                else
                    log "не удалось получить ядро $a $b"
                fi
                ;;
            OPTS)
                OPT_TCP="$a"; OPT_HTTP="$b"; OPT_EXIT="$c"; OPT_SPEED="$d"
                [ -n "${e:-}" ] && SOCKS_PORT="$e"
                ;;
            CELL)
                run_cell "$a" "$b" "$c" "$d" "$e" "$f"
                ;;
        esac
    done <<< "$payload"
}

main "$@"

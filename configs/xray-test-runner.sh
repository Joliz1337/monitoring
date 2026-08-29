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

RUNNER_VERSION="2.9.0"

TOOLS_DIR="/opt/monitoring-node/tools"
CORES_DIR="$TOOLS_DIR/cores"
# Порты зарезервированы от эфемерной выдачи (tune-sysctl.sh): иначе исходящее
# соединение могло бы занять один ровно между проверками. Панель раздаёт их из
# того же диапазона — по одному на проверку в пачке.
PORTS=""
CORE_START_TIMEOUT=5
PROBE_TIMEOUT=10
# Столько проверок идёт одновременно. Значение равно размеру пачки не случайно:
# ядро уже слушает свой порт на каждую проверку, и если гнать меньше — половина
# занятых портов простаивает, а пропускная способность ноды падает вдвое.
# Нагрузка при этом небольшая: процесс ядра один, остальное — ожидание сети.
PARALLEL_CELLS=8
SPEED_TIMEOUT=20
SPEED_BYTES=10000000
DEGRADED_RTT_MS=1500
# Больше самого длинного задания: пачки идут параллельно, и уборщик не должен
# добивать живое ядро соседнего прогона
STALE_CORE_SECONDS=900
SWEEP_MARKER="/tmp/.mon-xtest-sweep"
# Реже минуты убирать нечего: брошенное ядро никуда не денется
SWEEP_MIN_INTERVAL=60

GENERATE_204_URL="https://cp.cloudflare.com/generate_204"
# Повторная проба идёт на другой адрес: сбой одного проверочного хоста не
# должен выглядеть сбоем всех конфигураций разом
FALLBACK_204_URL="https://www.gstatic.com/generate_204"
TRACE_URL="https://cloudflare.com/cdn-cgi/trace"
SPEED_URL="https://speed.cloudflare.com/__down?bytes=${SPEED_BYTES}"

WORKDIR=""
CORE_PGID=""

umask 077

cleanup() {
    stop_core
    # Фоновые проверки держат stdout открытым, и поток на панели не закроется,
    # пока жив хоть один потомок — панель будет ждать вечно
    pkill -P $$ >/dev/null 2>&1
    [ -n "$WORKDIR" ] && [ -d "$WORKDIR" ] && rm -rf "$WORKDIR"
}
trap cleanup EXIT INT TERM

log() { printf '{"type":"log","line":"%s"}\n' "$(escape "$1")"; }

# Экранирование без запуска процессов. Прежний конвейер printf|tr|sed|tr|cut
# стоил пяти процессов на вызов, а вызывается он по шесть раз на проверку: при
# десятках параллельных проверок это тысячи запусков в секунду, и почти два ядра
# уходило в ядро операционной системы вместо полезной работы.
ESC=""
BSLASH=$'\x5c'
escape_var() {
    local s=${1//$'\r'/}
    s=${s//"$BSLASH"/"$BSLASH$BSLASH"}
    s=${s//'"'/"$BSLASH"'"'}
    s=${s//$'\n'/ }
    s=${s//$'\t'/ }
    ESC=${s:0:700}
}

escape() { escape_var "$1"; printf '%s' "$ESC"; }

# Значение JSON в переменную с именем $1: строка в кавычках либо null
json_str_to() {
    local -n out="$1"
    if [ -n "${2:-}" ]; then escape_var "$2"; out='"'"$ESC"'"'; else out=null; fi
}

# Секунды с точкой в миллисекунды, без awk
MS=""
secs_to_ms() {
    local v="${1:-}" int frac
    if [ -z "$v" ]; then MS=""; return; fi
    int=${v%%.*}; frac=${v#*.}
    [ "$frac" = "$v" ] && frac=0
    frac="${frac}000"; frac=${frac:0:3}
    MS=$(( 10#${int:-0} * 1000 + 10#$frac ))
}

# Время без запуска процесса: now_ms зовётся по нескольку раз на проверку, а
# проверок идут десятки параллельно — форк на каждый замер выливается в
# заметную нагрузку на ровном месте.
if [ -n "${EPOCHREALTIME:-}" ]; then
    now_ms() {
        local t=${EPOCHREALTIME/,/.}
        printf '%s' "$(( ${t%.*} * 1000 + 10#${t#*.} / 1000 ))"
    }
else
    now_ms() { date +%s%3N; }
fi

# ── ядра ────────────────────────────────────────────────────────────────────

# Ядро от прошлого прогона, пережившее аварийный выход, займёт порт и будет
# жечь CPU. Опознаём такое по исчезнувшему каталогу задания — это точный признак,
# в отличие от возраста: пачки идут параллельно, и живому ядру соседнего прогона
# возраст ничего не доказывает. Возраст оставлен запасным правилом на случай,
# когда каталог тоже уцелел.
# Уборка брошенных ядер стоит обхода всех процессов системы, поэтому делается
# изредка, а не в каждом из десятков параллельных заданий: иначе сама уборка,
# помноженная на параллельность, становится основной нагрузкой на ноду.
# Брошенное ядро опознаём по исчезнувшему каталогу задания — это точный
# признак, в отличие от возраста: задания идут параллельно, и живому ядру
# соседнего прогона возраст ничего не доказывает.
kill_stale_cores() {
    local now=${EPOCHSECONDS:-0} stamp=0
    [ "$now" = "0" ] && now=$(date +%s)
    [ -f "$SWEEP_MARKER" ] && stamp=$(stat -c %Y "$SWEEP_MARKER" 2>/dev/null || echo 0)
    [ $(( now - stamp )) -lt "$SWEEP_MIN_INTERVAL" ] && return 0
    : > "$SWEEP_MARKER" 2>/dev/null || return 0

    # Один обход процессов вместо ps на каждое найденное ядро
    local pid etimes args dir
    while read -r pid etimes args; do
        case "$args" in
            *"$CORES_DIR/"*) ;;
            *) continue ;;
        esac
        dir=""
        case "$args" in
            *"-c /tmp/mon-xtest."*)
                dir=${args#*-c }
                dir=${dir%%/config.json*}
                ;;
        esac
        if [ -n "$dir" ] && [ ! -d "$dir" ]; then
            kill -9 "$pid" 2>/dev/null
        elif [ "${etimes:-0}" -gt "$STALE_CORE_SECONDS" ] 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null
        fi
    done < <(ps -eo pid=,etimes=,args= 2>/dev/null)
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

# Один процесс на всю пачку: ядро держит сколько угодно inbound'ов, и каждый
# порт правилом маршрутизации связан со своей конфигурацией. Готовым считается
# только состояние, когда открыты все порты: половина открытых хуже отказа —
# часть проверок пошла бы в ядро, которое их ещё не слушает.
start_core() {
    local binary="$1" config="$2" ports="$3"
    setsid "$binary" run -c "$config" >"$WORKDIR/core.log" 2>&1 &
    local pid=$!
    CORE_PGID=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
    [ -n "$CORE_PGID" ] || CORE_PGID="$pid"

    # SECONDS — встроенный счётчик оболочки: цикл крутится десять раз в секунду,
    # и date здесь означал бы столько же запусков процесса на каждое задание
    local deadline=$(( SECONDS + CORE_START_TIMEOUT ))
    local port pending
    while [ "$SECONDS" -lt "$deadline" ]; do
        kill -0 "$pid" 2>/dev/null || return 1
        pending=""
        for port in $ports; do
            if ! (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
                pending="yes"
                break
            fi
            exec 3>&- 2>/dev/null
        done
        [ -z "$pending" ] && return 0
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

# Два запроса одним вызовом: второй переиспользует соединение, поэтому меряет
# чистую задержку без рукопожатия и не тратит процессор на повторную проверку
# цепочки сертификатов. На боевой ноде это была самая дорогая часть проверки:
# curl съедал больше процессора, чем сами прокси-ядра.
probe_pair() {
    local port="$1" url="$2"
    curl_socks "$port" -o /dev/null -o /dev/null --max-time "$PROBE_TIMEOUT" \
        -w '%{http_code} %{time_total}\n' \
        "$url" "$url" 2>/dev/null
}

# Разбор пары ответов без внешних команд: раньше здесь было по четыре запуска
# процесса (printf, sed, cut, awk) на каждое из трёх значений.
PAIR_STATUS=""; PAIR_HANDSHAKE=""; PAIR_RTT=""
read_pair() {
    local first second code
    { read -r first; read -r second; } <<< "$1"
    PAIR_STATUS=""; PAIR_HANDSHAKE=""; PAIR_RTT=""
    if [ -n "$first" ]; then
        PAIR_STATUS=${first%% *}
        secs_to_ms "${first##* }"; PAIR_HANDSHAKE=$MS
    fi
    if [ -n "$second" ]; then
        secs_to_ms "${second##* }"; PAIR_RTT=$MS
    fi
}

curl_socks() { local port="$1"; shift; curl -s --socks5-hostname "127.0.0.1:$port" "$@"; }

# У REALITY есть запасной ход: «неправильному» клиенту сервер отдаёт настоящий
# сайт-маскировку. Живой и достижимый сервер обязан ответить на обычное
# TLS-рукопожатие со своим SNI; молчание при живом TCP-порте означает, что
# соединение душат по пути, а не что не подошли параметры ключа.
fallback_alive() {
    local address="$1" port="$2" sni="$3" code
    [ -n "$sni" ] || return 0
    code=$(curl -sk --max-time 8 -o /dev/null -w '%{http_code}' \
        --connect-to "${sni}:443:${address}:${port}" "https://${sni}/" 2>/dev/null)
    [ -n "$code" ] && [ "$code" != "000" ]
}

# Последняя строка лога ядра со следами ошибки. Просто tail отдавал бы рабочий
# вывод («accepted tcp:…», «dialing TCP to …»), который ничего не объясняет.
# Слот — номер проверки внутри пачки. Теги [mon-test-in-N -> mon-test-out-N]
# ядро ставит только на строки приёма и маршрута; сами ошибки («failed to
# process outbound traffic > … i/o timeout») оно печатает с одним номером
# сессии. Поэтому по тегам сначала собираются номера сессий своей проверки, а
# лог фильтруется уже по тегу вместе с ними — отбор по одним тегам оставлял бы
# наружу безобидные «accepted», пряча настоящую причину. Строки без тега общие
# для всей пачки. Отбор тега идёт по границе цифр, иначе слот 1 забирал бы
# строки слота 11; у номера сессии минимум семь цифр, чтобы не путать его со
# счётчиком секунд sing-box вида INFO[0000].
core_failure_line() {
    local slot="${1:-}" log="$WORKDIR/core.log" line src ids
    [ -f "$log" ] || return 0
    src="$log"
    if [ -n "$slot" ]; then
        src="$WORKDIR/slot-$slot.log"
        local tag_re="mon-test-(in|out)-${slot}([^0-9]|\$)"
        ids=$(grep -E "$tag_re" "$log" 2>/dev/null \
            | grep -oE '\[[0-9]{7,}[^]]*\]' | grep -oE '[0-9]{7,}' \
            | sort -u | paste -sd'|' -)
        if [ -n "$ids" ]; then
            grep -E "$tag_re|\[($ids)[] ]" "$log" > "$src" 2>/dev/null
        else
            grep -E "$tag_re" "$log" > "$src" 2>/dev/null
        fi
        [ -s "$src" ] || grep -vE 'mon-test-(in|out)-[0-9]+' "$log" > "$src" 2>/dev/null
        [ -s "$src" ] || src="$log"
    fi
    line=$(grep -iE 'fail|error|refused|timeout|rejected' "$src" 2>/dev/null | tail -1)
    # Ошибок нет — отдаём хвост как есть: ядро могло замолчать на рукопожатии,
    # и отсутствие ошибки само по себе диагноз, который разберёт панель
    [ -n "$line" ] || line=$(tail -2 "$src" 2>/dev/null | tr '\n' ' ')
    printf '%s' "$line" | cut -c1-700
}

# ── основной цикл ───────────────────────────────────────────────────────────

# Строка результата собирается одной печатью и без единой подстановки:
# каждая $( ) — это запуск процесса, а проверок идут десятки параллельно.
# Одна печать заодно атомарна, поэтому строки соседних проверок не
# налезают друг на друга в общем выводе.
emit_cell() {
    escape_var "$4"
    printf '{"type":"cell","index":%s,"verdict":"%s","reason":%s,"detail":"%s","tcp_min_ms":%s,"handshake_ms":%s,"rtt_ms":%s,"http_status":%s,"exit_ip":%s,"exit_country":%s,"speed_mbps":%s,"resolved_ip":%s,"dns_ms":%s,"tcp_avg_ms":%s,"tcp_jitter_ms":%s}\n' \
        "$1" "$2" "$3" "$ESC" \
        "${5:-null}" "${6:-null}" "${7:-null}" "${8:-null}" \
        "${9:-null}" "${10:-null}" "${11:-null}" \
        "${12:-null}" "${13:-null}" "${14:-null}" "${15:-null}"
}


run_cell() {
    local index="$1" core_name="$2" address="$3" port="$4" udp="$5" socks="$6" sni="$7"
    local tcp_ms="" tcp_avg="" tcp_jitter="" handshake="" rtt="" status=""
    local exit_ip="" exit_country="" speed="" resolved_ip="" dns_ms=""

    local resolved J_RESOLVED=null J_EXIT_IP=null J_EXIT_CC=null
    resolved=$(resolve_host "$address")
    if [ -n "$resolved" ]; then
        resolved_ip=${resolved%% *}
        dns_ms=${resolved##* }
        json_str_to J_RESOLVED "$resolved_ip"
    else
        emit_cell "$index" "fail" '"DNS_FAIL"' "домен $address не резолвится"
        return
    fi

    if [ "$OPT_TCP" = "1" ] && [ "$udp" != "1" ]; then
        local tcp
        tcp=$(tcp_ping_ms "$address" "$port")
        if [ -z "$tcp" ]; then
            emit_cell "$index" "fail" '"TCP_REFUSED"' "порт $port недоступен" \
                null null null null null null null "$J_RESOLVED" "${dns_ms:-null}"
            return
        fi
        read -r tcp_ms tcp_avg tcp_jitter <<< "$tcp"
    fi

    if [ "$OPT_HTTP" = "1" ]; then
        # Оба запроса одним вызовом curl: второй идёт по уже установленному
        # соединению, то есть без повторного TLS-рукопожатия. Раздельные вызовы
        # означали лишнюю проверку цепочки сертификатов на каждую проверку, а это
        # самая дорогая её часть — на боевой ноде curl съедал процессора больше,
        # чем сами прокси-ядра.
        local pair fallback_pid=""
        pair=$(probe_pair "$socks" "$GENERATE_204_URL")
        read_pair "$pair"
        status=$PAIR_STATUS; handshake=$PAIR_HANDSHAKE; rtt=$PAIR_RTT

        # Любой первый отказ переспрашиваем, как это делает пробер панели:
        # проверки стартуют залпом со всех локаций разом, и разовый сбой канала
        # в этот момент — таймаут или мгновенный сброс туннеля — хоронил бы
        # живую конфигурацию. Повтор идёт на запасной адрес, времени в худшем
        # случае это стоит столько же, сколько прежний повтор по таймауту.
        # Контрольное TLS-рукопожатие с сервером (отличает блокировку по пути
        # от неподходящих параметров ключа) от повтора не зависит, поэтому идёт
        # фоном и целиком прячется внутри паузы и повторной пробы: после них
        # оно добавляло мёртвой ячейке до восьми секунд, а именно из таких
        # ячеек состоит хвост прогона.
        if [ -z "$status" ] || [ "$status" = "000" ] || [ -z "$rtt" ]; then
            fallback_alive "$address" "$port" "$sni" &
            fallback_pid=$!
            sleep 1.5
            pair=$(probe_pair "$socks" "$FALLBACK_204_URL")
            read_pair "$pair"
            status=$PAIR_STATUS; handshake=$PAIR_HANDSHAKE; rtt=$PAIR_RTT
        fi

        if [ -z "$status" ] || [ "$status" = "000" ]; then
            sleep 0.4
            local fail_reason='"PROXY_HANDSHAKE_FAILED"'
            wait "$fallback_pid" 2>/dev/null || fail_reason='"DPI_BLOCK"'
            emit_cell "$index" "fail" "$fail_reason" \
                "$(core_failure_line "$index")" \
                "${tcp_ms:-null}" null null null null null null \
                "$J_RESOLVED" "${dns_ms:-null}" "${tcp_avg:-null}" "${tcp_jitter:-null}"
            return
        fi

        # Повтор прошёл — контрольная проба больше не нужна, а её процесс держал
        # бы общий stdout, и с ним поток к панели, до своего таймаута
        if [ -n "$fallback_pid" ]; then
            kill "$fallback_pid" 2>/dev/null
            wait "$fallback_pid" 2>/dev/null
        fi

        if [ "$status" != "204" ] && [ "$status" != "200" ]; then
            emit_cell "$index" "fail" '"HTTP_BAD_STATUS"' "HTTP $status" \
                "${tcp_ms:-null}" "${handshake:-null}" "${rtt:-null}" "$status" null null null \
                "$J_RESOLVED" "${dns_ms:-null}" "${tcp_avg:-null}" "${tcp_jitter:-null}"
            return
        fi

        if [ "$OPT_EXIT" = "1" ]; then
            local trace
            trace=$(curl_socks "$socks" --max-time "$PROBE_TIMEOUT" "$TRACE_URL" 2>/dev/null)
            local tline
            while IFS= read -r tline; do
                case "$tline" in
                    ip=*)  exit_ip=${tline#ip=} ;;
                    loc=*) exit_country=${tline#loc=} ;;
                esac
            done <<< "$trace"
        fi

        if [ "$OPT_SPEED" = "1" ]; then
            local bps
            bps=$(curl_socks "$socks" -o /dev/null --max-time "$SPEED_TIMEOUT" \
                -w '%{speed_download}' "$SPEED_URL" 2>/dev/null)
            speed=$(printf '%s' "$bps" | awk '{printf "%.2f", $1*8/1000000}')
        fi
    fi

    json_str_to J_EXIT_IP "$exit_ip"
    json_str_to J_EXIT_CC "$exit_country"

    # Оговорка без причины бесполезна: вердикт и код проставляются вместе
    local verdict="ok" reason='null'
    if [ "$OPT_HTTP" = "1" ]; then
        if [ -n "$rtt" ] && [ "$rtt" -gt "$DEGRADED_RTT_MS" ]; then
            verdict="degraded"; reason='"SLOW_RTT"'
        elif [ "$OPT_EXIT" = "1" ] && [ -z "$exit_ip" ]; then
            verdict="degraded"; reason='"EXIT_IP_UNKNOWN"'
        fi
    fi

    emit_cell "$index" "$verdict" "$reason" "" "${tcp_ms:-null}" "${handshake:-null}" \
        "${rtt:-null}" "${status:-null}" "$J_EXIT_IP" \
        "$J_EXIT_CC" "${speed:-null}" \
        "$J_RESOLVED" "${dns_ms:-null}" "${tcp_avg:-null}" "${tcp_jitter:-null}"
}

main() {
    [ "${1:-}" = "version" ] && { printf '%s
' "$RUNNER_VERSION"; return 0; }
    [ -n "${1:-}" ] || { log "нет полезной нагрузки"; return 2; }

    kill_stale_cores
    declare -A CORE_PATHS=()
    OPT_TCP=1; OPT_HTTP=1; OPT_EXIT=1; OPT_SPEED=0
    local batch_core="" batch_conf="" ports=""
    local cells=()

    local payload
    payload=$(printf '%s' "$1" | base64 -d 2>/dev/null) || { log "не разобрать payload"; return 2; }

    while IFS=$'	' read -r kind a b c d e f g; do
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
                ;;
            CONF)
                batch_core="$a"; batch_conf="$b"
                ;;
            CELL)
                cells+=("$a"$'	'"$b"$'	'"$c"$'	'"$d"$'	'"$e"$'	'"$f"$'	'"$g")
                ports="$ports $f"
                ;;
        esac
    done <<< "$payload"

    [ "${#cells[@]}" -gt 0 ] || { log "в задании нет проверок"; return 0; }

    WORKDIR=$(mktemp -d /tmp/mon-xtest.XXXXXX) || { log "нет временного каталога"; return 2; }

    # Ядро одно на всю пачку: у каждой проверки свой socks-порт, а маршрут
    # внутри конфига связывает порт с её конфигурацией.
    local started=0 start_detail=""
    if [ "$OPT_HTTP" != "1" ]; then
        started=1
    elif [ -z "${CORE_PATHS[$batch_core]:-}" ]; then
        start_detail="ядро $batch_core недоступно"
    else
        printf '%s' "$batch_conf" | base64 -d > "$WORKDIR/config.json" 2>/dev/null
        if start_core "${CORE_PATHS[$batch_core]}" "$WORKDIR/config.json" "$ports"; then
            started=1
        else
            start_detail=$(core_failure_line)
            [ -n "$start_detail" ] || start_detail=$(tail -c 400 "$WORKDIR/core.log" 2>/dev/null)
            stop_core
        fi
    fi

    # Ждём только сами проверки. Ядро запущено фоновым заданием этой же оболочки
    # и само не завершается никогда, поэтому голый `wait` висел бы на нём до
    # таймаута всего задания — проверка в 0.3 секунды превращалась в полторы
    # минуты, а `stop_core` ниже просто не получал управления.
    local row running=0
    local pids=()
    for row in "${cells[@]}"; do
        if [ "$started" != "1" ]; then
            emit_cell "${row%%$'	'*}" "fail" '"CORE_START_FAILED"' "$start_detail"
            continue
        fi
        run_cell_row "$row" &
        pids+=("$!")
        running=$((running + 1))
        if [ "$running" -ge "$PARALLEL_CELLS" ]; then
            wait -n "${pids[@]}" 2>/dev/null
            running=$((running - 1))
        fi
    done
    [ "${#pids[@]}" -gt 0 ] && wait "${pids[@]}" 2>/dev/null

    stop_core
    rm -rf "$WORKDIR"; WORKDIR=""
}

run_cell_row() {
    local index core_name address port udp socks sni
    IFS=$'	' read -r index core_name address port udp socks sni <<< "$1"
    run_cell "$index" "$core_name" "$address" "$port" "$udp" "$socks" "$sni"
}

main "$@"

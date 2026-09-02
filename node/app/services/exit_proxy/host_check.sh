#!/usr/bin/env bash
# Проверки выхода для exit-прокси ноды: как Google видит IP, капча в поиске,
# доступность Gemini и пользовательские URL.
#
# Агент кладёт скрипт в /opt/monitoring/scripts/exit-proxy-check.sh и зовёт его
# через nsenter. Все запросы идут curl'ом с хоста: для IP-кандидата — с привязкой
# исходящего адреса, для WARP — через его socks на 127.0.0.1:9091. Ответ — ровно
# одна строка JSON в stdout.
#
# Запуск: exit-proxy-check.sh probe <ip|warp> <address> <timeout> <base64 payload>
#         exit-proxy-check.sh selftest <port> <timeout>
#
# Payload — строка на запись, поля через \x1f (табуляция и пробел в bash
# схлопывают пустые поля, разделитель-непробел их сохраняет):
#   BUILTIN <country 0/1> <captcha 0/1> <gemini 0/1>
#   CHECK   <name> <url> <block_status csv> <block_regex> <block_url_regex> <expect_status>
set -uo pipefail

UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36'
TRACE_URL="https://cloudflare.com/cdn-cgi/trace"
# YouTube отдаёт страну по IP прямо в конфиге страницы; cookie SOCS обходит
# европейский экран согласия, за которым конфига нет
YOUTUBE_URL="https://www.youtube.com/"
CONFIRM_URL="https://www.google.com/search?q=hello&hl=en"
CAPTCHA_URL="https://www.google.com/search?q=exit+proxy+check"
GEMINI_URL="https://gemini.google.com/"
# Апостроф в «isn't» бывает и прямым, и типографским — любой байт между n и t
GEMINI_BLOCK_RE="isn.{0,3}t (currently )?(supported|available) in your (country|region)"
COUNTRY_NAMES="Netherlands|Russia|Germany|Finland|France|Sweden|Norway|Poland|Latvia|Lithuania|Estonia|Kazakhstan|Turkey|Spain|Italy|Austria|Switzerland|Czechia|United Kingdom|United States|Canada|Japan|Singapore|Hong Kong"

FS=$'\x1f'
KIND=""
ADDRESS=""
TIMEOUT=15
WORKDIR=""

umask 077

usage() {
    echo "usage: $0 probe <ip|warp> <address> <timeout> <base64 payload> | selftest <port> <timeout>" >&2
    exit 2
}

BSLASH='\'
ESC=""
escape_var() {
    local s=${1//$'\r'/}
    s=${s//"$BSLASH"/"$BSLASH$BSLASH"}
    s=${s//'"'/"$BSLASH"'"'}
    s=${s//$'\n'/ }
    s=${s//$'\t'/ }
    ESC=${s:0:300}
}

# Строковое значение JSON в переменную с именем $1: строка в кавычках либо null
json_str_to() {
    local -n out="$1"
    if [ -n "${2:-}" ]; then escape_var "$2"; out='"'"$ESC"'"'; else out=null; fi
}

now_ms() { date +%s%3N; }

trace_field() { printf '%s\n' "$1" | sed -n "s/^$2=//p" | head -n1; }

via() {
    if [ "$KIND" = "warp" ]; then
        curl -s --socks5-hostname "$ADDRESS" --max-time "$TIMEOUT" -A "$UA" "$@"
    else
        curl -4 -s --interface "$ADDRESS" --max-time "$TIMEOUT" -A "$UA" "$@"
    fi
}

CHECK_JSON=""
run_check() {
    local name="$1" url="$2" block_status="$3" block_regex="$4" block_url_regex="$5" expect="$6"
    local body="$WORKDIR/check" out code final ok=true detail="" status=null
    out=$(via -L -o "$body" -w '%{http_code}\t%{url_effective}' "$url" 2>/dev/null)
    code=${out%%$'\t'*}
    final=${out#*$'\t'}
    if [ -z "$code" ] || [ "$code" = "000" ]; then
        ok=false; detail="no response"
    else
        status=$code
        detail="status $code"
        if [ -n "$block_status" ] && [[ ",$block_status," == *",$code,"* ]]; then
            ok=false; detail="blocked: status $code"
        fi
        if [ "$ok" = true ] && [ -n "$block_url_regex" ] && printf '%s' "$final" | grep -qiE -- "$block_url_regex"; then
            ok=false; detail="blocked: redirected to $final"
        fi
        if [ "$ok" = true ] && [ -n "$block_regex" ] && grep -qiaE -- "$block_regex" "$body" 2>/dev/null; then
            ok=false; detail="blocked: page matches pattern"
        fi
        if [ "$ok" = true ] && [ -n "$expect" ] && [ "$code" != "$expect" ]; then
            ok=false; detail="status $code, expected $expect"
        fi
    fi
    rm -f "$body"
    local jname jdetail
    json_str_to jname "$name"
    json_str_to jdetail "$detail"
    CHECK_JSON="{\"name\":$jname,\"ok\":$ok,\"status\":$status,\"detail\":$jdetail}"
}

emit_result() {
    local ok="$1" ip="$2" country="$3" confirm="$4" captcha="$5" gemini="$6" warp="$7" checks="$8" error="$9" started="${10}"
    local jip jcountry jconfirm jwarp jerror elapsed
    json_str_to jip "$ip"
    json_str_to jcountry "$country"
    json_str_to jconfirm "$confirm"
    json_str_to jwarp "$warp"
    json_str_to jerror "$error"
    elapsed=$(( $(now_ms) - started ))
    printf '{"ok":%s,"ip":%s,"country":%s,"country_confirm":%s,"captcha":%s,"gemini":"%s","warp":%s,"checks":%s,"error":%s,"elapsed_ms":%s}\n' \
        "$ok" "$jip" "$jcountry" "$jconfirm" "$captcha" "$gemini" "$jwarp" "$checks" "$jerror" "$elapsed"
}

probe() {
    local started payload
    started=$(now_ms)
    payload=$(printf '%s' "$1" | base64 -d 2>/dev/null) || { emit_result false "" "" "" false skipped "" "[]" "bad payload" "$started"; return; }

    local want_country=1 want_captcha=1 want_gemini=1 record f1 f2 f3 f4 f5 f6
    local -a check_lines=()
    while IFS="$FS" read -r record f1 f2 f3 f4 f5 f6; do
        case "$record" in
            BUILTIN) want_country=$f1; want_captcha=$f2; want_gemini=$f3 ;;
            CHECK) check_lines+=("$f1$FS$f2$FS$f3$FS$f4$FS$f5$FS$f6") ;;
        esac
    done <<< "$payload"

    local trace ip warp
    trace=$(via "$TRACE_URL" 2>/dev/null)
    ip=$(trace_field "$trace" ip)
    warp=$(trace_field "$trace" warp)
    if [ -z "$ip" ]; then
        emit_result false "" "" "" false skipped "" "[]" "no route through this exit (trace failed)" "$started"
        return
    fi

    local country="" confirm="" captcha=false gemini=skipped
    if [ "$want_country" = "1" ]; then
        country=$(via -L -H 'Cookie: SOCS=CAI' "$YOUTUBE_URL" 2>/dev/null \
            | grep -oiE '"GL":"[A-Z]{2}"' | head -n1 | cut -d'"' -f4 | tr '[:lower:]' '[:upper:]')
        confirm=$(via -L "$CONFIRM_URL" 2>/dev/null \
            | grep -oE "$COUNTRY_NAMES" | sort | uniq -c | sort -rn | head -n1 | sed -E 's/^ *[0-9]+ //')
    fi

    if [ "$want_captcha" = "1" ]; then
        local out code redirect
        out=$(via -o /dev/null -w '%{http_code} %{redirect_url}' "$CAPTCHA_URL" 2>/dev/null)
        code=${out%% *}
        redirect=${out#* }
        [ "$code" = "429" ] && captcha=true
        case "$redirect" in *"/sorry/"*) captcha=true ;; esac
    fi

    if [ "$want_gemini" = "1" ]; then
        local gcode
        gcode=$(via -L -o "$WORKDIR/gemini" -w '%{http_code}' "$GEMINI_URL" 2>/dev/null)
        if grep -qiaE "$GEMINI_BLOCK_RE" "$WORKDIR/gemini" 2>/dev/null; then
            gemini=blocked
        elif [ "$gcode" = "200" ]; then
            gemini=ok
        elif [ "$gcode" = "403" ] || [ "$gcode" = "429" ]; then
            gemini=blocked
        else
            gemini=error
        fi
        rm -f "$WORKDIR/gemini"
    fi

    local checks="" line name url block_status block_regex block_url_regex expect
    for line in "${check_lines[@]}"; do
        IFS="$FS" read -r name url block_status block_regex block_url_regex expect <<< "$line"
        run_check "$name" "$url" "$block_status" "$block_regex" "$block_url_regex" "$expect"
        checks="${checks:+$checks,}$CHECK_JSON"
    done

    emit_result true "$ip" "$country" "$confirm" "$captcha" "$gemini" "$warp" "[$checks]" "" "$started"
}

selftest() {
    local port="$1" timeout="$2" trace ip loc warp jip jloc jwarp
    trace=$(curl -s --socks5-hostname "127.0.0.1:$port" --max-time "$timeout" "$TRACE_URL" 2>/dev/null)
    ip=$(trace_field "$trace" ip)
    loc=$(trace_field "$trace" loc)
    warp=$(trace_field "$trace" warp)
    if [ -z "$ip" ]; then
        printf '{"ok":false,"ip":null,"loc":null,"warp":null,"error":"no response through local socks"}\n'
        return
    fi
    json_str_to jip "$ip"
    json_str_to jloc "$loc"
    json_str_to jwarp "$warp"
    printf '{"ok":true,"ip":%s,"loc":%s,"warp":%s,"error":null}\n' "$jip" "$jloc" "$jwarp"
}

main() {
    local verb="${1:-}"
    case "$verb" in
        probe)
            [ $# -eq 5 ] || usage
            KIND=$2; ADDRESS=$3; TIMEOUT=$4
            case "$KIND" in ip|warp) ;; *) usage ;; esac
            WORKDIR=$(mktemp -d) || exit 1
            trap 'rm -rf "$WORKDIR"' EXIT INT TERM
            probe "$5"
            ;;
        selftest)
            [ $# -eq 3 ] || usage
            selftest "$2" "$3"
            ;;
        *) usage ;;
    esac
}

main "$@"

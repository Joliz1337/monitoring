#!/bin/bash
# Итоговая проверка слоя тюнинга и анти-DDoS. Только чтение, кроме повторного
# render — он идемпотентен, и его ответ «no change» и есть доказательство.
set -u
P=0; F=0; W=0
ok()   { echo "  [ ok ] $*"; P=$((P+1)); }
bad()  { echo "  [FAIL] $*"; F=$((F+1)); }
warn() { echo "  [ ?? ] $*"; W=$((W+1)); }
h()    { echo; echo "== $* =="; }

R=/opt/monitoring/scripts/tune-sysctl.sh
FJ=/opt/monitoring/configs/tuning-facts.json
FE=/opt/monitoring/configs/tuning-facts.env
S=/etc/sysctl.d/99-vless-tuning.conf
sv() { sysctl -n "$1" 2>/dev/null; }
fact() { grep -oE "\"$1\": *[0-9]+" "$FJ" 2>/dev/null | head -1 | grep -oE '[0-9]+$'; }

echo "### $(hostname) — $(date '+%F %T') — uptime $(uptime -p)"
echo "### $(nproc) ядер, $(awk '/^MemTotal/{printf "%d МБ", $2/1024}' /proc/meminfo)"

h "Версии"
[ -x "$R" ] && ok "рендерер установлен" || { bad "рендерера нет — оптимизации не применялись"; exit 1; }
echo "  configs=$(cat /opt/monitoring/configs/VERSION 2>/dev/null)  профиль=$(cat /opt/monitoring/configs/OPT_PROFILE 2>/dev/null)"
echo "  формула=$(grep -oE '^FORMULA_VERSION="[^"]+"' "$R" | cut -d'"' -f2)  вотчдог=$(/opt/monitoring/scripts/ddos-watchdog.sh version 2>/dev/null)"
echo "  агент=$(docker exec monitoring-api cat /app/VERSION 2>/dev/null || echo '—')"

h "Значения в ядре"
V=$("$R" verify 2>/dev/null)
case "$V" in
    *'"success": true'*) ok "verify: $(echo "$V" | grep -oE '"checked_count": [0-9]+')" ;;
    *) bad "verify: $V" ;;
esac

h "Идемпотентность"
OUT=$("$R" render 2>&1 >/dev/null)
case "$OUT" in
    *"no change"*) ok "повторный render ничего не переписал" ;;
    *) bad "render переписал файл: $(echo "$OUT" | head -1)" ;;
esac

h "Цепочка дескрипторов"
NR=$(sv fs.nr_open); LIM=$(grep -E '^\*[[:space:]]+hard[[:space:]]+nofile' /etc/security/limits.d/99-nofile.conf 2>/dev/null | awk '{print $4}')
SD=$(grep -E '^DefaultLimitNOFILE=' /etc/systemd/system.conf.d/limits.conf 2>/dev/null | cut -d= -f2)
HD=$(grep -E '^LimitNOFILE=' /etc/systemd/system/haproxy.service.d/limits.conf 2>/dev/null | cut -d= -f2)
echo "  nr_open=$NR  limits=$LIM  systemd=$SD  haproxy_dropin=${HD:-—}"
[ -n "$LIM" ] && [ "$LIM" -le "$NR" ] && ok "limits ≤ nr_open" || bad "limits $LIM > nr_open $NR — PAM сломается"
[ "$LIM" = "$SD" ] && ok "limits == DefaultLimitNOFILE" || bad "расходятся: $LIM vs $SD"
UMAX=$(grep -rhoE '^LimitNOFILE=[0-9]+' /usr/lib/systemd/system /etc/systemd/system 2>/dev/null | cut -d= -f2 | sort -n | tail -1)
[ -n "$UMAX" ] && [ "$UMAX" -le "$NR" ] && ok "самый жадный юнит ($UMAX) влезает в nr_open" || bad "юнит просит $UMAX > nr_open $NR — упадёт с 205/LIMITS"

h "Сервисы, которые ломались на лимитах"
systemctl is-active systemd-logind >/dev/null 2>&1 && ok "systemd-logind активен" || bad "systemd-logind не активен"
# --plain убирает ведущий символ-маркер, иначе в $1 попадает он, а не имя юнита
LF=$(systemctl --failed --plain --no-legend --no-pager 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
[ -z "$LF" ] && ok "упавших юнитов нет" || warn "упали: $LF"
journalctl -b --no-pager 2>/dev/null | grep -q '205/LIMITS' && bad "в журнале есть 205/LIMITS" || ok "205/LIMITS в журнале нет"

h "HAProxy"
if systemctl is-active haproxy >/dev/null 2>&1; then
    MC=$(grep -oE '^[[:space:]]+maxconn[[:space:]]+[0-9]+' /etc/haproxy/haproxy.cfg 2>/dev/null | awk '{print $2}' | head -1)
    RL=$(systemctl show haproxy -p LimitNOFILE --value 2>/dev/null)
    CAP=$(( (RL - 1024) / 3 ))
    echo "  maxconn=$MC  LimitNOFILE=$RL  потолок=$CAP"
    [ -n "$MC" ] && [ "$MC" -le "$CAP" ] && ok "maxconn влезает в лимит дескрипторов" || bad "maxconn $MC > $CAP — strict-limits не даст стартовать"
else
    echo "  не запущен — цепочка maxconn на этой ноде не применима"
fi

h "Ключевые значения"
for kv in "net.ipv4.tcp_timestamps=1" "net.netfilter.nf_conntrack_tcp_be_liberal=1" \
          "net.ipv4.tcp_retries2=10" "net.ipv4.tcp_autocorking=0" "net.core.default_qdisc=fq" \
          "net.ipv4.tcp_congestion_control=bbr"; do
    k=${kv%%=*}; want=${kv#*=}; got=$(sv "$k")
    [ "$got" = "$want" ] && ok "$k=$got" || warn "$k=$got (ожидалось $want)"
done
CC=$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null); CM=$(sv net.netfilter.nf_conntrack_max)
[ -n "$CM" ] && [ "$CM" -gt 0 ] && echo "  conntrack: $CC / $CM = $(( CC * 100 / CM ))%"

h "Анти-DDoS"
ST=$(/opt/monitoring/scripts/ddos-watchdog.sh status 2>/dev/null)
echo "  $ST"
case "$ST" in *'"mode":"off"'*) ok "аварийный режим выключен" ;; *) warn "аварийный режим ВКЛЮЧЕН" ;; esac
case "$ST" in *'"watchdog":"on"'*) ok "детект включён" ;; *) warn "детект выключен" ;; esac
iptables -nL INPUT 2>/dev/null | grep -q ANTIDDOS && warn "цепочка ANTIDDOS в INPUT" || ok "цепочки ANTIDDOS в INPUT нет"
grep -qE '^LISTEN_OVERFLOW_DELTA=' /opt/monitoring/antiddos/config.auto 2>/dev/null \
    && ok "пороги масштабированы ($(grep -E '^(LISTEN_OVERFLOW_DELTA|SOFTIRQ_PCT|NEWRATE)=' /opt/monitoring/antiddos/config.auto | tr '\n' ' '))" \
    || bad "config.auto старого формата — вотчдог на плоских порогах"
N=$(journalctl -u ddos-watchdog --since '24 hours ago' --no-pager 2>/dev/null | grep -c 'emergency ON')
[ "${N:-0}" -eq 0 ] && ok "за сутки срабатываний нет" || warn "за сутки срабатываний: $N"
echo "  ListenOverflows=$(nstat -az TcpExtListenOverflows 2>/dev/null | awk 'NR==2{print $2}') (сигнал)  ListenDrops=$(nstat -az TcpExtListenDrops 2>/dev/null | awk 'NR==2{print $2}') (фон, игнорируется)"

h "Контейнеры"
if [ -f /opt/remnawave/docker-compose.yml ]; then
    DM=$(grep -oE '^\s+(soft|hard): [0-9]+' /opt/remnawave/docker-compose.yml | awk '{print $2}' | sort -n | tail -1)
    [ -n "$DM" ] && [ "$DM" -le "$NR" ] && ok "ulimits Remnawave ($DM) ≤ nr_open" || bad "ulimits Remnawave $DM > nr_open $NR — контейнер не стартует"
else
    echo "  Remnawave не установлен"
fi

h "Операторские переопределения"
OV=/opt/monitoring/configs/local-overrides.conf
if [ -s "$OV" ]; then warn "активны: $(grep -vE '^\s*#|^\s*$' "$OV" | tr '\n' ';')"; else ok "нет (штатная конфигурация)"; fi

echo
echo "### ИТОГ: ок=$P  провал=$F  внимание=$W"
[ "$F" -eq 0 ] && echo "### Провалов нет." || echo "### ЕСТЬ ПРОВАЛЫ — не раскатывать дальше."

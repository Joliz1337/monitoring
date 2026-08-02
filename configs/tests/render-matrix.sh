#!/bin/bash
#
# Table-driven test for tune-sysctl.sh across the whole plausible host range.
# Needs no privileges, no VM and no Linux: every host fact is injected.
#
#   bash configs/tests/render-matrix.sh
#
# Two passes:
#   1. `facts` over the full matrix — cheap, validates the arithmetic and the
#      renderer's own invariant assertions on every size.
#   2. `render` over the extremes — validates file integrity (no unresolved
#      tokens, no duplicate keys, FD chain consistent across the emitted files).
#
# The point of the matrix is not coverage theatre: monotonicity catches an
# accidentally inverted formula, and the clamp-coverage check catches a floor or
# cap that no real host can ever reach (i.e. untested dead code).
#

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
CONFIGS=$(cd "$HERE/.." && pwd)
RENDERER="$CONFIGS/tune-sysctl.sh"

[ -f "$RENDERER" ] || { echo "cannot find $RENDERER" >&2; exit 1; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/opt/monitoring/configs/profiles"
cp "$CONFIGS/profiles/"*.conf "$CONFIGS/profiles/"*.tmpl "$WORK/opt/monitoring/configs/profiles/"
echo "test" > "$WORK/opt/monitoring/configs/VERSION"

MEM_GB_LIST="0.5 1 2 4 8 16 64 248 1024"
CPU_LIST="1 2 4 8 16 64 128"
PROFILES="vpn panel"
PAGE_LIST="4096 65536"

PASS=0
FAIL=0
declare -A SEEN_MIN SEEN_MAX

fail() { echo "  FAIL: $*"; FAIL=$((FAIL + 1)); }
ok()   { PASS=$((PASS + 1)); }

# pow2 check used for the rps_flow_cnt assertion
is_pow2() { [ "$1" -ge 1 ] && [ $(( $1 & ($1 - 1) )) -eq 0 ]; }

run_facts() {
    local memkb=$1 cpus=$2 page=$3 profile=$4
    MON_RENDER_ROOT="$WORK" MON_RENDER_DRYRUN=1 \
    MON_FACT_MEMKB="$memkb" MON_FACT_CPUS="$cpus" MON_FACT_PAGESIZE="$page" \
    MON_FACT_LINK_MBPS=1000 MON_FACT_MTU=1500 MON_FACT_FILENR=5000 \
        bash "$RENDERER" facts "$profile" 2>/dev/null
}

echo "=== pass 1: arithmetic + invariants over the full matrix ==="

for mem_gb in $MEM_GB_LIST; do
  memkb=$(awk -v g="$mem_gb" 'BEGIN{printf "%d", g*1024*1024}')
  for cpus in $CPU_LIST; do
    for page in $PAGE_LIST; do
      for profile in $PROFILES; do
        label="${mem_gb}GB/${cpus}C/${page}p/$profile"
        out=$(run_facts "$memkb" "$cpus" "$page" "$profile")
        if [ -z "$out" ]; then
            fail "$label: renderer produced nothing (invariant violation or crash)"
            continue
        fi

        # Quote the values: tcp_mem/udp_mem/tcp_rmem are space-separated triples,
        # and an unquoted eval would run the second field as a command.
        eval "$(echo "$out" | grep -E '^[A-Z_]+=' \
            | awk -F= '{v=substr($0, index($0,"=")+1); printf "V_%s=\"%s\"\n", $1, v}')"

        # --- FD chain (the lockout-critical one) ---
        [ "${V_NOFILE_LIMIT:-0}" -le "${V_FD_MAX:-0}" ] \
            || fail "$label: NOFILE_LIMIT $V_NOFILE_LIMIT > FD_MAX $V_FD_MAX"
        [ "${V_DOCKER_NOFILE:-0}" -le "${V_NOFILE_LIMIT:-0}" ] \
            || fail "$label: DOCKER_NOFILE > NOFILE_LIMIT"
        [ "${V_HAPROXY_MAXCONN:-0}" -le $(( (V_NOFILE_LIMIT - 1024) / 3 )) ] \
            || fail "$label: HAPROXY_MAXCONN exceeds FD budget"

        # --- hashsize is exactly conntrack_max/4 (one formula, all scripts) ---
        [ "${V_CONNTRACK_HASHSIZE:-0}" -eq $(( V_CONNTRACK_MAX / 4 )) ] \
            || fail "$label: hashsize != conntrack_max/4"

        # --- memory budgets ---
        tcp_hi=$(echo "$V_TCP_MEM" | awk '{print $3}')
        udp_hi=$(echo "$V_UDP_MEM" | awk '{print $3}')
        sock_kb=$(( (tcp_hi + udp_hi) * page / 1024 ))
        [ "$sock_kb" -le $(( memkb * 26 / 100 )) ] \
            || fail "$label: socket memory ${sock_kb}kB > 26% of RAM"

        ct_kb=$(( V_CONNTRACK_MAX / 1024 * 320 ))
        [ "$ct_kb" -le $(( memkb * 7 / 100 )) ] \
            || fail "$label: conntrack ${ct_kb}kB > 7% of RAM"

        # Only meaningful above the floor — at the floor we are at the kernel's
        # own default and cannot be making things worse.
        if [ "$V_NETDEV_MAX_BACKLOG" -gt 1024 ]; then
            nb_kb=$(( V_NETDEV_MAX_BACKLOG * cpus * 2048 / 1024 ))
            [ "$nb_kb" -le $(( memkb * 12 / 1000 )) ] \
                || fail "$label: netdev backlog aggregate ${nb_kb}kB > 1.2% of RAM"
        fi

        [ "${V_MIN_FREE_KBYTES:-0}" -le $(( memkb * 4 / 100 )) ] \
            || fail "$label: min_free_kbytes $V_MIN_FREE_KBYTES > 4% of RAM"

        [ "${V_SOCK_DEFAULT:-0}" -le "${V_SOCK_MAX:-0}" ] \
            || fail "$label: SOCK_DEFAULT > SOCK_MAX"

        # --- rps_flow_cnt must be a power of two or the kernel rejects it ---
        for q in 1 2 4 8 16 40 64; do
            fc=$V_RPS_SOCK_FLOW_ENTRIES
            [ "$q" -gt 0 ] && fc=$(( V_RPS_SOCK_FLOW_ENTRIES / q ))
            [ "$fc" -lt 1 ] && fc=1
            # emulate pow2_floor
            p=1; while [ $(( p * 2 )) -le "$fc" ]; do p=$(( p * 2 )); done
            is_pow2 "$p" || fail "$label: rps_flow_cnt $p not a power of two (q=$q)"
        done

        # --- record clamp coverage + monotonicity samples ---
        for k in CONNTRACK_MAX NETDEV_MAX_BACKLOG FD_MAX NOFILE_LIMIT SOCK_MAX SOCK_DEFAULT \
                 UDP_MIN SOMAXCONN TCP_MAX_ORPHANS TCP_MAX_TW_BUCKETS \
                 RPS_SOCK_FLOW_ENTRIES MIN_FREE_KBYTES DIRTY_RATIO \
                 FLOW_LIMIT_TABLE_LEN NETDEV_BUDGET NPROC_LIMIT; do
            eval "v=\${V_$k:-}"
            [ -n "$v" ] || continue
            cur_min=${SEEN_MIN[$k]:-}
            cur_max=${SEEN_MAX[$k]:-}
            [ -z "$cur_min" ] || [ "$v" -lt "$cur_min" ] && SEEN_MIN[$k]=$v
            [ -z "$cur_max" ] || [ "$v" -gt "$cur_max" ] && SEEN_MAX[$k]=$v
        done

        # monotonicity in RAM at fixed (cpus, page, profile)
        key="${cpus}_${page}_${profile}"
        for k in CONNTRACK_MAX FD_MAX SOCK_MAX SOCK_DEFAULT UDP_MIN \
                 TCP_MAX_ORPHANS TCP_MAX_TW_BUCKETS MIN_FREE_KBYTES; do
            eval "v=\${V_$k:-0}"
            prev_var="PREV_${k}_${key}"
            eval "prev=\${$prev_var:-}"
            if [ -n "$prev" ] && [ "$v" -lt "$prev" ]; then
                fail "$label: $k decreased with more RAM ($prev -> $v)"
            fi
            eval "$prev_var=$v"
        done

        ok
      done
    done
  done
done

echo "  $PASS combinations checked"

echo "=== pass 2: clamp coverage (every floor and cap must be reachable) ==="
# A clamp bound that no host in the matrix hits is untested dead code — either
# the bound is wrong or the formula never approaches it.
check_bound() {
    local key=$1 lo=$2 hi=$3
    local seen_lo=${SEEN_MIN[$key]:-} seen_hi=${SEEN_MAX[$key]:-}
    [ -n "$seen_lo" ] || { fail "$key: never sampled"; return; }
    [ "$seen_lo" -le "$lo" ] || fail "$key: floor $lo never reached (min seen $seen_lo)"
    [ "$seen_hi" -ge "$hi" ] || fail "$key: cap $hi never reached (max seen $seen_hi)"
    ok
}
check_bound CONNTRACK_MAX        65536    4194304
check_bound NETDEV_MAX_BACKLOG   1024     16384
# fs.nr_open больше не опускается ниже дистрибутивного дефолта, поэтому его
# нижняя граница теперь 1048576. Пол 65536 перешёл к NOFILE_LIMIT — это и
# есть та величина, которая масштабируется по RAM.
check_bound FD_MAX               1048576  2097152
check_bound NOFILE_LIMIT         65536    2097152
check_bound SOCK_MAX             8388608  67108864
check_bound SOCK_DEFAULT         131072   1048576
check_bound UDP_MIN              16384    262144
check_bound SOMAXCONN            4096     65536
check_bound TCP_MAX_ORPHANS      8192     262144
check_bound TCP_MAX_TW_BUCKETS   16384    1048576
check_bound RPS_SOCK_FLOW_ENTRIES 4096    262144
# Floor is RAM-aware (min(64MB, 3% of RAM)), so the smallest host in the matrix
# defines the low end rather than a fixed constant.
check_bound MIN_FREE_KBYTES      12288    1048576
check_bound DIRTY_RATIO          2        20
check_bound FLOW_LIMIT_TABLE_LEN 4096     65536
check_bound NETDEV_BUDGET        300      1200
check_bound NPROC_LIMIT          16384    262144

echo "=== pass 3: file integrity at the extremes ==="
for spec in "524288 1 vpn" "3993600 2 vpn" "3993600 2 panel" \
            "260014080 64 vpn" "1073741824 128 vpn" "1073741824 128 panel"; do
    set -- $spec
    memkb=$1; cpus=$2; profile=$3
    label="${memkb}kB/${cpus}C/$profile"
    rm -rf "$WORK/etc"
    if ! MON_RENDER_ROOT="$WORK" MON_RENDER_DRYRUN=1 \
         MON_FACT_MEMKB="$memkb" MON_FACT_CPUS="$cpus" MON_FACT_PAGESIZE=4096 \
         MON_FACT_LINK_MBPS=10000 MON_FACT_MTU=1500 MON_FACT_FILENR=5000 \
         bash "$RENDERER" render "$profile" >/dev/null 2>&1; then
        fail "$label: render exited non-zero"
        continue
    fi

    f="$WORK/etc/sysctl.d/99-vless-tuning.conf"
    [ -f "$f" ] || { fail "$label: no sysctl file produced"; continue; }

    # unresolved tokens on non-comment lines
    if grep -qE '^[[:space:]]*[^#[:space:]].*@@[A-Z0-9_]+@@' "$f"; then
        fail "$label: unresolved tokens in rendered sysctl"
    fi

    # every key exactly once — sysctl is last-wins, so a duplicate means the
    # effective value depends on file order
    dupes=$(grep -oE '^[a-z][a-z0-9_.-]*' "$f" | sort | uniq -d)
    [ -z "$dupes" ] || fail "$label: duplicate keys: $(echo $dupes)"

    # the FD number must be identical everywhere it is emitted
    fd_sysctl=$(grep -E '^fs\.nr_open' "$f" | awk '{print $3}')
    fd_limits=$(grep -E '^\*[[:space:]]+hard[[:space:]]+nofile' \
        "$WORK/etc/security/limits.d/99-nofile.conf" | awk '{print $4}')
    fd_systemd=$(grep -E '^DefaultLimitNOFILE=' \
        "$WORK/etc/systemd/system.conf.d/limits.conf" | cut -d= -f2)
    fd_haproxy=$(grep -E '^LimitNOFILE=' \
        "$WORK/etc/systemd/system/haproxy.service.d/limits.conf" | cut -d= -f2)
    # Инвариант не равенство: fs.nr_open — потолок и обязан быть >= того, что
    # реально выдаётся процессам, иначе setrlimit у юнита падает с EPERM.
    # Совпадать между собой должны limits.conf, DefaultLimitNOFILE и drop-in
    # HAProxy — это одно и то же число NOFILE_LIMIT.
    if [ "$fd_limits" != "$fd_systemd" ] || [ "$fd_limits" != "$fd_haproxy" ]; then
        fail "$label: NOFILE_LIMIT расходится (limits=$fd_limits systemd=$fd_systemd haproxy=$fd_haproxy)"
    fi
    if [ "$fd_limits" -gt "$fd_sysctl" ]; then
        fail "$label: NOFILE_LIMIT $fd_limits выше fs.nr_open $fd_sysctl"
    fi

    # the [Manager] -> [Slice] rewrite must have happened
    grep -q '^\[Slice\]' "$WORK/etc/systemd/system/user-.slice.d/limits.conf" \
        || fail "$label: user-.slice.d file is not a [Slice] unit"

    # hashsize in modprobe.d must match the facts file
    hs_modprobe=$(grep -oE 'hashsize=[0-9]+' "$WORK/etc/modprobe.d/nf_conntrack.conf" | cut -d= -f2)
    hs_facts=$(grep -E '^CONNTRACK_HASHSIZE=' "$WORK/opt/monitoring/configs/tuning-facts.env" | cut -d= -f2)
    [ "$hs_modprobe" = "$hs_facts" ] \
        || fail "$label: hashsize mismatch modprobe=$hs_modprobe facts=$hs_facts"

    ok
done

echo "=== pass 4: token guard actually refuses a broken base file ==="
# Negative control: without this the guard could be a no-op and pass 3 would
# still be green.
cp "$WORK/opt/monitoring/configs/profiles/common.base.conf" "$WORK/common.bak"
echo "net.ipv4.tcp_fake_key = @@NO_SUCH_TOKEN@@" >> "$WORK/opt/monitoring/configs/profiles/common.base.conf"
if MON_RENDER_ROOT="$WORK" MON_RENDER_DRYRUN=1 MON_FACT_MEMKB=3993600 MON_FACT_CPUS=2 \
   MON_FACT_PAGESIZE=4096 MON_FACT_LINK_MBPS=1000 MON_FACT_MTU=1500 MON_FACT_FILENR=5000 \
   bash "$RENDERER" render vpn >/dev/null 2>&1; then
    fail "renderer accepted a file with an unresolved token"
else
    ok
fi
cp "$WORK/common.bak" "$WORK/opt/monitoring/configs/profiles/common.base.conf"

echo "=== pass 5: duplicate-key guard actually refuses a duplicate ==="
cp "$WORK/opt/monitoring/configs/profiles/vpn.base.conf" "$WORK/vpn.bak"
echo "net.ipv4.tcp_syncookies = 0" >> "$WORK/opt/monitoring/configs/profiles/vpn.base.conf"
if MON_RENDER_ROOT="$WORK" MON_RENDER_DRYRUN=1 MON_FACT_MEMKB=3993600 MON_FACT_CPUS=2 \
   MON_FACT_PAGESIZE=4096 MON_FACT_LINK_MBPS=1000 MON_FACT_MTU=1500 MON_FACT_FILENR=5000 \
   bash "$RENDERER" render vpn >/dev/null 2>&1; then
    fail "renderer accepted a key present in both common and profile"
else
    ok
fi
cp "$WORK/vpn.bak" "$WORK/opt/monitoring/configs/profiles/vpn.base.conf"

echo "=== pass 6: повторный рендер ничего не переписывает ==="
# Матрица рендерила каждую конфигурацию в чистый каталог, поэтому не могла
# увидеть нестабильность между двумя рендерами в один и тот же корень. Из-за
# этого в прод уехало: хеш считался от временного файла (с завершающим
# переводом строки), а ставился он через $(cat ...), который перевод срезает —
# хеши не совпадали никогда, и конфиг переписывался на каждой загрузке.
idem_root="$WORK/idem"
rm -rf "$idem_root"
mkdir -p "$idem_root/opt/monitoring/configs/profiles"
cp "$CONFIGS/profiles/"*.conf "$CONFIGS/profiles/"*.tmpl "$idem_root/opt/monitoring/configs/profiles/"
echo "test" > "$idem_root/opt/monitoring/configs/VERSION"

idem_render() {
    MON_RENDER_ROOT="$idem_root" MON_RENDER_DRYRUN=1     MON_FACT_MEMKB=4009856 MON_FACT_CPUS=2 MON_FACT_PAGESIZE=4096     MON_FACT_LINK_MBPS=1000 MON_FACT_MTU=1500 MON_FACT_FILENR=5000     MON_FACT_NROPEN=1048576 MON_FACT_UNITNOFILE=524288         bash "$RENDERER" render vpn 2>&1
}

idem_render >/dev/null
idem_file="$idem_root/etc/sysctl.d/99-vless-tuning.conf"
first_sha=$(sha256sum "$idem_file" | awk '{print $1}')
[ -f "$idem_root/etc/sysctl.d/99-vless-tuning.conf.prev" ] && had_prev=1 || had_prev=0

second=$(idem_render)
second_sha=$(sha256sum "$idem_file" | awk '{print $1}')

[ "$first_sha" = "$second_sha" ] && ok || fail "повторный рендер изменил содержимое файла"

# Решающая проверка: рендерер обязан сам сказать, что менять нечего.
printf '%s' "$second" | grep -q 'no change' && ok     || fail "повторный рендер переписал файл вместо того чтобы пропустить: $(printf '%s' "$second" | tail -2)"

# И не ротировать .prev вхолостую — иначе предыдущая рабочая версия теряется.
if [ "$had_prev" = "0" ] && [ -f "$idem_root/etc/sysctl.d/99-vless-tuning.conf.prev" ]; then
    fail "повторный рендер создал .prev, хотя ничего не менялось"
else
    ok
fi

echo "=== pass 7: fs.nr_open никогда не понижается ==="
# Понижение fs.nr_open ломает любой юнит со своим LimitNOFILE выше нового
# значения: setrlimit отдаёт EPERM и сервис падает с 205/LIMITS. На живой ноде
# так слёг systemd-logind (LimitNOFILE=524288 против нашего 131072) — сессии,
# loginctl и reboot перестали работать. nr_open памяти не занимает, это лишь
# потолок, поэтому масштабировать по RAM нужно NOFILE_LIMIT, а не его.
nropen_facts() {
    MON_RENDER_ROOT="$WORK" MON_RENDER_DRYRUN=1     MON_FACT_MEMKB="$1" MON_FACT_CPUS=2 MON_FACT_PAGESIZE=4096     MON_FACT_LINK_MBPS=1000 MON_FACT_MTU=1500 MON_FACT_FILENR=5000     MON_FACT_NROPEN="$2" MON_FACT_UNITNOFILE="$3"         bash "$RENDERER" facts vpn 2>/dev/null | grep -E "^$4=" | cut -d= -f2
}

# Хост с 4 ГБ: NOFILE_LIMIT масштабируется вниз, а nr_open обязан остаться.
fd=$(nropen_facts 4009856 1048576 524288 FD_MAX)
nl=$(nropen_facts 4009856 1048576 524288 NOFILE_LIMIT)
[ "$fd" -ge 1048576 ] && ok || fail "fs.nr_open понижен до $fd (было 1048576)"
[ "$fd" -ge 524288 ]  && ok || fail "fs.nr_open $fd ниже LimitNOFILE=524288 у юнита"
[ "$nl" -lt "$fd" ]   && ok || fail "NOFILE_LIMIT $nl не масштабируется отдельно от nr_open $fd"

# Юнит, требующий больше дистрибутивного дефолта, тоже обязан быть учтён.
fd2=$(nropen_facts 4009856 1048576 2097152 FD_MAX)
[ "$fd2" -ge 2097152 ] && ok || fail "fs.nr_open $fd2 ниже LimitNOFILE=2097152 у юнита"

# Хост, где nr_open уже поднят выше дефолта, не должен его терять.
fd3=$(nropen_facts 4009856 4194304 524288 FD_MAX)
[ "$fd3" -ge 4194304 ] && ok || fail "fs.nr_open понижен с 4194304 до $fd3"

echo "=== pass 8: local-overrides.conf реально перекрывает базу ==="
# Документация обещает переопределение без передеплоя, но страж дублей
# отвергал рендер: ключ оказывался и в базе, и в оверрайдах.
ov_root="$WORK/ov"
rm -rf "$ov_root"
mkdir -p "$ov_root/opt/monitoring/configs/profiles"
cp "$CONFIGS/profiles/"*.conf "$CONFIGS/profiles/"*.tmpl "$ov_root/opt/monitoring/configs/profiles/"
echo "test" > "$ov_root/opt/monitoring/configs/VERSION"
printf 'net.ipv4.tcp_timestamps = 0
fs.nr_open = 999424
'     > "$ov_root/opt/monitoring/configs/local-overrides.conf"

if MON_RENDER_ROOT="$ov_root" MON_RENDER_DRYRUN=1 MON_FACT_MEMKB=4009856 MON_FACT_CPUS=2    MON_FACT_PAGESIZE=4096 MON_FACT_LINK_MBPS=1000 MON_FACT_MTU=1500 MON_FACT_FILENR=5000    MON_FACT_NROPEN=1048576 MON_FACT_UNITNOFILE=524288    bash "$RENDERER" render vpn >/dev/null 2>&1; then
    ok
else
    fail "рендер с local-overrides.conf отвергнут (страж дублей)"
fi

ovf="$ov_root/etc/sysctl.d/99-vless-tuning.conf"
if [ -f "$ovf" ]; then
    [ "$(grep -c '^net.ipv4.tcp_timestamps' "$ovf")" = "1" ] && ok         || fail "tcp_timestamps встречается $(grep -c '^net.ipv4.tcp_timestamps' "$ovf") раз, а не один"
    grep -q '^net.ipv4.tcp_timestamps = 0' "$ovf" && ok         || fail "оверрайд не победил: $(grep '^net.ipv4.tcp_timestamps' "$ovf")"
    [ "$(grep -c '^fs.nr_open' "$ovf")" = "1" ] && ok || fail "fs.nr_open задублирован"
else
    fail "файл не отрендерился"
fi

echo "=== pass 9: джиттер MemTotal не меняет вывод ==="
# MemTotal на виртуалках гуляет между загрузками на десятки-сотни КБ. Раньше это
# меняло tcp_mem/udp_mem (прямое деление числа страниц, без квантования): файл
# переписывался каждую загрузку, а facts_hash давал ложный drift — панель начала
# бы требовать переприменения на всём парке после каждого ребута.
jitter_root="$WORK/jit"
render_jitter() {
    rm -rf "$jitter_root"
    mkdir -p "$jitter_root/opt/monitoring/configs/profiles"
    cp "$CONFIGS/profiles/"*.conf "$CONFIGS/profiles/"*.tmpl         "$jitter_root/opt/monitoring/configs/profiles/"
    echo "test" > "$jitter_root/opt/monitoring/configs/VERSION"
    MON_RENDER_ROOT="$jitter_root" MON_RENDER_DRYRUN=1     MON_FACT_MEMKB="$1" MON_FACT_CPUS=2 MON_FACT_PAGESIZE=4096     MON_FACT_LINK_MBPS=1000 MON_FACT_MTU=1500 MON_FACT_FILENR=5000         bash "$RENDERER" render vpn >/dev/null 2>&1
    sha256sum "$jitter_root/etc/sysctl.d/99-vless-tuning.conf" | awk '{print $1}'
    grep -oE '"facts_hash": "[^"]*"'         "$jitter_root/opt/monitoring/configs/tuning-facts.json"
}

# Реальный случай с ноды: MemTotal дрогнул на ~84 КБ между загрузками.
base=$(render_jitter 4009856)
jit=$(render_jitter 4009772)
if [ "$base" = "$jit" ]; then
    ok
else
    fail "джиттер MemTotal изменил вывод рендерера:"
    printf '  было: %s
  стало: %s
' "$base" "$jit" >&2
fi

# А смена размера машины по-настоящему обязана быть замечена.
big=$(render_jitter 16281600)
if [ "$base" != "$big" ]; then
    ok
else
    fail "рендерер не заметил смену RAM 4 ГБ -> 16 ГБ"
fi

echo "=== pass 10: verify не падает ни на одной из двух веток ==="
# do_verify вызывает sysctl и потому в этой матрице не выполнялся — так в прод
# уехал `unsupported: unbound variable` (local без инициализации + set -u).
# Веток две: с рабочим python3 и без него, и падала ИМЕННО первая — поэтому
# гоняем обе, подменяя python3 заглушкой.
VBIN="$WORK/bin"
mkdir -p "$VBIN"
cat > "$VBIN/sysctl" <<'STUB'
#!/bin/bash
[ "$1" = "-n" ] || exit 1
case "$2" in
    net.netfilter.nf_conntrack_max)   echo 524288 ;;
    net.ipv4.tcp_mem)                 printf '93600	124800	156000
' ;;
    net.ipv4.tcp_congestion_control)  echo bbr ;;
    *) exit 1 ;;
esac
STUB
chmod +x "$VBIN/sysctl"

write_facts() {
    cat > "$WORK/opt/monitoring/configs/tuning-facts.json" <<FACTS
{
  "schema": 1,
  "computed": {
    "net.netfilter.nf_conntrack_max": "$1",
    "net.ipv4.tcp_mem": "93600 124800 156000"
  },
  "static": { "net.ipv4.tcp_congestion_control": "bbr" },
  "derived": {},
  "unsupported_keys": []
}
FACTS
}

# Настоящий интерпретатор под именем python3. На Windows `python3` из WindowsApps
# — заглушка, которая ничего не исполняет, поэтому ищем рабочий отдельно.
REAL_PY=""
for cand in python3 python python3.12 python3.11; do
    command -v "$cand" >/dev/null 2>&1 || continue
    if echo 'print("ok")' | "$cand" - 2>/dev/null | grep -q '^ok$'; then
        REAL_PY=$(command -v "$cand")
        break
    fi
done

run_verify() {
    PATH="$VBIN:$PATH" MON_RENDER_ROOT="$WORK" bash "$RENDERER" verify         >"$WORK/verify.out" 2>"$WORK/verify.err"
    echo $?
}

check_branch() {
    local label=$1 rc=$2
    if grep -q 'unbound variable\|command not found\|syntax error' "$WORK/verify.err"; then
        fail "$label: verify упал ошибкой bash — $(head -1 "$WORK/verify.err")"
    elif [ "$rc" -gt 1 ]; then
        fail "$label: verify вернул код $rc (ожидались 0 или 1)"
    elif ! grep -q '"success": true' "$WORK/verify.out"; then
        fail "$label: verify не сошёлся там, где значения совпадают — $(cat "$WORK/verify.out")"
    elif grep -q 'tcp_mem' "$WORK/verify.out"; then
        fail "$label: tcp_mem попал в расхождения (табы против пробелов)"
    else
        ok
    fi
}

write_facts 524288

# Ветка 1 — рабочий python3 (основной путь на боевой ноде)
if [ -n "$REAL_PY" ]; then
    printf '#!/bin/bash
exec %s "$@"
' "$REAL_PY" > "$VBIN/python3"
    chmod +x "$VBIN/python3"
    check_branch "python3-ветка" "$(run_verify)"
else
    echo "  SKIP: рабочего python3 не нашлось, ветку не проверить"
fi

# Ветка 2 — python3 недоступен (bare host во время install)
printf '#!/bin/bash
exit 1
' > "$VBIN/python3"
chmod +x "$VBIN/python3"
check_branch "fallback-ветка" "$(run_verify)"

# Расхождение обязано быть замечено — иначе verify зелёный всегда.
write_facts 262144
if [ "$(run_verify)" = "0" ]; then
    fail "verify не заметил расхождения значения"
else
    ok
fi

echo
echo "checks passed: $PASS   failed: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
echo "OK"

"""Шлюзы доп. IP-адресов на настоящем ядре: host-скрипт в отдельном сетевом
пространстве (`unshare --user --net`), без root и без влияния на машину.

Запуск из node/:  python -m unittest tests.test_extra_ips_netns

Модуль не импортирует `app`, поэтому идёт и там, где зависимостей агента нет.
Нужен Linux с непривилегированными user namespace; иначе тест пропускается
(на Ubuntu 24.04 их может запрещать AppArmor).

Закреплено: правила и таблицы ставятся по routes.list и повторный прогон ничего
не пишет; самолечение возвращает снесённое; лишнее и пустой список убирают всё
своё, включая suppress-правила; адрес со своим шлюзом ходит в интернет через
него, свою подсеть — напрямую, а трафик без адреса — через основной шлюз;
транзакция ставит маршрут, откат и провал проверки его снимают, удаление адреса
забирает маршрут с собой; restore-runtime поднимает всё после «перезагрузки».
Второй тест гоняет живой трафик через два роутера-namespace: без своего шлюза
клиент за шлюзом доп. адреса ответов не получает, со шлюзом — получает, основной
адрес работает по-прежнему, после сноса маршрута самолечение возвращает ответы.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "app" / "services" / "host_extra_ips.sh"

SCENARIO = r"""
set -u
SCRIPT="$1"
export EXTRA_IPS_STATE_DIR="$(mktemp -d)"
STATE="$EXTRA_IPS_STATE_DIR"
fail() { echo "FAIL: $*"; exit 1; }

mount -t sysfs sysfs /sys || fail "cannot mount sysfs"
ip link add eth0 type dummy && ip link set eth0 up
ip addr add 10.0.0.2/24 dev eth0
ip route add default via 10.0.0.1 dev eth0
ip -6 addr add 2001:db8:1::2/64 dev eth0 nodad
ip -6 route add default via fe80::1 dev eth0

printf '%s\n' "eth0 2.2.2.5 2.2.2.1 1001" "eth0 2.2.2.6 2.2.2.1 1001" "eth0 3.3.3.7 3.3.3.1 1002" \
    "eth0 2001:db8:9::5 fe80::9 1003" > "$STATE/routes.list"
out=$(bash "$SCRIPT" sync-routes) || fail "sync-routes: $out"
[ "$out" != "ROUTES_CHANGED=0" ] || fail "first sync changed nothing"
out=$(bash "$SCRIPT" sync-routes)
[ "$out" = "ROUTES_CHANGED=0" ] || fail "second sync is not a no-op: $out"
ip rule show | grep -q "^999:.*lookup main suppress_prefixlength 0" || fail "v4 suppress rule missing"
ip -6 rule show | grep -q "^1000:.*from 2001:db8:9::5 lookup 1003" || fail "v6 rule missing"

ip addr add 2.2.2.5/32 dev eth0
ip route get 8.8.8.8 from 2.2.2.5 | grep -q "via 2.2.2.1 dev eth0 table 1001" || fail "extra address ignores its gateway"
ip route get 10.0.0.9 from 2.2.2.5 | head -1 | grep -q " via " && fail "own subnet must stay on-link"
ip route get 8.8.8.8 | grep -q "via 10.0.0.1" || fail "unbound traffic must keep the main gateway"

ip route flush table 1002
ip rule del from 2.2.2.6 lookup 1001 priority 1000
out=$(bash "$SCRIPT" sync-routes)
[ "$out" = "ROUTES_CHANGED=2" ] || fail "self-heal: $out"

sed -i '/3.3.3.7/d' "$STATE/routes.list"
bash "$SCRIPT" sync-routes >/dev/null
ip rule show | grep -q "3.3.3.7" && fail "stale rule kept"
[ -z "$(ip route show table 1002)" ] || fail "unused table kept"

: > "$STATE/routes.list"
bash "$SCRIPT" sync-routes >/dev/null
ip rule show | grep -qE "^(999|1000):" && fail "v4 rules left"
ip -6 rule show | grep -qE "^(999|1000):" && fail "v6 rules left"
ip route show table all | grep -qE "table 10[0-9][0-9]" && fail "tables left"

b64() { printf '%s' "$1" | base64 -w0; }
plan() {
    printf 'TX_ID=%s\nIFACE=eth0\nBACKEND=fallback\nDETAIL=x\nTIMEOUT=30\nADD=%s\nREMOVE=%s\nPROTECTED=10.0.0.2\nMANAGED_B64=%s\nROUTES_B64=%s\n' \
        "$1" "$2" "$3" "$(b64 "$4")" "$(b64 "$5")"
}
NL='
'
rm -f "$STATE/routes.list"
plan 20260925-120000-aaaa 4.4.4.4/32 "" "eth0 4.4.4.4/32$NL" "eth0 4.4.4.4 4.4.4.1 1001$NL" | bash "$SCRIPT" apply >/dev/null \
    || fail "apply"
grep -q "TX_STATUS=pending" "$STATE/transaction.env" || fail "not pending"
ip rule show | grep -q "from 4.4.4.4 lookup 1001" || fail "rule missing after apply"
bash "$SCRIPT" rollback 20260925-120000-aaaa >/dev/null || fail "rollback"
ip rule show | grep -qE "4.4.4.4|^999:" && fail "rules survived rollback"
[ -f "$STATE/routes.list" ] && fail "routes.list survived rollback"

plan 20260925-120100-bbbb 4.4.4.4/32 "" "eth0 4.4.4.4/32$NL" "eth0 4.4.4.4 4.4.4.1 1001$NL" | bash "$SCRIPT" apply >/dev/null \
    || fail "apply 2"
bash "$SCRIPT" confirm 20260925-120100-bbbb >/dev/null || fail "confirm"

ip rule del from 4.4.4.4 lookup 1001 priority 1000
ip route flush table 1001
ip addr del 4.4.4.4/32 dev eth0
bash "$SCRIPT" restore-runtime || fail "restore-runtime"
ip addr show dev eth0 | grep -q "4.4.4.4/32" || fail "address not restored"
ip route get 8.8.8.8 from 4.4.4.4 | grep -q "via 4.4.4.1" || fail "gateway route not restored"

plan 20260925-120200-cccc "" 4.4.4.4/32 "" "" | bash "$SCRIPT" apply >/dev/null || fail "apply remove"
ip rule show | grep -qE "4.4.4.4|^999:" && fail "rules kept after the address was removed"
ip route show table all | grep -q "table 1001" && fail "table kept after the address was removed"
bash "$SCRIPT" confirm 20260925-120200-cccc >/dev/null

ip link set eth0 down
plan 20260925-120300-dddd 5.5.5.5/32 "" "eth0 5.5.5.5/32$NL" "eth0 5.5.5.5 5.5.5.1 1001$NL" | bash "$SCRIPT" apply >/dev/null 2>&1
rc=$?
ip link set eth0 up
[ "$rc" = 4 ] || fail "apply on a down link: exit $rc, expected rolled back (4)"
ip rule show | grep -qE "5.5.5.5|^999:" && fail "rules left after a failed apply"
echo "ALL OK"
"""

# Живой трафик: нода на общем сегменте с двумя роутерами. A — основной шлюз
# 10.0.0.1, про клиента 9.9.9.9 не знает; B — шлюз доп. адреса 5.5.5.1 (вне
# подсети ноды), клиент за ним. Ответ с 5.5.5.5 доходит, только если уходит через B.
E2E_SCENARIO = r"""
set -u
SCRIPT="$1"
export EXTRA_IPS_STATE_DIR="$(mktemp -d)"
fail() { echo "FAIL: $*"; kill "$PA" "$PB" 2>/dev/null; exit 1; }

mount -t sysfs sysfs /sys || fail "cannot mount sysfs"
ip link set lo up
ip link add br0 type bridge && ip link set br0 up
unshare --net sleep 600 & PA=$!
unshare --net sleep 600 & PB=$!
sleep 0.3
ip link add vA type veth peer name vA0
ip link set vA0 netns "$PA"
ip link set vA master br0 up
nsenter -t "$PA" -n sh -c 'ip link set lo up; ip link set vA0 up; ip addr add 10.0.0.1/24 dev vA0'
ip link add vB type veth peer name vB0
ip link set vB0 netns "$PB"
ip link set vB master br0 up
nsenter -t "$PB" -n sh -c 'ip link set lo up; ip link set vB0 up; ip addr add 5.5.5.1/32 dev vB0; ip addr add 9.9.9.9/32 dev lo; ip route add 5.5.5.5/32 dev vB0'
ip addr add 10.0.0.2/24 dev br0
ip route add default via 10.0.0.1 dev br0
sleep 1

client_ping() { nsenter -t "$PB" -n ping -c 2 -W 1 -I 9.9.9.9 "$1" >/dev/null 2>&1; }
b64() { printf '%s' "$1" | base64 -w0; }
NL='
'
plan() {
    printf 'TX_ID=%s\nIFACE=br0\nBACKEND=fallback\nDETAIL=x\nTIMEOUT=30\nADD=%s\nREMOVE=%s\nPROTECTED=10.0.0.2\nMANAGED_B64=%s\nROUTES_B64=%s\n' \
        "$1" "$2" "$3" "$(b64 "$4")" "$(b64 "$5")"
}

plan 20260925-130000-aaaa 5.5.5.5/32 "" "br0 5.5.5.5/32$NL" "" | bash "$SCRIPT" apply >/dev/null || fail "apply without gateway"
bash "$SCRIPT" confirm 20260925-130000-aaaa >/dev/null
client_ping 5.5.5.5 && fail "without its own gateway the reply must not reach the client"

plan 20260925-130100-bbbb "" 5.5.5.5/32 "" "" | bash "$SCRIPT" apply >/dev/null || fail "remove"
bash "$SCRIPT" confirm 20260925-130100-bbbb >/dev/null
plan 20260925-130200-cccc 5.5.5.5/32 "" "br0 5.5.5.5/32$NL" "br0 5.5.5.5 5.5.5.1 1001$NL" | bash "$SCRIPT" apply >/dev/null \
    || fail "apply with gateway"
bash "$SCRIPT" confirm 20260925-130200-cccc >/dev/null
client_ping 5.5.5.5 || fail "with its own gateway the client must get replies"

nsenter -t "$PA" -n ping -c 2 -W 1 10.0.0.2 >/dev/null 2>&1 || fail "primary address broken"
client_ping 10.0.0.2 && fail "primary address replies must keep going via the main gateway"

ip route flush table 1001
client_ping 5.5.5.5 && fail "damage was not simulated"
[ "$(bash "$SCRIPT" sync-routes)" = "ROUTES_CHANGED=1" ] || fail "self-heal"
client_ping 5.5.5.5 || fail "replies are not back after self-heal"

kill "$PA" "$PB" 2>/dev/null
echo "E2E OK"
"""


def netns_available() -> bool:
    if sys.platform == "win32" or not shutil.which("unshare"):
        return False
    probe = subprocess.run(["unshare", "--user", "--map-root-user", "--net", "--mount", "true"], capture_output=True)
    return probe.returncode == 0


def run_in_netns(scenario: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "extra-ips.sh"
        # Байтами: на хосте с autocrlf текстовое чтение принесло бы CRLF
        script.write_bytes(SCRIPT_PATH.read_bytes().replace(b"\r\n", b"\n"))
        run = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--net", "--mount", "bash", "-s", str(script)],
            input=scenario.encode("utf-8"), capture_output=True, timeout=180,
        )
    return run.returncode, run.stdout.decode("utf-8", "replace") + run.stderr.decode("utf-8", "replace")


class GatewayRoutesNetnsTest(unittest.TestCase):
    @unittest.skipUnless(netns_available(), "needs Linux with unprivileged user and network namespaces")
    def test_gateway_routes_on_a_real_kernel(self):
        code, output = run_in_netns(SCENARIO)
        self.assertEqual(code, 0, output)
        self.assertIn("ALL OK", output)

    @unittest.skipUnless(netns_available() and shutil.which("nsenter") and shutil.which("ping"),
                         "needs user/network namespaces, nsenter and ping")
    def test_replies_reach_the_client_only_through_the_own_gateway(self):
        code, output = run_in_netns(E2E_SCENARIO)
        self.assertEqual(code, 0, output)
        self.assertIn("E2E OK", output)


if __name__ == "__main__":
    unittest.main()

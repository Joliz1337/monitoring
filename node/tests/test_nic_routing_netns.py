"""Ответы через свою карту на настоящем ядре: host-скрипт в отдельном сетевом
пространстве (`unshare --user --net`), без root и без влияния на машину.

Запуск из node/:  python -m unittest tests.test_nic_routing_netns

Модуль не импортирует `app`, поэтому идёт и там, где зависимостей агента нет.
Нужен Linux с непривилегированными user namespace; иначе тест пропускается
(на Ubuntu 24.04 их может запрещать AppArmor). У dummy- и veth-карт нет
device/, поэтому «физические» карты подсовываются через NIC_ROUTING_SYS_NET.

Закреплено: карта основного default не получает ни правил, ни таблицы; адрес
второй карты ходит в интернет и к соседям по подсети через неё, а docker и
трафик без адреса — по main; повторный проход ничего не пишет; снесённое
(перезапуск networkd) возвращается проходом и службой watch; VIP и смена адреса
подхватываются, правила старого адреса уходят; шлюз берётся из аренды DHCP;
карта основного default меняется вместе с default; опущенная карта теряет
правила; чужие правила агента не трогаются ни проходом, ни clear.
Второй тест гоняет живой трафик через роутер-namespace со строгим rp_filter —
так облако режет пакет с чужим адресом на порту: без правил адрес второй карты
не отвечает, с правилами отвечает, основной адрес работает всё время.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "app" / "services" / "host_nic_routing.sh"

PRELUDE = r"""
set -u
SCRIPT="$1"
FAKE=$(mktemp -d)
export NIC_ROUTING_SYS_NET="$FAKE/net" NIC_ROUTING_LEASES_DIR="$FAKE/leases"
mkdir -p "$NIC_ROUTING_SYS_NET" "$NIC_ROUTING_LEASES_DIR"
mark_physical() { mkdir -p "$NIC_ROUTING_SYS_NET/$1/device"; }
ifindex() { ip -o link show dev "$1" | awk -F': ' '{print $1}'; }
route_via() { ip -4 route get "$@" 2>&1 | head -1; }
nsr() { bash "$SCRIPT" "$@"; }
ip link set lo up
"""

SCENARIO = PRELUDE + r"""
fail() { echo "FAIL: $*"; ip rule; ip route show table all | grep -v "table local"; exit 1; }
expect_dev() {
    local want="$1"; shift
    route_via "$@" | grep -q "dev $want " || fail "route get $* -> $(route_via "$@") (want $want)"
}

for nic in eth0 eth1; do ip link add "$nic" type dummy && ip link set "$nic" up && mark_physical "$nic"; done
ip link add docker0 type dummy && ip link set docker0 up
ip addr add 192.168.0.106/24 dev eth0
ip addr add 192.168.0.127/24 dev eth1
ip addr add 172.17.0.1/16 dev docker0
ip route add default via 192.168.0.1 dev eth0 metric 100
T0=$((20000 + $(ifindex eth0))); T1=$((20000 + $(ifindex eth1)))
# Правила агента: пул исходящих (101), доп. IP (999/1000 и таблица 1001)
ip rule add fwmark 101 lookup 101 priority 101
ip rule add lookup main suppress_prefixlength 0 priority 999
ip rule add from 10.9.9.9 lookup 1001 priority 1000
ip route add default via 192.168.0.1 dev eth0 onlink table 1001
foreign() { ip rule show | grep -E '^(101|999|1000):'; ip route show table 1001; }
FOREIGN=$(foreign)

nsr sync >/dev/null || fail "sync exit code"
ip rule show | grep -q "192.168.0.106" && fail "the main-default NIC got rules"
[ -z "$(ip route show table "$T0" 2>/dev/null)" ] || fail "the main-default NIC got a table"
expect_dev eth1 8.8.8.8 from 192.168.0.127
expect_dev eth1 192.168.0.50 from 192.168.0.127
expect_dev docker0 172.17.0.2 from 192.168.0.127
expect_dev eth0 8.8.8.8 from 192.168.0.106
route_via 8.8.8.8 | grep -q "dev eth0 src 192.168.0.106" || fail "unbound traffic left main: $(route_via 8.8.8.8)"
ip route show table all | grep -qE "dev docker0 table 2[0-9]{4}" && fail "docker0 got a table"

status=$(nsr status)
echo "$status" | grep -qx "NIC eth0 primary $T0 192.168.0.1 192.168.0.106/24" || fail "status of eth0: $status"
echo "$status" | grep -qx "NIC eth1 routed $T1 192.168.0.1 192.168.0.127/24" || fail "status of eth1: $status"
echo "$status" | grep -qx "IN_SYNC=yes" || fail "not in sync: $status"
[ -z "$(nsr sync | grep '^ip -4')" ] || fail "the second pass wrote something"

ip rule del priority 1101 from 192.168.0.127 lookup "$T1"; ip route flush table "$T1"
nsr status | grep -qx "IN_SYNC=no" || fail "damage is not seen"
nsr sync >/dev/null
expect_dev eth1 8.8.8.8 from 192.168.0.127
nsr status | grep -qx "IN_SYNC=yes" || fail "not restored"

ip addr add 192.168.0.200/24 dev eth1
nsr sync >/dev/null
expect_dev eth1 8.8.8.8 from 192.168.0.200
[ "$(ip route show table "$T1" | grep -c '192.168.0.0/24')" -eq 1 ] || fail "duplicate subnet route"

ip addr del 192.168.0.127/24 dev eth1; ip addr add 192.168.0.128/24 dev eth1
nsr sync >/dev/null
ip rule show | grep -q "192.168.0.127" && fail "rules of the old address kept"
expect_dev eth1 8.8.8.8 from 192.168.0.128

echo "ROUTER=192.168.0.254 192.168.0.253" > "$NIC_ROUTING_LEASES_DIR/$(ifindex eth1)"
nsr sync >/dev/null
ip route show table "$T1" | grep -q "default via 192.168.0.254 dev eth1" || fail "lease router ignored"
rm -f "$NIC_ROUTING_LEASES_DIR/$(ifindex eth1)"

# Default переехал на eth1: теперь eth1 основная, а eth0 отвечает через свою таблицу
ip route del default via 192.168.0.1 dev eth0
ip route add default via 192.168.0.1 dev eth1 metric 100
nsr sync >/dev/null
ip rule show | grep -q "lookup $T1" && fail "the new main-default NIC kept its rules"
expect_dev eth0 8.8.8.8 from 192.168.0.106
ip route del default via 192.168.0.1 dev eth1
ip route add default via 192.168.0.1 dev eth0 metric 100
nsr sync >/dev/null

ip link set eth1 down
nsr sync >/dev/null
ip rule show | grep -q "lookup $T1" && fail "rules of a down NIC kept"
[ -z "$(ip route show table "$T1" 2>/dev/null)" ] || fail "table of a down NIC kept"
ip link set eth1 up
nsr sync >/dev/null

[ "$(foreign)" = "$FOREIGN" ] || fail "agent rules changed"

nsr watch >"$FAKE/watch.log" 2>&1 &
WATCH=$!
sleep 2
ip rule del priority 1101 from 192.168.0.128 lookup "$T1"
sleep 3
ip rule show | grep -q "^1101:.*from 192.168.0.128 lookup $T1" || { cat "$FAKE/watch.log"; fail "watch did not restore the rule"; }
pkill -P "$WATCH"; kill "$WATCH" 2>/dev/null; wait 2>/dev/null

nsr clear >/dev/null
ip rule show | grep -qE '^(995|1100|1101):' && fail "clear left rules"
ip route show table all | grep -qE 'table 2[0-9]{4}' && fail "clear left tables"
[ "$(foreign)" = "$FOREIGN" ] || fail "clear touched agent rules"
echo "ALL OK"
"""

# Облако: один роутер на обе карты, пакет с чужим адресом на порту отбрасывается
# (строгий rp_filter роутера). Клиент 9.9.9.9 — на loopback роутера.
E2E_SCENARIO = PRELUDE + r"""
fail() { echo "FAIL: $*"; kill "$PR" 2>/dev/null; exit 1; }
unshare --net sleep 600 & PR=$!
sleep 0.3
in_router() { nsenter -t "$PR" -n sh -c "$1"; }
for i in 0 1; do
    ip link add "eth$i" type veth peer name "r$i"
    ip link set "r$i" netns "$PR"
    ip link set "eth$i" up
    mark_physical "eth$i"
done
in_router 'ip link set lo up; ip addr add 9.9.9.9/32 dev lo
    sysctl -qw net.ipv4.conf.all.rp_filter=1
    for i in 0 1; do sysctl -qw net.ipv4.conf.r$i.rp_filter=1; ip link set r$i up; ip addr add 192.168.0.1/32 dev r$i; done
    ip route add 192.168.0.106/32 dev r0; ip route add 192.168.0.127/32 dev r1' || fail "router setup"
ip addr add 192.168.0.106/24 dev eth0
ip addr add 192.168.0.127/24 dev eth1
ip route add default via 192.168.0.1 dev eth0
sleep 1

client_ping() { nsenter -t "$PR" -n ping -c 2 -W 1 -I 9.9.9.9 "$1" >/dev/null 2>&1; }
client_ping 192.168.0.106 || fail "primary address does not answer at all"
client_ping 192.168.0.127 && fail "the cloud drop was not simulated: the second address answers without rules"

nsr sync >/dev/null
client_ping 192.168.0.127 || fail "the second address does not answer with the rules"
client_ping 192.168.0.106 || fail "primary address broken by the rules"

nsr clear >/dev/null
client_ping 192.168.0.127 && fail "the second address still answers after clear"
client_ping 192.168.0.106 || fail "primary address broken after clear"
kill "$PR" 2>/dev/null
echo "E2E OK"
"""


def netns_available() -> bool:
    if sys.platform == "win32" or not shutil.which("unshare"):
        return False
    probe = subprocess.run(["unshare", "--user", "--map-root-user", "--net", "--mount", "true"], capture_output=True)
    return probe.returncode == 0


def run_in_netns(scenario: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "nic-routing.sh"
        # Байтами: на хосте с autocrlf текстовое чтение принесло бы CRLF
        script.write_bytes(SCRIPT_PATH.read_bytes().replace(b"\r\n", b"\n"))
        run = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--net", "--mount", "bash", "-s", str(script)],
            input=scenario.encode("utf-8"), capture_output=True, timeout=180,
        )
    return run.returncode, run.stdout.decode("utf-8", "replace") + run.stderr.decode("utf-8", "replace")


class NicRoutingNetnsTest(unittest.TestCase):
    @unittest.skipUnless(netns_available(), "needs Linux with unprivileged user and network namespaces")
    def test_rules_on_a_real_kernel(self):
        code, output = run_in_netns(SCENARIO)
        self.assertEqual(code, 0, output)
        self.assertIn("ALL OK", output)

    @unittest.skipUnless(netns_available() and shutil.which("nsenter") and shutil.which("ping"),
                         "needs user/network namespaces, nsenter and ping")
    def test_second_address_answers_only_through_its_own_nic(self):
        code, output = run_in_netns(E2E_SCENARIO)
        self.assertEqual(code, 0, output)
        self.assertIn("E2E OK", output)


if __name__ == "__main__":
    unittest.main()

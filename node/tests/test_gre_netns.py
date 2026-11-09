"""GRE-туннели DDoS-Guard на настоящем ядре: host-скрипт в отдельном сетевом
пространстве (`unshare --user --net`), без root и без влияния на машину.

Запуск из node/:  python -m unittest tests.test_gre_netns

Модуль не импортирует `app`, поэтому идёт и там, где зависимостей агента нет.
Нужен Linux с непривилегированными user namespace и модулем ip_gre (в namespace
его не загрузить); иначе тест пропускается.

Закреплено: туннель поднимается с CE/30, MTU от карты и loose rp_filter; трафик с
CE (наружу и к сети проверок) уходит в туннель, docker и трафик без адреса — по
main; повторный проход ничего не пишет; снесённое правило и удалённый интерфейс
возвращаются проходом и службой watch; смена CE переносит адрес и правило;
без GRE SRC на интерфейсах и при чужом туннеле с теми же адресами своего туннеля
нет, а статус называет причину; чужие правила агента не трогаются; clear снимает всё.
Второй тест гоняет живой трафик через «DDoS-Guard» в соседнем namespace со строгим
rp_filter: без туннеля CE не отвечает, с туннелем отвечает и сети проверок, и
клиенту, основной адрес сервера работает всё время.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "app" / "services" / "host_gre.sh"

PRELUDE = r"""
set -u
SCRIPT="$1"
FAKE=$(mktemp -d)
export GRE_CONFIG="$FAKE/tunnels.list"
nsg() { bash "$SCRIPT" "$@"; }
route_via() { ip -4 route get "$@" 2>&1 | head -1; }
ip link set lo up
ip link add gre-probe type gre local 127.0.0.1 remote 127.0.0.2 2>/dev/null || { echo "SKIP: no ip_gre"; exit 0; }
ip link del gre-probe
LOCAL=203.0.113.5
TUN="1 $LOCAL 185.129.101.255 91.215.40.173 91.215.40.174 198.18.100.0/24"
"""

SCENARIO = PRELUDE + r"""
fail() { echo "FAIL: $*"; ip -d link show type gre; ip addr; ip rule; ip route show table all | grep -v "table local"; exit 1; }
expect_dev() {
    local want="$1"; shift
    route_via "$@" | grep -q "dev $want " || fail "route get $* -> $(route_via "$@") (want $want)"
}
in_sync() { nsg status | grep -qx "IN_SYNC=yes"; }

ip link add eth0 type dummy && ip link set eth0 up
ip link add docker0 type dummy && ip link set docker0 up
ip addr add "$LOCAL/24" dev eth0
ip addr add 172.17.0.1/16 dev docker0
ip route add default via 203.0.113.1 dev eth0
# Правила агента: пул исходящих (101), доп. IP (999/1000), своя карта (1100/1101)
ip rule add fwmark 101 lookup 101 priority 101
ip rule add lookup main suppress_prefixlength 0 priority 999
ip rule add from 10.9.9.9 lookup 1001 priority 1000
ip rule add from 10.9.9.8 lookup 20005 priority 1101
foreign() { ip rule show | grep -E '^(101|999|1000|1101):'; }
FOREIGN=$(foreign)

nsg status | grep -qx "IN_SYNC=yes" || fail "empty config is not in sync"
echo "$TUN" > "$GRE_CONFIG"
nsg status | grep -q "^TUN 1 .* missing " || fail "missing tunnel not seen: $(nsg status)"
nsg sync >/dev/null || fail "sync exit code"

ip -d -o link show dev gre-ddg1 | grep -q "remote 185.129.101.255 local $LOCAL" || fail "endpoints"
ip -o link show dev gre-ddg1 | grep -q "mtu 1476" || fail "mtu: $(ip -o link show dev gre-ddg1)"
[ "$(ip -4 -o addr show dev gre-ddg1 | awk '{print $4}')" = "91.215.40.174/30" ] || fail "address"
[ "$(cat /proc/sys/net/ipv4/conf/gre-ddg1/rp_filter)" = 2 ] || fail "rp_filter"
expect_dev gre-ddg1 1.1.1.1 from 91.215.40.174
route_via 1.1.1.1 from 91.215.40.174 | grep -q "via 91.215.40.173" || fail "replies do not go to PE"
expect_dev gre-ddg1 198.18.100.7 from 91.215.40.174
expect_dev docker0 172.17.0.2 from 91.215.40.174
expect_dev eth0 1.1.1.1
expect_dev eth0 1.1.1.1 from "$LOCAL"
expect_dev eth0 185.129.101.255 from "$LOCAL"
nsg status | grep -qx "TUN $TUN ok 1476 -" || fail "status: $(nsg status)"
in_sync || fail "not in sync"
[ -z "$(nsg sync | grep -E '^(ip -4|rp_filter)')" ] || fail "the second pass wrote something"

ip rule del priority 1151 from 91.215.40.174 lookup 1201
in_sync && fail "damage is not seen"
nsg sync >/dev/null
expect_dev gre-ddg1 1.1.1.1 from 91.215.40.174
ip link del gre-ddg1
nsg status | grep -q "^TUN 1 .* missing " || fail "deleted link is not seen"
nsg sync >/dev/null
in_sync || fail "deleted link not restored"

# Карта с меньшим MTU (облако с VXLAN): туннель уменьшается вместе с ней
ip link set eth0 mtu 1450
nsg sync >/dev/null
ip -o link show dev gre-ddg1 | grep -q "mtu 1426" || fail "mtu not derived from the underlay"
ip link set eth0 mtu 1500

# Сменился CE: старый адрес и правило уходят, новые встают
echo "1 $LOCAL 185.129.101.255 91.215.40.178 91.215.40.177 198.18.100.0/24" > "$GRE_CONFIG"
nsg sync >/dev/null
[ "$(ip -4 -o addr show dev gre-ddg1 | awk '{print $4}')" = "91.215.40.177/30" ] || fail "CE not moved"
ip rule show | grep -q "91.215.40.174" && fail "rule of the old CE kept"
expect_dev gre-ddg1 1.1.1.1 from 91.215.40.177
in_sync || fail "not in sync after the CE change"
echo "$TUN" > "$GRE_CONFIG"
nsg sync >/dev/null

# GRE SRC пропал с интерфейса — туннеля нет, статус называет причину
ip addr del "$LOCAL/24" dev eth0
nsg sync >/dev/null
link_gone() { ! ip link show dev gre-ddg1 >/dev/null 2>&1; }
link_gone || fail "tunnel kept without GRE SRC"
nsg status | grep -q "^TUN 1 .* no_local " || fail "no_local not reported"
ip rule show | grep -qE '^115[01]:' && fail "rules kept without GRE SRC"
ip addr add "$LOCAL/24" dev eth0
ip route add default via 203.0.113.1 dev eth0 2>/dev/null
nsg sync >/dev/null
in_sync || fail "not restored with GRE SRC back"

# Чужой туннель с теми же адресами (старый ddg-tunnel.sh): ядро не даст второй
nsg clear >/dev/null
ip link add ddosguard1 type gre local "$LOCAL" remote 185.129.101.255
nsg sync >/dev/null
link_gone || fail "tunnel created next to a foreign one"
nsg status | grep -q "^TUN 1 .* conflict - ddosguard1$" || fail "conflict not reported: $(nsg status)"
ip link del ddosguard1
nsg sync >/dev/null
in_sync || fail "not restored after the foreign tunnel left"

[ "$(foreign)" = "$FOREIGN" ] || fail "agent rules changed"

nsg watch >"$FAKE/watch.log" 2>&1 &
WATCH=$!
sleep 2
ip rule del priority 1151 from 91.215.40.174 lookup 1201
sleep 3
ip rule show | grep -q "^1151:.*from 91.215.40.174 lookup 1201" || { cat "$FAKE/watch.log"; fail "watch did not restore the rule"; }
pkill -P "$WATCH"; kill "$WATCH" 2>/dev/null; wait 2>/dev/null

nsg clear >/dev/null
ip link show dev gre-ddg1 >/dev/null 2>&1 && fail "clear left the link"
ip rule show | grep -qE '^115[01]:' && fail "clear left rules"
ip route show table all | grep -qE 'table 120[1-8]' && fail "clear left routes"
[ "$(foreign)" = "$FOREIGN" ] || fail "clear touched agent rules"
: > "$GRE_CONFIG"
in_sync || fail "empty config after clear is not in sync"
echo "ALL OK"
"""

# «DDoS-Guard» — соседний namespace: его конец GRE, PE, сеть проверок и клиент.
# Строгий rp_filter на его стыке с сервером отбрасывает ответ с CE, ушедший мимо
# туннеля, — как сделал бы провайдер сервера.
E2E_SCENARIO = PRELUDE + r"""
fail() { echo "FAIL: $*"; kill "$PR" 2>/dev/null; exit 1; }
unshare --net sleep 600 & PR=$!
sleep 0.3
in_ddg() { nsenter -t "$PR" -n sh -c "$1"; }
ip link add eth0 type veth peer name r0
ip link set r0 netns "$PR"
ip link set eth0 up
ip addr add "$LOCAL/24" dev eth0
ip route add default via 203.0.113.1 dev eth0
in_ddg 'ip link set lo up; ip link set r0 up; ip addr add 203.0.113.1/24 dev r0
    ip addr add 185.129.101.255/32 dev lo; ip addr add 198.18.100.7/32 dev lo; ip addr add 9.9.9.9/32 dev lo
    sysctl -qw net.ipv4.conf.all.rp_filter=1; sysctl -qw net.ipv4.conf.r0.rp_filter=1
    ip link add ddg type gre local 185.129.101.255 remote 203.0.113.5
    ip link set ddg up; ip addr add 91.215.40.173/30 dev ddg' || fail "ddos-guard side setup"
sleep 1

ping_from() { nsenter -t "$PR" -n ping -c 2 -W 1 -I "$1" "$2" >/dev/null 2>&1; }
ping_from 9.9.9.9 "$LOCAL" || fail "server address does not answer at all"
ping_from 198.18.100.7 91.215.40.174 && fail "CE answers without the tunnel"

echo "$TUN" > "$GRE_CONFIG"
nsg sync >/dev/null
ping_from 198.18.100.7 91.215.40.174 || fail "health check gets no answer through the tunnel"
ping_from 9.9.9.9 91.215.40.174 || fail "client gets no answer through the tunnel"
ping_from 9.9.9.9 "$LOCAL" || fail "server address broken by the tunnel"
ping -c 2 -W 1 -I 91.215.40.174 91.215.40.173 >/dev/null 2>&1 || fail "PE does not answer from CE"

nsg clear >/dev/null
ping_from 198.18.100.7 91.215.40.174 && fail "CE still answers after clear"
ping_from 9.9.9.9 "$LOCAL" || fail "server address broken after clear"
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
        script = Path(tmp) / "gre-tunnels.sh"
        # Байтами: на хосте с autocrlf текстовое чтение принесло бы CRLF
        script.write_bytes(SCRIPT_PATH.read_bytes().replace(b"\r\n", b"\n"))
        run = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--net", "--mount", "bash", "-s", str(script)],
            input=scenario.encode("utf-8"), capture_output=True, timeout=180,
        )
    return run.returncode, run.stdout.decode("utf-8", "replace") + run.stderr.decode("utf-8", "replace")


class GreNetnsTest(unittest.TestCase):
    def assert_passed(self, code: int, output: str, marker: str) -> None:
        if "SKIP: no ip_gre" in output:
            self.skipTest("ip_gre is not loaded on this kernel")
        self.assertEqual(code, 0, output)
        self.assertIn(marker, output)

    @unittest.skipUnless(netns_available(), "needs Linux with unprivileged user and network namespaces")
    def test_tunnel_on_a_real_kernel(self):
        self.assert_passed(*run_in_netns(SCENARIO), "ALL OK")

    @unittest.skipUnless(netns_available() and shutil.which("nsenter") and shutil.which("ping"),
                         "needs user/network namespaces, nsenter and ping")
    def test_ce_answers_only_through_the_tunnel(self):
        self.assert_passed(*run_in_netns(E2E_SCENARIO), "E2E OK")


if __name__ == "__main__":
    unittest.main()

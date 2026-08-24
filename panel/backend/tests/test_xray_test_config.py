"""Тесты сборки конфигов, выбора ядра и матрицы SNI.

Голый unittest, без сети и БД.

Форматы сверены с живыми бинарниками Xray 26.3.27 и sing-box 1.13.19. Главное
здесь — санитизация вставленного JSON: чужой конфиг может нести inbound на
0.0.0.0 или clash_api, а на ноде с host-сетью это открытый порт наружу.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.xray_test.config_builder import build_config  # noqa: E402
from app.services.xray_test.core_manager import select_core  # noqa: E402
from app.services.xray_test.errors import (  # noqa: E402
    LimitExceededError,
    LinkParseError,
    UnsupportedConfigError,
)
from app.services.xray_test.matrix import build_matrix  # noqa: E402
from app.services.xray_test.models import Core, Transport  # noqa: E402
from app.services.xray_test.parsers import parse_link  # noqa: E402
from app.services.xray_test.parsers.json_config import parse_config  # noqa: E402

UUID = "11111111-2222-3333-4444-555555555555"
PBK = "p0by5Raay70X-2hllYoFctRFOd6ONT7y9RPWz2KAHUU"


class CoreSelectionTest(unittest.TestCase):
    def test_vless_goes_to_xray(self):
        self.assertIs(select_core(parse_link(f"vless://{UUID}@h.io:443?security=tls#x")), Core.XRAY)

    def test_quic_protocols_go_to_singbox(self):
        for link in ("hysteria2://pw@h.io:443#h", f"tuic://{UUID}:pw@h.io:443#t",
                     "anytls://pw@h.io:443#a"):
            self.assertIs(select_core(parse_link(link)), Core.SINGBOX, link)

    def test_xhttp_goes_to_xray(self):
        ep = parse_link(f"vless://{UUID}@h.io:443?type=xhttp&security=tls#x")
        self.assertIs(select_core(ep), Core.XRAY)

    def test_allow_insecure_goes_to_singbox(self):
        """Xray 26 удалил allowInsecure: молча проверять сертификат нельзя."""
        ep = parse_link(f"vless://{UUID}@h.io:443?security=tls&allowInsecure=1#x")
        self.assertIs(select_core(ep), Core.SINGBOX)

    def test_h2_transport_goes_to_singbox(self):
        ep = parse_link(f"vless://{UUID}@h.io:443?type=http&security=tls#x")
        self.assertIs(select_core(ep), Core.SINGBOX)

    def test_conflicting_requirements_rejected(self):
        ep = parse_link(f"vless://{UUID}@h.io:443?type=xhttp&security=tls&allowInsecure=1#x")
        with self.assertRaises(UnsupportedConfigError):
            select_core(ep)

    def test_mkcp_obfuscation_rejected(self):
        ep = parse_link(f"vless://{UUID}@h.io:443?type=kcp&seed=abc&headerType=srtp#x")
        with self.assertRaises(UnsupportedConfigError):
            select_core(ep)


class XrayConfigTest(unittest.TestCase):
    def _stream(self, link):
        config = build_config(parse_link(link), Core.XRAY, 10800)
        return config["outbounds"][0]["streamSettings"]

    def test_inbound_is_loopback_only(self):
        config = build_config(parse_link(f"vless://{UUID}@h.io:443#x"), Core.XRAY, 10800)
        inbound = config["inbounds"][0]
        self.assertEqual(inbound["listen"], "127.0.0.1")
        self.assertEqual(inbound["port"], 10800)
        self.assertFalse(inbound["settings"]["udp"])

    def test_reality_fields(self):
        stream = self._stream(
            f"vless://{UUID}@h.io:443?security=reality&sni=a.com&fp=chrome"
            f"&pbk={PBK}&sid=00aa&spx=%2F&flow=xtls-rprx-vision#x"
        )
        reality = stream["realitySettings"]
        self.assertEqual(stream["security"], "reality")
        self.assertEqual(reality["publicKey"], PBK)
        self.assertEqual(reality["serverName"], "a.com")
        self.assertEqual(reality["shortId"], "00aa")
        self.assertEqual(reality["spiderX"], "/")

    def test_flow_present_only_when_declared(self):
        with_flow = build_config(
            parse_link(f"vless://{UUID}@h.io:443?security=tls&flow=xtls-rprx-vision#x"),
            Core.XRAY, 1080,
        )["outbounds"][0]["settings"]["vnext"][0]["users"][0]
        without = build_config(
            parse_link(f"vless://{UUID}@h.io:443?security=tls#x"), Core.XRAY, 1080,
        )["outbounds"][0]["settings"]["vnext"][0]["users"][0]

        self.assertEqual(with_flow["flow"], "xtls-rprx-vision")
        self.assertNotIn("flow", without)

    def test_ws_host_header(self):
        stream = self._stream(
            f"vless://{UUID}@h.io:443?type=ws&security=tls&path=%2Fp&host=cdn.io#x"
        )
        self.assertEqual(stream["wsSettings"]["path"], "/p")
        self.assertEqual(stream["wsSettings"]["headers"]["Host"], "cdn.io")

    def test_alpn_is_list(self):
        stream = self._stream(
            f"vless://{UUID}@h.io:443?security=tls&alpn=h2%2Chttp%2F1.1#x"
        )
        self.assertEqual(stream["tlsSettings"]["alpn"], ["h2", "http/1.1"])

    def test_grpc_multi_mode(self):
        stream = self._stream(
            f"vless://{UUID}@h.io:443?type=grpc&serviceName=svc&mode=multi&security=tls#x"
        )
        self.assertEqual(stream["grpcSettings"]["serviceName"], "svc")
        self.assertTrue(stream["grpcSettings"]["multiMode"])

    def test_trojan_uses_servers_section(self):
        config = build_config(parse_link("trojan://pw@h.io:443#x"), Core.XRAY, 1080)
        self.assertEqual(config["outbounds"][0]["settings"]["servers"][0]["password"], "pw")

    def test_routing_binds_inbound_to_outbound(self):
        config = build_config(parse_link(f"vless://{UUID}@h.io:443#x"), Core.XRAY, 1080)
        rule = config["routing"]["rules"][0]
        self.assertEqual(rule["inboundTag"], [config["inbounds"][0]["tag"]])
        self.assertEqual(rule["outboundTag"], config["outbounds"][0]["tag"])


class SingboxConfigTest(unittest.TestCase):
    def test_inbound_is_loopback_only(self):
        config = build_config(parse_link("hysteria2://pw@h.io:443#x"), Core.SINGBOX, 10800)
        inbound = config["inbounds"][0]
        self.assertEqual(inbound["listen"], "127.0.0.1")
        self.assertEqual(inbound["listen_port"], 10800)

    def test_hysteria2_obfs(self):
        config = build_config(
            parse_link("hysteria2://pw@h.io:443?obfs=salamander&obfs-password=zz#x"),
            Core.SINGBOX, 1080,
        )
        outbound = config["outbounds"][0]
        self.assertEqual(outbound["obfs"], {"type": "salamander", "password": "zz"})
        self.assertTrue(outbound["tls"]["enabled"])

    def test_reality_requires_utls(self):
        config = build_config(
            parse_link(f"vless://{UUID}@h.io:443?security=reality&pbk={PBK}&sni=a.com#x"),
            Core.SINGBOX, 1080,
        )
        tls = config["outbounds"][0]["tls"]
        self.assertTrue(tls["reality"]["enabled"])
        self.assertTrue(tls["utls"]["enabled"])

    def test_insecure_flag_passed(self):
        config = build_config(
            parse_link(f"vless://{UUID}@h.io:443?security=tls&allowInsecure=1#x"),
            Core.SINGBOX, 1080,
        )
        self.assertTrue(config["outbounds"][0]["tls"]["insecure"])

    def test_route_final_set(self):
        config = build_config(parse_link("tuic://%s:pw@h.io:443#x" % UUID), Core.SINGBOX, 1080)
        self.assertEqual(config["route"]["final"], config["outbounds"][0]["tag"])


class UserConfigSanitizationTest(unittest.TestCase):
    """Чужой JSON превращается в endpoint'ы; всё остальное отбрасывается."""

    def test_xray_config_inbounds_dropped(self):
        raw = json.dumps({
            "log": {"loglevel": "debug"},
            "api": {"tag": "api", "services": ["HandlerService"]},
            "inbounds": [{"listen": "0.0.0.0", "port": 1080, "protocol": "dokodemo-door"}],
            "outbounds": [{
                "tag": "proxy", "protocol": "vless",
                "settings": {"vnext": [{"address": "h.io", "port": 443, "users": [
                    {"id": UUID, "encryption": "none", "flow": "xtls-rprx-vision"}]}]},
                "streamSettings": {"network": "tcp", "security": "reality",
                                   "realitySettings": {"serverName": "a.com", "publicKey": PBK,
                                                       "shortId": "00aa"}},
            }, {"tag": "direct", "protocol": "freedom"}],
        })
        endpoints, dropped = parse_config(raw)

        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0].address, "h.io")
        self.assertEqual(endpoints[0].tls.reality_public_key, PBK)
        self.assertEqual(endpoints[0].flow, "xtls-rprx-vision")
        self.assertIn("inbounds", dropped)
        self.assertIn("api", dropped)

        built = build_config(endpoints[0], Core.XRAY, 10800)
        self.assertEqual(len(built["inbounds"]), 1)
        self.assertEqual(built["inbounds"][0]["listen"], "127.0.0.1")
        self.assertNotIn("api", built)

    def test_singbox_config_experimental_dropped(self):
        raw = json.dumps({
            "experimental": {"clash_api": {"external_controller": "0.0.0.0:9090"}},
            "inbounds": [{"type": "tun", "tag": "tun-in"}],
            "outbounds": [
                {"type": "hysteria2", "tag": "hy", "server": "h.io", "server_port": 443,
                 "password": "pw", "tls": {"enabled": True, "server_name": "a.com"}},
                {"type": "direct", "tag": "direct"},
            ],
        })
        endpoints, dropped = parse_config(raw)

        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0].protocol.value, "hysteria2")
        self.assertIn("experimental", dropped)
        self.assertIn("inbounds", dropped)

    def test_config_without_outbounds_rejected(self):
        with self.assertRaises(LinkParseError):
            parse_config(json.dumps({"inbounds": []}))

    def test_only_service_outbounds_rejected(self):
        with self.assertRaises(LinkParseError):
            parse_config(json.dumps({"outbounds": [{"protocol": "freedom", "tag": "d"}]}))

    def test_broken_json_rejected(self):
        with self.assertRaises(LinkParseError):
            parse_config("{not json")


class BalancerConfigTest(unittest.TestCase):
    """Конфиг с балансером: каждый его сервер — отдельная проверка.

    Балансировка выбирает сервер сама, поэтому через неё нельзя узнать, какой
    именно узел лёг. Секция routing отбрасывается, а outbounds превращаются в
    самостоятельные конфигурации — иначе смысл проверки теряется.
    """

    def _config(self, *, mode=False, extra_outbound=None):
        outbounds = [
            {
                "protocol": "trojan", "tag": f"proxy{'' if i == 0 else f'-{i + 1}'}",
                "settings": {"servers": [{"address": f"10.0.0.{i + 1}", "password": "pw", "port": 443}]},
                "streamSettings": {
                    "network": "grpc", "security": "tls",
                    "grpcSettings": {"authority": "auth.example", "mode": mode, "serviceName": "trgrpc"},
                    "tlsSettings": {"alpn": ["h2"], "fingerprint": "qq", "serverName": "sni.example"},
                },
            }
            for i in range(3)
        ]
        outbounds += [{"protocol": "freedom", "tag": "direct"}, {"protocol": "blackhole", "tag": "block"}]
        if extra_outbound:
            outbounds.insert(0, extra_outbound)

        return json.dumps({
            "burstObservatory": {"subjectSelector": ["proxy"]},
            "dns": {"servers": ["1.1.1.1"]},
            "inbounds": [{"listen": "127.0.0.1", "port": 10808, "protocol": "socks", "tag": "socks"}],
            "outbounds": outbounds,
            "routing": {
                "balancers": [{"tag": "Super_Balancer", "selector": ["proxy"],
                               "strategy": {"type": "leastLoad"}}],
                "rules": [{"balancerTag": "Super_Balancer", "port": "53", "type": "field"}],
            },
        })

    def test_every_balancer_member_becomes_separate_config(self):
        endpoints, dropped = parse_config(self._config())

        self.assertEqual(len(endpoints), 3)
        self.assertEqual([e.remark for e in endpoints], ["proxy", "proxy-2", "proxy-3"])
        self.assertEqual([e.address for e in endpoints], ["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        self.assertIn("routing", dropped)
        self.assertIn("burstObservatory", dropped)

    def test_service_outbounds_excluded(self):
        endpoints, _ = parse_config(self._config())
        self.assertNotIn("direct", [e.remark for e in endpoints])
        self.assertNotIn("block", [e.remark for e in endpoints])

    def test_grpc_details_preserved(self):
        endpoints, _ = parse_config(self._config())
        first = endpoints[0]

        self.assertEqual(first.transport.kind, Transport.GRPC)
        self.assertEqual(first.transport.service_name, "trgrpc")
        self.assertEqual(first.transport.authority, "auth.example")
        self.assertEqual(first.tls.sni, "sni.example")
        self.assertEqual(first.tls.fingerprint, "qq")
        self.assertEqual(first.tls.alpn, ("h2",))

    def test_boolean_mode_false_is_not_a_string(self):
        """«mode»: false пишут вместо multiMode — строка «False» была бы мусором."""
        endpoints, _ = parse_config(self._config(mode=False))
        self.assertIsNone(endpoints[0].transport.mode)

        stream = build_config(endpoints[0], Core.XRAY, 1080)["outbounds"][0]["streamSettings"]
        self.assertFalse(stream["grpcSettings"]["multiMode"])
        self.assertEqual(stream["grpcSettings"]["authority"], "auth.example")

    def test_boolean_mode_true_means_multi(self):
        endpoints, _ = parse_config(self._config(mode=True))
        self.assertEqual(endpoints[0].transport.mode, "multi")

        stream = build_config(endpoints[0], Core.XRAY, 1080)["outbounds"][0]["streamSettings"]
        self.assertTrue(stream["grpcSettings"]["multiMode"])

    def test_legacy_multimode_flag_honoured(self):
        raw = json.dumps({"outbounds": [{
            "protocol": "trojan", "tag": "p",
            "settings": {"servers": [{"address": "h.io", "password": "pw", "port": 443}]},
            "streamSettings": {"network": "grpc", "security": "tls",
                               "grpcSettings": {"serviceName": "s", "multiMode": True}},
        }]})
        endpoints, _ = parse_config(raw)
        self.assertEqual(endpoints[0].transport.mode, "multi")

    def test_mixed_protocols_get_their_own_cores(self):
        extra = {"type": "hysteria2", "tag": "hy", "server": "h.io", "server_port": 443,
                 "password": "pw", "tls": {"enabled": True, "server_name": "a.com"}}
        endpoints, _ = parse_config(self._config(extra_outbound=extra))

        self.assertEqual(len(endpoints), 4)
        self.assertIs(select_core(endpoints[0]), Core.SINGBOX)
        self.assertIs(select_core(endpoints[1]), Core.XRAY)


class MatrixTest(unittest.TestCase):
    def _endpoint(self, link=None):
        return parse_link(link or f"vless://{UUID}@h.io:443?security=tls&sni=orig.com#x")

    def test_without_sni_one_cell_per_endpoint(self):
        cells = build_matrix([self._endpoint(), self._endpoint()])
        self.assertEqual(len(cells), 2)
        self.assertIsNone(cells[0].sni_label)

    def test_matrix_is_product(self):
        cells = build_matrix([self._endpoint()], ["a.com", "b.com", "c.com"])
        self.assertEqual(len(cells), 3)
        self.assertEqual([cell.endpoint.tls.sni for cell in cells], ["a.com", "b.com", "c.com"])

    def test_duplicates_and_case_normalized(self):
        cells = build_matrix([self._endpoint()], ["A.com", "a.com", " a.com ", "b.com"])
        self.assertEqual([cell.sni_label for cell in cells], ["a.com", "b.com"])

    def test_host_follows_sni_for_ws(self):
        endpoint = self._endpoint(
            f"vless://{UUID}@h.io:443?type=ws&security=tls&host=cdn.io&path=%2Fp#x"
        )
        cells = build_matrix([endpoint], ["new.com"], sync_transport_host=True)
        self.assertEqual(cells[0].endpoint.transport.host, "new.com")

    def test_host_kept_when_sync_disabled(self):
        endpoint = self._endpoint(
            f"vless://{UUID}@h.io:443?type=ws&security=tls&host=cdn.io&path=%2Fp#x"
        )
        cells = build_matrix([endpoint], ["new.com"], sync_transport_host=False)
        self.assertEqual(cells[0].endpoint.transport.host, "cdn.io")

    def test_tcp_host_untouched(self):
        endpoint = self._endpoint()
        cells = build_matrix([endpoint], ["new.com"], sync_transport_host=True)
        self.assertEqual(cells[0].endpoint.transport.kind, Transport.TCP)
        self.assertEqual(cells[0].endpoint.tls.sni, "new.com")

    def test_original_endpoint_not_mutated(self):
        endpoint = self._endpoint()
        build_matrix([endpoint], ["a.com", "b.com"])
        self.assertEqual(endpoint.tls.sni, "orig.com")

    def test_cell_limit_enforced(self):
        endpoints = [self._endpoint() for _ in range(20)]
        with self.assertRaises(LimitExceededError):
            build_matrix(endpoints, [f"s{i}.com" for i in range(30)])

    def test_sni_limit_enforced(self):
        with self.assertRaises(LimitExceededError):
            build_matrix([self._endpoint()], [f"s{i}.com" for i in range(60)])

    def test_empty_endpoints_rejected(self):
        with self.assertRaises(LimitExceededError):
            build_matrix([])


class LocationMatrixTest(unittest.TestCase):
    """Одна конфигурация из нескольких точек — отдельная проверка на каждую.

    Ключ, живой из Германии, может быть мёртв из России: усреднять такие
    результаты нельзя, у каждой локации своя строка.
    """

    def _endpoint(self):
        return parse_link(f"vless://{UUID}@h.io:443?security=tls&sni=a.com#node")

    def test_locations_multiply_cells(self):
        cells = build_matrix(
            [self._endpoint()],
            ["a.com", "b.com"],
            locations=[("panel", ""), ("node:1", "Берлин"), ("node:2", "Москва")],
        )
        self.assertEqual(len(cells), 6)
        self.assertEqual(
            {(cell.sni_label, cell.location) for cell in cells},
            {(sni, loc) for sni in ("a.com", "b.com")
             for loc in ("panel", "node:1", "node:2")},
        )

    def test_location_name_carried(self):
        cells = build_matrix([self._endpoint()], locations=[("node:7", "Амстердам")])
        self.assertEqual(cells[0].location, "node:7")
        self.assertEqual(cells[0].location_name, "Амстердам")

    def test_default_location_is_panel(self):
        cells = build_matrix([self._endpoint()])
        self.assertEqual(cells[0].location, "panel")

    def test_indexes_stay_unique(self):
        cells = build_matrix(
            [self._endpoint(), self._endpoint()],
            ["a.com"],
            locations=[("panel", ""), ("node:1", "N")],
        )
        self.assertEqual(len({cell.index for cell in cells}), len(cells))

    def test_cell_limit_counts_locations(self):
        endpoints = [self._endpoint() for _ in range(10)]
        with self.assertRaises(LimitExceededError):
            build_matrix(endpoints, [f"s{i}.com" for i in range(11)],
                         locations=[("panel", ""), ("node:1", "N")])

    def test_too_many_locations_rejected(self):
        with self.assertRaises(LimitExceededError):
            build_matrix([self._endpoint()],
                         locations=[(f"node:{i}", str(i)) for i in range(25)])


class SubscriptionConfigListTest(unittest.TestCase):
    """Подписка отдаёт массив целых конфигов, а не список outbounds.

    Так устроены реальные подписки: каждый элемент — самостоятельный профиль
    со своим именем в `remarks`, своими outbounds и балансером. Массив
    outbounds на верхнем уровне тоже встречается, поэтому вид определяется по
    содержимому, а не по типу корня.
    """

    def _profile(self, name, hosts):
        return {
            "remarks": name,
            "dns": {"servers": ["1.1.1.1"]},
            "inbounds": [{"listen": "127.0.0.1", "port": 10808, "protocol": "socks"}],
            "routing": {"balancers": [{"tag": "B", "selector": ["proxy"]}], "rules": []},
            "outbounds": [
                {
                    "protocol": "vless", "tag": f"proxy{'' if i == 0 else f'-{i + 1}'}",
                    "settings": {"vnext": [{"address": host, "port": 2053, "users": [
                        {"id": UUID, "encryption": "none"}]}]},
                    "streamSettings": {"network": "grpc", "security": "reality",
                                       "grpcSettings": {"serviceName": "s"},
                                       "realitySettings": {"serverName": "eh.vk.com",
                                                           "publicKey": PBK}},
                }
                for i, host in enumerate(hosts)
            ] + [{"protocol": "freedom", "tag": "direct"}],
        }

    def test_array_of_configs_parsed(self):
        raw = json.dumps([
            self._profile("Европа", ["1.1.1.1", "2.2.2.2"]),
            self._profile("Сингапур", ["3.3.3.3"]),
        ])
        endpoints, dropped = parse_config(raw)

        self.assertEqual(len(endpoints), 3)
        self.assertEqual(
            [e.address for e in endpoints], ["1.1.1.1", "2.2.2.2", "3.3.3.3"]
        )
        self.assertIn("routing", dropped)
        self.assertIn("inbounds", dropped)

    def test_profile_name_prefixes_tag(self):
        raw = json.dumps([self._profile("Германия", ["1.1.1.1", "2.2.2.2"])])
        endpoints, _ = parse_config(raw)

        self.assertEqual(endpoints[0].remark, "Германия · proxy")
        self.assertEqual(endpoints[1].remark, "Германия · proxy-2")

    def test_profile_without_remarks_keeps_tag(self):
        profile = self._profile("", ["1.1.1.1"])
        profile.pop("remarks")
        endpoints, _ = parse_config(json.dumps([profile]))
        self.assertEqual(endpoints[0].remark, "proxy")

    def test_plain_outbound_array_still_works(self):
        raw = json.dumps([{
            "protocol": "trojan", "tag": "single",
            "settings": {"servers": [{"address": "h.io", "password": "pw", "port": 443}]},
            "streamSettings": {"network": "tcp", "security": "tls"},
        }])
        endpoints, _ = parse_config(raw)

        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0].remark, "single")

    def test_reality_details_survive(self):
        endpoints, _ = parse_config(json.dumps([self._profile("EU", ["1.1.1.1"])]))
        endpoint = endpoints[0]

        self.assertEqual(endpoint.tls.security.value, "reality")
        self.assertEqual(endpoint.tls.sni, "eh.vk.com")
        self.assertEqual(endpoint.tls.reality_public_key, PBK)
        self.assertEqual(endpoint.transport.kind, Transport.GRPC)


if __name__ == "__main__":
    unittest.main()

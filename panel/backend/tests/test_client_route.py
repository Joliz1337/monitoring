"""Маршрут клиента на карте: из трассы с ноды — остановки в городах и участки.

Здесь решается, через какие города линия пройдёт и какой участок покраснеет.
Ошибка видна сразу: роутер «в Москве» в 2 мс от Амстердама, красный участок
из-за роутера, который просто медленно отвечает на ICMP, или линия, которая
обрывается, не дойдя до клиента.
"""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.client_route import (  # noqa: E402
    HiddenReason, LegStatus, RoutePoint, StopKind, assemble_route, find_node_server,
)
from app.services.geoip import GeoRecord  # noqa: E402

AMSTERDAM = RoutePoint(lat=52.37, lon=4.90, place="Amsterdam", country_code="NL")
CLIENT_IP = "85.140.1.1"
PLACES = {
    "Amsterdam": ("NL", 52.37, 4.90),
    "Berlin": ("DE", 52.52, 13.40),
    "Warsaw": ("PL", 52.23, 21.01),
    "Moscow": ("RU", 55.76, 37.62),
    "Khimki": ("RU", 55.89, 37.43),
    "Krasnodar": ("RU", 45.04, 38.98),
}


def record(place: str) -> GeoRecord:
    country, lat, lon = PLACES[place]
    return GeoRecord(country_code=country, region=None, place=place, lat=lat, lon=lon, asn=None, as_org=None)


def point(place: str) -> RoutePoint:
    country, lat, lon = PLACES[place]
    return RoutePoint(lat=lat, lon=lon, place=place, country_code=country)


def hop(number: int, host, avg=None, best=None, received=15):
    answered = host is not None and avg is not None
    return {
        "hop": number, "host": host, "asn": None, "as_name": None,
        "sent": 15, "received": received if answered else 0,
        "loss_pct": 0.0 if answered else 100.0,
        "avg_ms": avg, "best_ms": best if best is not None else avg, "worst_ms": avg,
    }


def trace(*hops):
    return {"ip": CLIENT_IP, "state": "done", "hops": list(hops)}


def build(hops, located: dict[str, str], client=point("Krasnodar")):
    return assemble_route(trace(*hops), AMSTERDAM, client, lambda ip: record(located[ip]) if ip in located else None)


class StopsTests(unittest.TestCase):
    def test_stops_run_from_client_to_node(self):
        route = build(
            [hop(1, "10.0.0.1", 0.5), hop(2, "80.0.0.2", 1.0), hop(3, "80.0.0.3", 8.0),
             hop(4, "80.0.0.4", 15.0), hop(5, "80.0.0.5", 30.0)],
            {"80.0.0.2": "Amsterdam", "80.0.0.3": "Berlin", "80.0.0.4": "Warsaw", "80.0.0.5": "Moscow"},
        )
        self.assertEqual([s["place"] for s in route["stops"]], ["Krasnodar", "Moscow", "Warsaw", "Berlin", "Amsterdam"])
        self.assertEqual(route["stops"][0]["kind"], StopKind.CLIENT)
        self.assertEqual(route["stops"][-1]["kind"], StopKind.NODE)

    def test_location_impossible_by_latency_is_hidden(self):
        # 2 мс туда-обратно — не дальше ~200 км: роутер «в Москве» на самом деле рядом с нодой
        route = build(
            [hop(1, "80.0.0.1", 2.0), hop(2, "80.0.0.2", 8.0)],
            {"80.0.0.1": "Moscow", "80.0.0.2": "Berlin"},
        )
        self.assertNotIn("Moscow", [s["place"] for s in route["stops"]][1:])
        hidden = [h for leg in route["legs"] for h in leg["hidden"]]
        self.assertEqual([(h["host"], h["reason"]) for h in hidden], [("80.0.0.1", HiddenReason.IMPOSSIBLE)])

    def test_hops_in_one_city_share_a_stop(self):
        route = build(
            [hop(1, "80.0.0.3", 8.0), hop(2, "80.0.0.5", 30.0), hop(3, "80.0.0.6", 31.0)],
            {"80.0.0.3": "Berlin", "80.0.0.5": "Moscow", "80.0.0.6": "Khimki"},
        )
        moscow = next(s for s in route["stops"] if s["place"] == "Moscow")
        self.assertEqual([h["host"] for h in moscow["hops"]], ["80.0.0.5", "80.0.0.6"])

    def test_silent_and_private_hops_are_reported_as_hidden(self):
        route = build(
            [hop(1, "10.0.0.1", 0.5), hop(2, None), hop(3, "80.0.0.3", 8.0)],
            {"80.0.0.3": "Berlin"},
        )
        reasons = [h["reason"] for leg in route["legs"] for h in leg["hidden"]]
        self.assertEqual(reasons, [HiddenReason.PRIVATE, HiddenReason.SILENT])

    def test_client_in_city_of_last_router_becomes_that_stop(self):
        route = build(
            [hop(1, "80.0.0.3", 8.0), hop(2, "80.0.0.9", 50.0)],
            {"80.0.0.3": "Berlin", "80.0.0.9": "Krasnodar"},
        )
        self.assertEqual([s["place"] for s in route["stops"]], ["Krasnodar", "Berlin", "Amsterdam"])
        self.assertEqual(route["stops"][0]["kind"], StopKind.CLIENT)
        self.assertEqual([h["host"] for h in route["stops"][0]["hops"]], ["80.0.0.9"])

    def test_answered_client_merged_with_router_keeps_its_own_latency(self):
        route = build(
            [hop(1, "80.0.0.3", 8.0), hop(2, "80.0.0.9", 50.0), hop(3, CLIENT_IP, 58.0)],
            {"80.0.0.3": "Berlin", "80.0.0.9": "Krasnodar"},
        )
        self.assertEqual(route["stops"][0]["kind"], StopKind.CLIENT)
        self.assertAlmostEqual(route["stops"][0]["rtt_ms"], 58.0)
        self.assertAlmostEqual(route["legs"][0]["added_ms"], 50.0)

    def test_unknown_client_location_ends_route_at_last_router(self):
        route = build([hop(1, "80.0.0.3", 8.0), hop(2, "80.0.0.5", 30.0), hop(3, None)],
                      {"80.0.0.3": "Berlin", "80.0.0.5": "Moscow"}, client=None)
        self.assertFalse(route["client_located"])
        self.assertEqual(route["stops"][0]["place"], "Moscow")
        self.assertEqual([h["reason"] for h in route["hidden_tail"]], [HiddenReason.SILENT])


class LegsTests(unittest.TestCase):
    def test_router_slow_to_answer_does_not_redden_leg(self):
        # Роутер отвечает на ICMP медленно (60 мс), но следующие узлы — 15 мс:
        # настоящей задержки на этом участке нет
        route = build(
            [hop(1, "80.0.0.3", 60.0), hop(2, "80.0.0.4", 15.0)],
            {"80.0.0.3": "Berlin", "80.0.0.4": "Warsaw"},
        )
        statuses = [leg["status"] for leg in route["legs"] if leg["status"] is not None]
        self.assertNotIn(LegStatus.BAD, statuses)

    def test_latency_jump_far_beyond_distance_marks_leg_bad(self):
        route = build(
            [hop(1, "80.0.0.3", 8.0), hop(2, "80.0.0.5", 70.0)],
            {"80.0.0.3": "Berlin", "80.0.0.5": "Moscow"},
        )
        moscow_berlin = next(leg for leg in route["legs"] if leg["added_ms"] and leg["added_ms"] > 50)
        self.assertEqual(moscow_berlin["status"], LegStatus.BAD)

    def test_normal_leg_is_ok(self):
        route = build([hop(1, "80.0.0.3", 8.0)], {"80.0.0.3": "Berlin"})
        node_leg = route["legs"][-1]
        self.assertEqual(node_leg["status"], LegStatus.OK)
        self.assertAlmostEqual(node_leg["added_ms"], 8.0)

    def test_usual_latency_over_long_distance_is_ok(self):
        # Амстердам → Москва ~2150 км по прямой, обычные ~30 мс: кабель длиннее прямой,
        # участок не должен желтеть только из-за этого
        route = build([hop(1, "80.0.0.5", 30.0)], {"80.0.0.5": "Moscow"})
        self.assertEqual(route["legs"][-1]["status"], LegStatus.OK)

    def test_unanswered_client_gets_dashed_last_mile_without_latency(self):
        route = build([hop(1, "80.0.0.3", 8.0), hop(2, None)], {"80.0.0.3": "Berlin"})
        last_mile = route["legs"][0]
        self.assertTrue(last_mile["last_mile"])
        self.assertIsNone(last_mile["added_ms"])
        self.assertIsNone(last_mile["status"])
        self.assertFalse(route["client_reached"])

    def test_answered_client_closes_the_route(self):
        route = build([hop(1, "80.0.0.3", 8.0), hop(2, CLIENT_IP, 45.0)], {"80.0.0.3": "Berlin"})
        self.assertTrue(route["client_reached"])
        self.assertFalse(route["legs"][0]["last_mile"])
        self.assertAlmostEqual(route["stops"][0]["rtt_ms"], 45.0)
        self.assertAlmostEqual(route["totals"]["rtt_ms"], 45.0)

    def test_leg_lines_run_from_client_side_to_node_side(self):
        route = build([hop(1, "80.0.0.3", 8.0)], {"80.0.0.3": "Berlin"})
        first, last = route["legs"][0], route["legs"][-1]
        self.assertAlmostEqual(first["coords"][0][1], PLACES["Krasnodar"][1], places=2)
        self.assertAlmostEqual(last["coords"][-1][0], AMSTERDAM.lon, places=2)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, node, servers):
        self.node, self.servers = node, servers

    async def get(self, _model, key):
        return self.node if key == self.node.uuid else None

    async def execute(self, _statement):
        return _Rows(self.servers)


def server(server_id: int, url: str, interface_ips=()):
    addresses = [{"type": "ipv4", "address": ip} for ip in interface_ips]
    metrics = json.dumps({"network": {"interfaces": [{"name": "eth0", "addresses": addresses}]}})
    return SimpleNamespace(id=server_id, url=url, last_metrics=metrics)


class FindNodeServerTests(unittest.IsolatedAsyncioTestCase):
    """Трасса идёт с того сервера панели, на котором стоит нода Remnawave клиента."""

    async def find(self, address, servers, dns=None):
        dns = dns or {}

        async def resolve(host):
            return host if host and host[0].isdigit() else dns.get(host)

        node = SimpleNamespace(uuid="n1", address=address)
        with patch("app.services.client_route.host_to_ip", new=resolve):
            found = await find_node_server(_Session(node, servers), "n1")
        return found.id if found else None

    async def test_address_matches_server_url(self):
        servers = [server(1, "https://5.5.5.5:9100"), server(2, "https://6.6.6.6:9100")]
        self.assertEqual(await self.find("6.6.6.6", servers), 2)

    async def test_address_matches_extra_ip_from_metrics(self):
        # Remnawave знает ноду по второму адресу, панель ходит к ней по первому
        servers = [server(1, "https://5.5.5.5:9100"), server(2, "https://6.6.6.6:9100", ["6.6.6.6", "7.7.7.7"])]
        self.assertEqual(await self.find("7.7.7.7", servers), 2)

    async def test_domains_resolve_to_same_address(self):
        servers = [server(1, "https://panel-node.example.com:9100")]
        dns = {"node.example.com": "8.8.4.4", "panel-node.example.com": "8.8.4.4"}
        self.assertEqual(await self.find("node.example.com", servers, dns), 1)

    async def test_node_not_managed_by_panel(self):
        self.assertIsNone(await self.find("9.9.9.9", [server(1, "https://5.5.5.5:9100")]))


if __name__ == "__main__":
    unittest.main()

"""Карта клиентов Remnawave: склейка ответов нод и подсчёт мест.

Склейка решает, через какие ноды виден IP и что попадёт в историю, подсчёт —
сколько клиентов в каком городе. Ошибка здесь видна прямо на карте: потерянная
нода, клиент не в том городе или упавший на кривой записи цикл сбора.
"""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # локальный прогон без установленного рантайма панели
    from app.services.geoip import GeoRecord  # noqa: E402
    from app.services.remnawave_geo_ingest import (  # noqa: E402
        PlaceCount, merge_node_results, place_counts, plan_geo_resolve,
    )
    from app.services.remnawave_geo_queries import (  # noqa: E402
        CycleCache, PrevPlace, country_rollup, merge_prev, parse_bbox,
    )
except ImportError as e:  # pragma: no cover
    raise unittest.SkipTest(f"remnawave geo requires the panel runtime: {e}")


NOW = datetime(2026, 11, 9, 12, 0, 0)
FRESH_AFTER = NOW - timedelta(minutes=5)


def _seen(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat() + "Z"


def _geo(place: str, lat: float | None, lon: float | None, cc: str = "RU") -> GeoRecord:
    return GeoRecord(cc, None, place, lat, lon, 1, "ISP")


class MergeNodeResultsTest(unittest.TestCase):
    def test_merge_keeps_nodes_per_ip(self):
        results = [
            ("a", [{"userId": 7, "ips": [{"ip": "1.2.3.4", "lastSeen": _seen(3)}]}]),
            ("b", [{"userId": 7, "ips": [{"ip": "1.2.3.4", "lastSeen": _seen(1)}]}]),
        ]
        snapshot, history = merge_node_results(results, {7}, FRESH_AFTER)

        entry = snapshot[(7, "1.2.3.4")]
        self.assertEqual(entry.node_uuids, {"a", "b"})
        self.assertEqual(entry.last_seen, NOW - timedelta(minutes=1))
        self.assertEqual(set(history), {(7, "1.2.3.4", "a"), (7, "1.2.3.4", "b")})

    def test_merge_stale_goes_only_to_history(self):
        results = [("a", [{"userId": 7, "ips": [{"ip": "1.2.3.4", "lastSeen": _seen(30)}]}])]
        snapshot, history = merge_node_results(results, {7}, FRESH_AFTER)

        self.assertEqual(snapshot, {})
        self.assertEqual(history[(7, "1.2.3.4", "a")], NOW - timedelta(minutes=30))

    def test_merge_skips_bad_entries(self):
        results = [("a", [
            {"userId": "12", "ips": [{"ip": "5.5.5.5", "lastSeen": _seen(1)}]},
            {"userId": "x", "ips": [{"ip": "6.6.6.6", "lastSeen": _seen(1)}]},
            {"userId": None, "ips": [{"ip": "6.6.6.7", "lastSeen": _seen(1)}]},
            {"userId": 99, "ips": [{"ip": "6.6.6.8", "lastSeen": _seen(1)}]},
            {"userId": 12, "ips": [{"ip": "", "lastSeen": _seen(1)}]},
            {"userId": 12, "ips": [{"ip": "6.6.6.9", "lastSeen": "garbage"}]},
            {"userId": 12},
        ])]
        snapshot, history = merge_node_results(results, {12}, FRESH_AFTER)

        self.assertEqual(list(snapshot), [(12, "5.5.5.5")])
        self.assertEqual(list(history), [(12, "5.5.5.5", "a")])


class PlaceCountsTest(unittest.TestCase):
    def test_place_counts_average_coords_and_counts(self):
        rows = [
            ("1.1.1.1", 1, _geo("Moscow", 55.70, 37.60)),
            ("2.2.2.2", 2, _geo("Moscow", 55.80, 37.70)),
            ("2.2.2.2", 3, _geo("Moscow", 55.80, 37.70)),
            ("3.3.3.3", 4, _geo("Nowhere", None, None)),
        ]
        places = place_counts(rows)

        self.assertEqual(list(places), [("RU", "Moscow")])
        moscow = places[("RU", "Moscow")]
        self.assertEqual(moscow.ips, 2)
        self.assertEqual(moscow.users, 3)
        self.assertAlmostEqual(moscow.lat, 55.75)
        self.assertAlmostEqual(moscow.lon, 37.65)


class PlanGeoResolveTest(unittest.TestCase):
    def test_online_ips_go_first(self):
        plan = plan_geo_resolve(
            priority=["9.9.9.9", "1.1.1.1"], others=["2.2.2.2", "3.3.3.3"], fresh=set(), relays={}, limit=3,
        )
        self.assertEqual(plan, ["9.9.9.9", "1.1.1.1", "2.2.2.2"])

    def test_skips_fresh_relays_and_duplicates(self):
        plan = plan_geo_resolve(
            priority=["1.1.1.1", "5.5.5.5"], others=["1.1.1.1", "2.2.2.2", "3.3.3.3"],
            fresh={"2.2.2.2"}, relays={"5.5.5.5": "relay-msk"}, limit=10,
        )
        self.assertEqual(plan, ["1.1.1.1", "3.3.3.3"])


class TableCacheTest(unittest.TestCase):
    def test_cache_drops_entries_of_previous_cycles(self):
        cache = CycleCache(max_entries=10)
        cache.put(1, ("map", None), {"a": 1})
        self.assertEqual(cache.get(1, ("map", None)), {"a": 1})
        self.assertIsNone(cache.get(2, ("map", None)))
        cache.put(2, ("map", "n1"), {"b": 2})
        self.assertEqual(len(cache), 1)

    def test_cache_is_bounded(self):
        cache = CycleCache(max_entries=3)
        for i in range(5):
            cache.put(7, ("ips", i), {"i": i})
        self.assertEqual(len(cache), 3)
        self.assertIsNone(cache.get(7, ("ips", 0)))
        self.assertEqual(cache.get(7, ("ips", 4)), {"i": 4})


class ParseBboxTest(unittest.TestCase):
    def test_parse_bbox_plain(self):
        self.assertEqual(parse_bbox("30,40,50,60"), ((30.0, 40.0, 50.0, 60.0),))

    def test_parse_bbox_antimeridian(self):
        self.assertEqual(
            parse_bbox("170,-10,190,10"),
            ((170.0, -10.0, 180.0, 10.0), (-180.0, -10.0, -170.0, 10.0)),
        )

    def test_parse_bbox_shifted_world_copy(self):
        self.assertEqual(parse_bbox("390,40,410,60"), ((30.0, 40.0, 50.0, 60.0),))

    def test_parse_bbox_world(self):
        self.assertEqual(parse_bbox("-400,-80,400,80"), ((-180.0, -80.0, 180.0, 80.0),))

    def test_parse_bbox_garbage(self):
        for raw in (None, "", "1,2,3", "a,b,c,d", "nan,1,2,3", "0,50,10,40"):
            self.assertIsNone(parse_bbox(raw), raw)


class MergePrevTest(unittest.TestCase):
    def test_merge_prev_marks_known_places(self):
        now = {("RU", "Moscow"): PlaceCount(55.7, 37.6, 40, 30), ("RU", "Tver"): PlaceCount(56.8, 35.9, 2, 2)}
        prev = {("RU", "Moscow"): PrevPlace(55.7, 37.6, 99.6)}
        rows = {r["key"]: r for r in merge_prev(now, prev)}

        self.assertEqual(rows["RU|Moscow"]["prev"], 100)
        self.assertEqual(rows["RU|Tver"]["prev"], 0)
        self.assertEqual(rows["RU|Moscow"]["ips"], 40)

    def test_merge_prev_adds_vanished_place(self):
        prev = {("RU", "Krasnodar"): PrevPlace(45.0, 39.0, 12.0)}
        rows = merge_prev({}, prev)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "RU|Krasnodar")
        self.assertEqual(rows[0]["ips"], 0)
        self.assertEqual(rows[0]["prev"], 12)

    def test_merge_prev_keeps_small_vanished_for_sums(self):
        # Тридцать городков по 3 клиента, пропавших разом, — это авария региона:
        # их вчерашние клиенты должны попасть в суммы кластера и страны
        rows = merge_prev({}, {("RU", "Sochi"): PrevPlace(43.6, 39.7, 3.0)})
        self.assertEqual([(r["key"], r["ips"], r["prev"]) for r in rows], [("RU|Sochi", 0, 3)])

    def test_merge_prev_drops_vanished_rounding_to_zero(self):
        self.assertEqual(merge_prev({}, {("RU", "Sochi"): PrevPlace(43.6, 39.7, 0.3)}), [])

    def test_merge_prev_without_comparison(self):
        rows = merge_prev({("RU", "Moscow"): PlaceCount(55.7, 37.6, 40, 30)}, None)
        self.assertIsNone(rows[0]["prev"])


class CountryRollupTest(unittest.TestCase):
    def test_country_rollup_weighted(self):
        locations = [
            {"country_code": "RU", "lat": 55.0, "lon": 37.0, "ips": 3, "prev": None},
            {"country_code": "RU", "lat": 57.0, "lon": 39.0, "ips": 1, "prev": None},
            {"country_code": "DE", "lat": 50.0, "lon": 8.0, "ips": 2, "prev": 4},
        ]
        countries = {c["country_code"]: c for c in country_rollup(locations)}

        self.assertEqual(countries["RU"]["ips"], 4)
        self.assertAlmostEqual(countries["RU"]["lat"], 55.5)
        self.assertAlmostEqual(countries["RU"]["lon"], 37.5)
        self.assertIsNone(countries["RU"]["prev"])
        self.assertEqual(countries["DE"]["prev"], 4)

    def test_country_rollup_counts_small_vanished_places(self):
        locations = [
            {"country_code": "RU", "lat": 55.0, "lon": 37.0, "ips": 100, "prev": 100},
            *({"country_code": "RU", "lat": 45.0, "lon": 39.0, "ips": 0, "prev": 3} for _ in range(30)),
        ]
        ru = country_rollup(locations)[0]
        self.assertEqual(ru["ips"], 100)
        self.assertEqual(ru["prev"], 190)

    def test_country_rollup_vanished_country_keeps_position(self):
        countries = country_rollup([{"country_code": "KZ", "lat": 43.2, "lon": 76.9, "ips": 0, "prev": 8}])
        self.assertAlmostEqual(countries[0]["lat"], 43.2)
        self.assertEqual(countries[0]["ips"], 0)
        self.assertEqual(countries[0]["prev"], 8)


if __name__ == "__main__":
    unittest.main()

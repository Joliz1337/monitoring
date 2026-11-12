"""Карта клиентов Remnawave: склейка ответов нод, подсчёт мест и запись кэша нод.

Склейка решает, через какие ноды виден IP и что попадёт в историю, подсчёт —
сколько клиентов в каком городе. Ошибка здесь видна прямо на карте: потерянная
нода, клиент не в том городе или упавший на кривой записи цикл сбора.
"""

import asyncio
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # локальный прогон без установленного рантайма панели
    from sqlalchemy.dialects import postgresql  # noqa: E402

    from app.services import remnawave_geo_ingest  # noqa: E402
    from app.services.geoip import GeoRecord  # noqa: E402
    from app.services.remnawave_geo_ingest import (  # noqa: E402
        UPSERT_BATCH, NodeAnswer, PlaceCount, merge_node_results, place_counts, plan_geo_resolve,
    )
    from app.services.remnawave_geo_queries import (  # noqa: E402
        CycleCache, PrevPlace, country_rollup, merge_prev, parse_bbox,
    )
except ImportError as e:  # pragma: no cover
    raise unittest.SkipTest(f"remnawave geo requires the panel runtime: {e}")


NOW = datetime(2026, 11, 9, 12, 0, 0)
FRESH_WINDOW = timedelta(minutes=5)
# Цикл сбора дольше интервала: последняя нода парка ответила намного позже первой
LATE = NOW + 3 * FRESH_WINDOW
ASYNCPG_MAX_QUERY_ARGS = 32767
COMMIT = "commit"


def _seen(minutes_ago: int, before: datetime = NOW) -> str:
    return (before - timedelta(minutes=minutes_ago)).isoformat() + "Z"


def _answer(node_uuid: str, entries: list[dict], received_at: datetime = NOW) -> NodeAnswer:
    return NodeAnswer(node_uuid, entries, received_at)


def _geo(place: str, lat: float | None, lon: float | None, cc: str = "RU") -> GeoRecord:
    return GeoRecord(cc, None, place, lat, lon, 1, "ISP")


class MergeNodeResultsTest(unittest.TestCase):
    def test_merge_keeps_nodes_per_ip(self):
        answers = [
            _answer("a", [{"userId": 7, "ips": [{"ip": "1.2.3.4", "lastSeen": _seen(3)}]}]),
            _answer("b", [{"userId": 7, "ips": [{"ip": "1.2.3.4", "lastSeen": _seen(1)}]}]),
        ]
        snapshot, history = merge_node_results(answers, {7}, FRESH_WINDOW)

        entry = snapshot[(7, "1.2.3.4")]
        self.assertEqual(entry.node_uuids, {"a", "b"})
        self.assertEqual(entry.last_seen, NOW - timedelta(minutes=1))
        self.assertEqual(set(history), {(7, "1.2.3.4", "a"), (7, "1.2.3.4", "b")})

    def test_merge_stale_goes_only_to_history(self):
        answers = [_answer("a", [{"userId": 7, "ips": [{"ip": "1.2.3.4", "lastSeen": _seen(30)}]}])]
        snapshot, history = merge_node_results(answers, {7}, FRESH_WINDOW)

        self.assertEqual(snapshot, {})
        self.assertEqual(history[(7, "1.2.3.4", "a")], NOW - timedelta(minutes=30))

    def test_merge_skips_bad_entries(self):
        answers = [_answer("a", [
            {"userId": "12", "ips": [{"ip": "5.5.5.5", "lastSeen": _seen(1)}]},
            {"userId": "x", "ips": [{"ip": "6.6.6.6", "lastSeen": _seen(1)}]},
            {"userId": None, "ips": [{"ip": "6.6.6.7", "lastSeen": _seen(1)}]},
            {"userId": 99, "ips": [{"ip": "6.6.6.8", "lastSeen": _seen(1)}]},
            {"userId": 12, "ips": [{"ip": "", "lastSeen": _seen(1)}]},
            {"userId": 12, "ips": [{"ip": "6.6.6.9", "lastSeen": "garbage"}]},
            {"userId": 12},
        ])]
        snapshot, history = merge_node_results(answers, {12}, FRESH_WINDOW)

        self.assertEqual(list(snapshot), [(12, "5.5.5.5")])
        self.assertEqual(list(history), [(12, "5.5.5.5", "a")])

    def test_merge_judges_freshness_from_the_answer_of_the_same_node(self):
        answers = [
            _answer("early", [{"userId": 7, "ips": [{"ip": "1.1.1.1", "lastSeen": _seen(1)}]}]),
            _answer("late", [{"userId": 9, "ips": [{"ip": "3.3.3.3", "lastSeen": _seen(1, before=LATE)}]}], LATE),
        ]
        snapshot, _ = merge_node_results(answers, {7, 9}, FRESH_WINDOW)

        self.assertEqual(set(snapshot), {(7, "1.1.1.1"), (9, "3.3.3.3")})

    def test_merge_drops_ip_stale_for_its_node_though_newer_than_fresh_ip_of_another(self):
        answers = [
            _answer("early", [{"userId": 7, "ips": [{"ip": "1.1.1.1", "lastSeen": _seen(1)}]}]),
            _answer("late", [{"userId": 8, "ips": [{"ip": "2.2.2.2", "lastSeen": _seen(6, before=LATE)}]}], LATE),
        ]
        snapshot, history = merge_node_results(answers, {7, 8}, FRESH_WINDOW)

        self.assertEqual(set(snapshot), {(7, "1.1.1.1")})
        self.assertEqual(history[(8, "2.2.2.2", "late")], LATE - timedelta(minutes=6))

    def test_merge_lists_only_nodes_where_ip_is_still_fresh(self):
        answers = [
            _answer("early", [{"userId": 7, "ips": [{"ip": "1.1.1.1", "lastSeen": _seen(1)}]}]),
            _answer("late", [{"userId": 7, "ips": [{"ip": "1.1.1.1", "lastSeen": _seen(6, before=LATE)}]}], LATE),
        ]
        snapshot, history = merge_node_results(answers, {7}, FRESH_WINDOW)

        self.assertEqual(snapshot[(7, "1.1.1.1")].node_uuids, {"early"})
        self.assertEqual(set(history), {(7, "1.1.1.1", "early"), (7, "1.1.1.1", "late")})


class RecordingSession:
    """Подмена async_session: копит запросы вместо PostgreSQL."""

    def __init__(self, log: list):
        self._log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, statement):
        self._log.append(statement.compile(dialect=postgresql.dialect()))

    async def commit(self):
        self._log.append(COMMIT)


class NodeCacheSaveTest(unittest.TestCase):
    """Кэш нод пишется пачками: один INSERT на весь парк растёт с числом нод и
    упирается в предел asyncpg — 32767 параметров на запрос при 7 на ноду."""

    @staticmethod
    def _save(node_count: int) -> list:
        nodes = [{"uuid": f"node-{i}", "name": f"Node {i}", "countryCode": "DE"} for i in range(node_count)]
        log: list = []
        with mock.patch.object(remnawave_geo_ingest, "async_session", lambda: RecordingSession(log)):
            asyncio.run(remnawave_geo_ingest._save_node_cache(nodes, NOW))
        return log

    @staticmethod
    def _uuids(statement) -> list[str]:
        return [value for name, value in statement.params.items() if name.startswith("uuid_m")]

    def test_fleet_is_written_in_batches_within_one_transaction(self):
        *statements, last = self._save(2 * UPSERT_BATCH + 1)

        self.assertEqual([len(self._uuids(stmt)) for stmt in statements], [UPSERT_BATCH, UPSERT_BATCH, 1])
        self.assertEqual(last, COMMIT)
        self.assertNotIn(COMMIT, statements)

    def test_every_node_is_written_exactly_once(self):
        node_count = 4 * UPSERT_BATCH + 1
        *statements, _ = self._save(node_count)

        written = [uuid for stmt in statements for uuid in self._uuids(stmt)]
        self.assertEqual(sorted(written), sorted(f"node-{i}" for i in range(node_count)))

    def test_statement_fits_the_asyncpg_argument_limit_on_any_fleet(self):
        *statements, _ = self._save(5000)

        self.assertLessEqual(max(len(stmt.params) for stmt in statements), ASYNCPG_MAX_QUERY_ARGS)


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

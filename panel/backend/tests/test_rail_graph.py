"""Граф железных дорог для линий маршрута клиента на карте.

Линия между городами трассы рисуется по рельсам, вдоль которых лежит
магистральная оптика. Ошибка здесь видна сразу: линия срезает напрямую через
полстраны, начинается не от города или уходит в объезд через соседнее море.
"""

import gzip
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.rail_graph import LegMode, RailGraph, build_graph, haversine_km, leg_path  # noqa: E402


def arc(center: tuple[float, float], radius: float, start_deg: float, end_deg: float, steps: int = 12):
    cx, cy = center
    return [
        (cx + radius * math.cos(math.radians(start_deg + (end_deg - start_deg) * i / steps)),
         cy + radius * math.sin(math.radians(start_deg + (end_deg - start_deg) * i / steps)))
        for i in range(steps + 1)
    ]


def close(a, b, tol=1e-3) -> bool:
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


class BuildGraphTests(unittest.TestCase):
    def test_chain_of_points_becomes_one_edge(self):
        graph = build_graph([[(10.0, 50.0), (10.1, 50.0), (10.2, 50.0)]])
        self.assertEqual((graph.node_count, graph.edge_count), (2, 1))

    def test_junction_splits_lines_into_edges(self):
        graph = build_graph([
            [(10.0, 50.0), (10.1, 50.0), (10.2, 50.0)],
            [(10.1, 50.0), (10.1, 50.1)],
        ])
        self.assertEqual((graph.node_count, graph.edge_count), (4, 3))

    def test_dangling_end_near_other_line_is_connected(self):
        # Концы линий в исходных данных не всегда совпадают точно — без сшивки
        # маршрут через такой стык не нашёлся бы
        graph = build_graph([
            [(10.0, 50.0), (10.1, 50.0), (10.2, 50.0)],
            [(10.1, 50.005), (10.1, 50.3)],
        ])
        self.assertIsNotNone(graph.route((10.0, 50.0), (10.1, 50.3)))

    def test_bytes_roundtrip_keeps_routes(self):
        graph = build_graph([arc((10.0, 50.0), 0.5, 180, 90), [(10.0, 50.5), (10.5, 50.5)]])
        restored = RailGraph.from_bytes(graph.to_bytes())
        self.assertEqual((restored.node_count, restored.edge_count), (graph.node_count, graph.edge_count))
        original = graph.route((9.5, 50.0), (10.5, 50.5))
        again = restored.route((9.5, 50.0), (10.5, 50.5))
        self.assertAlmostEqual(original.km, again.km, places=3)
        self.assertEqual(len(original.coords), len(again.coords))


class RouteTests(unittest.TestCase):
    def test_route_follows_rails_around_corner(self):
        graph = build_graph([[(10.0, 50.0), (10.0, 50.5), (10.5, 50.5)]])
        path = graph.route((10.0, 50.0), (10.5, 50.5))
        self.assertTrue(any(close(point, (10.0, 50.5)) for point in path.coords))
        expected = haversine_km((10.0, 50.0), (10.0, 50.5)) + haversine_km((10.0, 50.5), (10.5, 50.5))
        self.assertAlmostEqual(path.km, expected, delta=1.0)

    def test_route_starts_from_nearest_point_of_long_edge(self):
        # Город у середины длинной дуги: путь идёт от ближайшей точки дуги, а не
        # от её дальнего конца
        rails = arc((10.0, 50.0), 1.0, 0, 180, steps=36)
        graph = build_graph([rails])
        city = (rails[18][0], rails[18][1] + 0.01)
        path = graph.route(city, rails[-1])
        self.assertTrue(close(path.coords[0], city))
        self.assertTrue(close(path.coords[1], rails[18], tol=0.01))
        full = sum(haversine_km(a, b) for a, b in zip(rails, rails[1:]))
        self.assertLess(path.km, full * 0.6)

    def test_both_cities_on_one_edge_take_the_slice_between_them(self):
        rails = arc((10.0, 50.0), 1.0, 0, 180, steps=36)
        graph = build_graph([rails])
        path = graph.route(rails[9], rails[27])
        between = sum(haversine_km(a, b) for a, b in zip(rails[9:28], rails[10:28]))
        self.assertAlmostEqual(path.km, between, delta=between * 0.05)

    def test_search_gives_up_beyond_length_limit(self):
        # Цель недостижима короче лимита (другой берег, другой материк) — поиск не должен
        # обходить весь граф, а путь длиннее лимита не нужен
        graph = build_graph([[(10.0, 50.0), (10.0, 52.0), (10.6, 52.0), (10.6, 50.0)]])
        self.assertIsNotNone(graph.route((10.0, 50.0), (10.6, 50.0)))
        self.assertIsNone(graph.route((10.0, 50.0), (10.6, 50.0), max_km=200.0))

    def test_truncated_file_is_rejected(self):
        data = gzip.decompress(build_graph([arc((10.0, 50.0), 0.5, 180, 90)]).to_bytes())
        with self.assertRaises(ValueError):
            RailGraph.from_bytes(gzip.compress(data[:-8]))

    def test_no_rails_nearby_gives_no_route(self):
        graph = build_graph([[(10.0, 50.0), (10.5, 50.0)]])
        self.assertIsNone(graph.route((30.0, 60.0), (10.5, 50.0)))


class LegPathTests(unittest.TestCase):
    def setUp(self):
        self.graph = build_graph([[(10.0, 50.0), (10.0, 51.0), (11.0, 51.0)]])

    def test_reasonable_rail_path_is_used(self):
        leg = leg_path(self.graph, (10.0, 50.0), (11.0, 51.0))
        self.assertEqual(leg.mode, LegMode.RAIL)
        self.assertTrue(close(leg.coords[0], (10.0, 50.0)))
        self.assertTrue(close(leg.coords[-1], (11.0, 51.0)))

    def test_long_detour_draws_direct_line(self):
        # Концы подковы рядом, а по рельсам — вокруг: так выглядит пролив,
        # через который идёт кабель, а не поезд
        graph = build_graph([[(10.0, 50.0), (10.0, 52.0), (10.6, 52.0), (10.6, 50.0)]])
        leg = leg_path(graph, (10.0, 50.0), (10.6, 50.0))
        self.assertEqual(leg.mode, LegMode.DIRECT)
        self.assertAlmostEqual(leg.km, haversine_km((10.0, 50.0), (10.6, 50.0)), delta=0.5)

    def test_no_rails_draws_direct_line(self):
        leg = leg_path(self.graph, (30.0, 60.0), (31.0, 60.5))
        self.assertEqual(leg.mode, LegMode.DIRECT)
        self.assertTrue(close(leg.coords[0], (30.0, 60.0)))
        self.assertTrue(close(leg.coords[-1], (31.0, 60.5)))

    def test_short_hop_is_direct(self):
        leg = leg_path(self.graph, (10.0, 50.0), (10.0, 50.1))
        self.assertEqual(leg.mode, LegMode.DIRECT)

    def test_without_graph_everything_is_direct(self):
        leg = leg_path(None, (10.0, 50.0), (11.0, 51.0))
        self.assertEqual(leg.mode, LegMode.DIRECT)

    def test_direct_line_crosses_antimeridian_the_short_way(self):
        leg = leg_path(None, (179.0, 60.0), (-179.0, 60.0))
        self.assertTrue(all(178.0 <= lon <= 182.0 for lon, _ in leg.coords))


if __name__ == "__main__":
    unittest.main()

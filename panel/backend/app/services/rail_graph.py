"""Граф железных дорог для линий маршрута клиента на карте.

Магистральная оптика на суше почти всегда лежит вдоль железных дорог, поэтому
участок трассы между городами рисуется кратчайшим путём по рельсам, а не прямой
поверх карты. Данные — Natural Earth (railroads 1:10m, общественное достояние).
Граф собирается заранее скриптом `scripts/build_rail_graph.py` и лежит в
`app/geodata/rail_graph.bin.gz`: цепочки точек между развилками схлопнуты в
рёбра, геометрия рёбер упрощена примерно до 500 м.

Где пути по рельсам нет (море, пустые места в данных) или он делает объезд
больше чем в DETOUR_LIMIT раз, участок рисуется плавной кривой напрямую.
"""

import asyncio
import gzip
import heapq
import logging
import math
import struct
import sys
from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

Point = tuple[float, float]  # (lon, lat)

EARTH_RADIUS_KM = 6371.0
KM_PER_DEG_LAT = 111.2
COORD_SCALE = 100_000
GRAPH_MAGIC = b"RAILG1\0\0"
RAIL_GRAPH_PATH = Path(__file__).resolve().parents[1] / "geodata" / "rail_graph.bin.gz"

# Концы линий в Natural Earth на стыках не всегда совпадают точно
STITCH_KM = 3.0
STITCH_CELL_DEG = 0.05
SIMPLIFY_DEG = 0.005

SNAP_CELL_DEG = 0.25
SNAP_MAX_KM = 60.0
# Ближе этого рельсы ничего не добавляют к картинке — рисуем напрямую
MIN_RAIL_KM = 30.0
DETOUR_LIMIT = 1.8
CURVE_BEND = 0.12
CURVE_STEP_KM = 15.0
CURVE_MIN_STEPS = 16
CURVE_MAX_STEPS = 96
OUTPUT_DECIMALS = 4

_CELL_KEY_BASE = 2048


class LegMode(str, Enum):
    RAIL = "rail"
    DIRECT = "direct"


@dataclass(frozen=True)
class RailPath:
    coords: list[Point]
    km: float


@dataclass(frozen=True)
class LegPath:
    coords: tuple[Point, ...]
    km: float
    mode: LegMode


@dataclass(frozen=True)
class _Snap:
    point: Point
    dist_km: float
    node: int
    edge: int
    offset: int  # индекс точки в геометрии ребра (с концами)


def haversine_km(a: Point, b: Point) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def _polyline_km(points: Sequence[Point]) -> float:
    return sum(haversine_km(a, b) for a, b in zip(points, points[1:]))


def _cell_key(cx: int, cy: int) -> int:
    return (cx + _CELL_KEY_BASE) * _CELL_KEY_BASE * 2 + (cy + _CELL_KEY_BASE)


def _native(values: array) -> array:
    # Файл графа — little-endian; на big-endian машине массивы разворачиваются
    if sys.byteorder == "big":
        values = array(values.typecode, values)
        values.byteswap()
    return values


class RailGraph:
    """Сжатый граф: узлы — развилки и концы линий, рёбра — участки между ними
    с упрощённой геометрией. Координаты — целые в 1e-5 градуса."""

    def __init__(self, nx: array, ny: array, ea: array, eb: array, elen: array,
                 pstart: array, px: array, py: array):
        self._nx, self._ny = nx, ny
        self._ea, self._eb, self._elen = ea, eb, elen
        self._pstart, self._px, self._py = pstart, px, py
        self._build_adjacency()
        self._build_snap_grid()

    @property
    def node_count(self) -> int:
        return len(self._nx)

    @property
    def edge_count(self) -> int:
        return len(self._ea)

    def _build_adjacency(self) -> None:
        nodes = self.node_count
        degree = [0] * (nodes + 1)
        for a, b in zip(self._ea, self._eb):
            degree[a + 1] += 1
            degree[b + 1] += 1
        for i in range(nodes):
            degree[i + 1] += degree[i]
        self._adj_start = array("I", degree)
        fill = list(degree[:-1])
        adjacency = [0] * (2 * self.edge_count)
        for edge, (a, b) in enumerate(zip(self._ea, self._eb)):
            adjacency[fill[a]] = edge
            fill[a] += 1
            adjacency[fill[b]] = edge
            fill[b] += 1
        self._adj_edge = array("I", adjacency)

    def _build_snap_grid(self) -> None:
        cell = int(SNAP_CELL_DEG * COORD_SCALE)
        keys = [_cell_key(x // cell, y // cell) for x, y in zip(self._nx, self._ny)]
        keys += [_cell_key(x // cell, y // cell) for x, y in zip(self._px, self._py)]
        order = sorted(range(len(keys)), key=keys.__getitem__)
        self._grid_keys = array("q", (keys[i] for i in order))
        self._grid_points = array("i", order)

    def _point(self, index: int) -> Point:
        if index < self.node_count:
            return self._nx[index] / COORD_SCALE, self._ny[index] / COORD_SCALE
        k = index - self.node_count
        return self._px[k] / COORD_SCALE, self._py[k] / COORD_SCALE

    def _edge_coords(self, edge: int) -> list[Point]:
        interior = range(self._pstart[edge], self._pstart[edge + 1])
        return ([self._point(self._ea[edge])]
                + [(self._px[k] / COORD_SCALE, self._py[k] / COORD_SCALE) for k in interior]
                + [self._point(self._eb[edge])])

    def _snap(self, target: Point, max_km: float) -> Optional[_Snap]:
        lon, lat = target
        lat_cells = math.ceil(max_km / (KM_PER_DEG_LAT * SNAP_CELL_DEG))
        lon_cells = math.ceil(max_km / (KM_PER_DEG_LAT * SNAP_CELL_DEG * max(math.cos(math.radians(lat)), 0.05)))
        cx, cy = math.floor(lon / SNAP_CELL_DEG), math.floor(lat / SNAP_CELL_DEG)
        best_index, best_km = -1, max_km
        for dx in range(-lon_cells, lon_cells + 1):
            for dy in range(-lat_cells, lat_cells + 1):
                key = _cell_key(cx + dx, cy + dy)
                low = bisect_left(self._grid_keys, key)
                high = bisect_right(self._grid_keys, key, low)
                for position in range(low, high):
                    index = self._grid_points[position]
                    km = haversine_km(target, self._point(index))
                    if km <= best_km:
                        best_index, best_km = index, km
        if best_index < 0:
            return None
        point = self._point(best_index)
        if best_index < self.node_count:
            return _Snap(point, best_km, best_index, -1, -1)
        k = best_index - self.node_count
        edge = bisect_right(self._pstart, k) - 1
        return _Snap(point, best_km, -1, edge, k - self._pstart[edge] + 1)

    def _exits(self, snap: _Snap) -> list[tuple[int, float, list[Point]]]:
        """Узлы, до которых можно дойти от точки привязки: (узел, км, путь от точки к узлу)."""
        if snap.node >= 0:
            return [(snap.node, 0.0, [snap.point])]
        coords = self._edge_coords(snap.edge)
        to_a, to_b = coords[snap.offset::-1], coords[snap.offset:]
        along_a, along_b = _polyline_km(to_a), _polyline_km(to_b)
        # Упрощённая геометрия короче настоящей — делим длину ребра пропорционально
        length = self._elen[snap.edge]
        cost_a = length * along_a / (along_a + along_b) if along_a + along_b else 0.0
        return [(self._ea[snap.edge], cost_a, to_a), (self._eb[snap.edge], length - cost_a, to_b)]

    def _same_edge_slice(self, start: _Snap, end: _Snap) -> Optional[tuple[float, list[Point]]]:
        if start.edge < 0 or start.edge != end.edge:
            return None
        coords = self._edge_coords(start.edge)
        i, j = start.offset, end.offset
        piece = coords[i:j + 1] if i <= j else coords[j:i + 1][::-1]
        total = _polyline_km(coords)
        km = self._elen[start.edge] * _polyline_km(piece) / total if total else 0.0
        return km, piece

    def route(self, start: Point, end: Point, max_km: float = math.inf,
              max_snap_km: float = SNAP_MAX_KM) -> Optional[RailPath]:
        """Кратчайший путь по рельсам (A*) от точки до точки с подводками от городов.

        Путь длиннее max_km не ищется: без лимита недостижимая цель (другой берег)
        заставила бы обойти всю связную часть графа."""
        source, target = self._snap(start, max_snap_km), self._snap(end, max_snap_km)
        if source is None or target is None:
            return None

        best_km, best_path = max_km - source.dist_km - target.dist_km, None
        same_edge = self._same_edge_slice(source, target)
        if same_edge and same_edge[0] < best_km:
            best_km, best_path = same_edge

        targets: dict[int, tuple[float, list[Point]]] = {}
        for node, cost, path in self._exits(target):
            if node not in targets or cost < targets[node][0]:
                targets[node] = (cost, path[::-1])

        dist: dict[int, float] = {}
        prev: dict[int, tuple[int, int]] = {}
        lead_in: dict[int, list[Point]] = {}
        queue: list[tuple[float, float, int]] = []
        for node, cost, path in self._exits(source):
            if cost < dist.get(node, math.inf):
                dist[node] = cost
                lead_in[node] = path
                heapq.heappush(queue, (cost + haversine_km(self._point(node), target.point), cost, node))

        best_end = -1
        while queue:
            estimate, cost, node = heapq.heappop(queue)
            if estimate >= best_km:
                break
            if cost > dist[node]:
                continue
            if node in targets and cost + targets[node][0] < best_km:
                best_km, best_end = cost + targets[node][0], node
            for slot in range(self._adj_start[node], self._adj_start[node + 1]):
                edge = self._adj_edge[slot]
                other = self._eb[edge] if self._ea[edge] == node else self._ea[edge]
                next_cost = cost + self._elen[edge]
                if next_cost < dist.get(other, math.inf):
                    dist[other] = next_cost
                    prev[other] = (node, edge)
                    lead_in.pop(other, None)
                    heapq.heappush(queue, (next_cost + haversine_km(self._point(other), target.point), next_cost, other))

        if best_end >= 0:
            best_path = self._assemble(best_end, prev, lead_in, targets[best_end][1])
        if best_path is None:
            return None
        coords = _dedupe([start, *best_path, end])
        return RailPath(coords, best_km + source.dist_km + target.dist_km)

    def _assemble(self, end_node: int, prev: dict[int, tuple[int, int]],
                  lead_in: dict[int, list[Point]], lead_out: list[Point]) -> list[Point]:
        steps: list[tuple[int, int]] = []
        node = end_node
        while node not in lead_in:
            parent, edge = prev[node]
            steps.append((parent, edge))
            node = parent
        path = list(lead_in[node])
        for parent, edge in reversed(steps):
            coords = self._edge_coords(edge)
            path += coords[1:] if self._ea[edge] == parent else coords[-2::-1]
        return path + lead_out[1:]

    def to_bytes(self) -> bytes:
        dx, dy = array("i"), array("i")
        for edge in range(self.edge_count):
            last_x, last_y = self._nx[self._ea[edge]], self._ny[self._ea[edge]]
            for k in range(self._pstart[edge], self._pstart[edge + 1]):
                dx.append(self._px[k] - last_x)
                dy.append(self._py[k] - last_y)
                last_x, last_y = self._px[k], self._py[k]
        header = GRAPH_MAGIC + struct.pack("<III", self.node_count, self.edge_count, len(self._px))
        body = b"".join(_native(values).tobytes() for values in (
            self._nx, self._ny, self._ea, self._eb, self._elen, self._pstart, dx, dy,
        ))
        return gzip.compress(header + body, compresslevel=9, mtime=0)

    @classmethod
    def from_bytes(cls, data: bytes) -> "RailGraph":
        raw = gzip.decompress(data)
        if not raw.startswith(GRAPH_MAGIC):
            raise ValueError("not a rail graph file")
        offset = len(GRAPH_MAGIC)
        nodes, edges, points = struct.unpack_from("<III", raw, offset)
        offset += 12
        # Узлы: 2 массива по nodes, рёбра: 3 по edges и pstart на edges + 1, точки: 2 по points
        if len(raw) != offset + 4 * (2 * nodes + 4 * edges + 1 + 2 * points):
            raise ValueError("rail graph file is truncated")

        def take(typecode: str, count: int) -> array:
            nonlocal offset
            values = array(typecode)
            values.frombytes(raw[offset:offset + count * values.itemsize])
            offset += count * values.itemsize
            return _native(values)

        nx, ny = take("i", nodes), take("i", nodes)
        ea, eb, elen = take("I", edges), take("I", edges), take("f", edges)
        pstart = take("I", edges + 1)
        dx, dy = take("i", points), take("i", points)
        px, py = array("i", bytes(4 * points)), array("i", bytes(4 * points))
        for edge in range(edges):
            last_x, last_y = nx[ea[edge]], ny[ea[edge]]
            for k in range(pstart[edge], pstart[edge + 1]):
                last_x += dx[k]
                last_y += dy[k]
                px[k], py[k] = last_x, last_y
        return cls(nx, ny, ea, eb, elen, pstart, px, py)


def _dedupe(points: list[Point]) -> list[Point]:
    result = [points[0]]
    for point in points[1:]:
        if point != result[-1]:
            result.append(point)
    return result


# ---------------------------------------------------------------------------
# Сборка графа из линий (scripts/build_rail_graph.py и тесты)
# ---------------------------------------------------------------------------

def build_graph(lines: Iterable[Sequence[Sequence[float]]], stitch_km: float = STITCH_KM,
                simplify_deg: float = SIMPLIFY_DEG) -> RailGraph:
    index: dict[tuple[int, int], int] = {}
    coords: list[tuple[int, int]] = []
    neighbors: list[set[int]] = []

    def vertex(point: Sequence[float]) -> int:
        key = (round(point[0] * COORD_SCALE), round(point[1] * COORD_SCALE))
        found = index.get(key)
        if found is None:
            found = index[key] = len(coords)
            coords.append(key)
            neighbors.append(set())
        return found

    for line in lines:
        ids = [vertex(point) for point in line]
        for a, b in zip(ids, ids[1:]):
            if a != b:
                neighbors[a].add(b)
                neighbors[b].add(a)
    _stitch_dangling_ends(coords, neighbors, stitch_km)
    return _contract(coords, neighbors, simplify_deg)


def _scaled_point(xy: tuple[int, int]) -> Point:
    return xy[0] / COORD_SCALE, xy[1] / COORD_SCALE


def _stitch_dangling_ends(coords: list[tuple[int, int]], neighbors: list[set[int]], stitch_km: float) -> None:
    cell = int(STITCH_CELL_DEG * COORD_SCALE)
    grid: dict[tuple[int, int], list[int]] = {}
    for vertex_id, (x, y) in enumerate(coords):
        grid.setdefault((x // cell, y // cell), []).append(vertex_id)
    dangling = [vertex_id for vertex_id, near in enumerate(neighbors) if len(near) == 1]
    for vertex_id in dangling:
        x, y = coords[vertex_id]
        point = _scaled_point(coords[vertex_id])
        reach_y = math.ceil(stitch_km / (KM_PER_DEG_LAT * STITCH_CELL_DEG))
        reach_x = math.ceil(stitch_km / (KM_PER_DEG_LAT * STITCH_CELL_DEG * max(math.cos(math.radians(point[1])), 0.05)))
        best, best_km = -1, stitch_km
        for dx in range(-reach_x, reach_x + 1):
            for dy in range(-reach_y, reach_y + 1):
                for other in grid.get((x // cell + dx, y // cell + dy), ()):
                    if other == vertex_id or other in neighbors[vertex_id]:
                        continue
                    km = haversine_km(point, _scaled_point(coords[other]))
                    if km <= best_km:
                        best, best_km = other, km
        if best >= 0:
            neighbors[vertex_id].add(best)
            neighbors[best].add(vertex_id)


def _simplify(points: list[tuple[int, int]], tolerance: float) -> list[int]:
    """Индексы точек, которые оставляет Дуглас — Пекер (без рекурсии)."""
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        ax, ay = points[first]
        bx, by = points[last]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        worst, worst_index = -1.0, -1
        for i in range(first + 1, last):
            px, py = points[i]
            distance = abs(dy * (px - ax) - dx * (py - ay)) / norm if norm else math.hypot(px - ax, py - ay)
            if distance > worst:
                worst, worst_index = distance, i
        if worst > tolerance:
            keep[worst_index] = True
            stack += [(first, worst_index), (worst_index, last)]
    return [i for i, kept in enumerate(keep) if kept]


def _contract(coords: list[tuple[int, int]], neighbors: list[set[int]], simplify_deg: float) -> RailGraph:
    junction = [len(near) != 2 for near in neighbors]
    visited: set[tuple[int, int]] = set()
    renumber: dict[int, int] = {}
    nx, ny = array("i"), array("i")
    ea, eb, elen = array("I"), array("I"), array("f")
    pstart, px, py = array("I", [0]), array("i"), array("i")
    tolerance = simplify_deg * COORD_SCALE

    def node_id(vertex_id: int) -> int:
        if vertex_id not in renumber:
            renumber[vertex_id] = len(nx)
            nx.append(coords[vertex_id][0])
            ny.append(coords[vertex_id][1])
        return renumber[vertex_id]

    def walk(start: int, first: int) -> None:
        chain, previous, current = [start, first], start, first
        visited.add((min(start, first), max(start, first)))
        while not junction[current]:
            following = next(n for n in neighbors[current] if n != previous)
            visited.add((min(current, following), max(current, following)))
            chain.append(following)
            previous, current = current, following
        if chain[0] == chain[-1]:
            return
        points = [coords[v] for v in chain]
        ea.append(node_id(chain[0]))
        eb.append(node_id(chain[-1]))
        elen.append(_polyline_km([_scaled_point(p) for p in points]))
        for i in _simplify(points, tolerance)[1:-1]:
            px.append(points[i][0])
            py.append(points[i][1])
        pstart.append(len(px))

    for vertex_id, near in enumerate(neighbors):
        if junction[vertex_id]:
            for other in near:
                if (min(vertex_id, other), max(vertex_id, other)) not in visited:
                    walk(vertex_id, other)
    # Кольца без развилок: точка кольца становится развилкой, ребро-петля отбрасывается
    for vertex_id, near in enumerate(neighbors):
        for other in near:
            if (min(vertex_id, other), max(vertex_id, other)) not in visited:
                junction[vertex_id] = True
                walk(vertex_id, other)
    return RailGraph(nx, ny, ea, eb, elen, pstart, px, py)


# ---------------------------------------------------------------------------
# Участок маршрута
# ---------------------------------------------------------------------------

def direct_curve(start: Point, end: Point) -> list[Point]:
    """Плавная дуга (квадратичная кривая Безье) — участок без дороги не выглядит
    отрезком поверх карты. Через антимеридиан идёт коротким путём."""
    end_lon = end[0]
    if end_lon - start[0] > 180:
        end_lon -= 360
    elif end_lon - start[0] < -180:
        end_lon += 360
    dx, dy = end_lon - start[0], end[1] - start[1]
    control = ((start[0] + end_lon) / 2 - dy * CURVE_BEND, (start[1] + end[1]) / 2 + dx * CURVE_BEND)
    steps = max(CURVE_MIN_STEPS, min(CURVE_MAX_STEPS, int(haversine_km(start, end) / CURVE_STEP_KM)))
    points = []
    for i in range(steps + 1):
        t = i / steps
        points.append((
            (1 - t) ** 2 * start[0] + 2 * (1 - t) * t * control[0] + t * t * end_lon,
            (1 - t) ** 2 * start[1] + 2 * (1 - t) * t * control[1] + t * t * end[1],
        ))
    return points


def _rounded(points: Iterable[Point]) -> tuple[Point, ...]:
    return tuple((round(lon, OUTPUT_DECIMALS), round(lat, OUTPUT_DECIMALS)) for lon, lat in points)


def leg_path(graph: Optional[RailGraph], start: Point, end: Point) -> LegPath:
    direct_km = haversine_km(start, end)
    if graph is not None and direct_km >= MIN_RAIL_KM:
        path = graph.route(start, end, max_km=direct_km * DETOUR_LIMIT)
        if path is not None:
            return LegPath(_rounded(path.coords), round(path.km, 1), LegMode.RAIL)
    return LegPath(_rounded(direct_curve(start, end)), round(direct_km, 1), LegMode.DIRECT)


# ---------------------------------------------------------------------------
# Загрузка
# ---------------------------------------------------------------------------

_graph: Optional[RailGraph] = None
_load_failed = False
_load_lock = asyncio.Lock()


def load_rail_graph(path: Path = RAIL_GRAPH_PATH) -> RailGraph:
    return RailGraph.from_bytes(path.read_bytes())


async def get_rail_graph() -> Optional[RailGraph]:
    """Граф грузится при первом маршруте, не на старте панели: пока окно маршрута
    никто не открывал, памяти он не занимает. Не загрузился — участки рисуются
    напрямую, повторной попытки до перезапуска нет."""
    global _graph, _load_failed
    if _graph is not None or _load_failed:
        return _graph
    async with _load_lock:
        if _graph is None and not _load_failed:
            try:
                _graph = await asyncio.to_thread(load_rail_graph)
                logger.info("rail_graph_loaded nodes=%s edges=%s", _graph.node_count, _graph.edge_count)
            except (OSError, ValueError, EOFError, struct.error) as exc:
                _load_failed = True
                logger.error("rail_graph_load_failed path=%s error=%s", RAIL_GRAPH_PATH, exc)
    return _graph

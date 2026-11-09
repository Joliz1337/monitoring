"""Собирает app/geodata/rail_graph.bin.gz — граф железных дорог для маршрута клиента.

Исходник — Natural Earth, railroads 1:10m (общественное достояние):
https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_railroads.geojson

Запуск из panel/backend:
    python scripts/build_rail_graph.py ne_10m_railroads.geojson

Панель граф не пересобирает: скрипт нужен, только когда обновляются данные.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.rail_graph import RAIL_GRAPH_PATH, build_graph  # noqa: E402


def read_lines(source: Path) -> list[list[list[float]]]:
    lines: list[list[list[float]]] = []
    for feature in json.loads(source.read_text(encoding="utf-8"))["features"]:
        geometry = feature.get("geometry") or {}
        if geometry.get("type") == "LineString":
            lines.append(geometry["coordinates"])
        elif geometry.get("type") == "MultiLineString":
            lines.extend(geometry["coordinates"])
    return lines


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    graph = build_graph(read_lines(Path(sys.argv[1])))
    RAIL_GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = graph.to_bytes()
    RAIL_GRAPH_PATH.write_bytes(data)
    print(f"{RAIL_GRAPH_PATH}: {graph.node_count} nodes, {graph.edge_count} edges, {len(data) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()

"""
Статистика выданных облаком адресов — общая для всех провайдеров IP-Search.

Одна строка на (аккаунт, зона, адрес) со счётчиком повторов: пул адресов облака
конечен, поэтому таблица не растёт с каждым раундом. Зона — то, по чему провайдер
раскладывает выдачу: зона Cloud.ru и Yandex Cloud, регион Selectel, внешняя
подсеть VK Cloud. Таблица у каждого провайдера своя, чтобы удалённый аккаунт
уносил статистику с собой по внешнему ключу.
"""

import ipaddress
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SUBNET_EXPR = "text(network(set_masklen(CAST(ip_address AS inet), 24)))"
COUNTERS = (
    "SUM(times_received), COUNT(DISTINCT ip_address), "
    "COUNT(DISTINCT ip_address) FILTER (WHERE caught)"
)


def received_ips_ddl(table: str, accounts_table: str) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    account_id INTEGER NOT NULL,
    zone VARCHAR(50) NOT NULL,
    ip_address VARCHAR(50) NOT NULL,
    times_received INTEGER NOT NULL DEFAULT 1,
    caught BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_id, zone, ip_address),
    FOREIGN KEY (account_id) REFERENCES {accounts_table}(id) ON DELETE CASCADE
)"""


class ReceivedIpsStore:

    def __init__(self, table: str):
        self.table = table

    def _account_filter(self, account_id: Optional[int]) -> tuple:
        if account_id is None:
            return "", {}
        return "WHERE account_id = :aid", {"aid": account_id}

    async def save(self, db: AsyncSession, account_id: int, items: List[Dict[str, Any]]) -> int:
        if not items:
            return 0
        await db.execute(text(
            f"INSERT INTO {self.table} (account_id, zone, ip_address, caught) "
            "VALUES (:account_id, :zone, :ip_address, :caught) "
            "ON CONFLICT (account_id, zone, ip_address) DO UPDATE SET "
            f"times_received = {self.table}.times_received + 1, "
            f"caught = {self.table}.caught OR EXCLUDED.caught, "
            "last_seen = NOW()"
        ), [{"account_id": account_id, **item} for item in items])
        await db.commit()
        return len(items)

    async def stats(
        self, db: AsyncSession, account_id: Optional[int], whitelist_cidrs: Iterable[str],
    ) -> Dict[str, Any]:
        where, params = self._account_filter(account_id)

        total = (await db.execute(text(
            f"SELECT {COUNTERS}, MIN(first_seen), MAX(last_seen) FROM {self.table} {where}"
        ), params)).fetchone()

        zone_rows = (await db.execute(text(
            f"SELECT zone, {COUNTERS}, MIN(first_seen), MAX(last_seen) "
            f"FROM {self.table} {where} GROUP BY zone ORDER BY zone"
        ), params)).fetchall()

        subnet_rows = (await db.execute(text(
            f"SELECT {SUBNET_EXPR} AS subnet, {COUNTERS}, MAX(last_seen) "
            f"FROM {self.table} {where} GROUP BY subnet"
        ), params)).fetchall()

        subnet_zone_rows = (await db.execute(text(
            f"SELECT {SUBNET_EXPR} AS subnet, zone, SUM(times_received) "
            f"FROM {self.table} {where} GROUP BY subnet, zone"
        ), params)).fetchall()

        received_by_zone: Dict[str, Dict[str, int]] = {}
        for subnet, zone, received in subnet_zone_rows:
            received_by_zone.setdefault(subnet, {})[zone] = int(received)

        whitelist = [ipaddress.ip_network(c, strict=False) for c in whitelist_cidrs]

        subnets = [{
            "subnet": subnet,
            "received": int(received),
            "unique_ips": unique_ips,
            "caught": caught,
            "last_seen": last_seen,
            "in_whitelist": any(ipaddress.ip_network(subnet).overlaps(w) for w in whitelist),
            "zones": received_by_zone.get(subnet, {}),
        } for subnet, received, unique_ips, caught, last_seen in subnet_rows]
        subnets.sort(key=lambda s: (-s["received"], s["subnet"]))

        return {
            "totals": {
                "received": int(total[0] or 0),
                "unique_ips": total[1] or 0,
                "caught": total[2] or 0,
                "subnets": len(subnets),
                "first_seen": total[3],
                "last_seen": total[4],
            },
            "zones": [{
                "zone": zone,
                "received": int(received),
                "unique_ips": unique_ips,
                "caught": caught,
                "first_seen": first_seen,
                "last_seen": last_seen,
            } for zone, received, unique_ips, caught, first_seen, last_seen in zone_rows],
            "subnets": subnets,
        }

    async def subnet_ips(
        self, db: AsyncSession, subnet: str, account_id: Optional[int],
    ) -> List[Dict[str, Any]]:
        query = (
            "SELECT ip_address, zone, SUM(times_received), BOOL_OR(caught), MIN(first_seen), MAX(last_seen) "
            f"FROM {self.table} WHERE CAST(ip_address AS inet) << CAST(:subnet AS inet)"
        )
        params: Dict[str, Any] = {"subnet": subnet}
        if account_id is not None:
            query += " AND account_id = :aid"
            params["aid"] = account_id
        query += " GROUP BY ip_address, zone ORDER BY CAST(ip_address AS inet)"

        result = await db.execute(text(query), params)
        return [{
            "ip_address": r[0],
            "zone": r[1],
            "received": int(r[2]),
            "caught": bool(r[3]),
            "first_seen": r[4],
            "last_seen": r[5],
        } for r in result.fetchall()]

    async def clear(self, db: AsyncSession, account_id: Optional[int]) -> int:
        where, params = self._account_filter(account_id)
        result = await db.execute(text(f"DELETE FROM {self.table} {where}"), params)
        await db.commit()
        return result.rowcount

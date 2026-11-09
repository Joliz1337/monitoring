"""Разбор записей локальной базы DB-IP и выбор файла месяца.

От разбора зависит, в какой город попадёт клиент на карте: районы Москвы должны
сливаться в один город, запись без города — падать в регион, а мусор вместо IP
не должен доходить до читателя базы.
"""

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # локальный прогон без установленного рантайма панели
    from app.services.geoip import (  # noqa: E402
        GeoIpDatabase, GeoRecord, candidate_months, dbip_url, parse_record,
    )
except ImportError as e:  # pragma: no cover
    raise unittest.SkipTest(f"geoip requires the panel runtime: {e}")


MOSCOW_DISTRICT = {
    "city": {"names": {"en": "Moscow (Tsentralnyy administrativnyy okrug)"}},
    "country": {"iso_code": "RU", "names": {"en": "Russia", "ru": "Россия"}},
    "location": {"latitude": 55.745, "longitude": 37.6222},
    "subdivisions": [{"names": {"en": "Moscow"}}],
}
MEGAFON = {"autonomous_system_number": 31213, "autonomous_system_organization": "PJSC MegaFon"}


class _FakeReader:
    def __init__(self, records: dict):
        self.records = records
        self.asked: list[str] = []

    def get(self, ip: str):
        self.asked.append(ip)
        return self.records.get(ip)


class ParseRecordTest(unittest.TestCase):
    def test_parse_record_city_and_asn(self):
        self.assertEqual(
            parse_record(MOSCOW_DISTRICT, MEGAFON),
            GeoRecord("RU", "Moscow", "Moscow", 55.745, 37.6222, 31213, "PJSC MegaFon"),
        )

    def test_parse_record_region_only(self):
        rec = {
            "country": {"iso_code": "RU"},
            "location": {"latitude": 57.6, "longitude": 39.9},
            "subdivisions": [{"names": {"en": "Yaroslavl Oblast"}}],
        }
        self.assertEqual(parse_record(rec, None).place, "Yaroslavl Oblast")

    def test_parse_record_empty(self):
        self.assertIsNone(parse_record(None, None))
        self.assertIsNone(parse_record({}, {}))

    def test_parse_record_asn_only(self):
        geo = parse_record(None, MEGAFON)
        self.assertIsNone(geo.country_code)
        self.assertIsNone(geo.lat)
        self.assertIsNone(geo.lon)
        self.assertEqual(geo.place, "")
        self.assertEqual(geo.asn, 31213)


class MonthUrlTest(unittest.TestCase):
    def test_candidate_months_january(self):
        self.assertEqual(candidate_months(datetime(2027, 1, 3)), [(2027, 1), (2026, 12)])

    def test_dbip_url(self):
        self.assertEqual(
            dbip_url("city", 2026, 11),
            "https://download.db-ip.com/free/dbip-city-lite-2026-11.mmdb.gz",
        )
        self.assertEqual(
            dbip_url("asn", 2026, 1),
            "https://download.db-ip.com/free/dbip-asn-lite-2026-01.mmdb.gz",
        )


class LookupTest(unittest.TestCase):
    def setUp(self):
        self.city = _FakeReader({"2a00:1450:4001::1": MOSCOW_DISTRICT})
        self.asn = _FakeReader({"2a00:1450:4001::1": MEGAFON})
        self.db = GeoIpDatabase.from_readers(self.city, self.asn)

    def test_lookup_rejects_garbage(self):
        self.assertIsNone(self.db.lookup("not-an-ip"))
        self.assertEqual(self.city.asked, [])

    def test_lookup_ipv6(self):
        geo = self.db.lookup("2a00:1450:4001::1")
        self.assertEqual(geo.place, "Moscow")
        self.assertEqual(self.city.asked, ["2a00:1450:4001::1"])

    def test_lookup_many_skips_unknown(self):
        found = self.db.lookup_many(["2a00:1450:4001::1", "10.0.0.1", "bad"])
        self.assertEqual(list(found), ["2a00:1450:4001::1"])

    def test_not_ready_without_readers(self):
        db = GeoIpDatabase()
        self.assertFalse(db.ready)
        self.assertIsNone(db.lookup("8.8.8.8"))


if __name__ == "__main__":
    unittest.main()

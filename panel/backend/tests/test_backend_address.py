"""Tests for editing a backend address across HAProxy and DNAT profiles.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Главное: правка HAProxy точечная — конфиг, собранный генератором, после неё
разбирается с новыми адресами, а всё прочее в тексте (ручные строки, соседние
адреса) остаётся побайтно прежним; единственный бэкенд не удаляется.
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import DnatProfile, HAProxyConfigProfile  # noqa: E402
from app.services import backend_address  # noqa: E402
from app.services.backend_address import (  # noqa: E402
    Action,
    AddressEdit,
    AddressEditError,
    Outcome,
    SkipReason,
    edit_dnat_rules,
    ProfileRef,
    edit_haproxy_config,
    plan_batch,
    rollout_progress,
    suggest_addresses,
    validate_edit,
)
from app.services.haproxy_config import (  # noqa: E402
    BackendServer,
    BalancerOptions,
    HAProxyRule,
    ProfileOptions,
    get_config_generator,
)
from app.services.loss_registry import LossReading, RelaySnapshot  # noqa: E402

OLD = "62.50.146.225"
NEW = "62.50.146.231"
NEIGHBOUR = "162.50.146.225"  # содержит OLD подстрокой — не должен задеваться


def build_config() -> str:
    rules = [
        HAProxyRule(name="vless-nl", rule_type="tcp", listen_port=443, target_ip=OLD, target_port=8449,
                    send_proxy=True),
        HAProxyRule(name="panel", rule_type="https", listen_port=8443, target_ip=OLD, target_port=443,
                    cert_domain="example.com", target_ssl=True),
        HAProxyRule(name="lb", rule_type="tcp", listen_port=9443, target_ip=OLD, target_port=8443,
                    is_balancer=True, balancer_options=BalancerOptions(), servers=[
                        BackendServer("a", OLD, 8443), BackendServer("b", "62.50.146.227", 8443),
                        BackendServer("c", NEIGHBOUR, 8443),
                    ]),
    ]
    return get_config_generator().generate_full_config(rules, ProfileOptions()) + "\n# ручная пометка: старый " + OLD + "\n"


def parsed(config: str) -> dict:
    return {rule.name: rule for rule in get_config_generator().parse_rules_from_config(config)}


def edit(**overrides) -> AddressEdit:
    fields = {"ip": OLD, "port": 8443, "all_ports": True, "action": Action.REPLACE, "new_ip": NEW}
    fields.update(overrides)
    return AddressEdit(**fields)


class ValidateTests(unittest.TestCase):
    def test_rejects_noop_and_port_change_for_all_ports(self):
        with self.assertRaises(AddressEditError):
            validate_edit(edit(new_ip=OLD, new_port=None))
        with self.assertRaises(AddressEditError):
            validate_edit(edit(new_port=9000, all_ports=True))
        validate_edit(edit(new_port=9000, all_ports=False))
        validate_edit(edit(action=Action.DELETE, new_ip=None))


class HAProxyEditTests(unittest.TestCase):
    def test_replace_ip_on_all_ports(self):
        config = build_config()
        result, outcomes = edit_haproxy_config(config, edit())
        rules = parsed(result)
        self.assertEqual((rules["vless-nl"].target_ip, rules["vless-nl"].target_port), (NEW, 8449))
        self.assertEqual(rules["panel"].target_ip, NEW)
        self.assertEqual([s.address for s in rules["lb"].servers], [NEW, "62.50.146.227", NEIGHBOUR])
        self.assertIn(f"sni str({NEW})", result)
        self.assertIn(f"http-request set-header Host {NEW}", result)
        self.assertNotIn(f"sni str({OLD})", result)
        self.assertEqual({o.rule for o in outcomes}, {"vless-nl", "panel", "lb"})
        self.assertTrue(all(o.outcome is Outcome.CHANGED for o in outcomes))

    def test_everything_else_is_untouched(self):
        config = build_config()
        result, _ = edit_haproxy_config(config, edit())
        before, after = config.split("\n"), result.split("\n")
        self.assertEqual(len(before), len(after))
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        # server tcp, server https (с sni в той же строке), Host у https, сервер a балансировщика
        self.assertEqual(len(changed), 4)
        self.assertTrue(all(OLD in b for b, _ in changed))
        self.assertIn(f"# ручная пометка: старый {OLD}", result)

    def test_port_change_touches_only_that_port(self):
        result, outcomes = edit_haproxy_config(build_config(), edit(all_ports=False, port=8449, new_ip=OLD, new_port=9449))
        rules = parsed(result)
        self.assertEqual((rules["vless-nl"].target_ip, rules["vless-nl"].target_port), (OLD, 9449))
        self.assertEqual(rules["lb"].servers[0].port, 8443)
        self.assertEqual([o.rule for o in outcomes], ["vless-nl"])

    def test_delete_skips_the_only_backend(self):
        result, outcomes = edit_haproxy_config(build_config(), edit(action=Action.DELETE, new_ip=None))
        by_rule = {o.rule: o for o in outcomes}
        self.assertEqual(by_rule["vless-nl"].reason, SkipReason.LAST_BACKEND)
        self.assertEqual(by_rule["panel"].outcome, Outcome.SKIPPED)
        self.assertEqual(by_rule["lb"].outcome, Outcome.CHANGED)
        self.assertEqual([s.address for s in parsed(result)["lb"].servers], ["62.50.146.227", NEIGHBOUR])
        self.assertEqual(parsed(result)["vless-nl"].target_ip, OLD)

    def test_unrelated_address_changes_nothing(self):
        config = build_config()
        self.assertEqual(edit_haproxy_config(config, edit(ip="10.9.9.9")), (config, []))


class DnatEditTests(unittest.TestCase):
    RULES = [
        {"name": "balanced", "protocol": "tcp", "listen_port": 443, "listen_port_end": None,
         "target_ip": f"62.50.146.227,{OLD},62.50.146.172", "distribution": "per_server", "target_port": 8443},
        {"name": "single", "protocol": "tcp", "listen_port": 8449, "listen_port_end": None,
         "target_ip": OLD, "distribution": "per_server", "target_port": 0},
        {"name": "range", "protocol": "tcp", "listen_port": 10000, "listen_port_end": 10100,
         "target_ip": OLD, "distribution": "per_server", "target_port": 0},
    ]

    def test_replace_in_place_keeps_positions(self):
        rules, outcomes = edit_dnat_rules(self.RULES, edit())
        self.assertEqual(rules[0]["target_ip"], f"62.50.146.227,{NEW},62.50.146.172")
        self.assertEqual(rules[1]["target_ip"], NEW)
        self.assertEqual(rules[2]["target_ip"], NEW)
        self.assertEqual(len(outcomes), 3)

    def test_new_ip_already_in_list_is_merged(self):
        rules, outcomes = edit_dnat_rules(self.RULES[:1], edit(new_ip="62.50.146.172"))
        self.assertEqual(rules[0]["target_ip"], "62.50.146.227,62.50.146.172")
        self.assertEqual(outcomes[0].outcome, Outcome.MERGED)

    def test_port_match_uses_effective_port(self):
        # «single» идёт на 8449 (target_port 0 = входящий), диапазон без all_ports не совпадает
        rules, outcomes = edit_dnat_rules(self.RULES, edit(all_ports=False, port=8449))
        self.assertEqual([o.rule for o in outcomes], ["single"])
        self.assertEqual(rules[2]["target_ip"], OLD)

    def test_port_change(self):
        rules, outcomes = edit_dnat_rules(self.RULES, edit(all_ports=False, port=8443, new_ip=OLD, new_port=9443))
        self.assertEqual(outcomes, [backend_address.RuleOutcome("balanced", Outcome.SKIPPED, SkipReason.SHARED_PORT)])
        rules, outcomes = edit_dnat_rules(self.RULES, edit(all_ports=False, port=8449, new_ip=OLD, new_port=9449))
        self.assertEqual((rules[1]["target_port"], rules[1]["target_ip"]), (9449, OLD))

    def test_delete(self):
        rules, outcomes = edit_dnat_rules(self.RULES, edit(action=Action.DELETE, new_ip=None))
        self.assertEqual(rules[0]["target_ip"], "62.50.146.227,62.50.146.172")
        self.assertEqual([o.outcome for o in outcomes], [Outcome.CHANGED, Outcome.SKIPPED, Outcome.SKIPPED])


class SuggestTests(unittest.TestCase):
    def test_owner_addresses_clean_first_private_hidden(self):
        owner = RelaySnapshot(1, "NL 2", (), frozenset({OLD, NEW, "62.50.146.227", "62.50.146.230", "172.17.0.1", "10.0.0.2"}), 0)
        relay = RelaySnapshot(2, "VK", (
            LossReading(NEW, 8443, 0.0, 44.0, 60), LossReading("62.50.146.227", 8443, 35.0, 45.0, 60),
        ), frozenset({"95.163.209.184"}), 0)
        self.assertEqual(suggest_addresses([owner, relay], OLD), [
            {"ip": NEW, "worst_loss": 0.0},
            {"ip": "62.50.146.227", "worst_loss": 35.0},
            {"ip": "62.50.146.230", "worst_loss": None},
        ])
        self.assertEqual(suggest_addresses([relay], OLD), [])


class PlanTests(unittest.IsolatedAsyncioTestCase):
    """План и сохранение по профилям базы: пропуски не сохраняются, изменения — да."""

    def setUp(self):
        self.haproxy = SimpleNamespace(id=1, name="NL relays", config_content=build_config())
        self.dnat = SimpleNamespace(id=2, name="VK", rules_json=json.dumps(DnatEditTests.RULES))
        self.untouched = SimpleNamespace(id=3, name="Other", rules_json=json.dumps([
            {"name": "x", "protocol": "tcp", "listen_port": 1, "target_ip": "9.9.9.9", "target_port": 0},
        ]))
        self.db = mock.Mock()
        self.db.execute = mock.AsyncMock(side_effect=self._execute)

    async def _execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        rows = {HAProxyConfigProfile: [self.haproxy], DnatProfile: [self.dnat, self.untouched]}.get(entity)
        if rows is not None:
            return SimpleNamespace(scalars=lambda: iter(rows))
        return SimpleNamespace(scalar=lambda: 4)

    async def test_preview_does_not_touch_profiles(self):
        before = (self.haproxy.config_content, self.dnat.rules_json)
        plan = await plan_batch(self.db, [edit()])
        self.assertEqual([(c.kind, c.profile_name, c.servers) for c in plan.items[0]],
                         [("haproxy", "NL relays", 4), ("dnat", "VK", 4)])
        self.assertEqual((self.haproxy.config_content, self.dnat.rules_json), before)

    async def test_save_rewrites_and_marks_pending(self):
        server = SimpleNamespace(dnat_rules_hash="stale", dnat_sync_status="synced")
        with mock.patch.object(backend_address.haproxy_profile_sync, "mark_outdated_pending", mock.AsyncMock()) as mark, \
             mock.patch.object(backend_address.dnat_profile_sync, "ordered_linked_servers", mock.AsyncMock(return_value=[server])):
            plan = await plan_batch(self.db, [edit()], save=True)
        self.assertIn(f"server srv1 {NEW}:8449", self.haproxy.config_content)
        self.assertIn(NEW, json.loads(self.dnat.rules_json)[0]["target_ip"])
        mark.assert_awaited_once()
        self.assertEqual(server.dnat_sync_status, "pending")
        self.assertEqual(plan.changed, [ProfileRef("haproxy", 1, "NL relays"), ProfileRef("dnat", 2, "VK")])

    async def test_edits_see_each_other(self):
        # Три удаления из одного балансировщика: третье оставило бы его пустым
        deletes = [edit(ip=ip, action=Action.DELETE, new_ip=None) for ip in (OLD, "62.50.146.227", NEIGHBOUR)]
        plan = await plan_batch(self.db, deletes)
        lb = [rule for change in plan.items[2] for rule in change.rules if rule.rule == "lb"]
        self.assertEqual(lb, [backend_address.RuleOutcome("lb", Outcome.SKIPPED, SkipReason.LAST_BACKEND)])
        first_lb = [rule for change in plan.items[0] for rule in change.rules if rule.rule == "lb"]
        self.assertEqual(first_lb[0].outcome, Outcome.CHANGED)

    async def test_preview_of_unchanged_profiles_changes_nothing(self):
        plan = await plan_batch(self.db, [edit(ip="10.9.9.9")])
        self.assertEqual((plan.items, plan.changed), ([[]], []))


class RolloutProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_counts_statuses_of_linked_nodes(self):
        db = mock.Mock()
        db.execute = mock.AsyncMock(return_value=SimpleNamespace(
            all=lambda: [("synced", 3), ("pending", 1), (None, 2), ("failed", 1)]))
        progress = await rollout_progress(db, [ProfileRef("haproxy", 1, "NL relays")])
        self.assertEqual(progress, [{
            "kind": "haproxy", "profile_id": 1, "profile_name": "NL relays",
            "total": 7, "synced": 3, "failed": 1, "denied": 0, "pending": 3,
        }])


if __name__ == "__main__":
    unittest.main()

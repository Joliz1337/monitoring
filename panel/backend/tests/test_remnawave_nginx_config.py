"""Tests for the Remnawave nginx config generator.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests
(pytest picks these up too — they are ordinary unittest TestCases.)

Главное свойство — round-trip: generate → parse → generate даёт идентичный
конфиг, иначе CRUD правил на панели молча терял бы данные.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.remnawave_nginx_config import (  # noqa: E402
    AUTO_MARKER,
    DOMAIN_PLACEHOLDER,
    LOCAL_STUB_ROOT,
    GrpcRule,
    MissingMarkersError,
    OptionsValidationError,
    ProfileOptions,
    ProxyRule,
    RuleValidationError,
    XhttpRule,
    detect_domain,
    generate_full_config,
    has_markers,
    parse_rules_from_config,
    render_for_server,
    replace_domain_with_placeholder,
    splice_rules,
    validate_options,
    validate_rules,
)

GRPC_RULES = [
    GrpcRule(name="trgrpc", service_path="trgrpc", port=8443),
    GrpcRule(name="vlgrpc", service_path="vlgrpc", port=8444),
]
XHTTP_RULES = [
    XhttpRule(name="vlxhttp", path="/api/v2/upload/ab12", port=2081),
]
PROXY_RULES = [
    ProxyRule(name="fallback", path="/", target_url="https://example.com"),
]
ALL_RULES = [*GRPC_RULES, *XHTTP_RULES, *PROXY_RULES]


class RoundTripTests(unittest.TestCase):
    def _assert_round_trip(self, options: ProfileOptions):
        config = generate_full_config(options, ALL_RULES)
        parsed = parse_rules_from_config(config)
        self.assertEqual(parsed, ALL_RULES)
        regenerated = generate_full_config(options, parsed)
        self.assertEqual(config, regenerated)

    def test_scheme_direct(self):
        self._assert_round_trip(ProfileOptions())

    def test_scheme_cdn(self):
        self._assert_round_trip(ProfileOptions(
            cdn_enabled=True, cdn_ranges=["173.245.48.0/20", "2400:cb00::/32"],
        ))

    def test_scheme_proxy_protocol(self):
        self._assert_round_trip(ProfileOptions(
            proxy_protocol_enabled=True, proxy_protocol_port=8449, haproxy_ip="10.0.0.1",
        ))

    def test_scheme_universal(self):
        self._assert_round_trip(ProfileOptions(
            cdn_enabled=True, cdn_ranges=["173.245.48.0/20"],
            proxy_protocol_enabled=True, proxy_protocol_port=8449, haproxy_ip="10.0.0.1",
            reject_default_server=True,
        ))

    def test_scheme_with_fallback(self):
        options = ProfileOptions(fallback_url="https://example.com")
        rules = [*GRPC_RULES, *XHTTP_RULES]
        config = generate_full_config(options, rules)
        parsed = parse_rules_from_config(config)
        self.assertEqual(parsed, rules)
        self.assertEqual(config, generate_full_config(options, parsed))

    def test_rule_order_is_preserved(self):
        """Порядок локаций определяет содержимое конфига и хэш синхронизации —
        парсер обязан вернуть правила в том же порядке, а не по типам."""
        rules = [PROXY_RULES[0], XHTTP_RULES[0], GRPC_RULES[0]]
        config = generate_full_config(ProfileOptions(), rules)
        self.assertEqual(parse_rules_from_config(config), rules)

    def test_splice_preserves_manual_edits_outside_markers(self):
        config = generate_full_config(ProfileOptions(), ALL_RULES)
        edited = config.replace("keepalive_timeout 75s;", "keepalive_timeout 90s;")
        new_rules = [*ALL_RULES, GrpcRule(name="vmgrpc", service_path="vmgrpc", port=8445)]
        spliced = splice_rules(edited, new_rules, ProfileOptions())
        self.assertIn("keepalive_timeout 90s;", spliced)
        self.assertEqual(parse_rules_from_config(spliced), new_rules)

    def test_splice_with_same_rules_is_identity(self):
        """Splice обеих секций должен давать байт-в-байт тот же текст, что
        и генерация — иначе каждый CRUD менял бы хэш без причины."""
        config = generate_full_config(ProfileOptions(), ALL_RULES)
        self.assertEqual(splice_rules(config, ALL_RULES, ProfileOptions()), config)

    def test_splice_adds_xhttp_upstream(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES)
        self.assertNotIn("upstream xhttp_", config)
        spliced = splice_rules(config, [*GRPC_RULES, *XHTTP_RULES], ProfileOptions())
        self.assertIn("upstream xhttp_vlxhttp {", spliced)
        self.assertIn("proxy_pass http://xhttp_vlxhttp;", spliced)

    def test_splice_without_upstream_markers_proxies_directly(self):
        """Конфиг, собранный до появления секции UPSTREAMS: правило не может
        ссылаться на несуществующий upstream и проксирует напрямую."""
        config = generate_full_config(ProfileOptions(), GRPC_RULES)
        start = config.find("    # === UPSTREAMS START ===")
        end = config.find("# === UPSTREAMS END ===") + len("# === UPSTREAMS END ===\n")
        legacy = config[:start] + config[end:]
        spliced = splice_rules(legacy, XHTTP_RULES, ProfileOptions())
        self.assertNotIn("upstream xhttp_", spliced)
        self.assertIn("proxy_pass http://127.0.0.1:2081;", spliced)
        self.assertEqual(parse_rules_from_config(spliced), XHTTP_RULES)


class ContentTests(unittest.TestCase):
    def test_grpc_uses_grpc_set_header_overwrite(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES)
        self.assertIn("grpc_set_header X-Forwarded-For $remote_addr;", config)
        self.assertNotIn("$proxy_add_x_forwarded_for", config)

    def test_cdn_switches_to_client_ip(self):
        config = generate_full_config(
            ProfileOptions(cdn_enabled=True, cdn_ranges=["173.245.48.0/20"]), ALL_RULES,
        )
        self.assertIn("grpc_set_header X-Forwarded-For $client_ip;", config)
        self.assertIn("geo $remote_addr $from_edge", config)
        self.assertIn("173.245.48.0/20 1;", config)

    def test_proxy_protocol_listen_and_realip(self):
        config = generate_full_config(
            ProfileOptions(proxy_protocol_enabled=True, proxy_protocol_port=8449,
                           haproxy_ip="10.0.0.1"),
            GRPC_RULES,
        )
        self.assertIn("listen 8449 ssl proxy_protocol", config)
        self.assertIn("set_real_ip_from 10.0.0.1;", config)
        self.assertIn("real_ip_header proxy_protocol;", config)

    def test_proxy_protocol_without_haproxy_ip_trusts_all(self):
        # Пустой IP HAProxy = принимать PP-заголовок от всех, защита — файрвол
        config = generate_full_config(
            ProfileOptions(proxy_protocol_enabled=True, proxy_protocol_port=8449), GRPC_RULES,
        )
        self.assertIn("set_real_ip_from 0.0.0.0/0;", config)

    def test_fallback_generates_locations_and_error_page(self):
        config = generate_full_config(
            ProfileOptions(fallback_url="https://example.com"), GRPC_RULES,
        )
        # Доменная цель проксируется через переменную, чтобы применялся resolver
        self.assertIn("set $rw_upstream https://example.com;", config)
        self.assertIn("proxy_pass $rw_upstream$request_uri;", config)
        self.assertIn("error_page 418 502 503 504 = @fallback;", config)
        self.assertIn("location @fallback {", config)
        self.assertIn("location / {", config)

    def test_no_fallback_no_error_page(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES)
        self.assertNotIn("@fallback", config)

    def test_fallback_conflicts_with_root_proxy_rule(self):
        with self.assertRaises(RuleValidationError):
            generate_full_config(
                ProfileOptions(fallback_url="https://1.2.3.4:8445"),
                ALL_RULES,  # PROXY_RULES содержит path="/"
            )

    def test_host_specific_limits_are_marked_auto(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES)
        for directive in ("worker_rlimit_nofile", "worker_connections", "ssl_session_cache"):
            line = next(l for l in config.splitlines() if l.strip().startswith(directive))
            self.assertIn(AUTO_MARKER, line, f"{directive} должен пересчитываться нодой")

    def test_grpc_stream_not_limited_by_body_size(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES)
        self.assertIn("client_max_body_size 0;", config)

    def test_xhttp_packet_up_headers_fit_in_buffers(self):
        """packet-up умеет нести данные в заголовке — дефолтных буферов мало."""
        config = generate_full_config(ProfileOptions(), XHTTP_RULES)
        self.assertIn("large_client_header_buffers 8 32k;", config)

    def test_response_is_indistinguishable_from_backend(self):
        """Никаких своих заголовков в ответ: клиент должен получать ровно то,
        что отдала бы заглушка при прямом обращении."""
        config = generate_full_config(
            ProfileOptions(fallback_url="https://1.2.3.4:8445"), GRPC_RULES,
        )
        self.assertNotIn("add_header", config)
        self.assertIn("proxy_pass_header Server;", config)

    def test_nginx_own_errors_drop_connection(self):
        """Страницу ошибки с подписью nginx клиент не должен увидеть никогда —
        ни при битом запросе, ни при HTTP на TLS-порт (497), ни при мёртвой
        заглушке (502)."""
        config = generate_full_config(
            ProfileOptions(fallback_url="https://1.2.3.4:8445"), GRPC_RULES,
        )
        error_page = next(l for l in config.splitlines() if "= @drop;" in l)
        for code in ("400", "404", "497", "502", "503", "504"):
            self.assertIn(code, error_page, f"код {code} должен уходить в разрыв")
        self.assertIn("location @drop {", config)
        self.assertIn("return 444;", config)

    def test_default_server_also_drops_plain_http(self):
        """ssl_reject_handshake рвёт только TLS: обычный HTTP на 443 доходит
        до обработки запроса, и default-блоку тоже нужен свой @drop."""
        config = generate_full_config(
            ProfileOptions(reject_default_server=True), GRPC_RULES,
        )
        default_block = config[config.find("ssl_reject_handshake on;"):]
        default_block = default_block[:default_block.find("\n    server {")]
        self.assertIn("= @drop;", default_block)
        self.assertIn("return 444;", default_block)

    def test_fallback_upstream_errors_are_not_intercepted(self):
        """404 самой заглушки обязан дойти до клиента как есть — иначе
        обрыв на несуществующей странице выдал бы прокси."""
        config = generate_full_config(
            ProfileOptions(fallback_url="https://1.2.3.4:8445"), GRPC_RULES,
        )
        self.assertNotIn("proxy_intercept_errors", config)

    def test_drop_exists_whenever_referenced(self):
        """Правила внутри маркеров не должны ссылаться на @drop: их вставляют
        и в конфиги с ручными правками, где этой локации может не быть."""
        for options in (ProfileOptions(), ProfileOptions(fallback_url="https://example.com")):
            config = generate_full_config(options, [*GRPC_RULES, *XHTTP_RULES])
            start = config.find("# === LOCATIONS START ===")
            end = config.find("# === LOCATIONS END ===")
            self.assertNotIn("@drop", config[start:end])
            self.assertIn("location @drop {", config)

    def test_non_grpc_request_on_grpc_path_goes_to_fallback(self):
        """Браузер или сканер по gRPC-пути должен получить сайт, а не ответ
        Xray: путь выглядит как обычная несуществующая страница."""
        config = generate_full_config(
            ProfileOptions(fallback_url="https://example.com"), GRPC_RULES,
        )
        self.assertIn('if ($content_type !~* "^application/grpc") { return 418; }', config)

    def test_non_grpc_request_drops_without_fallback(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES)
        self.assertIn('if ($content_type !~* "^application/grpc") { return 444; }', config)

    def test_proxy_locations_use_http11_and_websocket(self):
        config = generate_full_config(
            ProfileOptions(fallback_url="https://1.2.3.4:8445"), GRPC_RULES,
        )
        self.assertIn("proxy_http_version 1.1;", config)
        self.assertIn("proxy_set_header Upgrade $http_upgrade;", config)
        self.assertIn("proxy_set_header Connection $connection_upgrade;", config)
        self.assertIn("map $http_upgrade $connection_upgrade {", config)

    def test_domain_placeholder_render(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES)
        self.assertIn(f"server_name {DOMAIN_PLACEHOLDER};", config)
        self.assertIn(f"/etc/letsencrypt/live/{DOMAIN_PLACEHOLDER}/fullchain.pem", config)
        rendered = render_for_server(config, "node1.example.com")
        self.assertNotIn(DOMAIN_PLACEHOLDER, rendered)
        self.assertIn("server_name node1.example.com;", rendered)
        self.assertIn("/etc/letsencrypt/live/node1.example.com/fullchain.pem", rendered)


class XhttpTests(unittest.TestCase):
    """XHTTP-правило обслуживает все режимы транспорта одной локацией."""

    @staticmethod
    def _xhttp_section(config: str) -> str:
        return config[config.find("# rule: vlxhttp"):config.find("# === LOCATIONS END")]

    def test_grpc_typed_modes_get_full_duplex(self):
        # stream-one и аплоад stream-up приходят с application/grpc
        config = generate_full_config(ProfileOptions(), XHTTP_RULES)
        self.assertIn("location ^~ /api/v2/upload/ab12 {", config)
        self.assertIn("grpc_pass grpc://127.0.0.1:2081;", config)
        self.assertIn('if ($content_type !~* "^application/grpc") { return 418; }', config)

    def test_plain_mode_streams_without_buffering(self):
        # packet-up и даунлоад-стримы: буферизация в обе стороны их ломает
        config = generate_full_config(ProfileOptions(), XHTTP_RULES)
        self.assertIn("error_page 418 = @xhttp_vlxhttp;", config)
        self.assertIn("location @xhttp_vlxhttp {", config)
        self.assertIn("proxy_http_version 1.1;", self._xhttp_section(config))
        self.assertIn("proxy_request_buffering off;", config)
        self.assertIn("proxy_buffering off;", config)

    def test_plain_mode_reuses_upstream_connections(self):
        """packet-up — отдельный POST на каждый чанк; без пула это новый
        TCP-коннект к loopback на каждый пост и TIME_WAIT на стороне nginx."""
        config = generate_full_config(ProfileOptions(), XHTTP_RULES)
        self.assertIn("upstream xhttp_vlxhttp {", config)
        self.assertIn("server 127.0.0.1:2081;", config)
        self.assertIn("keepalive 64;", config)
        section = self._xhttp_section(config)
        self.assertIn("proxy_pass http://xhttp_vlxhttp;", section)
        self.assertIn('proxy_set_header Connection "";', section)

    def test_long_streams_are_not_cut_by_default_timeouts(self):
        # Дефолтные 60 с рвали бы даунлоад-стрим и простаивающий stream-one
        section = self._xhttp_section(generate_full_config(ProfileOptions(), XHTTP_RULES))
        for directive in ("grpc_read_timeout", "grpc_send_timeout",
                          "proxy_read_timeout", "proxy_send_timeout"):
            self.assertIn(f"{directive} 1h;", section)

    def test_body_limit_overridden_in_both_locations(self):
        """Блок вставляют и в чужие конфиги с унаследованным лимитом 1m —
        аплоад stream-up умер бы на первом мегабайте с 413."""
        section = self._xhttp_section(generate_full_config(ProfileOptions(), XHTTP_RULES))
        self.assertEqual(section.count("client_max_body_size 0;"), 2)

    def test_probe_and_dead_xray_get_fallback_site(self):
        """Голый 404 от Xray на угаданном пути выдал бы, что там не сайт."""
        config = generate_full_config(
            ProfileOptions(fallback_url="https://example.com"), XHTTP_RULES,
        )
        self.assertIn("grpc_intercept_errors on;", config)
        self.assertIn("proxy_intercept_errors on;", config)
        fallback_lines = [l for l in self._xhttp_section(config).splitlines() if "= @fallback;" in l]
        self.assertEqual(len(fallback_lines), 2)
        for line in fallback_lines:
            for code in ("404", "405", "502", "503", "504"):
                self.assertIn(code, line)

    def test_xray_client_errors_pass_through(self):
        """400/409/413 — ответы Xray своему клиенту (рассинхрон сессии,
        коллизия, большой пост); подменённые заглушкой, они оставили бы
        клиента с 200 и HTML вместо причины."""
        config = generate_full_config(
            ProfileOptions(fallback_url="https://example.com"), XHTTP_RULES,
        )
        error_pages = [l for l in self._xhttp_section(config).splitlines() if "error_page" in l]
        for code in ("400", "409", "413"):
            for line in error_pages:
                self.assertNotIn(f" {code} ", f" {line.split('=')[0]} ")

    def test_nginx_own_errors_drop_via_own_location(self):
        """Своя drop-локация: правило вставляют и в чужие конфиги, где @drop
        нет, а собственный error_page отменяет наследование серверного."""
        for options in (ProfileOptions(), ProfileOptions(fallback_url="https://example.com")):
            config = generate_full_config(options, XHTTP_RULES)
            section = self._xhttp_section(config)
            self.assertIn("location @xhttp_vlxhttp_drop {", section)
            drop_lines = [l for l in section.splitlines() if "= @xhttp_vlxhttp_drop;" in l]
            own_error_lines = [l for l in drop_lines if "404" not in l]
            self.assertEqual(len(own_error_lines), 2)
            for line in own_error_lines:
                for code in ("497", "500", "408"):
                    self.assertIn(code, line)

    def test_without_fallback_probe_is_dropped(self):
        config = generate_full_config(ProfileOptions(), XHTTP_RULES)
        self.assertNotIn("@fallback", config)
        self.assertIn("error_page 404 405 502 503 504 = @xhttp_vlxhttp_drop;", config)

    def test_real_ip_header_is_overwritten(self):
        config = generate_full_config(
            ProfileOptions(cdn_enabled=True, cdn_ranges=["173.245.48.0/20"]), XHTTP_RULES,
        )
        self.assertIn("grpc_set_header X-Forwarded-For $client_ip;", config)
        self.assertIn("proxy_set_header X-Forwarded-For $client_ip;", config)
        self.assertNotIn("$proxy_add_x_forwarded_for", config)


class TlsAndConnectionsTests(unittest.TestCase):
    """Опции группы «TLS и соединения» живут вне маркеров и не влияют на парсер."""

    @staticmethod
    def _listen_lines(config: str) -> list[str]:
        return [l.strip() for l in config.splitlines() if l.strip().startswith("listen ")]

    def test_session_tickets_default_on(self):
        # TLS 1.3 возобновляет сессию только через тикеты — без них каждое
        # переподключение мобильного клиента стоит полного рукопожатия
        config = generate_full_config(ProfileOptions(), GRPC_RULES)
        self.assertIn("ssl_session_tickets on;", config)
        self.assertNotIn("ssl_session_tickets off;", config)

    def test_session_tickets_can_be_disabled(self):
        config = generate_full_config(ProfileOptions(tls_session_tickets=False), GRPC_RULES)
        self.assertIn("ssl_session_tickets off;", config)
        self.assertNotIn("ssl_session_tickets on;", config)

    def test_client_keepalive_on_all_tls_listens(self):
        """Клиентские сокеты держит nginx, а не Xray: keepalive нужен на каждой
        TLS-listen, включая PP-порт, но не на редиректе 80."""
        config = generate_full_config(
            ProfileOptions(proxy_protocol_enabled=True, proxy_protocol_port=8449), GRPC_RULES,
        )
        self.assertIn("listen 443 ssl so_keepalive=30s:10s:3;", config)
        self.assertIn("listen 8449 ssl proxy_protocol so_keepalive=30s:10s:3;", config)
        self.assertIn("listen 80;", self._listen_lines(config))

    def test_client_keepalive_empty_omits_parameter(self):
        config = generate_full_config(ProfileOptions(client_tcp_keepalive=""), GRPC_RULES)
        self.assertNotIn("so_keepalive", config)
        self.assertIn("listen 443 ssl;", config)

    def test_client_keepalive_bad_format_rejected(self):
        for bad in ("30:10", "abc", "30s:10s:0", "30s:10s:101", "30h:10s:3"):
            with self.subTest(value=bad), self.assertRaises(OptionsValidationError):
                validate_options(ProfileOptions(client_tcp_keepalive=bad))

    def test_client_keepalive_accepts_minutes(self):
        validate_options(ProfileOptions(client_tcp_keepalive="1m:30s:5"))

    def test_access_log_disabled_by_default(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES)
        http_block = config[config.find("http {"):config.find("# === UPSTREAMS START")]
        self.assertIn("access_log off;", http_block)

    def test_access_log_can_be_enabled(self):
        config = generate_full_config(ProfileOptions(access_log_enabled=True), GRPC_RULES)
        http_block = config[config.find("http {"):config.find("# === UPSTREAMS START")]
        self.assertNotIn("access_log", http_block)

    def test_options_round_trip_dict(self):
        options = ProfileOptions(
            tls_session_tickets=False, client_tcp_keepalive="1m:30s:5", access_log_enabled=True,
        )
        self.assertEqual(ProfileOptions.from_dict(options.to_dict()), options)

    def test_legacy_options_get_new_defaults(self):
        # Старый профиль без новых ключей в JSON получает дефолты при
        # следующем сохранении опций / «Вставить шаблон»
        legacy = ProfileOptions.from_dict({"reject_default_server": True})
        self.assertTrue(legacy.tls_session_tickets)
        self.assertEqual(legacy.client_tcp_keepalive, "30s:10s:3")
        self.assertFalse(legacy.access_log_enabled)


class StabilityDirectivesTests(unittest.TestCase):
    """Директивы стабильности в http/main-контексте — вне маркеров, парсер их
    не касается; на gRPC/WS/проксирование они не влияют."""

    def test_worker_shutdown_timeout_bounds_old_worker_generations(self):
        # Без потолка старые воркеры живут до grpc_read_timeout 1h после reload
        config = generate_full_config(ProfileOptions(), GRPC_RULES)
        self.assertIn("worker_shutdown_timeout 60s;", config)

    def test_client_keepalive_requests_raised_for_packet_up(self):
        # packet-up выбирает 10000 за минуты и провоцирует шторм рукопожатий
        config = generate_full_config(ProfileOptions(), XHTTP_RULES)
        self.assertIn("keepalive_requests 1000000;", config)
        self.assertNotIn("keepalive_requests 10000;", config)

    def test_grpc_block_unchanged_by_keepalive_requests(self):
        # У gRPC одно долгое соединение — лимит запросов ему безразличен,
        # блок остаётся прежним
        config = generate_full_config(ProfileOptions(), GRPC_RULES)
        self.assertIn("grpc_pass grpc://127.0.0.1:8443;", config)
        self.assertIn("grpc_read_timeout 1h;", config)

    def test_resolver_present_with_ipv6_off(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES)
        self.assertIn("resolver ", config)
        self.assertIn("ipv6=off", config)


class UpstreamKeepaliveTests(unittest.TestCase):
    def test_upstream_keepalive_is_marked_auto(self):
        # Пул захардкожен безопасным минимумом, нода считает его от worker_connections
        config = generate_full_config(ProfileOptions(), XHTTP_RULES)
        line = next(l for l in config.splitlines() if l.strip().startswith("keepalive ")
                    and "keepalive_requests" not in l)
        self.assertIn("keepalive 64;", line)
        self.assertIn(AUTO_MARKER, line)

    def test_upstream_keepalive_requests_not_marked(self):
        # keepalive_requests в upstream — счётчик запросов, от размера хоста не
        # зависит, маркером не помечен
        config = generate_full_config(ProfileOptions(), XHTTP_RULES)
        line = next(l for l in config.splitlines() if "keepalive_requests 100000;" in l)
        self.assertNotIn(AUTO_MARKER, line)

    def test_xhttp_plain_location_reuses_loopback_socket(self):
        section = XhttpTests._xhttp_section(generate_full_config(ProfileOptions(), XHTTP_RULES))
        self.assertIn("proxy_socket_keepalive on;", section)


class ProxyTargetResolutionTests(unittest.TestCase):
    """Доменная цель проксируется через переменную (resolver перечитывает DNS)
    и с SNI; цель-IP и домен с путём — литеральным proxy_pass без изменений."""

    def _proxy_section(self, config: str) -> str:
        return config[config.find("# rule:"):config.find("# === LOCATIONS END")]

    def test_domain_target_uses_variable_and_sni(self):
        rules = [ProxyRule(name="site", path="/site", target_url="https://cdn.example.com")]
        section = self._proxy_section(generate_full_config(ProfileOptions(), rules))
        self.assertIn("set $rw_upstream https://cdn.example.com;", section)
        self.assertIn("proxy_pass $rw_upstream$request_uri;", section)
        self.assertIn("proxy_ssl_server_name on;", section)
        self.assertIn("proxy_ssl_name cdn.example.com;", section)

    def test_domain_target_round_trips(self):
        rules = [ProxyRule(name="site", path="/site", target_url="https://cdn.example.com")]
        config = generate_full_config(ProfileOptions(), rules)
        self.assertEqual(parse_rules_from_config(config), rules)
        self.assertEqual(generate_full_config(ProfileOptions(), parse_rules_from_config(config)), config)

    def test_ip_target_unchanged_no_sni(self):
        rules = [ProxyRule(name="ip", path="/ip", target_url="https://1.2.3.4:8445")]
        section = self._proxy_section(generate_full_config(ProfileOptions(), rules))
        self.assertIn("proxy_pass https://1.2.3.4:8445;", section)
        self.assertNotIn("set $rw_upstream", section)
        self.assertNotIn("proxy_ssl_server_name", section)

    def test_domain_target_with_path_stays_literal(self):
        # Переменная сломала бы подстановку URI локации — остаёмся на литерале,
        # SNI при этом добавляем
        rules = [ProxyRule(name="base", path="/base", target_url="https://cdn.example.com/app")]
        section = self._proxy_section(generate_full_config(ProfileOptions(), rules))
        self.assertIn("proxy_pass https://cdn.example.com/app;", section)
        self.assertNotIn("set $rw_upstream", section)
        self.assertIn("proxy_ssl_name cdn.example.com;", section)

    def test_http_domain_target_gets_no_proxy_ssl(self):
        rules = [ProxyRule(name="plain", path="/p", target_url="http://cdn.example.com")]
        section = self._proxy_section(generate_full_config(ProfileOptions(), rules))
        self.assertIn("set $rw_upstream http://cdn.example.com;", section)
        self.assertNotIn("proxy_ssl", section)

    def test_ip_fallback_stays_literal(self):
        config = generate_full_config(ProfileOptions(fallback_url="https://1.2.3.4:8445"), GRPC_RULES)
        self.assertIn("proxy_pass https://1.2.3.4:8445;", config)
        self.assertNotIn("set $rw_upstream", config)
        self.assertNotIn("proxy_ssl_server_name", config)


class LocalStubTests(unittest.TestCase):
    def test_disabled_by_default(self):
        config = generate_full_config(ProfileOptions(fallback_url="https://example.com"), GRPC_RULES)
        self.assertNotIn("@stub", config)

    def test_enabled_routes_stub_errors_to_static_page(self):
        config = generate_full_config(
            ProfileOptions(fallback_url="https://example.com", local_stub_enabled=True), GRPC_RULES,
        )
        self.assertIn("error_page 502 503 504 = @stub;", config)
        self.assertIn("location @stub {", config)
        self.assertIn(f"root {LOCAL_STUB_ROOT};", config)

    def test_options_round_trip_dict(self):
        options = ProfileOptions(local_stub_enabled=True)
        self.assertEqual(ProfileOptions.from_dict(options.to_dict()), options)

    def test_legacy_options_default_stub_off(self):
        legacy = ProfileOptions.from_dict({"reject_default_server": True})
        self.assertFalse(legacy.local_stub_enabled)


class ValidationTests(unittest.TestCase):
    def test_trust_everyone_allowed(self):
        # Осознанный выбор оператора: 0.0.0.0/0 допустим (заголовок с IP
        # будет приниматься от любого источника)
        validate_options(ProfileOptions(cdn_enabled=True, cdn_ranges=["0.0.0.0/0"]))
        validate_options(ProfileOptions(cdn_enabled=True, cdn_ranges=["::/0"]))

    def test_bad_cidr_rejected(self):
        with self.assertRaises(OptionsValidationError):
            validate_options(ProfileOptions(cdn_enabled=True, cdn_ranges=["not-a-cidr"]))

    def test_pp_bad_haproxy_ip_rejected(self):
        with self.assertRaises(OptionsValidationError):
            validate_options(ProfileOptions(proxy_protocol_enabled=True, haproxy_ip="not-an-ip"))

    def test_bad_fallback_url_rejected(self):
        with self.assertRaises(OptionsValidationError):
            validate_options(ProfileOptions(fallback_url="ftp://bad"))

    def test_pp_port_cannot_clash_with_public_ports(self):
        with self.assertRaises(OptionsValidationError):
            validate_options(ProfileOptions(
                proxy_protocol_enabled=True, proxy_protocol_port=443, haproxy_ip="10.0.0.1",
            ))

    def test_duplicate_rule_names_rejected(self):
        with self.assertRaises(RuleValidationError):
            validate_rules([
                GrpcRule(name="a", service_path="x", port=8443),
                ProxyRule(name="a", path="/", target_url="https://1.2.3.4"),
            ])

    def test_duplicate_paths_rejected(self):
        with self.assertRaises(RuleValidationError):
            validate_rules([
                XhttpRule(name="a", path="/xh", port=2081),
                ProxyRule(name="b", path="/xh", target_url="https://1.2.3.4"),
            ])

    def test_bad_service_path_rejected(self):
        with self.assertRaises(RuleValidationError):
            validate_rules([GrpcRule(name="a", service_path="x y", port=8443)])

    def test_xhttp_path_must_be_absolute_and_not_root(self):
        with self.assertRaises(RuleValidationError):
            validate_rules([XhttpRule(name="a", path="api/upload", port=2081)])
        with self.assertRaises(RuleValidationError):
            validate_rules([XhttpRule(name="a", path="/", port=2081)])


class ImportHelpersTests(unittest.TestCase):
    USER_CONFIG = """
    server {
        listen 443 ssl;
        server_name example.com;
        ssl_certificate /etc/letsencrypt/live/example.com/fullchain.pem;
    }
    """

    def test_detect_domain(self):
        self.assertEqual(detect_domain(self.USER_CONFIG), "example.com")
        self.assertIsNone(detect_domain("server_name _;"))

    def test_replace_domain_with_placeholder(self):
        result = replace_domain_with_placeholder(self.USER_CONFIG, "example.com")
        self.assertNotIn("example.com", result)
        self.assertIn(f"server_name {DOMAIN_PLACEHOLDER};", result)

    def test_imported_config_without_markers(self):
        self.assertFalse(has_markers(self.USER_CONFIG))
        with self.assertRaises(MissingMarkersError):
            parse_rules_from_config(self.USER_CONFIG)


class WildcardDomainTests(unittest.TestCase):
    OPTIONS = ProfileOptions(wildcard_domain="example.com")

    def test_server_name_accepts_domain_and_all_subdomains(self):
        config = generate_full_config(self.OPTIONS, GRPC_RULES)
        self.assertEqual(config.count("server_name example.com *.example.com;"), 2)
        self.assertNotIn(DOMAIN_PLACEHOLDER, config)

    def test_cert_paths_use_base_domain(self):
        config = generate_full_config(self.OPTIONS, [])
        self.assertIn("ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;", config)
        self.assertIn("ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;", config)

    def test_rendered_config_is_identity_without_placeholder(self):
        config = generate_full_config(self.OPTIONS, GRPC_RULES)
        self.assertEqual(render_for_server(config, "node1.example.com"), config)

    def test_round_trip_keeps_rules(self):
        config = generate_full_config(self.OPTIONS, ALL_RULES)
        self.assertEqual(parse_rules_from_config(config), ALL_RULES)
        self.assertEqual(generate_full_config(self.OPTIONS, ALL_RULES), config)

    def test_empty_domain_keeps_placeholder(self):
        config = generate_full_config(ProfileOptions(), [])
        self.assertIn(f"server_name {DOMAIN_PLACEHOLDER};", config)

    def test_from_dict_normalizes_domain(self):
        options = ProfileOptions.from_dict({"wildcard_domain": "  Example.COM "})
        self.assertEqual(options.wildcard_domain, "example.com")
        self.assertEqual(ProfileOptions.from_dict({"wildcard_domain": None}).wildcard_domain, "")

    def test_rejects_star_prefix_and_invalid_domain(self):
        with self.assertRaises(OptionsValidationError):
            validate_options(ProfileOptions(wildcard_domain="*.example.com"))
        with self.assertRaises(OptionsValidationError):
            validate_options(ProfileOptions(wildcard_domain="not a domain"))


if __name__ == "__main__":
    unittest.main()

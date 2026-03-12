"""Parser for Xray subscription URLs and individual keys (vless, vmess, trojan, shadowsocks)."""

import base64
import json
import logging
import re
from urllib.parse import urlparse, parse_qs, unquote

import httpx

logger = logging.getLogger(__name__)

SUPPORTED_PROTOCOLS = {"vless", "vmess", "trojan", "ss"}


def parse_vless(uri: str) -> dict | None:
    """Parse vless://uuid@host:port?params#name"""
    try:
        without_scheme = uri[len("vless://"):]
        fragment = ""
        if "#" in without_scheme:
            without_scheme, fragment = without_scheme.rsplit("#", 1)
            fragment = unquote(fragment)

        user_info, rest = without_scheme.split("@", 1)
        uuid = user_info

        query_str = ""
        if "?" in rest:
            host_port, query_str = rest.split("?", 1)
        else:
            host_port = rest

        if host_port.startswith("["):
            bracket_end = host_port.index("]")
            host = host_port[1:bracket_end]
            port = int(host_port[bracket_end + 2:])
        else:
            host, port_str = host_port.rsplit(":", 1)
            port = int(port_str)

        params = parse_qs(query_str, keep_blank_values=True)
        flat_params = {k: v[0] for k, v in params.items()}

        config = {"id": uuid, **flat_params}

        return {
            "name": fragment or f"{host}:{port}",
            "protocol": "vless",
            "address": host,
            "port": port,
            "config": config,
        }
    except Exception as e:
        logger.debug(f"Failed to parse VLESS URI: {e}")
        return None


def parse_vmess(uri: str) -> dict | None:
    """Parse vmess://base64json"""
    try:
        encoded = uri[len("vmess://"):]
        padding = 4 - len(encoded) % 4
        if padding != 4:
            encoded += "=" * padding
        decoded = base64.b64decode(encoded).decode("utf-8")
        data = json.loads(decoded)

        host = data.get("add", "")
        port = int(data.get("port", 0))
        name = data.get("ps", "") or f"{host}:{port}"

        config = {
            "id": data.get("id", ""),
            "alterId": int(data.get("aid", 0)),
            "security": data.get("scy", "auto"),
            "net": data.get("net", "tcp"),
            "type": data.get("type", "none"),
            "host": data.get("host", ""),
            "path": data.get("path", ""),
            "tls": data.get("tls", ""),
            "sni": data.get("sni", ""),
            "alpn": data.get("alpn", ""),
            "fp": data.get("fp", ""),
        }

        return {
            "name": name,
            "protocol": "vmess",
            "address": host,
            "port": port,
            "config": config,
        }
    except Exception as e:
        logger.debug(f"Failed to parse VMess URI: {e}")
        return None


def parse_trojan(uri: str) -> dict | None:
    """Parse trojan://password@host:port?params#name"""
    try:
        without_scheme = uri[len("trojan://"):]
        fragment = ""
        if "#" in without_scheme:
            without_scheme, fragment = without_scheme.rsplit("#", 1)
            fragment = unquote(fragment)

        password, rest = without_scheme.split("@", 1)

        query_str = ""
        if "?" in rest:
            host_port, query_str = rest.split("?", 1)
        else:
            host_port = rest

        if host_port.startswith("["):
            bracket_end = host_port.index("]")
            host = host_port[1:bracket_end]
            port = int(host_port[bracket_end + 2:])
        else:
            host, port_str = host_port.rsplit(":", 1)
            port = int(port_str)

        params = parse_qs(query_str, keep_blank_values=True)
        flat_params = {k: v[0] for k, v in params.items()}

        config = {"password": password, **flat_params}

        return {
            "name": fragment or f"{host}:{port}",
            "protocol": "trojan",
            "address": host,
            "port": port,
            "config": config,
        }
    except Exception as e:
        logger.debug(f"Failed to parse Trojan URI: {e}")
        return None


def parse_shadowsocks(uri: str) -> dict | None:
    """Parse ss://base64(method:password)@host:port#name or SIP002 format."""
    try:
        without_scheme = uri[len("ss://"):]
        fragment = ""
        if "#" in without_scheme:
            without_scheme, fragment = without_scheme.rsplit("#", 1)
            fragment = unquote(fragment)

        if "@" in without_scheme:
            user_part, host_part = without_scheme.split("@", 1)
            try:
                padding = 4 - len(user_part) % 4
                if padding != 4:
                    user_part += "=" * padding
                decoded_user = base64.b64decode(user_part).decode("utf-8")
                method, password = decoded_user.split(":", 1)
            except Exception:
                method, password = user_part.split(":", 1)
        else:
            padding = 4 - len(without_scheme) % 4
            if padding != 4:
                without_scheme += "=" * padding
            decoded = base64.b64decode(without_scheme).decode("utf-8")
            user_part, host_part = decoded.split("@", 1)
            method, password = user_part.split(":", 1)

        if host_part.startswith("["):
            bracket_end = host_part.index("]")
            host = host_part[1:bracket_end]
            port = int(host_part[bracket_end + 2:])
        else:
            host, port_str = host_part.rsplit(":", 1)
            port = int(port_str)

        config = {"method": method, "password": password}

        return {
            "name": fragment or f"{host}:{port}",
            "protocol": "shadowsocks",
            "address": host,
            "port": port,
            "config": config,
        }
    except Exception as e:
        logger.debug(f"Failed to parse Shadowsocks URI: {e}")
        return None


_PARSERS = {
    "vless://": parse_vless,
    "vmess://": parse_vmess,
    "trojan://": parse_trojan,
    "ss://": parse_shadowsocks,
}

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?)+$")


def is_valid_server(address: str, port: int) -> bool:
    """Check if address looks like a real IP or domain and port is valid."""
    if not address or not port or port < 1 or port > 65535:
        return False
    address = address.strip()
    if not address:
        return False
    if _IP_RE.match(address):
        return True
    if _DOMAIN_RE.match(address):
        return True
    if ":" in address:
        return True  # IPv6
    return False


def parse_single_key(line: str) -> dict | None:
    """Try to parse a single key line."""
    line = line.strip()
    if not line:
        return None
    for prefix, parser in _PARSERS.items():
        if line.startswith(prefix):
            return parser(line)
    return None


def parse_keys(text: str) -> list[dict]:
    """Parse raw text containing multiple keys (one per line)."""
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = parse_single_key(line)
        if parsed:
            parsed["raw_key"] = line
            results.append(parsed)
    return results


def _try_decode_base64(text: str) -> str | None:
    """Attempt base64 decode of subscription body."""
    text = text.strip()
    try:
        padding = 4 - len(text) % 4
        if padding != 4:
            text += "=" * padding
        decoded = base64.b64decode(text).decode("utf-8")
        if any(decoded.startswith(p) for p in _PARSERS):
            return decoded
        if "\n" in decoded and any(
            line.strip().startswith(p) for line in decoded.splitlines() for p in _PARSERS
        ):
            return decoded
    except Exception:
        pass
    return None


_SKIP_PROTOCOLS = {"freedom", "blackhole", "dns", "loopback"}


def _extract_from_xray_outbound(outbound: dict, remarks: str = "") -> dict | None:
    """Extract server info from a full xray-core outbound config into flat params."""
    protocol = outbound.get("protocol", "").lower()
    if protocol in _SKIP_PROTOCOLS or not protocol:
        return None

    settings = outbound.get("settings", {})
    stream = outbound.get("streamSettings", {})
    address = ""
    port = 0
    config: dict = {}

    if protocol in ("vless", "vmess"):
        vnext = settings.get("vnext", [])
        if not vnext:
            return None
        srv = vnext[0]
        address = srv.get("address", "")
        port = int(srv.get("port", 0))
        users = srv.get("users", [])
        if users:
            u = users[0]
            config["id"] = u.get("id", "")
            if protocol == "vless":
                config["encryption"] = u.get("encryption", "none")
                if u.get("flow"):
                    config["flow"] = u["flow"]
            else:
                config["alterId"] = int(u.get("alterId", 0))
                config["security"] = u.get("security", "auto")

    elif protocol in ("trojan",):
        srvs = settings.get("servers", [])
        if not srvs:
            return None
        srv = srvs[0]
        address = srv.get("address", "")
        port = int(srv.get("port", 0))
        config["password"] = srv.get("password", "")

    elif protocol in ("shadowsocks", "ss"):
        protocol = "shadowsocks"
        srvs = settings.get("servers", [])
        if not srvs:
            return None
        srv = srvs[0]
        address = srv.get("address", srv.get("server", ""))
        port = int(srv.get("port", srv.get("server_port", 0)))
        config["method"] = srv.get("method", "")
        config["password"] = srv.get("password", "")

    else:
        return None

    if not address or not port:
        return None

    network = stream.get("network", "tcp")
    security = stream.get("security", "none")
    config["net"] = network

    if security == "reality":
        config["security"] = "reality"
        rs = stream.get("realitySettings", {})
        config["sni"] = rs.get("serverName", "")
        config["pbk"] = rs.get("publicKey", "")
        config["sid"] = rs.get("shortId", "")
        config["spx"] = rs.get("spiderX", "")
        config["fp"] = rs.get("fingerprint", "chrome")
    elif security == "tls":
        config["security"] = "tls"
        ts = stream.get("tlsSettings", {})
        config["sni"] = ts.get("serverName", "")
        config["fp"] = ts.get("fingerprint", "")
        alpn = ts.get("alpn", [])
        if isinstance(alpn, list) and alpn:
            config["alpn"] = ",".join(alpn)
        elif isinstance(alpn, str) and alpn:
            config["alpn"] = alpn
        if ts.get("allowInsecure"):
            config["allowInsecure"] = "1"
    else:
        config["security"] = "none"

    if network == "ws":
        ws_s = stream.get("wsSettings", {})
        config["path"] = ws_s.get("path", "/")
        host = ws_s.get("headers", {}).get("Host", "")
        if host:
            config["host"] = host
    elif network == "grpc":
        grpc_s = stream.get("grpcSettings", {})
        config["serviceName"] = grpc_s.get("serviceName", "")
    elif network in ("h2", "http"):
        http_s = stream.get("httpSettings", {})
        config["path"] = http_s.get("path", "/")
        hosts = http_s.get("host", [])
        if hosts:
            config["host"] = hosts[0] if isinstance(hosts, list) else hosts
    elif network == "tcp":
        tcp_s = stream.get("tcpSettings", {})
        header = tcp_s.get("header", {})
        if header.get("type") == "http":
            config["headerType"] = "http"
            paths = header.get("request", {}).get("path", [])
            if paths:
                config["path"] = paths[0]
    elif network in ("splithttp", "xhttp"):
        ns = stream.get(f"{network}Settings", {})
        config["path"] = ns.get("path", "/")
        if ns.get("host"):
            config["host"] = ns["host"]

    tag = outbound.get("tag", "")
    name = remarks or tag or f"{address}:{port}"

    return {
        "name": name,
        "protocol": protocol,
        "address": str(address),
        "port": int(port),
        "config": config,
    }


def _extract_simple_server(item: dict) -> dict | None:
    """Parse a flat server object with top-level address/port/protocol fields."""
    protocol = item.get("protocol", item.get("type", "")).lower()
    address = item.get("address", item.get("server", item.get("add", "")))
    port = item.get("port", item.get("server_port", 0))
    name = item.get("tag", item.get("ps", item.get("name", "")))

    if not protocol or not address or not port:
        return None

    proto_map = {"ss": "shadowsocks"}
    return {
        "name": name or f"{address}:{port}",
        "protocol": proto_map.get(protocol, protocol),
        "address": str(address),
        "port": int(port),
        "config": item,
        "raw_key": json.dumps(item, ensure_ascii=False),
    }


def _servers_from_full_config(data: dict) -> list[dict]:
    """Extract proxy servers from a single full xray-core config JSON."""
    remarks = data.get("remarks", "")
    servers = []
    for ob in data.get("outbounds", []):
        if not isinstance(ob, dict):
            continue
        srv = _extract_from_xray_outbound(ob, remarks)
        if srv:
            servers.append(srv)
    return servers


def _try_parse_json_subscription(text: str) -> list[dict] | None:
    """Parse JSON subscription: full xray configs, arrays of configs, or simple server objects."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None

    servers: list[dict] = []

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            if "outbounds" in item:
                servers.extend(_servers_from_full_config(item))
            else:
                srv = _extract_from_xray_outbound(item)
                if not srv:
                    srv = _extract_simple_server(item)
                if srv:
                    servers.append(srv)

    elif isinstance(data, dict):
        if "outbounds" in data:
            servers = _servers_from_full_config(data)
        else:
            srv = _extract_from_xray_outbound(data)
            if not srv:
                srv = _extract_simple_server(data)
            if srv:
                servers.append(srv)

    return servers if servers else None


async def fetch_subscription(url: str) -> list[dict]:
    """Fetch a subscription URL and parse all keys from it."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, verify=False) as client:
        resp = await client.get(url, headers={"User-Agent": "v2rayN/6.0"})
        resp.raise_for_status()
        body = resp.text.strip()

    json_result = _try_parse_json_subscription(body)
    if json_result:
        return json_result

    decoded = _try_decode_base64(body)
    if decoded:
        return parse_keys(decoded)

    direct = parse_keys(body)
    if direct:
        return direct

    raise ValueError("Could not parse subscription: unknown format")

from __future__ import annotations

import ipaddress
from collections.abc import Iterable


def _parse_ip(value: str):
    raw = str(value or "").strip().strip("[]").split("%", 1)[0]
    try:
        address = ipaddress.ip_address(raw)
        if getattr(address, "ipv4_mapped", None):
            return address.ipv4_mapped
        return address
    except ValueError:
        return None


def parse_trusted_proxy_cidrs(raw: str | Iterable[str]) -> tuple:
    values = raw.split(",") if isinstance(raw, str) else raw
    networks = []
    for value in values:
        entry = str(value or "").strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _trusted(address, networks: tuple) -> bool:
    return address is not None and any(address.version == network.version and address in network for network in networks)


def resolve_client_ip(peer: str | None, forwarded_for: str | None, trusted_proxy_cidrs: str | Iterable[str] = ()) -> str:
    """Resolve client address without trusting X-Forwarded-For from untrusted peers."""
    peer_ip = _parse_ip(peer or "")
    if peer_ip is None:
        return "unknown"
    networks = parse_trusted_proxy_cidrs(trusted_proxy_cidrs)
    if not _trusted(peer_ip, networks):
        return str(peer_ip)

    raw_items = [item.strip() for item in str(forwarded_for or "").split(",") if item.strip()]
    if not raw_items:
        return str(peer_ip)
    chain = [_parse_ip(item) for item in raw_items]
    if any(address is None for address in chain):
        return str(peer_ip)
    chain.append(peer_ip)
    while len(chain) > 1 and _trusted(chain[-1], networks):
        chain.pop()
    return str(chain[-1])

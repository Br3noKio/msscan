"""Parser da saída de ``ip -j addr show`` (iproute2)."""

from __future__ import annotations

import json
from typing import Any

from msscan.parsers import ErroParser


def parse_ip_addr_json(texto: str) -> list[dict[str, Any]]:
    """Converte ``ip -j addr show`` numa lista de interfaces.

    Cada interface: ``nome``, ``estado`` (operstate: UP/DOWN/UNKNOWN),
    ``tipo_link`` (ex. "ether", "loopback") e ``enderecos``
    (``{"familia", "endereco", "prefixo", "escopo"}``).
    """
    if not texto.strip():
        return []
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError as erro:
        raise ErroParser(f"saída do 'ip -j addr' não é JSON válido: {erro}") from erro
    if not isinstance(dados, list):
        raise ErroParser("saída do 'ip -j addr' não é uma lista")
    interfaces = []
    for item in dados:
        if not isinstance(item, dict) or "ifname" not in item:
            continue
        enderecos = [
            {
                "familia": a.get("family", ""),
                "endereco": a.get("local", ""),
                "prefixo": a.get("prefixlen"),
                "escopo": a.get("scope", ""),
            }
            for a in item.get("addr_info", []) or []
            if isinstance(a, dict)
        ]
        interfaces.append(
            {
                "nome": item["ifname"],
                "estado": item.get("operstate", ""),
                "tipo_link": item.get("link_type", ""),
                "enderecos": enderecos,
            }
        )
    return interfaces


def parse_ip_link_estatisticas(texto: str) -> dict[str, dict[str, Any]]:
    """Interpreta ``ip -j -s link show``: ``{interface: {"estado", "rx": {...}, "tx": {...}}}``.

    Contadores (desde a inicialização): ``bytes``, ``packets``, ``errors``,
    ``dropped`` e, em tx, ``carrier_errors`` e ``collisions``.
    """
    if not texto.strip():
        return {}
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError as erro:
        raise ErroParser(f"saída do 'ip -j -s link' não é JSON válido: {erro}") from erro
    if not isinstance(dados, list):
        raise ErroParser("saída do 'ip -j -s link' não é uma lista")
    resultado = {}
    for item in dados:
        if not isinstance(item, dict) or "ifname" not in item:
            continue
        estatisticas = item.get("stats64") or item.get("stats") or {}
        resultado[item["ifname"]] = {
            "estado": item.get("operstate", ""),
            "rx": dict(estatisticas.get("rx", {}) or {}),
            "tx": dict(estatisticas.get("tx", {}) or {}),
        }
    return resultado

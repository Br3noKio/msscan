"""Parsers da saída do ``ethtool``."""

from __future__ import annotations

import re
from typing import Any

from msscan.parsers import ErroParser

_RE_MODO = re.compile(r"^(\d+)base")


def _campos(texto: str) -> dict[str, list[str]]:
    """Linhas "Chave: valor" com continuações indentadas (listas)."""
    campos: dict[str, list[str]] = {}
    atual: str | None = None
    for linha in texto.splitlines():
        if not linha.strip() or linha.startswith("Settings for"):
            continue
        conteudo = linha.strip()
        chave, sep, valor = conteudo.partition(":")
        # Continuação: linha sem ":" (ex.: segunda linha de "Supported link modes").
        if not sep:
            if atual:
                campos[atual].extend(conteudo.split())
            continue
        atual = chave.strip()
        campos[atual] = valor.split() if atual.endswith("link modes") else [valor.strip()]
    return campos


def velocidade_mbps(texto: str | None) -> int | None:
    """"1000Mb/s" → 1000; "Unknown!" → ``None``."""
    combinacao = re.match(r"^\s*(\d+)\s*Mb/s", texto or "")
    return int(combinacao.group(1)) if combinacao else None


def parse_ethtool(texto: str) -> dict[str, Any]:
    """Interpreta ``ethtool <interface>``.

    Devolve ``link`` (bool ou ``None``), ``velocidade_mbps``, ``duplex`` ("Full",
    "Half" ou ``None``), ``autonegociacao``, ``porta`` (ex. "Twisted Pair", "FIBRE"), ``velocidade_maxima_mbps`` (maior
    modo suportado) e ``modos_suportados``.
    """
    campos = _campos(texto)
    if not campos:
        raise ErroParser("saída do 'ethtool' vazia ou irreconhecível")
    link = (campos.get("Link detected") or [""])[0]
    duplex = (campos.get("Duplex") or [""])[0]
    modos = [m for m in campos.get("Supported link modes", []) if _RE_MODO.match(m)]
    velocidades = [int(_RE_MODO.match(m).group(1)) for m in modos]
    return {
        "link": {"yes": True, "no": False}.get(link),
        "velocidade_mbps": velocidade_mbps((campos.get("Speed") or [""])[0]),
        "duplex": duplex if duplex in ("Full", "Half") else None,
        "autonegociacao": (campos.get("Auto-negotiation") or [None])[0],
        "porta": (campos.get("Port") or [None])[0],
        "velocidade_maxima_mbps": max(velocidades) if velocidades else None,
        "modos_suportados": modos,
    }


def parse_ethtool_i(texto: str) -> dict[str, str]:
    """Interpreta ``ethtool -i <interface>`` (driver, versão, firmware, barramento)."""
    campos: dict[str, str] = {}
    for linha in texto.splitlines():
        chave, sep, valor = linha.partition(":")
        if sep:
            campos[chave.strip()] = valor.strip()
    if "driver" not in campos:
        raise ErroParser("saída do 'ethtool -i' sem o campo driver")
    return {
        "driver": campos["driver"],
        "versao_driver": campos.get("version", ""),
        "firmware": campos.get("firmware-version", ""),
        "barramento": campos.get("bus-info", ""),
    }

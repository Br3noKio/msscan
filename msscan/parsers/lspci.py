"""Parser da saída de ``lspci -nnk``."""

from __future__ import annotations

import re
from typing import Any

from msscan.parsers import ErroParser

_RE_DISPOSITIVO = re.compile(
    r"^(?P<slot>(?:[0-9a-fA-F]{4}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])\s+"
    r"(?P<classe_nome>.+?) \[(?P<classe>[0-9a-fA-F]{4})\]: "
    r"(?P<descricao>.*) \[(?P<fabricante_id>[0-9a-fA-F]{4}):(?P<dispositivo_id>[0-9a-fA-F]{4})\]"
    r"(?: \(rev (?P<revisao>[0-9a-fA-F]+)\))?"
    r"(?: \(prog-if .*\))?\s*$"
)
_RE_SUBSISTEMA = re.compile(r"^(?P<nome>.*?)\s*\[(?P<ids>[0-9a-fA-F]{4}:[0-9a-fA-F]{4})\]\s*$")


def parse_lspci_nnk(texto: str) -> list[dict[str, Any]]:
    """Converte ``lspci -nnk`` em uma lista de dispositivos PCI.

    Cada dispositivo: ``slot``, ``classe`` (4 dígitos hex, ex. "0104"),
    ``classe_nome``, ``descricao``, ``fabricante_id``, ``dispositivo_id``,
    ``revisao``, ``subsistema``, ``subsistema_ids``, ``driver`` (em uso) e
    ``modulos``.

    Levanta ``ErroParser`` se houver texto mas nenhuma linha reconhecível.
    """
    dispositivos: list[dict[str, Any]] = []
    atual: dict[str, Any] | None = None
    for linha in texto.splitlines():
        if not linha.strip():
            continue
        if not linha.startswith(("\t", " ")):
            combinacao = _RE_DISPOSITIVO.match(linha)
            if not combinacao:
                atual = None
                continue
            atual = {
                "slot": combinacao["slot"],
                "classe": combinacao["classe"].lower(),
                "classe_nome": combinacao["classe_nome"],
                "descricao": combinacao["descricao"],
                "fabricante_id": combinacao["fabricante_id"].lower(),
                "dispositivo_id": combinacao["dispositivo_id"].lower(),
                "revisao": combinacao["revisao"],
                "subsistema": None,
                "subsistema_ids": None,
                "driver": None,
                "modulos": [],
            }
            dispositivos.append(atual)
            continue
        if atual is None:
            continue
        chave, _, valor = linha.strip().partition(":")
        valor = valor.strip()
        if chave == "Subsystem":
            sub = _RE_SUBSISTEMA.match(valor)
            atual["subsistema"] = sub["nome"] if sub else valor
            atual["subsistema_ids"] = sub["ids"].lower() if sub else None
        elif chave == "Kernel driver in use":
            atual["driver"] = valor
        elif chave == "Kernel modules":
            atual["modulos"] = [m.strip() for m in valor.split(",") if m.strip()]
    if texto.strip() and not dispositivos:
        raise ErroParser("nenhum dispositivo reconhecido na saída do lspci -nnk")
    return dispositivos

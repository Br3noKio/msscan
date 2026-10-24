"""Parsers do ``mdadm`` (somente comandos de leitura)."""

from __future__ import annotations

import shlex
from typing import Any


def parse_examine_scan(texto: str) -> list[dict[str, Any]]:
    """Interpreta ``mdadm --examine --scan``: um item por linha ``ARRAY``.

    Cada array: ``dispositivo`` (ex. "/dev/md/0" ou "<ignore>") e os atributos
    ``chave=valor`` (metadata, UUID, name, container, member...).

    TODO(fixture): só há saída real VAZIA (R640 sem arrays). Precisa de saída real
    com array mdadm (metadata 1.2) e com volume Intel RST/VROC (metadata imsm).
    """
    arrays = []
    for linha in texto.splitlines():
        partes = shlex.split(linha, posix=True) if linha.strip() else []
        if not partes or partes[0] != "ARRAY":
            continue
        atributos = {}
        for parte in partes[2:]:
            chave, sep, valor = parte.partition("=")
            if sep:
                atributos[chave] = valor
        arrays.append({"dispositivo": partes[1] if len(partes) > 1 else "", "atributos": atributos})
    return arrays

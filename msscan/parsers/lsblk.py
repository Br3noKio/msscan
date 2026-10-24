"""Parser da saída de ``lsblk -J``."""

from __future__ import annotations

import json
from typing import Any

from msscan.parsers import ErroParser


def parse_lsblk_json(texto: str) -> list[dict[str, Any]]:
    """Interpreta ``lsblk -J -d -o NAME,TRAN,TYPE,SIZE,MODEL``: um dicionário por disco."""
    if not texto.strip():
        return []
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError as erro:
        raise ErroParser(f"saída do 'lsblk -J' não é JSON válido: {erro}") from erro
    dispositivos = dados.get("blockdevices") if isinstance(dados, dict) else None
    if not isinstance(dispositivos, list):
        raise ErroParser("saída do 'lsblk -J' sem 'blockdevices'")
    return [d for d in dispositivos if isinstance(d, dict)]


def parse_lsblk_arvore(texto: str) -> list[dict[str, Any]]:
    """Interpreta ``lsblk -J -p -o NAME,PKNAME,TYPE,...`` (sem ``-d``): lista plana de discos e partições.

    O ``lsblk -J`` aninha as partições em ``children``; aqui elas vêm logo depois
    do disco, sem a chave ``children`` (o disco-pai fica em ``pkname``).
    """
    pendentes = parse_lsblk_json(texto)
    resultado = []
    while pendentes:
        item = pendentes.pop(0)
        resultado.append({k: v for k, v in item.items() if k != "children"})
        filhos = item.get("children") or []
        pendentes[0:0] = [f for f in filhos if isinstance(f, dict)]
    return resultado

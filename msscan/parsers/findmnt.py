"""Parser da saída de ``findmnt -J``."""

from __future__ import annotations

import json
from typing import Any

from msscan.parsers import ErroParser


def parse_findmnt_json(texto: str) -> list[dict[str, Any]]:
    """Interpreta ``findmnt -J``: lista plana de sistemas de arquivos montados.

    Saída vazia (nada encontrado) devolve lista vazia.
    """
    if not texto.strip():
        return []
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError as erro:
        raise ErroParser(f"saída do 'findmnt -J' não é JSON válido: {erro}") from erro
    pendentes = list(dados.get("filesystems", []) if isinstance(dados, dict) else [])
    resultado = []
    while pendentes:
        item = pendentes.pop(0)
        if isinstance(item, dict):
            resultado.append({k: v for k, v in item.items() if k != "children"})
            pendentes.extend(item.get("children", []) or [])
    return resultado

"""Parser da saída de ``sensors -j`` (lm-sensors)."""

from __future__ import annotations

import json
import re
from typing import Any

from msscan.parsers import ErroParser

_RE_VIRGULA_SOBRANDO = re.compile(r",(\s*[}\]])")


def parse_sensors_json(texto: str) -> list[dict[str, Any]]:
    """Converte ``sensors -j`` em uma lista de chips.

    Cada chip: ``{"chip", "adaptador", "leituras": {rotulo: {subcampo: valor}}}``.
    Valores não numéricos são descartados (nunca inventados).

    Tolerância defensiva: se o JSON for inválido, tenta de novo removendo
    vírgulas sobrando antes de ``}``/``]``; se ainda falhar, levanta ``ErroParser``.

    TODO(fixture): precisa de saída real de ``sensors -j`` com erro de leitura de
    subcampo (hardware antigo / chip sem driver completo) para confirmar o formato.
    """
    if not texto.strip():
        return []
    try:
        bruto = json.loads(texto)
    except json.JSONDecodeError:
        try:
            bruto = json.loads(_RE_VIRGULA_SOBRANDO.sub(r"\1", texto))
        except json.JSONDecodeError as erro:
            raise ErroParser(f"saída do 'sensors -j' não é JSON válido: {erro}") from erro
    if not isinstance(bruto, dict):
        raise ErroParser("saída do 'sensors -j' não é um objeto JSON")
    chips: list[dict[str, Any]] = []
    for nome_chip, conteudo in bruto.items():
        if not isinstance(conteudo, dict):
            continue
        leituras: dict[str, dict[str, float]] = {}
        for rotulo, subcampos in conteudo.items():
            if not isinstance(subcampos, dict):
                continue
            numericos = {
                sub: float(valor)
                for sub, valor in subcampos.items()
                if isinstance(valor, (int, float)) and not isinstance(valor, bool)
            }
            if numericos:
                leituras[rotulo] = numericos
        chips.append({"chip": nome_chip, "adaptador": conteudo.get("Adapter", ""), "leituras": leituras})
    return chips

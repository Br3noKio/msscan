"""Parser de ``xrandr --props`` (saídas, modos, modo atual/preferido e presença de EDID).

Escrito contra saídas reais (``tests/fixtures/avulsas/xrandr/``). Atenção: sem EDID, o driver ainda
marca um modo como preferido (``+``); por isso ``edid`` vem da propriedade ``EDID:``, não do ``+``.
"""

from __future__ import annotations

import re
from typing import Any

from msscan.parsers import ErroParser

_RE_TELA = re.compile(r"^Screen \d+: minimum (\d+) x (\d+), current (\d+) x (\d+), maximum (\d+) x (\d+)")
_RE_SAIDA = re.compile(r"^(\S+) (connected|disconnected)( primary)?(?: (\d+)x(\d+)\+\d+\+\d+)?")
_RE_MODO = re.compile(r"^\s+(\d+)x(\d+)(i?)\s+(.*)$")
_RE_TAXA = re.compile(r"(\d+(?:\.\d+)?)(\*?)\s?(\+?)")


def parse_xrandr(texto: str) -> dict[str, Any]:
    """``{"maximo": (w, h) | None, "saidas": [...]}``.

    Cada saída: ``nome``, ``conectada``, ``principal``, ``atual`` ((w, h) ou None), ``edid`` (bool) e
    ``modos``: ``{"largura", "altura", "entrelacado", "taxas", "atual", "preferido"}`` na ordem do xrandr.
    """
    maximo = None
    saidas: list[dict[str, Any]] = []
    atual: dict[str, Any] | None = None
    for linha in texto.splitlines():
        tela = _RE_TELA.match(linha)
        if tela:
            maximo = (int(tela[5]), int(tela[6]))
            continue
        saida = _RE_SAIDA.match(linha)
        if saida and not linha.startswith((" ", "\t")):
            atual = {
                "nome": saida[1],
                "conectada": saida[2] == "connected",
                "principal": bool(saida[3]),
                "atual": (int(saida[4]), int(saida[5])) if saida[4] else None,
                "edid": False,
                "modos": [],
            }
            saidas.append(atual)
            continue
        if atual is None:
            continue
        if linha.startswith("\tEDID:"):
            atual["edid"] = True
            continue
        modo = _RE_MODO.match(linha)
        if modo and not linha.startswith("\t"):
            taxas = _RE_TAXA.findall(modo[4])
            atual["modos"].append({
                "largura": int(modo[1]),
                "altura": int(modo[2]),
                "entrelacado": bool(modo[3]),
                "taxas": [float(t[0]) for t in taxas],
                "atual": any(t[1] == "*" for t in taxas),
                "preferido": any(t[2] == "+" for t in taxas),
            })  # fmt: skip
    if not saidas:
        raise ErroParser("saída do 'xrandr' sem nenhuma saída de vídeo")
    return {"maximo": maximo, "saidas": saidas}

"""Conjuntos de símbolos de status para o terminal.

O console do Linux (tty1 da ISO) só tem 512 glyphs: nenhuma fonte padrão do
console-setup tem ✓ (U+2713) nem ⚠ (U+26A0). Com o codeset Uni2 existem
√, ▲ e ✗, usados no conjunto "console".
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from msscan.core.modelo import Status

UNICODE = {
    Status.OK: "✓",
    Status.ALERTA: "⚠",
    Status.FALHA: "✗",
    Status.NAO_VERIFICADO: "?",
    Status.NAO_APLICAVEL: "N/A",
}
CONSOLE = {
    Status.OK: "√",
    Status.ALERTA: "▲",
    Status.FALHA: "✗",
    Status.NAO_VERIFICADO: "?",
    Status.NAO_APLICAVEL: "N/A",
}
ASCII = {
    Status.OK: "OK",
    Status.ALERTA: "!",
    Status.FALHA: "X",
    Status.NAO_VERIFICADO: "?",
    Status.NAO_APLICAVEL: "N/A",
}
CONJUNTOS = {"unicode": UNICODE, "console": CONSOLE, "ascii": ASCII}

Simbolos = Mapping[Status, str]


def escolher_simbolos(nome: str = "auto", ambiente: Mapping[str, str] | None = None) -> Simbolos:
    """Escolhe o conjunto; "auto" usa "console" no console do Linux (TERM=linux)."""
    if nome in CONJUNTOS:
        return CONJUNTOS[nome]
    ambiente = os.environ if ambiente is None else ambiente
    return CONSOLE if ambiente.get("TERM") == "linux" else UNICODE

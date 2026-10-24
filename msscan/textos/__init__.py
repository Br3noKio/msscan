"""Textos da interface, num único arquivo (pedido do usuário, 09/10/2026): ``pt_BR.json``.

O mesmo arquivo serve a interface gráfica (o servidor entrega como ``/estaticos/textos.js``), o modo texto e os
relatórios. Regras de escrita e glossário: ``docs/guia_de_escrita.md``. Para traduzir, basta outro arquivo com as mesmas
chaves (ex.: ``en_US.json``) e trocar ``IDIOMA``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

IDIOMA = "pt_BR"
PASTA = Path(__file__).parent


@lru_cache(maxsize=4)
def carregar(idioma: str = IDIOMA) -> dict[str, str]:
    """Chaves planas (``secao.chave``) → texto."""
    dados = json.loads((PASTA / f"{idioma}.json").read_text(encoding="utf-8"))
    return {f"{secao}.{chave}": texto for secao, grupo in dados.items() for chave, texto in grupo.items()}


def t(chave: str, *valores: Any) -> str:
    """Texto da chave com ``{0}``, ``{1}``... trocados pelos valores. Chave ausente devolve a própria chave (visível, para
    o teste e a revisão acharem)."""
    texto = carregar().get(chave, chave)
    for posicao, valor in enumerate(valores):
        texto = texto.replace("{" + str(posicao) + "}", str(valor))
    return texto


def script_js(idioma: str = IDIOMA) -> str:
    """Conteúdo de ``/estaticos/textos.js``: os textos para a interface gráfica (sem nenhuma lógica)."""
    return "window.TEXTOS = " + json.dumps(carregar(idioma), ensure_ascii=False) + ";\n"


_MESES_EN = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august", "september",
                                         "october", "november", "december"), 1)}  # fmt: skip


def data_br(valor: Any, com_hora: bool = True) -> str:
    """Data no padrão brasileiro (``dd/mm/aaaa`` e, se houver, ``hh:mm``). Aceita ISO (``2026-10-06T14:25:00``) e o formato
    do catálogo da Dell (``August 11, 2026``). Valor não reconhecido volta como veio; vazio vira "data desconhecida"."""
    import re

    texto = str(valor or "").strip()
    if not texto:
        return t("comum.data_desconhecida")
    iso = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?", texto)
    if iso:
        return f"{iso[3]}/{iso[2]}/{iso[1]}" + (f" {iso[4]}:{iso[5]}" if com_hora and iso[4] else "")
    dell = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", texto)
    if dell and dell[1].lower() in _MESES_EN:
        return f"{int(dell[2]):02d}/{_MESES_EN[dell[1].lower()]:02d}/{dell[3]}"
    return texto

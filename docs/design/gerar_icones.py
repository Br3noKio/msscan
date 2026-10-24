"""Gera ``msscan/interface/estaticos/icones.js`` com os ícones Lucide (linha fina, licença ISC) usados pela interface.

Nova interface da 1.0.0 (pedido de 09/10/2026): um único conjunto de ícones de linha. Os nomes internos (à esquerda)
continuam os da interface antiga, para o ``app.js`` não mudar; cada um aponta para um ícone do Lucide (à direita).
Baixa só os SVGs listados, de ``lucide-static`` numa versão fixa; o arquivo gerado não depende de rede.

Uso: ``python3 docs/design/gerar_icones.py``
"""

from __future__ import annotations

import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

VERSAO = "0.452.0"
URL = "https://cdn.jsdelivr.net/npm/lucide-static@{versao}/icons/{nome}.svg"
DESTINO = Path(__file__).resolve().parents[2] / "msscan/interface/estaticos/icones.js"
ICONES = {
    "check-circle": "circle-check", "alert": "triangle-alert", "x-circle": "circle-x", "question": "message-circle-question",
    "skip": "skip-forward", "home": "house", "pulse": "activity", "file": "file-text", "light-bulb": "lightbulb",
    "download": "download", "info": "info", "globe": "globe", "server": "server", "search": "search",
    "chevron-right": "chevron-right", "arrow-left": "arrow-left", "sync": "refresh-cw", "plug": "plug", "clock": "clock",
    "command-palette": "keyboard", "x": "x", "law": "scale", "cpu": "cpu", "shield-check": "shield-check", "link": "link",
    "hourglass": "hourglass", "terminal": "square-terminal", "check": "check", "dot-fill": "dot", "stop": "circle-stop",
    "gear": "settings", "key": "key-round", "trash": "trash-2", "file-directory": "folder-open", "sign-out": "log-out",
    "chevron-down": "chevron-down", "chevron-up": "chevron-up", "fold": "fold-vertical", "unfold": "unfold-vertical",
    "zap": "zap", "copy": "copy", "paste": "clipboard-paste", "plus": "plus", "dash": "minus", "location": "map-pin",
    "screen-full": "maximize",
    # Nova interface: barra superior, painéis, doca e sensores ao vivo
    "thermometer": "thermometer", "fan": "fan", "power": "power", "wifi": "wifi", "wifi-off": "wifi-off", "bell": "bell",
    "bell-off": "bell-off", "panel-left": "panel-left", "panel-right": "panel-right", "hard-drive": "hard-drive",
    "memory": "memory-stick", "network": "network", "server-cog": "server-cog", "message": "message-square",
    "sparkles": "sparkles", "file-down": "file-down", "external-link": "external-link", "layout": "layout-dashboard",
    "gauge": "gauge", "activity-square": "square-activity", "battery": "plug-zap", "chart": "chart-column", "brush": "eraser",
}  # fmt: skip
CABECALHO = """/*
 * Ícones: Lucide (https://lucide.dev), lucide-static {versao}, só os usados aqui (docs/design/gerar_icones.py).
 * Um único conjunto de linha fina (nova interface da 1.0.0). Os nomes à esquerda são os nomes internos do MSscan.
 *
 * ISC License
 *
 * Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as part of Feather (MIT). All other copyright
 * (c) for Lucide are held by Lucide Contributors 2022.
 *
 * Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby
 * granted, provided that the above copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
 * INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN
 * AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
 * PERFORMANCE OF THIS SOFTWARE.
 */
"use strict";

/* Elementos SVG (viewBox 0 0 24 24, traço em currentColor) por nome do ícone: [tag, atributos]. */
"""
ATRIBUTOS = {"d", "cx", "cy", "r", "rx", "ry", "x", "y", "x1", "y1", "x2", "y2", "width", "height", "points"}


def elementos(svg: str) -> list[list]:
    raiz = ET.fromstring(re.sub(r"<!--.*?-->", "", svg, flags=re.S))
    saida = []
    for filho in raiz:
        tag = filho.tag.split("}")[-1]
        saida.append([tag, {k: v for k, v in filho.attrib.items() if k in ATRIBUTOS}])
    return saida


def main() -> None:
    dados = {}
    for nome, lucide in ICONES.items():
        with urllib.request.urlopen(URL.format(versao=VERSAO, nome=lucide), timeout=30) as resposta:  # noqa: S310 - URL fixa
            dados[nome] = elementos(resposta.read().decode("utf-8"))
    corpo = "const ICONES = {\n" + "".join(f"  {json.dumps(k)}: {json.dumps(v, separators=(',', ':'))},\n" for k, v in dados.items()) + "};\n"
    DESTINO.write_text(CABECALHO.format(versao=VERSAO) + corpo, encoding="utf-8")
    print(f"{len(dados)} ícones em {DESTINO}")


if __name__ == "__main__":
    main()

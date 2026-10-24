"""Proxy HTTP(S) configurado em ``config/proxy.conf`` na partição de dados.

Formatos aceitos (linhas; ``#`` inicia comentário):

    http://usuario:senha@proxy.empresa:3128      (uma URL: vale para HTTP e HTTPS)
    https_proxy=http://proxy.empresa:3128
    http_proxy=http://proxy.empresa:3128
    no_proxy=localhost,10.0.0.0/8

O arquivo é lido direto do disco (nunca pelo executor) para que a senha do
proxy não vá parar em coletas/fixtures.
"""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from pathlib import Path

from msscan.parsers import ErroParser

_CHAVES = {"https_proxy": "https", "http_proxy": "http", "proxy": "ambos", "no_proxy": "no_proxy"}


def ler_proxy(texto: str) -> dict[str, str]:
    """Interpreta ``proxy.conf``: devolve ``{"https", "http", "no_proxy"}`` (só as presentes)."""
    config: dict[str, str] = {}
    for bruta in texto.splitlines():
        linha = bruta.split("#", 1)[0].strip()
        if not linha:
            continue
        if linha.lower().startswith("export "):
            linha = linha[7:].strip()
        chave, sep, valor = linha.partition("=")
        if sep and chave.strip().lower() in _CHAVES:
            destino, valor = _CHAVES[chave.strip().lower()], valor.strip().strip("'\"")
        elif "://" in linha and not sep:
            destino, valor = "ambos", linha
        else:
            raise ErroParser(f"linha não reconhecida em proxy.conf: {bruta.strip()!r}")
        if destino != "no_proxy" and not re.match(r"^https?://\S+$", valor):
            raise ErroParser(f"URL de proxy inválida: {mascarar(valor)!r}")
        if destino == "ambos":
            config["http"] = config["https"] = valor
        else:
            config[destino] = valor
    return config


def mascarar(url: str) -> str:
    """Esconde a senha de uma URL de proxy (``http://usuario:***@host``)."""
    return re.sub(r"(://[^:/@]+:)[^@]+@", r"\1***@", url)


def aplicar_proxy(config: dict[str, str], ambiente: MutableMapping[str, str] | None = None) -> None:
    """Exporta as variáveis de proxy (maiúsculas e minúsculas) para requests e subprocessos."""
    ambiente = os.environ if ambiente is None else ambiente
    for chave, variavel in (("https", "https_proxy"), ("http", "http_proxy"), ("no_proxy", "no_proxy")):
        if chave in config:
            ambiente[variavel] = config[chave]
            ambiente[variavel.upper()] = config[chave]


def carregar_proxy(pasta_dados: str | Path) -> dict[str, str] | None:
    """Lê e aplica ``<pasta_dados>/config/proxy.conf``; ``None`` se não existir.

    Levanta ``ErroParser`` se o arquivo existir mas estiver inválido.
    """
    arquivo = Path(pasta_dados) / "config" / "proxy.conf"
    if not arquivo.is_file():
        return None
    config = ler_proxy(arquivo.read_text(encoding="utf-8", errors="replace"))
    aplicar_proxy(config)
    return config

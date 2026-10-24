"""Saídas do ``racadm`` local (Dell iDRAC Tools, em ``vendor/``): só leitura.

``racadm get <grupo>`` imprime ``[Key=iDRAC.Embedded.1#Grupo.1]`` e linhas ``Atributo=valor`` (atributo somente leitura
começa com ``#``), conforme os exemplos do "iDRAC9 RACADM CLI Guide".
TODO(fixture): sem saída real do racadm (não há racadm nesta máquina); conferir com uma coleta de iDRAC7/8/9.
"""

from __future__ import annotations

from msscan.parsers import ErroParser


def parse_racadm_get(texto: str) -> dict[str, str]:
    """``{"Atributo": "valor"}`` de um ``racadm get``; ``ErroParser`` se nada for reconhecido ou o racadm der erro."""
    campos: dict[str, str] = {}
    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("[") or "=" not in linha:
            continue
        chave, _, valor = linha.lstrip("#").partition("=")
        campos[chave.strip()] = valor.strip()
    if not campos:
        raise ErroParser("saída do 'racadm get' sem atributos reconhecíveis")
    return campos

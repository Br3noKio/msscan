"""Parsers de arquivos do /proc."""

from __future__ import annotations

import re
from typing import Any

from msscan.parsers import ErroParser


def parse_cpuinfo(texto: str) -> dict[str, Any]:
    """Interpreta ``/proc/cpuinfo``.

    Devolve ``processadores_logicos``, ``soquetes`` (ids físicos distintos, ou
    ``None`` se o kernel não informar) e ``modelos`` (nomes distintos).
    """
    blocos = [b for b in texto.split("\n\n") if b.strip()]
    logicos = 0
    soquetes: set[str] = set()
    modelos: list[str] = []
    for bloco in blocos:
        campos: dict[str, str] = {}
        for linha in bloco.splitlines():
            chave, sep, valor = linha.partition(":")
            if sep:
                campos[chave.strip()] = valor.strip()
        if "processor" not in campos:
            continue
        logicos += 1
        if "physical id" in campos:
            soquetes.add(campos["physical id"])
        modelo = campos.get("model name")
        if modelo and modelo not in modelos:
            modelos.append(modelo)
    if texto.strip() and logicos == 0:
        raise ErroParser("nenhum processador encontrado em /proc/cpuinfo")
    return {
        "processadores_logicos": logicos,
        "soquetes": len(soquetes) if soquetes else None,
        "modelos": modelos,
    }


def parse_cmdline(texto: str) -> dict[str, str]:
    """Interpreta ``/proc/cmdline``: ``{"parametro": "valor"}`` (valor vazio para flags).

    Se um parâmetro aparece mais de uma vez, vale o último (como no kernel).
    """
    parametros: dict[str, str] = {}
    for item in texto.split():
        chave, _, valor = item.partition("=")
        parametros[chave] = valor
    return parametros


PREFIXO_BOOT = "msscan."
PREFIXO_BOOT_ANTIGO = "serverdiag."
"""Nome antigo do projeto: ``serverdiag.*`` segue aceito durante a transição para ``msscan.*``."""


def parametro_boot(parametros: dict[str, str], nome: str) -> str | None:
    """Valor de ``msscan.<nome>`` (ou do antigo ``serverdiag.<nome>``) já lido por ``parse_cmdline``.

    O nome novo tem prioridade. ``None`` se nenhum dos dois estiver presente.
    """
    for prefixo in (PREFIXO_BOOT, PREFIXO_BOOT_ANTIGO):
        if prefixo + nome in parametros:
            return parametros[prefixo + nome]
    return None


def parse_uevent(texto: str) -> dict[str, str]:
    """Interpreta um arquivo ``uevent`` do sysfs (linhas ``CHAVE=valor``)."""
    campos: dict[str, str] = {}
    for linha in texto.splitlines():
        chave, sep, valor = linha.partition("=")
        if sep:
            campos[chave.strip()] = valor.strip()
    return campos


_RE_MD_ARRAY = re.compile(r"^(?P<nome>md\S+)\s*:\s*(?P<estado>\S+)\s+(?:\((?P<extra>[^)]*)\)\s+)?(?P<resto>.*)$")
_RE_MD_MEMBROS = re.compile(r"\[(?P<total>\d+)/(?P<ativos>\d+)\]\s+\[(?P<mapa>[U_]+)\]")
_RE_MD_PROGRESSO = re.compile(r"(?P<operacao>recovery|resync|reshape|check|repair)\s*=\s*(?P<pct>[\d.]+)%")


def parse_mdstat(texto: str) -> list[dict[str, Any]]:
    """Interpreta ``/proc/mdstat``: um item por array md.

    Cada array: ``nome``, ``estado`` (active/inactive), ``nivel`` (raid1...),
    ``dispositivos``, ``total``, ``ativos``, ``mapa`` (ex. "U_"), ``degradado``,
    ``operacao`` (recovery/resync/... ou ``None``) e ``progresso``.

    TODO(fixture): só há saída real SEM arrays (R640). Precisa de /proc/mdstat
    real com array saudável, degradado e em reconstrução.
    """
    arrays: list[dict[str, Any]] = []
    atual: dict[str, Any] | None = None
    for linha in texto.splitlines():
        combinacao = _RE_MD_ARRAY.match(linha)
        if combinacao:
            partes = combinacao["resto"].split()
            # O nível (raid1, raid5...) é o primeiro item sem "[n]"; arrays inativos não têm nível.
            nivel = partes[0] if partes and "[" not in partes[0] else None
            dispositivos = [re.sub(r"\[\d+\].*$", "", p) for p in partes[1 if nivel else 0 :]]
            atual = {
                "nome": combinacao["nome"],
                "estado": combinacao["estado"],
                "nivel": nivel,
                "dispositivos": dispositivos,
                "falhos": [re.sub(r"\[\d+\].*$", "", p) for p in partes if "(F)" in p],
                "total": None,
                "ativos": None,
                "mapa": None,
                "degradado": False,
                "operacao": None,
                "progresso": None,
            }
            arrays.append(atual)
            continue
        if atual is None:
            continue
        membros = _RE_MD_MEMBROS.search(linha)
        if membros:
            atual.update(total=int(membros["total"]), ativos=int(membros["ativos"]), mapa=membros["mapa"])
            atual["degradado"] = "_" in membros["mapa"] or int(membros["ativos"]) < int(membros["total"])
        progresso = _RE_MD_PROGRESSO.search(linha)
        if progresso:
            atual.update(operacao=progresso["operacao"], progresso=float(progresso["pct"]))
    return arrays

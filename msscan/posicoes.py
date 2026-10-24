"""Mapa por posição de cada componente (pedido de 22/10/2026): o que funciona, o que tem problema e onde está.

Função pura sobre os dados que cada provedor JÁ devolveu (nenhuma leitura nova): uma linha por soquete, fonte, ventoinha,
disco ou porta, com ``posicao``, ``local`` (``msscan/localizacao.py``), ``situacao`` e ``detalhe``. A memória monta o
próprio mapa (``provedores/sistema.py``: pente desabilitado no POST parece slot vazio e exige outras fontes).
Situações: ``OK``, ``ALERTA``, ``FALHA``, ``CONFERIR`` (sem leitura ou indício fraco; não muda o status) e ``VAZIO``.
"""

from __future__ import annotations

from typing import Any

from msscan.localizacao import local_de
from msscan.provedores.sistema import CONFERIR, VAZIO

_SITUACAO = {"OK": "OK", "ALERTA": "ALERTA", "FALHA": "FALHA", "NAO_VERIFICADO": CONFERIR, "NAO_APLICAVEL": VAZIO, VAZIO: VAZIO,
             CONFERIR: CONFERIR}


def _local(nome: str, entidade: str | None, dell: bool) -> tuple[str | None, str]:
    achado = local_de(nome, entidade, dell)
    return (achado[0], achado[1]) if achado else (None, nome)


def _linha(posicao: str, local: str, situacao: Any, detalhe: str = "") -> dict[str, Any]:
    return {"posicao": posicao, "local": local, "situacao": _SITUACAO.get(str(situacao or "").upper(), CONFERIR), "detalhe": detalhe}


def _sem_leitura(ignoradas: list[Any]) -> list[dict[str, Any]]:
    return [_linha(str(t).split(":", 1)[0], str(t).split(":", 1)[0], CONFERIR, str(t)) for t in ignoradas or []]


def _cpu(dados: dict[str, Any]) -> list[dict[str, Any]]:
    linhas = []
    historico = " ".join(str(h) for h in dados.get("historico") or [])
    for p in dados.get("processadores") or []:
        soquete = p.get("soquete") or "?"
        if not p.get("populado"):
            linhas.append(_linha(soquete, soquete, VAZIO))
            continue
        problema = [t for t in dados.get("problemas") or [] if soquete in str(t) and str(t) not in historico]
        situacao = "ALERTA" if problema or not p.get("habilitado") and not str(p.get("status", "")).lower().endswith("idle") else "OK"
        detalhe = "; ".join([str(x) for x in (p.get("modelo"), p.get("status")) if x] + [str(t) for t in problema])
        linhas.append(_linha(soquete, soquete, situacao, detalhe))
    return linhas


def _leituras(dados: dict[str, Any], dell: bool, so_fontes: bool) -> list[dict[str, Any]]:
    linhas = []
    for item in dados.get("leituras") or []:
        area, rotulo = _local(str(item.get("local") or item.get("nome") or ""), item.get("entidade"), dell)
        if so_fontes and not (area or "").startswith("psu"):
            continue  # "PS Redundancy" e afins: estado do conjunto, não uma posição
        if "redundan" in str(item.get("nome") or "").lower():
            continue
        linhas.append(_linha(str(item.get("nome") or rotulo), rotulo, item.get("avaliacao"), str(item.get("leitura") or "")))
    sem_leitura = [t for t in dados.get("ignoradas_sem_leitura") or [] if "redundan" not in str(t).lower()
                   and (not so_fontes or (_local(str(t).split(":", 1)[0], None, dell)[0] or "").startswith("psu"))]  # fmt: skip
    return linhas + _sem_leitura(sem_leitura)


def _discos(dados: dict[str, Any]) -> list[dict[str, Any]]:
    from msscan.provedores.smart import rotulo_disco

    linhas = []
    for d in dados.get("discos") or []:
        detalhe = "; ".join([str(x) for x in (d.get("modelo"), f"{d['temperatura']} °C" if d.get("temperatura") is not None else None) if x]
                            + [str(p) for p in d.get("problemas") or []])  # fmt: skip
        linhas.append(_linha(d.get("dispositivo") or "?", rotulo_disco(d), d.get("status"), detalhe))
    return linhas


def _rede(dados: dict[str, Any]) -> list[dict[str, Any]]:
    linhas = []
    for porta in dados.get("portas") or []:
        nome = str(porta.get("nome") or "?")
        link = porta.get("link")
        detalhe = "; ".join(x for x in (str(porta.get("driver") or ""), "com link" if link else "sem link (sem cabo ou desligada na outra ponta)"
                                        if link is False else "") if x)  # fmt: skip
        situacao = porta.get("avaliacao") or porta.get("status_msscan")
        if link is False and situacao not in ("ALERTA", "FALHA"):
            situacao = "NAO_APLICAVEL"  # sem cabo: posição livre, não defeito
        linhas.append(_linha(nome, nome, situacao, detalhe))
    return linhas


def montar(componente: str, dados: dict[str, Any] | None, dell: bool = False) -> list[dict[str, Any]] | None:
    """Linhas do mapa por posição; ``None`` se o componente não tem posições (ou já trouxe as dele)."""
    if not isinstance(dados, dict) or "posicoes" in dados:
        return None
    if componente == "cpu":
        linhas = _cpu(dados)
    elif componente in ("fontes", "ventoinhas"):
        linhas = _leituras(dados, dell, so_fontes=componente == "fontes")
    elif componente == "discos":
        linhas = _discos(dados)
    elif componente == "rede":
        linhas = _rede(dados)
    else:
        return None
    return linhas or None

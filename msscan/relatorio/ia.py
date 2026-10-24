"""Seção "Interpretação (IA)" do relatório. Só existe quando a análise foi concluída.

Falha, recusa ou análise não pedida → o relatório fica sem a seção (CLAUDE.md, seção 10).
Registra qual provedor e modelo gerou a análise e as tentativas do fallback (CLAUDE.md 10b).
"""

from __future__ import annotations

import textwrap
from html import escape
from typing import Any

from msscan import textos
from msscan.ia.analise import NAO_CONFIRMADA

ROTULOS_SEVERIDADE = {"critica": textos.t("py_rel_ia.critica"), "alta": "ALTA", "media": textos.t("py_rel_ia.media"), "baixa": "BAIXA"}
ROTULOS_PRIORIDADE = {"imediata": "imediata", "alta": "alta", "media": textos.t("py_rel_ia.media_2"), "baixa": "baixa"}
AVISO_IA = (
    textos.t("py_rel_ia.interpretacao_gerada_por_ia_a")
)


def secao_ia(analise: dict[str, Any], removidos: dict[str, int], tentativas: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Conteúdo de ``relatorio["interpretacao_ia"]`` para uma análise concluída."""
    secao = {"situacao": "concluida", **analise, "identificadores_removidos": removidos, "tentativas": tentativas or [],
             "aviso": AVISO_IA}  # fmt: skip
    secao["origem"] = _origem(secao)
    return secao


def _sim_nao(valor: Any) -> str:
    return "sim" if valor is True else "não" if valor is False else textos.t("py_rel_ia.nao_informado")


PAPEIS = {"principal": textos.t("py_rel_ia.ia_principal"), "reserva": textos.t("py_rel_ia.ia_reserva")}


def _origem(secao: dict[str, Any]) -> str:
    """Qual IA (provedor e modelo) gerou a análise, se foi a principal ou a reserva, e as falhas antes dela."""
    nome = secao.get("provedor_nome") or secao.get("provedor") or "?"
    titulo = secao.get("provedor_titulo")
    rotulo = f"{titulo} [{nome}]" if titulo and titulo != nome else nome
    texto = textos.t("py_rel_ia.gerado_por_modelo", rotulo, secao.get('provedor') or '?', secao.get('modelo') or '?')
    if secao.get("papel") in PAPEIS:
        texto += f", {PAPEIS[secao['papel']]}"
    falhas = [t for t in secao.get("tentativas") or [] if t.get("resultado") != "ok"]
    if falhas:
        texto += textos.t("py_rel_ia.antes_sem_sucesso") + "; ".join(
            f"{t['nome']}{' (' + PAPEIS[t['papel']] + ')' if t.get('papel') in PAPEIS else ''}: {t['detalhe']}" for t in falhas
        )  # fmt: skip
    return texto


def formatar_analise_ia(secao: dict[str, Any] | None) -> str:
    """Texto do terminal/TXT (vazio se a análise não foi concluída)."""
    if not secao or secao.get("situacao") != "concluida":
        return ""
    linhas = ["", textos.t("py_rel_ia.interpretacao_ia"), "-" * 78]
    linhas += textwrap.wrap(AVISO_IA, 78)
    linhas += textwrap.wrap(_origem(secao), 78)
    linhas.append("")
    linhas += textwrap.wrap(secao.get("resumo") or textos.t("py_rel_ia.sem_resumo"), 78)
    for i, problema in enumerate(secao.get("problemas") or [], 1):
        severidade = ROTULOS_SEVERIDADE.get(problema.get("severidade"), "?")
        linhas.append("")
        linhas.append(f"{i}. [{severidade}] {problema.get('titulo')}")
        if not problema.get("confirmado"):
            linhas.append(textos.t("py_rel_ia.atencao", NAO_CONFIRMADA))
        linhas += textwrap.wrap(problema.get("explicacao") or "", 78, initial_indent="   ", subsequent_indent="   ")
        for evidencia in problema.get("evidencias") or []:
            marca = "√" if evidencia.get("confirmada") else "?"
            linhas.append(textos.t("py_rel_ia.evidencia", marca, evidencia.get('componente'), evidencia.get('trecho')))
        plano = problema.get("plano") or {}
        linhas.append(
            textos.t("py_rel_ia.plano_prioridade_intervencao_fisica_troca", ROTULOS_PRIORIDADE.get(plano.get('prioridade'), '?'), _sim_nao(plano.get('intervencao_fisica')), _sim_nao(plano.get('troca_de_peca')))
        )
        for n, passo in enumerate(plano.get("passos") or [], 1):
            linhas += textwrap.wrap(f"{n}) {passo}", 78, initial_indent="     ", subsequent_indent="        ")
        if plano.get("acao"):
            linhas.append(textos.t("py_rel_ia.acao_do_catalogo_recomendada", plano['acao']['id'], plano['acao']['titulo'], plano['acao']['situacao']))
        if problema.get("aviso_acao"):
            linhas += textwrap.wrap(textos.t("py_rel_ia.atencao_2", problema['aviso_acao']), 78, initial_indent="   ", subsequent_indent="   ")
    if secao.get("limitacoes"):
        linhas.append("")
        linhas.append(textos.t("py_rel_ia.limitacoes_apontadas"))
        linhas += [f"  - {l}" for l in secao["limitacoes"]]
    return "\n".join(linhas)


def html_analise_ia(secao: dict[str, Any] | None) -> str:
    """Bloco HTML (vazio se a análise não foi concluída)."""
    if not secao or secao.get("situacao") != "concluida":
        return ""
    itens = []
    for problema in secao.get("problemas") or []:
        severidade = ROTULOS_SEVERIDADE.get(problema.get("severidade"), "?")
        aviso = "" if problema.get("confirmado") else f'<p class="nao-confirmada"><strong>{escape(NAO_CONFIRMADA)}</strong></p>'
        evidencias = "".join(
            f"<li>{'✓' if e.get('confirmada') else '?'} {escape(str(e.get('componente')))}: <code>{escape(str(e.get('trecho')))}</code></li>"
            for e in problema.get("evidencias") or []
        )
        plano = problema.get("plano") or {}
        passos = "".join(f"<li>{escape(str(p))}</li>" for p in plano.get("passos") or [])
        acao = ((f"<p><strong>{textos.t('py_rel_ia.acao_do_catalogo_recomendada_2')}</strong>"
                 f" <code>{escape(plano['acao']['id'])}</code> — {escape(plano['acao']['titulo'])} ({escape(plano['acao']['situacao'])})</p>")) if plano.get("acao") else ""  # fmt: skip
        acao_ignorada = f"<p><strong>{escape(problema['aviso_acao'])}</strong></p>" if problema.get("aviso_acao") else ""
        itens.append(
            (f'<div class="cartao"><strong>[{escape(severidade)}] {escape(str(problema.get("titulo")))}</strong>'
             f"{aviso}<p>{escape(str(problema.get('explicacao') or ''))}</p><ul>"
             f"{evidencias}</ul><p><strong>{textos.t('py_rel_ia.plano_de_solucao')}</strong>"
             f" {textos.t('py_rel_ia.prioridade_intervencao_fisica_troca_de', escape(ROTULOS_PRIORIDADE.get(plano.get('prioridade'), '?')), _sim_nao(plano.get('intervencao_fisica')), _sim_nao(plano.get('troca_de_peca')))}</p>"
             f"<ol>{passos}</ol>{acao}{acao_ignorada}</div>")
        )
    limitacoes = "".join(f"<li>{escape(str(l))}</li>" for l in secao.get("limitacoes") or [])
    return (
        (f"<h2>{textos.t('py_rel_ia.interpretacao_ia')}</h2><p class=\"sub\">"
         f"{escape(AVISO_IA)} {escape(_origem(secao))}.</p><div class=\"cartao\">"
         f'{escape(str(secao.get("resumo") or ""))}</div>')
        + "".join(itens)
        + ((f"<div class=\"cartao\"><strong>{textos.t('py_rel_ia.limitacoes')}</strong>"
            f"<ul>{limitacoes}</ul></div>") if limitacoes else "")
    )

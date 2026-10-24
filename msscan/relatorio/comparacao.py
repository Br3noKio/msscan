"""Comparação antes × depois de um diagnóstico refeito (CLAUDE.md 7b).

Funções puras sobre a seção ``medido`` de dois relatórios: para cada componente refeito, o status
anterior, o atual e a tendência (melhorou / piorou / igual). Vai para a tela, o terminal e o relatório.
"""

from __future__ import annotations

import textwrap
from html import escape
from typing import Any

from msscan import textos

MELHOROU, PIOROU, IGUAL = "melhorou", "piorou", "igual"
ROTULOS_TENDENCIA = {MELHOROU: "melhorou", PIOROU: "piorou", IGUAL: "igual"}
GRAVIDADE = {"OK": 0, "NAO_APLICAVEL": 0, "ALERTA": 1, "NAO_VERIFICADO": 2, "FALHA": 3}
"""NAO_VERIFICADO nunca é saudável: fica entre ALERTA e FALHA (ALERTA → NAO_VERIFICADO é piora)."""
ROTULOS_STATUS = {
    "OK": "OK", "ALERTA": "Alerta", "FALHA": "Falha", "NAO_VERIFICADO": textos.t("py_rel_comparacao.nao_verificado"),
    "NAO_APLICAVEL": textos.t("py_rel_comparacao.nao_aplicavel"), None: textos.t("py_rel_comparacao.nao_diagnosticado"),
}  # fmt: skip


def tendencia(antes: str | None, depois: str | None) -> str:
    """``melhorou``/``piorou``/``igual`` pela gravidade do status (componente ausente conta como não verificado)."""
    if antes == depois:
        return IGUAL
    g_antes = GRAVIDADE.get(antes or "NAO_VERIFICADO", 2)
    g_depois = GRAVIDADE.get(depois or "NAO_VERIFICADO", 2)
    if g_depois < g_antes:
        return MELHOROU
    if g_depois > g_antes:
        return PIOROU
    return IGUAL


def comparar(anterior: dict[str, Any], atual: dict[str, Any], refeitos: list[str] | None,
             pasta_anterior: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Seção ``relatorio["comparacao"]``. ``refeitos=None``: diagnóstico inteiro refeito."""
    antes = {m.get("componente"): m for m in anterior.get("medido") or []}
    depois = {m.get("componente"): m for m in atual.get("medido") or []}
    nomes = [n for n in depois if refeitos is None or n in refeitos]
    nomes += [n for n in antes if n not in depois and (refeitos is None or n in refeitos)]
    componentes = []
    for nome in nomes:
        a, d = antes.get(nome) or {}, depois.get(nome) or {}
        componentes.append({
            "componente": nome,
            "titulo": d.get("titulo") or a.get("titulo") or nome,
            "status_antes": a.get("status"),
            "status_depois": d.get("status"),
            "tendencia": tendencia(a.get("status"), d.get("status")),
            "mensagem_antes": a.get("mensagem") or "",
            "mensagem_depois": d.get("mensagem") or "",
        })  # fmt: skip
    resumo = {t: sum(c["tendencia"] == t for c in componentes) for t in (MELHOROU, PIOROU, IGUAL)}
    return {
        "escopo": "tudo" if refeitos is None else "componentes",
        "refeitos": nomes,
        "anterior": {"inicio": anterior.get("inicio"), "veredito": anterior.get("veredito"), "pasta": pasta_anterior},
        "atual": {"inicio": atual.get("inicio"), "veredito": atual.get("veredito")},
        "componentes": componentes,
        "resumo": resumo,
    }


def _escopo(comparacao: dict[str, Any]) -> str:
    if comparacao.get("escopo") == "tudo":
        return textos.t("py_rel_comparacao.diagnostico_completo_refeito")
    titulos = ", ".join(c["titulo"] for c in comparacao.get("componentes") or [])
    return textos.t("py_rel_comparacao.refeito_so", titulos)


def _resumo(comparacao: dict[str, Any]) -> str:
    r = comparacao.get("resumo") or {}
    return f"{r.get(MELHOROU, 0)} melhorou, {r.get(PIOROU, 0)} piorou, {r.get(IGUAL, 0)} igual"


def formatar_comparacao(comparacao: dict[str, Any] | None) -> str:
    """Texto do terminal/TXT (vazio se não houve diagnóstico refeito). Só caracteres do console do tty1."""
    if not comparacao:
        return ""
    anterior, atual = comparacao.get("anterior") or {}, comparacao.get("atual") or {}
    linhas = ["", textos.t("py_rel_comparacao.comparacao_antes_depois", _escopo(comparacao)), "-" * 78]
    linhas.append(f"Antes:  {textos.data_br(anterior.get('inicio'))} - {anterior.get('veredito') or '?'}")
    linhas.append(f"Depois: {textos.data_br(atual.get('inicio'))} - {atual.get('veredito') or '?'}")
    if anterior.get("pasta"):
        linhas += textwrap.wrap(textos.t("py_rel_comparacao.relatorio_anterior_guardado_em", anterior['pasta']), 78)
    linhas.append("")
    for c in comparacao.get("componentes") or []:
        mudanca = f"{ROTULOS_STATUS.get(c['status_antes'], c['status_antes'])} -> {ROTULOS_STATUS.get(c['status_depois'], c['status_depois'])}"
        linhas.append(f"  {c['titulo'][:28]:<28} {mudanca:<34} {ROTULOS_TENDENCIA[c['tendencia']].upper()}")
        if c["tendencia"] != IGUAL and c["mensagem_depois"]:
            linhas += textwrap.wrap(f"agora: {c['mensagem_depois']}", 78, initial_indent="    ", subsequent_indent="    ")
    linhas.append("")
    linhas.append(f"Resumo: {_resumo(comparacao)}")
    return "\n".join(linhas)


CORES_TENDENCIA = {MELHOROU: ("#1a7f37", "#e7f5ea", "▲"), PIOROU: ("#c62828", "#fde8e8", "▼"), IGUAL: ("#57606a", "#eef1f4", "=")}


def html_comparacao(comparacao: dict[str, Any] | None) -> str:
    """Bloco HTML (vazio se não houve diagnóstico refeito). Tendência com símbolo + texto + cor."""
    if not comparacao:
        return ""
    anterior, atual = comparacao.get("anterior") or {}, comparacao.get("atual") or {}
    linhas = []
    for c in comparacao.get("componentes") or []:
        cor, fundo, simbolo = CORES_TENDENCIA[c["tendencia"]]
        linhas.append(
            f"<tr><td>{escape(c['titulo'])}</td>"
            f"<td>{escape(ROTULOS_STATUS.get(c['status_antes'], str(c['status_antes'])))}</td>"
            f"<td>{escape(ROTULOS_STATUS.get(c['status_depois'], str(c['status_depois'])))}</td>"
            f'<td><span class="status" style="color:{cor};background:{fundo}">{simbolo} '
            f"{escape(ROTULOS_TENDENCIA[c['tendencia']])}</span></td>"
            f"<td>{escape(c['mensagem_depois'])}</td></tr>"
        )
    pasta = (f"<br>{textos.t('py_rel_comparacao.relatorio_anterior_guardado_em_2')} <code>"
             f"{escape(str(anterior['pasta']))}</code>") if anterior.get("pasta") else ""
    return (
        (f"<h2>{textos.t('py_rel_comparacao.comparacao_antes_depois_2')}</h2>"
         f"\n<div class=\"cartao\"><strong>{escape(_escopo(comparacao).capitalize())}.</strong>"
         f" {textos.t('py_rel_comparacao.resumo', escape(_resumo(comparacao)))}<br>"
         f"{textos.t('py_rel_comparacao.antes', escape(textos.data_br(anterior.get('inicio'))), escape(str(anterior.get('veredito') or '?')))}<br>"
         f"{textos.t('py_rel_comparacao.depois', escape(textos.data_br(atual.get('inicio'))), escape(str(atual.get('veredito') or '?')), pasta)}<div class=\"tabela\">"
         f"<table><tr><th>{textos.t('py_rel_comparacao.componente')}</th>"
         f"<th>{textos.t('py_rel_comparacao.antes_2')}</th><th>{textos.t('py_rel_comparacao.depois_2')}</th>"
         f"<th>{textos.t('py_rel_comparacao.tendencia')}</th><th>{textos.t('py_rel_comparacao.mensagem_atual')}</th>"
         f"</tr>{''.join(linhas)}</table></div></div>")
    )

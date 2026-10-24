"""Seção "Ações corretivas" do relatório (fase 10): o que foi pedido, a prévia, os passos, o antes × depois e se a
ação foi desfeita (ajuste de 28/09/2026: "o que era antes, o que ficou depois e se foi desfeita")."""

from __future__ import annotations

import textwrap
from html import escape
from typing import Any

from msscan import textos

TITULOS_MODO = {"simular": textos.t("py_rel_acoes.simulacao_nada_foi_alterado_no"), "aplicar": textos.t("py_rel_acoes.aplicacao")}
SITUACOES = {
    "simulada": textos.t("py_rel_acoes.simulacao_concluida"), "bloqueada": "BLOQUEADA", "bloqueada_trava": textos.t("py_rel_acoes.bloqueada_pela_trava_de_seguranca"),
    "cancelada": "cancelada", "falhou": "FALHOU", "so_informativo": textos.t("py_rel_acoes.somente_informativo"), "aplicada": "aplicada",
    "aplicada_sem_confirmacao": textos.t("py_rel_acoes.aceita_pelo_bmc_sem_confirmacao"),
    "desfeita": "desfeita", "desfeita_sem_confirmacao": textos.t("py_rel_acoes.desfeita_aceito_pelo_bmc_sem"),
}  # fmt: skip
ROTULOS = {"ntp_ligado": textos.t("py_rel_acoes.ntp_ligado"), "servidores": textos.t("py_rel_acoes.servidores_ntp"), "data_hora_bmc": textos.t("py_rel_acoes.data_e_hora_do_bmc"),
           "diferenca_s": textos.t("py_rel_acoes.diferenca_do_relogio_s"), "eventos": textos.t("py_rel_acoes.eventos_no_sel")}  # fmt: skip


def _valor(valor: Any) -> str:
    if valor is None:
        return "?"
    if isinstance(valor, bool):
        return "sim" if valor else "não"
    if isinstance(valor, list):
        return ", ".join(str(v) for v in valor) or "(nenhum)"
    return str(valor)


def antes_depois(acao: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Linhas ``(campo, antes, depois)``; depois = "—" quando nada foi alterado (simulação, bloqueio)."""
    antes, depois = acao.get("antes") or {}, acao.get("depois")
    return [(ROTULOS.get(k, k), _valor(antes.get(k)), _valor(depois.get(k)) if depois is not None else "—")
            for k in dict.fromkeys([*antes, *(depois or {})])]  # fmt: skip


def situacao_desfazer(acao: dict[str, Any]) -> str:
    """"sim (...)", "não" ou "não se aplica (...)" — sempre presente no relatório."""
    desfeita = acao.get("desfeita")
    if desfeita:
        modo = textos.t("py_rel_acoes.simulacao_nada_foi_alterado_no_2") if desfeita.get("modo") != "aplicar" else ""
        return f"sim, em {desfeita.get('quando', '?')} ({SITUACOES.get(desfeita.get('situacao'), desfeita.get('situacao'))}{modo})"
    info = acao.get("desfazer") or {}
    if info.get("disponivel"):
        return textos.t("py_rel_acoes.nao_pode_ser_desfeita_valores")
    if info.get("motivo"):
        return textos.t("py_rel_acoes.nao_se_aplica", info['motivo'])
    return textos.t("py_rel_acoes.nao_se_aplica_nada_foi")


def copias(acao: dict[str, Any]) -> list[str]:
    """Arquivos gravados e conferidos no pendrive por esta ação."""
    linhas = []
    if acao.get("exportacao"):
        e = acao["exportacao"]
        linhas.append(textos.t("py_rel_acoes.sel_exportado_evento_s_sha", e.get('no_pendrive') or e.get('arquivo'), e.get('eventos'), e.get('sha256')))
    if acao.get("anteriores"):
        a = acao["anteriores"]
        linhas.append(textos.t("py_rel_acoes.valores_anteriores_sha_256", a.get('no_pendrive') or a.get('arquivo'), a.get('sha256')))
    return linhas


def formatar_acoes(acoes: list[dict[str, Any]] | None) -> str:
    """Texto do terminal/TXT (vazio se nenhuma ação foi executada)."""
    if not acoes:
        return ""
    linhas = ["", textos.t("py_rel_acoes.acoes_corretivas"), "-" * 78]
    for acao in acoes:
        linhas.append(f"{acao.get('titulo')} [{acao.get('acao')}] — {TITULOS_MODO.get(acao.get('modo'), acao.get('modo'))}")
        linhas.append(textos.t("py_rel_acoes.situacao", SITUACOES.get(acao.get('situacao'), acao.get('situacao'))))
        linhas += textwrap.wrap(acao.get("mensagem") or "", 76, initial_indent="  ", subsequent_indent="  ")
        linhas.append(textos.t("py_rel_acoes.antes_depois"))
        for campo, antes, depois in antes_depois(acao):
            linhas.append(f"    {campo}: {antes} -> {depois}")
        linhas += textwrap.wrap(f"Desfeita: {situacao_desfazer(acao)}", 76, initial_indent="  ", subsequent_indent="    ")
        if acao.get("desfeita"):
            for campo, antes, depois in antes_depois(acao["desfeita"]):
                linhas.append(textos.t("py_rel_acoes.desfazer", campo, antes, depois))
        for copia in copias(acao):
            linhas += textwrap.wrap(copia, 76, initial_indent="  ", subsequent_indent="    ")
        for passo in (acao.get("passos") or []) + ((acao.get("desfeita") or {}).get("passos") or []):
            linhas += textwrap.wrap(f"[{passo['situacao']}] {passo['acao']}: {passo['detalhe']}", 76,
                                    initial_indent="  ", subsequent_indent="      ")  # fmt: skip
    return "\n".join(linhas)


def html_acoes(acoes: list[dict[str, Any]] | None) -> str:
    """Bloco HTML das ações corretivas (vazio se nenhuma)."""
    if not acoes:
        return ""
    partes = [(f"<h2>{textos.t('py_rel_acoes.acoes_corretivas')}</h2><p class=\"sub\">"
               f"{textos.t('py_rel_acoes.acoes_do_catalogo_fechado_do')}</p>")]  # fmt: skip
    for acao in acoes:
        linhas = "".join(f"<tr><td>{escape(c)}</td><td>{escape(a)}</td><td>{escape(d)}</td></tr>" for c, a, d in antes_depois(acao))
        tabela = (f"<div class=\"tabela\"><table><tr><th>{textos.t('py_rel_acoes.campo')}</th>"
                  f"<th>{textos.t('py_rel_acoes.antes')}</th><th>{textos.t('py_rel_acoes.depois')}</th>"
                  f"</tr>{linhas}</table></div>") if linhas else ""
        previa = "".join(f"<li>{escape(p)}</li>" for p in acao.get("previa") or [])
        passos = "".join(f"<li><strong>[{escape(p['situacao'])}] {escape(p['acao'])}</strong>: {escape(p['detalhe'])}</li>"
                         for p in acao.get("passos") or [])  # fmt: skip
        desfeita = acao.get("desfeita") or {}
        linhas_desfazer = "".join(f"<tr><td>{escape(c)}</td><td>{escape(a)}</td><td>{escape(d)}</td></tr>" for c, a, d in antes_depois(desfeita))
        tabela_desfazer = ((f"<div class=\"tabela\"><table><tr><th>{textos.t('py_rel_acoes.campo')}</th>"
                            f"<th>{textos.t('py_rel_acoes.antes_do_desfazer')}</th><th>{textos.t('py_rel_acoes.depois_do_desfazer')}</th>"
                            f"</tr>{linhas_desfazer}</table></div>")) if linhas_desfazer else ""  # fmt: skip
        passos += "".join(f"<li><strong>[{escape(p['situacao'])}] {escape(p['acao'])}</strong>: {escape(p['detalhe'])}</li>"
                          for p in desfeita.get("passos") or [])  # fmt: skip
        lista_copias = "".join(f"<li>{escape(c)}</li>" for c in copias(acao))
        partes.append(
            (f'<div class="cartao"><h3>{escape(str(acao.get("titulo")))} <small>'
             f'({escape(str(acao.get("acao")))} — {escape(TITULOS_MODO.get(acao.get("modo"), str(acao.get("modo"))))})</small>'
             f'</h3><p><strong>{escape(SITUACOES.get(acao.get("situacao"), str(acao.get("situacao"))))}</strong>'
             f': {escape(acao.get("mensagem") or "")}</p>{tabela}<p><strong>{textos.t("py_rel_acoes.desfeita")}</strong>'
             f" {escape(situacao_desfazer(acao))}</p>{tabela_desfazer}")
            + ((f"<p><strong>{textos.t('py_rel_acoes.copias_no_pendrive')}</strong>"
                f"</p><ul>{lista_copias}</ul>") if lista_copias else "")
            + (f"<p><strong>{textos.t('py_rel_acoes.previa')}</strong></p><ul>{previa}</ul>" if previa else "")
            + f"<p><strong>Passos</strong></p><ul>{passos}</ul></div>"
        )
    return "\n".join(partes)

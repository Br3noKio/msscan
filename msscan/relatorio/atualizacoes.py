"""Seção "Atualizações" do relatório (fase 8: somente informativo)."""

from __future__ import annotations

import textwrap
from html import escape
from typing import Any

from msscan import textos

AVISO = textos.t("py_rel_atualizacoes.verificacao_somente_informativa_nada_e")
TITULOS_MODO = {"simular": textos.t("py_rel_atualizacoes.modo_interno_de_teste_nada"), "aplicar": textos.t("py_rel_atualizacoes.instalacao_automatica")}
TEXTO_TEMPORARIA = {"desfeita": textos.t("py_rel_atualizacoes.desfeita_conferido_no_idrac"), "nao_desfeita": textos.t("py_rel_atualizacoes.nao_desfeita_o_msscan_tenta"),
                    "feita": "ativa", "pendente": "registrada"}  # fmt: skip
SITUACOES = {
    "simulada": textos.t("py_rel_atualizacoes.simulacao_concluida"), "bloqueada": "BLOQUEADA", "bloqueada_trava": textos.t("py_rel_atualizacoes.bloqueada_pela_trava_de_seguranca"),
    "falhou": "FALHOU", "so_informativo": textos.t("py_rel_atualizacoes.somente_informativo"), "aguardando_reinicio": textos.t("py_rel_atualizacoes.aguardando_o_reinicio"),
    "aguardando_reinicio_manual": textos.t("py_rel_atualizacoes.aguardando_o_reinicio_manual"), "aplicada": "aplicada",
    "possivelmente_agendado": textos.t("py_rel_atualizacoes.aceito_pelo_idrac_possivelmente_agendado"),
}


def formatar_atualizacoes(secao: dict[str, Any] | None) -> str:
    """Texto do terminal/TXT (vazio se a verificação não foi pedida)."""
    if not secao or secao.get("situacao") in (None, "nao_solicitadas"):
        return ""
    linhas = ["", textos.t("py_rel_atualizacoes.atualizacoes_de_firmware_somente_informativo"), "-" * 78]
    linhas += textwrap.wrap(secao.get("mensagem") or "", 78)
    catalogo = secao.get("catalogo") or {}
    if catalogo.get("versao"):
        linhas.append(textos.t("py_rel_atualizacoes.catalogo_dell_2", catalogo['versao'], catalogo.get('origem')))
    if catalogo.get("aviso"):
        linhas.append(f"  Aviso: {catalogo['aviso']}")
    if secao.get("itens"):
        linhas.append(textos.t("py_rel_atualizacoes.inventario", secao.get('fonte_inventario')))
        linhas.append(f"{'Componente':<34}{'Instalada':<20}{'Catálogo':<14}Situação")
        for item in secao["itens"]:
            linhas.append(
                f"{item['componente'][:33]:<34}{str(item['instalada'] or '?')[:19]:<20}"
                f"{str(item['disponivel'] or '-')[:13]:<14}{item['texto_situacao']}"
            )
            if item["situacao"] not in ("igual", "atualizacao"):
                linhas += textwrap.wrap(item["motivo"], 78, initial_indent="    ", subsequent_indent="    ")
            for bloqueio in item.get("bloqueios") or []:
                linhas.append(f"    BLOQUEIO: {bloqueio}")
    for rotulo, chave in ((textos.t("py_rel_atualizacoes.bloqueios_impediriam_a_aplicacao"), "bloqueios"), ("Avisos", "avisos")):
        if secao.get(chave):
            linhas.append(f"{rotulo}:")
            for texto in secao[chave]:
                linhas += textwrap.wrap(texto, 78, initial_indent="  - ", subsequent_indent="    ")
    linhas.append(AVISO)
    return "\n".join(linhas)


def html_atualizacoes(secao: dict[str, Any] | None) -> str:
    """Bloco HTML da seção "Atualizações"."""
    if not secao or secao.get("situacao") in (None, "nao_solicitadas"):
        return (f"<h2>{textos.t('py_rel_atualizacoes.atualizacoes')}</h2>\n<div class=\"cartao pendente\">"
                f"{textos.t('py_rel_atualizacoes.atualizacoes_de_firmware_nao_solicitadas')}</div>")
    catalogo = secao.get("catalogo") or {}
    linhas = "".join(
        f"<tr><td>{escape(i['componente'])}</td><td>{escape(str(i['instalada'] or '?'))}</td>"
        f"<td>{escape(str(i['disponivel'] or '-'))}</td><td>{escape(i['texto_situacao'])}"
        + (f"<br><small>{escape(i['motivo'])}</small>" if i["situacao"] not in ("igual", "atualizacao") else "")
        + "".join(f"<br><strong>Bloqueio:</strong> {escape(b)}" for b in i.get("bloqueios") or [])
        + "</td></tr>"
        for i in secao.get("itens") or []
    )
    tabela = (
        (f"<div class=\"tabela\"><table><tr><th>{textos.t('py_rel_atualizacoes.componente')}</th>"
         f"<th>{textos.t('py_rel_atualizacoes.instalada')}</th><th>{textos.t('py_rel_atualizacoes.catalogo')}</th>"
         f"<th>{textos.t('py_rel_atualizacoes.situacao_2')}</th></tr>{linhas}</table>"
         f"</div>") if linhas else ""
    )
    listas = "".join(
        f"<p><strong>{rotulo}</strong></p><ul>" + "".join(f"<li>{escape(t)}</li>" for t in secao[chave]) + "</ul>"
        for rotulo, chave in ((textos.t("py_rel_atualizacoes.bloqueios_impediriam_a_aplicacao_2"), "bloqueios"), ("Avisos", "avisos")) if secao.get(chave)
    )
    origem = textos.t("py_rel_atualizacoes.catalogo_dell", catalogo.get('versao'), catalogo.get('origem')) if catalogo.get("versao") else ""
    return (
        (f"<h2>{textos.t('py_rel_atualizacoes.atualizacoes')}</h2><p class=\"sub\">"
         f'{escape(origem + AVISO)}</p><div class="cartao">{escape(secao.get("mensagem") or "")}{tabela}{listas}</div>')
    )


def formatar_aplicacao(secao: dict[str, Any] | None) -> str:
    """Texto da aplicação de firmware (simulada ou real). Vazio se não foi pedida."""
    if not secao or not secao.get("modo"):
        return ""
    linhas = ["", textos.t("py_rel_atualizacoes.aplicacao_de_firmware", TITULOS_MODO.get(secao['modo'], secao['modo'])), "-" * 78]
    linhas.append(textos.t("py_rel_atualizacoes.situacao", SITUACOES.get(secao.get('situacao'), secao.get('situacao'))))
    linhas += textwrap.wrap(secao.get("mensagem") or "", 78)
    for item in secao.get("itens") or []:
        linhas.append(f"  {item['componente'][:40]}: {item.get('instalada')} -> {item.get('disponivel')} ({item['situacao']})")
    for passo in secao.get("passos") or []:
        linhas += textwrap.wrap(f"[{passo['situacao']}] {passo['acao']}: {passo['detalhe']}", 78,
                                initial_indent="  ", subsequent_indent="      ")  # fmt: skip
    if _riscos(secao):
        linhas.append(textos.t("py_rel_atualizacoes.riscos_assumidos_pelo_tecnico"))
        for linha in _riscos(secao):
            linhas += textwrap.wrap(linha, 78, initial_indent="  ", subsequent_indent="    ")
    linhas += _texto_extras(secao)
    if secao.get("antes_depois"):
        linhas.append(textos.t("py_rel_atualizacoes.antes_depois_inventario_relido_ao"))
        for item in secao["antes_depois"]:
            linhas.append(f"  {str(item['componente'])[:36]}: {item['antes']} -> {item['agora'] or '?'} — {item['texto']}")
    return "\n".join(linhas)


def _riscos(secao: dict[str, Any]) -> list[str]:
    """Riscos de hardware assumidos pelo técnico com ``ASSUMO OS RISCOS`` (pedido de 09/10/2026). Vazio se não houve."""
    registro = secao.get("riscos_assumidos") or {}
    if not registro.get("assumidos"):
        return []
    return [textos.t("py_rel_atualizacoes.riscos_confirmados_em", textos.data_br(registro.get("quando")))] + [
        textos.t("py_rel_atualizacoes.risco_assumido", c.get("item"), c.get("texto"), c.get("risco")) for c in registro.get("checagens") or []]


def _texto_extras(secao: dict[str, Any]) -> list[str]:
    """Tentativas por caminho (18.3), jobs do repositório (18.7) e mudanças temporárias no iDRAC (18.4)."""
    linhas: list[str] = []
    anterior = secao.get("anterior") or {}
    tentativas = list(anterior.get("tentativas") or []) + list(secao.get("tentativas") or [])
    if tentativas:
        linhas.append(textos.t("py_rel_atualizacoes.caminhos_tentados_em_ordem"))
        for t in tentativas:
            linhas += textwrap.wrap(f"{t['caminho']}) {t['descricao']} — {t['componente']}: {t['situacao']}. {t['detalhe']}", 78,
                                    initial_indent="  ", subsequent_indent="      ")  # fmt: skip
    repositorio = secao.get("repositorio") or {}
    if repositorio:
        linhas.append(textos.t("py_rel_atualizacoes.repositorio_pelo_idrac") + (repositorio.get("mensagem") or ""))
        for job in repositorio.get("jobs") or []:
            linhas.append(textos.t("py_rel_atualizacoes.job", job.get('id'), job.get('nome'), job.get('estado'), job.get('mensagem')))
    acompanhamento = secao.get("acompanhamento") or {}
    if acompanhamento.get("itens"):
        linhas.append(textos.t("py_rel_atualizacoes.etapas_por_componente_tempo_total", acompanhamento.get('decorrido_s', '?')))
        for item in acompanhamento["itens"]:
            etapas = " → ".join(f"{e['nome']} ({e['segundos']} s)" for e in item.get("etapas") or [])
            linhas += textwrap.wrap(f"{item['componente']}: {etapas or item['nome']}" + (f" — {item['detalhe']}" if item.get("detalhe") else ""),
                                    78, initial_indent="  ", subsequent_indent="      ")  # fmt: skip
    mudancas = list(anterior.get("temporarias") or []) + list(secao.get("temporarias") or [])
    linhas.append(textos.t("py_rel_atualizacoes.mudancas_temporarias_no_idrac") + ("" if mudancas else " nenhuma"))
    for m in mudancas:
        linhas.append(f"  {m['descricao']}: {TEXTO_TEMPORARIA.get(m['situacao'], m['situacao'])}")
    return linhas


def _html_extras(secao: dict[str, Any]) -> str:
    return "<pre>" + escape("\n".join(_texto_extras(secao))) + "</pre>"


def formatar_retomada(secao: dict[str, Any] | None) -> str:
    """Texto da retomada depois do reinício (antes × depois). Vazio se não houve."""
    if not secao or secao.get("situacao") != "conferida":
        return ""
    linhas = ["", textos.t("py_rel_atualizacoes.retomada_da_atualizacao_de_firmware"), "-" * 78,
              textos.t("py_rel_atualizacoes.atualizacao_iniciada_em_inventario", secao.get('criado'), secao.get('fonte_inventario'))]  # fmt: skip
    for item in secao["itens"]:
        linhas.append(f"  {str(item['componente'])[:36]}: {item['antes']} -> {item['agora'] or '?'} — {item['texto']}")
    if secao.get("pendentes"):
        linhas += textwrap.wrap((textos.t("py_rel_atualizacoes.continua_sozinho_plano_confirmado_antes") if secao.get("continua_sozinho") else
                                 textos.t("py_rel_atualizacoes.nao_continua_sozinho_confirme_de", secao.get('motivo_nao_continua') or textos.t("py_rel_atualizacoes.motivo_nao_informado")))
                                + ", ".join(map(str, secao["pendentes"])), 78)  # fmt: skip
    return "\n".join(linhas)


def formatar_jobs_idrac(fila: dict[str, Any] | None) -> str:
    """Fila de jobs do iDRAC lida ao iniciar (21.4), traduzida. Vazio se não houve nada a mostrar."""
    if not fila or not (fila.get("jobs") or fila.get("motivo")):
        return ""
    linhas = ["", textos.t("py_rel_atualizacoes.fila_de_jobs_do_idrac"), "-" * 78]
    if not fila.get("jobs"):
        return "\n".join(linhas + textwrap.wrap(textos.t("py_rel_atualizacoes.nao_lida", fila.get('motivo')), 78))
    for texto in fila.get("explicacao") or []:
        linhas += textwrap.wrap(texto, 78)
    for job in fila["jobs"]:
        linhas += textwrap.wrap(f"[{job['grupo']}] {job['id']} {job['nome']}: {job['estado']} — {job['texto']}", 78,
                                initial_indent="  ", subsequent_indent="      ")  # fmt: skip
    return "\n".join(linhas)


def html_aplicacao(secao: dict[str, Any] | None, retomada: dict[str, Any] | None = None) -> str:
    """Blocos HTML da aplicação (simulada ou real) e da retomada."""
    partes = []
    if retomada and retomada.get("situacao") == "conferida":
        linhas = "".join(f"<tr><td>{escape(str(i['componente']))}</td><td>{escape(str(i['antes']))}</td>"
                         f"<td>{escape(str(i['agora'] or '?'))}</td><td>{escape(i['texto'])}</td></tr>" for i in retomada["itens"])  # fmt: skip
        partes.append((f"<h2>{textos.t('py_rel_atualizacoes.retomada_da_atualizacao_antes_depois')}</h2>"
                       f"<div class=\"cartao\"><div class=\"tabela\"><table><tr><th>{textos.t('py_rel_atualizacoes.componente')}</th>"
                       f"<th>{textos.t('py_rel_atualizacoes.antes')}</th><th>{textos.t('py_rel_atualizacoes.agora')}</th>"
                       f"<th>{textos.t('py_rel_atualizacoes.resultado')}</th></tr>{linhas}</table>"
                       f"</div></div>"))  # fmt: skip
    if secao and secao.get("modo"):
        passos = "".join(f"<li><strong>[{escape(p['situacao'])}] {escape(p['acao'])}</strong>: {escape(p['detalhe'])}</li>"
                         for p in secao.get("passos") or [])  # fmt: skip
        partes.append((f"<h2>{textos.t('py_rel_atualizacoes.aplicacao_de_firmware', escape(TITULOS_MODO.get(secao['modo'], secao['modo'])))}</h2>"
                       f'<div class="cartao"><strong>{escape(SITUACOES.get(secao.get("situacao"), str(secao.get("situacao"))))}</strong>'
                       f": {escape(secao.get('mensagem') or '')}{_html_extras(secao)}<details>"
                       f"<summary>{textos.t('py_rel_atualizacoes.passos')}</summary><ul>"
                       f"{passos}</ul></details></div>"))  # fmt: skip
        riscos = _riscos(secao)
        if riscos:
            partes.append(f"<h2>{textos.t('py_rel_atualizacoes.riscos_assumidos_pelo_tecnico')}</h2>"
                          f'<div class="cartao"><p>{escape(riscos[0])}</p><ul>'
                          + "".join(f"<li>{escape(r)}</li>" for r in riscos[1:]) + "</ul></div>")
        if secao.get("antes_depois"):
            linhas = "".join(f"<tr><td>{escape(str(i['componente']))}</td><td>{escape(str(i['antes']))}</td>"
                             f"<td>{escape(str(i['agora'] or '?'))}</td><td>{escape(i['texto'])}</td></tr>" for i in secao["antes_depois"])  # fmt: skip
            partes.append((f"<h2>{textos.t('py_rel_atualizacoes.antes_depois_da_aplicacao')}</h2>"
                           f"<div class=\"cartao\"><div class=\"tabela\"><table><tr><th>{textos.t('py_rel_atualizacoes.componente')}</th>"
                           f"<th>{textos.t('py_rel_atualizacoes.antes')}</th><th>{textos.t('py_rel_atualizacoes.agora')}</th>"
                           f"<th>{textos.t('py_rel_atualizacoes.resultado')}</th></tr>{linhas}</table>"
                           f"</div></div>"))  # fmt: skip
    return "\n".join(partes)


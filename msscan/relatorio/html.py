"""Relatório HTML autocontido (CSS embutido, nenhum recurso externo)."""

from __future__ import annotations

import json
from html import escape
from typing import Any

from msscan import textos
from msscan.relatorio.acoes import html_acoes
from msscan.relatorio.chat import html_chat, html_pareamentos
from msscan.relatorio.atualizacoes import html_aplicacao, html_atualizacoes
from msscan.relatorio.comparacao import html_comparacao
from msscan.relatorio.ia import html_analise_ia

CORES = {
    "OK": ("#1a7f37", "#e7f5ea", "✓"),
    "ALERTA": ("#9a6700", "#fff4d6", "⚠"),
    "FALHA": ("#c62828", "#fde8e8", "✗"),
    "NAO_VERIFICADO": ("#57606a", "#eef1f4", "?"),
    "NAO_APLICAVEL": ("#57606a", "#f6f8fa", "N/A"),
}
ROTULOS = {
    "OK": "OK",
    "ALERTA": "Alerta",
    "FALHA": "Falha",
    "NAO_VERIFICADO": textos.t("py_rel_html.nao_verificado"),
    "NAO_APLICAVEL": textos.t("py_rel_html.nao_aplicavel"),
}

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 16px; background: #f6f8fa; color: #1f2328;
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
main { max-width: 980px; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 4px; }
h2 { font-size: 1.2rem; margin: 32px 0 12px; padding-bottom: 6px; border-bottom: 2px solid #d0d7de; }
.sub { color: #57606a; margin: 0 0 16px; }
.cartao { background: #fff; border: 1px solid #d0d7de; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
.veredito { font-size: 1.1rem; font-weight: 600; }
.grade { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 8px 24px; }
.grade div span { display: block; color: #57606a; font-size: .85rem; }
.comp { border-left: 6px solid var(--cor); }
.comp h3 { margin: 0 0 4px; font-size: 1.05rem; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.selo { display: inline-block; padding: 1px 10px; border-radius: 999px; font-size: .8rem; font-weight: 600;
  color: var(--cor); background: var(--fundo); border: 1px solid var(--cor); }
.fonte { color: #57606a; font-size: .85rem; }
ul { margin: 6px 0 0; padding-left: 20px; }
li { margin: 2px 0; }
.problemas li { color: #1f2328; }
.explicacao { background: #fff8e1; border-radius: 6px; padding: 8px 12px; margin-top: 8px; }
.pendente { color: #57606a; font-style: italic; }
details { margin-top: 8px; }
summary { cursor: pointer; color: #0969da; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #d0d7de; vertical-align: top; }
.tabela { overflow-x: auto; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .85rem; }
pre { white-space: pre-wrap; word-break: break-word; background: #f6f8fa; padding: 8px; border-radius: 6px; }
footer { color: #57606a; font-size: .8rem; margin-top: 32px; text-align: center; }
.ev-ok { color: #1a7f37; font-weight: 600; }
.ev-falha { color: #cf222e; font-weight: 600; background: #ffebe9; border-radius: 4px; padding: 0 4px; }
@media print { body { background: #fff; } details { display: none; } .cartao { break-inside: avoid; } }
"""


MODOS = {"replay": textos.t("py_rel_html.modo_demonstracao")}
"""Valor interno do modo → texto do relatório (glossário: modo de demonstração)."""

def _lista(itens: list[Any], classe: str = "") -> str:
    if not itens:
        return ""
    atributo = f' class="{classe}"' if classe else ""
    return f"<ul{atributo}>" + "".join(f"<li>{escape(str(i))}</li>" for i in itens) + "</ul>"


SITUACOES_POSICAO = {"FALHA": 0, "ALERTA": 1, "CONFERIR": 2, "OK": 3}


def _posicoes(posicoes: list[dict[str, Any]]) -> str:
    """Mapa por posição (pedido de 22/10/2026): as ocupadas e as que pedem conferência; as vazias só na contagem."""
    if not posicoes:
        return ""
    visiveis = sorted((p for p in posicoes if p.get("situacao") in SITUACOES_POSICAO), key=lambda p: SITUACOES_POSICAO[p["situacao"]])
    rotulos = {"OK": ROTULOS.get("OK", "OK"), "ALERTA": ROTULOS.get("ALERTA", "ALERTA"), "FALHA": ROTULOS.get("FALHA", "FALHA"),
               "CONFERIR": textos.t("status.conferir")}  # fmt: skip
    linhas = "".join(f"<tr><td>{escape(str(p.get('local') or p.get('posicao')))}</td><td>{escape(rotulos[p['situacao']])}</td>"
                     f"<td>{escape(str(p.get('detalhe') or '—'))}</td></tr>" for p in visiveis)  # fmt: skip
    vazias = sum(1 for p in posicoes if p.get("situacao") == "VAZIO")
    rodape = f"<p class=\"fonte\">{textos.t('py_rel_html.posicoes_vazias', vazias)}</p>" if vazias else ""
    return (f"<details open><summary>{textos.t('posicoes.titulo')}</summary><div class=\"tabela\"><table><tr>"
            f"<th>{textos.t('posicoes.local')}</th><th>{textos.t('posicoes.situacao')}</th><th>{textos.t('posicoes.detalhe')}</th>"
            f"</tr>{linhas}</table></div>{rodape}</details>")


def _componente(item: dict[str, Any]) -> str:
    status = item.get("status", "NAO_VERIFICADO")
    cor, fundo, simbolo = CORES.get(status, CORES["NAO_VERIFICADO"])
    dados = item.get("dados") or {}
    partes = [
        f'<section class="cartao comp" style="--cor:{cor};--fundo:{fundo}">',
        f"<h3>{escape(item.get('titulo') or item.get('componente', ''))}"
        f'<span class="selo">{simbolo} {ROTULOS.get(status, status)}</span></h3>',
        f'<div class="fonte">Fonte: {escape(item.get("fonte") or "—")}</div>',
    ]
    if item.get("mensagem"):
        partes.append(f"<p>{escape(item['mensagem'])}</p>")
    partes.append(_lista(dados.get("problemas", []), "problemas"))
    for explicacao in dados.get("explicacoes", []):
        partes.append(f'<div class="explicacao">{escape(explicacao)}</div>')
    partes.append(_posicoes(dados.get("posicoes") or []))
    if dados.get("observacoes"):
        partes.append(f"<details><summary>{textos.t('py_rel_html.observacoes')}</summary>" + _lista(dados["observacoes"]) + "</details>")
    tentativas = dados.get("tentativas", [])
    if tentativas:
        linhas = "".join(
            f"<tr><td>{escape(t.get('provedor', ''))}</td><td>{escape(t.get('resultado', ''))}</td>"
            f"<td>{escape(t.get('detalhe', ''))}</td></tr>"
            for t in tentativas
        )
        aberto = " open" if status == "NAO_VERIFICADO" else ""
        partes.append(
            (f"<details{aberto}><summary>{textos.t('py_rel_html.tentativas', len(tentativas))}</summary>"
             f"<div class=\"tabela\"><table><tr><th>{textos.t('py_rel_html.metodo')}</th>"
             f"<th>{textos.t('py_rel_html.resultado')}</th><th>{textos.t('py_rel_html.detalhe')}</th>"
             f"</tr>{linhas}</table></div></details>")
        )
    brutos = {k: v for k, v in dados.items() if k not in {"problemas", "explicacoes", "observacoes", "tentativas"}}
    if brutos:
        partes.append(
            (f"<details><summary>{textos.t('py_rel_html.dados_medidos')}</summary>"
             f"<pre>")
            + escape(json.dumps(brutos, ensure_ascii=False, indent=2, default=str))
            + "</pre></details>"
        )
    partes.append("</section>")
    return "".join(partes)


def _equipamento(relatorio: dict[str, Any]) -> str:
    eq = relatorio.get("equipamento") or {}
    ipmi, redfish = eq.get("ipmi") or {}, eq.get("redfish") or {}
    controladoras = ", ".join(f"{c.get('nome')} ({c.get('driver') or 'sem driver'})" for c in eq.get("controladoras", []))
    campos = [
        ("Fabricante", eq.get("fabricante") or "desconhecido"),
        ("Modelo", eq.get("modelo") or "desconhecido"),
        ("BIOS", eq.get("versao_bios") or "—"),
        ("IPMI", textos.t("py_rel_html.disponivel", ipmi.get('versao')) if ipmi.get("disponivel") else (ipmi.get("motivo") or textos.t("py_rel_html.nao_disponivel"))),
        ("Redfish", textos.t("py_rel_html.disponivel_2") if redfish.get("confirmado") else (redfish.get("motivo") or textos.t("py_rel_html.nao_disponivel"))),
        (textos.t("py_rel_html.controladoras_de_armazenamento"), controladoras or "nenhuma"),
        (textos.t("py_rel_html.ferramentas_do_fabricante"), ", ".join(eq.get("ferramentas_fabricante") or {}) or "nenhuma"),
    ]
    return '<div class="cartao grade">' + "".join(
        f"<div><span>{escape(rotulo)}</span>{escape(str(valor))}</div>" for rotulo, valor in campos
    ) + "</div>"


def _rede(relatorio: dict[str, Any]) -> str:
    rede = relatorio.get("rede")
    if not rede:
        return ""
    if rede.get("sem_rede_por_parametro"):
        texto = textos.t("py_rel_html.verificacao_de_rede_desativada_pelo")
    elif rede.get("ok"):
        texto = textos.t("py_rel_html.rede_ok_cabo_endereco_ip")
    else:
        texto = rede.get("titulo") or textos.t("py_rel_html.rede_indisponivel")
    return f'<div class="cartao"><strong>Rede:</strong> {escape(texto)}{_lista(rede.get("detalhes", []))}</div>'


def _pendrive(relatorio: dict[str, Any]) -> str:
    pendrive = relatorio.get("pendrive")
    if not pendrive:
        return ""
    gravacao = pendrive.get("gravacao") or {}
    texto = (
        (f"<strong>{textos.t('py_rel_html.pendrive')}</strong> {textos.t('py_rel_html.boot', escape(str(gravacao.get('descricao', ''))), escape(str(gravacao.get('descricao_boot', ''))))}<br>"
         f"<strong>{textos.t('py_rel_html.dados')}</strong> {escape(str(pendrive.get('mensagem', '')))}")
    )
    aviso = f"<p><strong>{escape(pendrive['aviso'])}</strong></p>" if pendrive.get("aviso") else ""
    return f'<div class="cartao">{texto}{aviso}</div>'


def _bmc(relatorio: dict[str, Any]) -> str:
    secao = relatorio.get("bmc")
    if not secao:
        return ""
    partes = [(f"<strong>{textos.t('py_rel_html.acesso_ao_bmc_redfish_em', escape(str(secao.get('endereco') or '?')))}</strong>"
               f" {escape(secao['titulo'])}")]
    if secao.get("situacao") == "validado":
        partes.append(f"<br>{textos.t('py_rel_html.perfil_do_usuario', escape(str(secao.get('perfil') or '')))}")
    elif secao.get("mensagem") and secao["mensagem"] != secao["titulo"]:
        partes.append(f"<br>{escape(secao['mensagem'])}")
    itens = [f"{item['titulo']}: {item['como']}" for item in secao.get("componentes", [])]
    if secao.get("situacao") != "validado" and itens:
        partes.append(f"<br>{textos.t('py_rel_html.itens_que_teriam_o_redfish')}")
    return f'<div class="cartao">{"".join(partes)}{_lista(itens)}</div>'


def _acesso_idrac(relatorio: dict[str, Any]) -> str:
    """Caixa "Acesso ao iDRAC" (19.2) no relatório: a bancada de 16/10/2026 só tinha "Redfish não respondeu", sem o porquê."""
    estado = relatorio.get("acesso_idrac")
    if not estado:
        return ""
    linhas = [m["texto"] for m in estado.get("mensagens") or []]
    if estado.get("racadm_texto"):
        linhas.insert(0, f"racadm: {estado['racadm_texto']}")
    return (f"<div class=\"cartao\"><strong>{escape(textos.t('py_acesso_idrac.acesso_ao_idrac_0', estado.get('titulo') or ''))}"
            f"</strong>{_lista(linhas)}</div>")


def _compatibilidade(relatorio: dict[str, Any]) -> str:
    compat = (relatorio.get("equipamento") or {}).get("compatibilidade")
    if not compat:
        return ""
    if not compat.get("encontrado"):
        texto = compat.get("erro") or compat.get("mensagem") or textos.t("py_rel_html.modelo_sem_entrada_no_banco")
        return (f"<div class=\"cartao\"><strong>{textos.t('py_rel_html.banco_de_compatibilidade')}</strong>"
                f" {escape(texto)}</div>")
    entrada = compat["entrada"]
    limitacoes = [textos.t("py_rel_html.limitacao_conhecida", l) for l in entrada.get("limitacoes") or []]
    divergencias = [textos.t("py_rel_html.divergencia_com_a_deteccao", d) for d in compat.get("divergencias") or []]
    return (
        (f"<div class=\"cartao\"><strong>{textos.t('py_rel_html.banco_de_compatibilidade_dica_a')}</strong>"
         f" {escape(entrada['nome'])} — {escape(str(entrada.get('bmc')))}, {escape(str(entrada.get('geracao') or ''))}{_lista(divergencias + limitacoes)}</div>")
    )


def _ia(ia: dict[str, Any]) -> str:
    """Concluída → análise completa; não solicitada → uma linha; falhou → sem seção (CLAUDE.md, seção 10)."""
    if ia.get("situacao") == "concluida":
        return html_analise_ia(ia)
    if ia.get("situacao") == "nao_solicitada":
        return (f"<h2>{textos.t('py_rel_html.interpretacao_ia')}</h2>\n<div class=\"cartao pendente\">"
                f"{textos.t('py_rel_html.analise_por_ia_nao_solicitada')}</div>")
    return ""


def _historico(relatorio: dict[str, Any]) -> str:
    historico = relatorio.get("historico_comandos") or []
    linhas = "".join(
        f"<tr><td><code>{escape(' '.join(h.get('comando', [])))}</code></td><td>{escape(str(h.get('rc')))}</td>"
        f"<td>{escape(str(h.get('duracao')))} s</td><td>{escape(h.get('erro') or '')}</td></tr>"
        for h in historico
    )
    return (
        (f"<details><summary>{textos.t('py_rel_html.historico_de_comandos', len(historico))}</summary>"
         f"<div class=\"tabela\"><table><tr><th>{textos.t('py_rel_html.comando')}</th>"
         f"<th>{textos.t('py_rel_html.codigo')}</th><th>{textos.t('py_rel_html.duracao')}</th>"
         f"<th>{textos.t('py_rel_html.erro')}</th></tr>{linhas}</table></div>"
         f"</details>")
    )


def _configuracao(relatorio: dict[str, Any]) -> str:
    """Configuração do servidor e avisos de montagem (pedido de 01/10/2026)."""
    config = relatorio.get("configuracao") or {}
    if not config:
        return ""
    avisos = "".join(f"<li><strong>{textos.t('py_rel_html.atencao')}</strong> {escape(a)}</li>" for a in config.get("avisos") or [])
    infos = "".join(f"<li>{escape(i)}</li>" for i in config.get("informacoes") or [])
    fonte = f"<p class=\"sub\">Regras: {escape(config['fonte_regras'])}.</p>" if config.get("fonte_regras") else ""
    return ((f"<h2>{textos.t('py_rel_html.configuracao_do_servidor')}</h2>\n<div class=\"cartao\">"
             f"<p>{escape(config.get('resumo') or '')}</p><ul>{avisos}{infos}</ul>"
             f"{fonte}</div>"))


def _localizacao(relatorio: dict[str, Any]) -> str:
    """"Onde está o problema" (CLAUDE.md 15.3): mapa do chassi (se o modelo tiver) e a área de cada problema."""
    from msscan.localizacao import svg_mapa

    loc = relatorio.get("localizacao") or {}
    itens = loc.get("itens") or []
    if not itens:
        return ""
    areas = {i["area"] for i in itens if i.get("area")}
    mapa = svg_mapa(loc.get("mapa"), areas, textos.t("py_rel_html.mapa_do_com_as_areas", loc.get('modelo') or 'chassi'))
    linhas = "".join(
        f"<tr><td>{escape(str(i.get('titulo') or i['componente']))}</td><td><strong>{escape(i['local'])}</strong>"
        + (f"<br><small>{escape(i['descricao_area'])}</small>" if i.get("descricao_area") else "")
        + f"</td><td>{escape(i['problema'])}{' (' + str(i['ocorrencias']) + 'x)' if i.get('ocorrencias', 1) > 1 else ''}</td></tr>"
        for i in itens
    )
    nota = (f"<p class=\"sub\">{textos.t('py_rel_html.area_destacada_problema_contorno_vermelho', escape(str((loc.get('mapa') or {}).get('vista') or '')))}</p>" if mapa else f"<p class=\"sub\">{textos.t('py_rel_html.sem_mapa_do_chassi_para')}</p>")
    return ((f"<h2>{textos.t('py_rel_html.onde_esta_o_problema')}</h2>\n<div class=\"cartao\">"
             f"{mapa}{nota}<table><thead><tr><th>{textos.t('py_rel_html.componente')}</th>"
             f"<th>{textos.t('py_rel_html.local_fisico')}</th><th>{textos.t('py_rel_html.problema')}</th>"
             f"</tr></thead><tbody>{linhas}</tbody></table></div>"))


def gerar_html(relatorio: dict[str, Any]) -> str:
    """Gera o HTML completo do relatório (todo texto escapado)."""
    eq = relatorio.get("equipamento") or {}
    titulo = textos.t("py_rel_html.msscan", eq.get('fabricante') or '', eq.get('modelo') or '').strip()
    info = relatorio.get("msscan") or {}
    ia = relatorio.get("interpretacao_ia") or {}
    atualizacoes = relatorio.get("atualizacoes") or {}
    componentes = "".join(_componente(item) for item in relatorio.get("medido", []))
    return (f"<!doctype html>\n<html lang=\"pt-BR\">\n<head>\n<meta charset=\"utf-8\">"
            f"\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"\n<title>{escape(titulo)}</title>\n<style>{CSS}</style>\n</head>\n<body>"
            f'\n<main>\n<h1>{escape(titulo)}</h1>\n<p class="sub">{textos.t("py_rel_html.diagnostico_de_msscan_modo", escape(textos.data_br(relatorio.get("inicio"))), escape(str(info.get("versao", ""))), escape(MODOS.get(info.get("modo"), str(info.get("modo", "")))))}</p>'
            f'\n<div class="cartao veredito">{escape(str(relatorio.get("veredito", "")))}</div>'
            f'\n{html_comparacao(relatorio.get("comparacao"))}\n{_equipamento(relatorio)}\n{_rede(relatorio)}\n{_pendrive(relatorio)}\n{_bmc(relatorio)}\n{_acesso_idrac(relatorio)}\n{_compatibilidade(relatorio)}\n{_configuracao(relatorio)}\n\n{_localizacao(relatorio)}\n\n<h2>'
            f"{textos.t('py_rel_html.medido')}</h2>\n<p class=\"sub\">{textos.t('py_rel_html.dados_obtidos_diretamente_das_ferramentas')}</p>"
            f'\n{componentes}\n\n{_ia(ia)}\n{html_chat(relatorio.get("chat"))}\n{html_pareamentos(relatorio.get("pareamentos_ia"))}\n{html_acoes(relatorio.get("acoes"))}\n\n{html_atualizacoes(atualizacoes)}\n{html_aplicacao(relatorio.get("aplicacao"), relatorio.get("retomada"))}\n\n<h2>'
            f"{textos.t('py_rel_html.registro_tecnico')}</h2>\n<div class=\"cartao\">"
            f'{_historico(relatorio)}</div>\n<footer>{textos.t("py_rel_html.gerado_pelo_msscan_em", escape(str(info.get("versao", ""))), escape(textos.data_br(relatorio.get("fim"))))}</footer>'
            f"\n</main>\n</body>\n</html>\n")

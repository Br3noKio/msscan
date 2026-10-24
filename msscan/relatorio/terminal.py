"""Relatório de diagnóstico em texto para o terminal (tty1, 80 colunas)."""

from __future__ import annotations

import textwrap
from collections.abc import Callable

from msscan import textos
from msscan.core.modelo import Resultado, Status
from msscan.relatorio.simbolos import UNICODE, Simbolos

MAX_PROBLEMAS = 8

VEREDITO_SUCESSO = textos.t("py_rel_terminal.servidor_diagnosticado_com_sucesso")
VEREDITO_PARCIAL = textos.t("py_rel_terminal.servidor_parcialmente_diagnosticado")
VEREDITO_NENHUM = textos.t("py_rel_terminal.nenhum_componente_pode_ser_verificado")


def _quantidade(chave: str, n: int) -> str:
    """"nenhuma falha", "1 falha", "3 falhas" (sem o "(s)", guia de escrita)."""
    return textos.t(f"py_rel_terminal.{chave}_{n if n < 2 else 'n'}", n)


def veredito(resultados: list[Resultado]) -> str:
    """Veredito geral: cobertura do diagnóstico e, separado dela, o resultado.

    "Diagnosticado com sucesso" fala da cobertura (todos os componentes verificados), nunca da saúde: o resultado vem
    depois, explícito ("4 alertas e nenhuma falha"). Um diagnóstico parcial nunca é apresentado como "tudo saudável".
    """
    verificados = [r for r in resultados if r.status not in (Status.NAO_VERIFICADO, Status.NAO_APLICAVEL)]
    nao_verificados = len([r for r in resultados if r.status == Status.NAO_VERIFICADO])
    if not verificados:
        return VEREDITO_NENHUM
    if nao_verificados:
        cobertura = VEREDITO_PARCIAL
        detalhe = textos.t("py_rel_terminal.nao_verificado_1") if nao_verificados == 1 else \
            textos.t("py_rel_terminal.nao_verificados_n", nao_verificados)
    else:
        cobertura, detalhe = VEREDITO_SUCESSO, textos.t("py_rel_terminal.cobertura_completa")
    falhas = sum(r.status == Status.FALHA for r in resultados)
    alertas = sum(r.status == Status.ALERTA for r in resultados)
    if falhas or alertas:
        # A falha vem primeiro quando existe; com só alertas, eles vêm primeiro ("4 alertas e nenhuma falha").
        partes = (_quantidade("falha", falhas), _quantidade("alerta", alertas))
        resultado = textos.t("py_rel_terminal.e", *(partes if falhas else partes[::-1]))
    elif nao_verificados:
        resultado = textos.t("py_rel_terminal.nenhum_problema_nos_componentes_verificados")
    else:
        resultado = textos.t("py_rel_terminal.nenhum_problema_encontrado")
    return textos.t("py_rel_terminal.veredito", cobertura, detalhe, resultado)


def formatar_relatorio(
    resultados: list[Resultado],
    titulo_de: Callable[[str], str] = lambda nome: nome,
    simbolos: Simbolos = UNICODE,
) -> str:
    """Tabela de componentes com símbolo, status, fonte e, se não verificados, as tentativas."""
    linhas = ["", textos.t("py_rel_terminal.resultado_do_diagnostico"), "-" * 78]
    for r in resultados:
        fonte = r.fonte or "-"
        # Nome com 25 colunas + um espaço fixo: nome longo ("Componentes internos (backplane, cabos, slots)") não encosta na situação.
        linhas.append(f"{simbolos[r.status]:<4}{titulo_de(r.componente):<25} {r.status.value:<16}fonte: {fonte}")
        if r.mensagem:
            linhas.append(f"    {r.mensagem}")
        if r.status in (Status.ALERTA, Status.FALHA):
            problemas = r.dados.get("problemas", [])
            for problema in problemas[:MAX_PROBLEMAS]:
                linhas.append(f"      - {problema}")
            if len(problemas) > MAX_PROBLEMAS:
                linhas.append(textos.t("py_rel_terminal.e_mais_ver_relatorio_completo", len(problemas) - MAX_PROBLEMAS))
            for explicacao in r.dados.get("explicacoes", []):
                linhas.extend(textwrap.wrap(explicacao, width=78, initial_indent="      » ", subsequent_indent="        "))
        for antigo in (r.dados.get("historico") or [])[:MAX_PROBLEMAS]:
            linhas.extend(textwrap.wrap(antigo, width=78, initial_indent="      · ", subsequent_indent="        "))
        if r.status == Status.NAO_VERIFICADO:
            for t in r.dados.get("tentativas", []):
                detalhe = f": {t['detalhe']}" if t.get("detalhe") else ""
                linhas.append(textos.t("py_rel_terminal.tentativa", t['provedor'], t['resultado'], detalhe))
    linhas.append("-" * 78)
    linhas.extend(textwrap.wrap(veredito(resultados), width=78))  # tty1 com 80 colunas
    return "\n".join(linhas)


def formatar_configuracao(configuracao: dict | None) -> str:
    """Configuração do servidor (pedido de 01/10/2026): resumo, avisos de montagem e informações."""
    if not configuracao:
        return ""
    linhas = ["", configuracao.get("resumo") or textos.t("py_rel_terminal.configuracao_nao_verificada")]
    for aviso in configuracao.get("avisos") or []:
        linhas.extend(textwrap.wrap(textos.t("py_rel_terminal.atencao") + aviso, width=78, initial_indent="  - ", subsequent_indent="    "))
    for info in configuracao.get("informacoes") or []:
        linhas.extend(textwrap.wrap(info, width=78, initial_indent="  · ", subsequent_indent="    "))
    return "\n".join(linhas)


def formatar_localizacao(localizacao: dict | None) -> str:
    """"Onde está o problema" (CLAUDE.md 15.3): área física de cada problema; sem como localizar, diz isso."""
    itens = (localizacao or {}).get("itens") or []
    if not itens:
        return ""
    linhas = ["", textos.t("py_rel_terminal.onde_esta_o_problema_local") + (textos.t("py_rel_terminal.mapa_do_chassi_no_relatorio") if (localizacao or {}).get("mapa") else ""),
              "-" * 78]  # fmt: skip
    for item in itens:
        vezes = f" ({item['ocorrencias']}x)" if item.get("ocorrencias", 1) > 1 else ""
        linhas.append(f"  {item.get('titulo') or item['componente']}: {item['local']}{vezes}")
        linhas.extend(textwrap.wrap(item["problema"], width=74, initial_indent="      ", subsequent_indent="      "))
    return "\n".join(linhas)

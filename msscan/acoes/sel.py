"""Ação ``bmc.sel_exportar_limpar``: exportar o log do BMC (SEL) para o pendrive, conferir a cópia e só então limpar.

Detectar primeiro: Redfish (serviço ``Sel`` do Manager com a ação ``LogService.ClearLog``, POST com corpo vazio —
documentação da Dell e norma DMTF) → IPMI local (``ipmitool sel elist`` e ``ipmitool sel clear``). A exportação é
só leitura (feita também no modo interno ``--simular``); a limpeza passa por ``Executor.acao_bmc``/``acao_comando`` (trava).
Sem local gravável no pendrive, a ação é bloqueada: o SEL nunca é limpo sem uma cópia conferida.

MOCKUP(rodada final): ClearLog do iDRAC9 sem coleta real.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from msscan.acoes.comum import (
    ERROS_LEITURA,
    MODO_APLICAR,
    PASTA_ACOES,
    garantir_administrador,
    gerenciador,
    gravar_conferido,
    no_pendrive,
    novo_resultado,
    passo,
    redfish_validado,
    resposta_de_escrita,
)
from msscan.core.executor import COMANDO_LIMPAR_SEL, LIMPAR_SEL, MOTIVO_TRAVA_ACOES
from msscan.core.fallback import FalhaColeta
from msscan.parsers.ipmitool import parse_sel_elist
from msscan.parsers.redfish import membros, parse_entradas_log, parse_servico_log, proxima_pagina

IDENTIFICADOR = "bmc.sel_exportar_limpar"
IDS_SEL = ("sel",)
MAX_ENTRADAS = 5000


def _servico_sel(ctx: Any) -> dict[str, Any] | None:
    """Serviço de log SEL do Manager (id ``Sel``), com o alvo do ClearLog, ou ``None``."""
    cliente = ctx.bmc.cliente
    _, gerente = gerenciador(ctx)
    if not gerente.get("logs"):
        return None
    for membro in membros(cliente.get(gerente["logs"])):
        servico = parse_servico_log(cliente.get(membro))
        if str(servico["id"] or "").lower() in IDS_SEL:
            return servico
    return None


def _entradas_redfish(ctx: Any, caminho: str) -> list[dict[str, Any]]:
    """Todas as entradas (leitura nova, sem cache). Levanta ``FalhaColeta`` se passar do limite."""
    cliente = ctx.bmc.cliente
    entradas: list[dict[str, Any]] = []
    pagina: str | None = caminho
    while pagina:
        dados = cliente.get(pagina)
        lote = parse_entradas_log(dados)
        if lote and not any(e["completa"] for e in lote):
            lote = [e for membro in membros(dados) for e in parse_entradas_log({"Members": [cliente.get(membro)]})]
        entradas += lote
        if len(entradas) > MAX_ENTRADAS:
            raise FalhaColeta(f"mais de {MAX_ENTRADAS} eventos: exportação completa não garantida")
        pagina = proxima_pagina(dados)
    return entradas


def _ler_ipmi(executor: Any) -> tuple[list[dict[str, str]], str]:
    rc, saida, erro = executor.rodar(["ipmitool", "sel", "elist"], timeout=120)
    if rc != 0:
        raise FalhaColeta(f"ipmitool sel elist: {erro.strip() or f'código {rc}'}")
    return parse_sel_elist(saida), saida


def ler(ctx: Any, executor: Any, metodo: str, servico: dict[str, Any] | None) -> tuple[list[dict[str, Any]], str]:
    """``(eventos, texto bruto)`` pelo método escolhido. Levanta ``FalhaColeta``/``ErroParser``."""
    if metodo == "redfish":
        return _entradas_redfish(ctx, str((servico or {}).get("entradas"))), ""
    return _ler_ipmi(executor)


def prechecar(ctx: Any, executor: Any, persistente: bool) -> dict[str, Any]:
    """Só leitura: método, quantidade de eventos, prévia e bloqueios. Nunca levanta exceção."""
    pre: dict[str, Any] = {"acao": IDENTIFICADOR, "titulo": "Exportar e limpar o log do BMC (SEL)", "metodo": None, "alvo": None,
                           "servico": None, "parametros": {}, "antes": {}, "previa": [], "avisos": [], "bloqueios": [],
                           "nada_a_fazer": False, "pode_seguir": False}  # fmt: skip
    if not persistente:
        pre["bloqueios"].append("Sem local gravável no pendrive: o SEL só é limpo depois de exportado e conferido no pendrive.")
    servico = None
    if redfish_validado(ctx):
        try:
            servico = _servico_sel(ctx)
        except ERROS_LEITURA as falha:
            pre["avisos"].append(f"Redfish: leitura do serviço de log falhou ({falha}); tentando pelo IPMI.")
        if servico and servico.get("limpar") and servico.get("entradas"):
            pre["metodo"], pre["alvo"], pre["servico"] = "redfish", servico["limpar"], servico
        elif servico is not None:
            pre["avisos"].append("Redfish: o serviço SEL não anuncia a ação ClearLog; tentando pelo IPMI.")
    if pre["metodo"] is None and getattr(getattr(ctx, "ipmi", None), "disponivel", False) and executor.tem_comando("ipmitool"):
        pre["metodo"] = "ipmi"
    if pre["metodo"] is None:
        pre["bloqueios"].append("Nem o Redfish (serviço SEL com ClearLog, com credenciais) nem o IPMI local estão disponíveis: "
                                "limpe o log pela interface do BMC.")  # fmt: skip
        return pre
    try:
        eventos, _ = ler(ctx, executor, pre["metodo"], servico)
    except ERROS_LEITURA as falha:
        pre["bloqueios"].append(f"Leitura do SEL falhou: {falha}.")
        return pre
    pre["antes"] = {"eventos": len(eventos)}
    if not eventos:
        pre["nada_a_fazer"] = True
        pre["avisos"].append("O SEL já está vazio: nada a limpar.")
        return pre
    pedido = f"POST {pre['alvo']} {{}}" if pre["metodo"] == "redfish" else " ".join(COMANDO_LIMPAR_SEL)
    pre["previa"] = [f"Exportar {len(eventos)} evento(s) para MSSCAN/{PASTA_ACOES}/ (JSON; texto do ipmitool, se for o caso)",
                     "Conferir a cópia (reler o arquivo, quantidade de eventos e SHA-256)",
                     f"Só então limpar o SEL: {pedido}", f"Eventos no SEL: {len(eventos)} → 0 (o BMC costuma registrar "
                     "um evento de \"log limpo\")"]  # fmt: skip
    pre["pode_seguir"] = not pre["bloqueios"]
    return pre


def exportar(eventos: list[dict[str, Any]], bruto: str, metodo: str, pasta_dados: str) -> tuple[Path, str]:
    """Grava e confere a cópia: ``(arquivo, sha256)``. Levanta ``OSError``/``ValueError`` se a conferência falhar.

    Conferência: o arquivo é relido do pendrive (SHA-256 igual ao gravado) e a quantidade de eventos relida bate.
    Quem chama abre a janela de escrita do pendrive (``escrita()``).
    """
    destino = Path(pasta_dados) / PASTA_ACOES / f"sel_{datetime.now():%Y-%m-%d_%H%M%S}.json"
    conteudo = {"exportado": datetime.now().isoformat(timespec="seconds"), "metodo": metodo, "quantidade": len(eventos),
                "eventos": eventos, "ipmitool_sel_elist": bruto or None}  # fmt: skip
    soma = gravar_conferido(destino, (json.dumps(conteudo, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    if len(json.loads(destino.read_bytes())["eventos"]) != len(eventos):
        raise ValueError("a cópia relida do pendrive não tem todos os eventos")
    return destino, soma


def executar(ctx: Any, executor: Any, pre: dict[str, Any], modo: str, pasta_dados: str | None,
             pedir_administrador: Any = None) -> dict[str, Any]:  # fmt: skip
    """Exporta e confere (sempre); limpa só em ``aplicar`` e só com a cópia conferida. Nunca levanta exceção."""
    resultado = novo_resultado(pre, modo)
    resultado["desfazer"] = {"disponivel": False, "motivo": "eventos apagados não voltam ao BMC; a cópia fica no pendrive"}
    if not pasta_dados:
        resultado["situacao"] = "falhou"
        resultado["mensagem"] = "Sem local gravável no pendrive: o SEL NÃO foi limpo."
        passo(resultado, "exportar", resultado["mensagem"], "falhou")
        return resultado
    try:
        eventos, bruto = ler(ctx, executor, pre["metodo"], pre.get("servico"))
        arquivo, soma = exportar(eventos, bruto, pre["metodo"], str(pasta_dados))
    except (*ERROS_LEITURA, OSError, ValueError) as falha:
        resultado["situacao"] = "falhou"
        resultado["mensagem"] = f"Exportação não concluída ou não conferida ({falha}): o SEL NÃO foi limpo."
        passo(resultado, "exportar", resultado["mensagem"], "falhou")
        return resultado
    local = no_pendrive(arquivo, pasta_dados)
    resultado["exportacao"] = {"arquivo": str(arquivo), "no_pendrive": local, "sha256": soma, "eventos": len(eventos)}
    passo(resultado, "exportar", f"{len(eventos)} evento(s) em {local} (relido do pendrive; SHA-256 {soma[:16]}… conferido)")
    if modo != MODO_APLICAR:
        pedido = f"POST {pre['alvo']} {{}}" if pre["metodo"] == "redfish" else " ".join(COMANDO_LIMPAR_SEL)
        passo(resultado, "limpar (simulado)", pedido, "simulado")
        resultado["situacao"] = "simulada"
        resultado["mensagem"] = f"Simulação concluída: SEL exportado e conferido em {local}; nada foi limpo no BMC."
        return resultado
    if pre["metodo"] == "redfish":
        if not garantir_administrador(ctx, pedir_administrador, resultado):
            return resultado
        if not resposta_de_escrita(resultado, "limpar", ctx.bmc.cliente.acao(LIMPAR_SEL, pre["alvo"], {})):
            return resultado
    else:
        rc, _, erro = executor.acao_comando(list(COMANDO_LIMPAR_SEL))
        if rc != 0:
            trava = erro == MOTIVO_TRAVA_ACOES
            resultado["situacao"] = "bloqueada_trava" if trava else "falhou"
            resultado["mensagem"] = erro.strip() or f"ipmitool sel clear: código {rc}"
            passo(resultado, "limpar", resultado["mensagem"], "bloqueado" if trava else "falhou")
            return resultado
        passo(resultado, "limpar", "ipmitool sel clear concluído")
    try:
        depois, _ = ler(ctx, executor, pre["metodo"], pre.get("servico"))
        resultado["depois"] = {"eventos": len(depois)}
    except ERROS_LEITURA as falha:
        passo(resultado, "conferir", f"releitura do SEL falhou: {falha}", "aviso")
    restantes = (resultado["depois"] or {}).get("eventos")
    if restantes is not None and restantes < len(eventos):
        resultado["situacao"] = "aplicada"
        resultado["mensagem"] = f"SEL limpo: {len(eventos)} → {restantes} evento(s). Cópia conferida em {local}."
        passo(resultado, "conferir", resultado["mensagem"])
    else:
        resultado["situacao"] = "aplicada_sem_confirmacao"
        resultado["mensagem"] = f"O BMC aceitou o pedido, mas a releitura não mostra o SEL limpo (confira no BMC). Cópia em {local}."
        passo(resultado, "conferir", resultado["mensagem"], "aviso")
    return resultado

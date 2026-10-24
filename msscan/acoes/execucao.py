"""Despacho das ações corretivas: pré-checagem (só leitura) e execução confirmada, pelo identificador do catálogo.

Usado igualmente pelo modo texto (``main.py``) e pela interface (``msscan/interface/sessao.py``): a interface não
duplica lógica. Nada aqui roda sozinho: toda execução exige a pré-checagem aprovada e ``CORRIGIR`` digitado.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from msscan.acoes import idrac, ntp, sel
from msscan.acoes.catalogo import CATALOGO
from msscan.acoes.comum import (
    CONFIRMACAO,
    CONFIRMACAO_DESFAZER,
    MODO_SIMULAR,
    conferir_confirmacao,
    novo_resultado,
    passo,
)


def sugestoes_ntp(executor: Any) -> list[dict[str, str]]:
    """Servidores NTP para oferecer ao técnico (DHCP da rede do cliente, senão ``pool.ntp.org``). Só leitura."""
    return ntp.sugestoes(executor)


def prechecar(identificador: str, ctx: Any, executor: Any, parametros: dict[str, Any] | None = None,
              persistente: bool = False, sugeridos: list[dict[str, str]] | None = None) -> dict[str, Any]:  # fmt: skip
    """Pré-checagem de uma ação do catálogo. Nunca levanta exceção; identificador fora do catálogo é recusado."""
    entrada = CATALOGO.get(identificador)
    if entrada is None:
        return {"acao": identificador, "titulo": identificador, "bloqueios": [f"Ação '{identificador}' não existe no catálogo."],
                "avisos": [], "previa": [], "antes": {}, "parametros": {}, "nada_a_fazer": False, "pode_seguir": False}  # fmt: skip
    try:
        if entrada["modulo"] == "ntp":
            return ntp.prechecar(ctx, parametros, persistente=persistente,
                                 sugeridos=sugeridos if sugeridos is not None else ntp.sugestoes(executor))  # fmt: skip
        if identificador == idrac.VER:
            return idrac.prechecar_ver(ctx)
        if identificador == idrac.IP_FIXO:
            return idrac.prechecar_ip_fixo(ctx, executor, parametros, persistente)
        return sel.prechecar(ctx, executor, persistente)
    except Exception as erro:  # noqa: BLE001 - a ação nunca derruba o programa
        return {"acao": identificador, "titulo": entrada["titulo"], "bloqueios": [f"Erro inesperado na pré-checagem: {erro}"],
                "avisos": [], "previa": [], "antes": {}, "parametros": {}, "nada_a_fazer": False, "pode_seguir": False}  # fmt: skip


def executar(pre: dict[str, Any], confirmacao: str, ctx: Any, executor: Any, pasta_dados: str | None,
             modo: str = MODO_SIMULAR, pedir_administrador: Any = None) -> dict[str, Any]:  # fmt: skip
    """Executa uma ação pré-checada e confirmada. Nunca levanta exceção. ``modo`` simular (padrão) ou aplicar."""
    motivo = conferir_confirmacao(confirmacao)
    if motivo or not pre.get("pode_seguir"):
        resultado = novo_resultado(pre, modo)
        resultado["situacao"] = "cancelada" if motivo else "bloqueada"
        resultado["mensagem"] = motivo or "; ".join(pre.get("bloqueios") or []) or "nada a alterar"
        passo(resultado, "confirmação" if motivo else "pré-checagem", resultado["mensagem"], "bloqueado")
        resultado["fim"] = datetime.now().isoformat(timespec="seconds")
        return resultado
    try:
        if CATALOGO[pre["acao"]]["modulo"] == "ntp":
            resultado = ntp.executar(ctx, pre, modo, pedir_administrador, pasta_dados)
        elif CATALOGO[pre["acao"]]["modulo"] == "idrac":
            resultado = idrac.executar(ctx, executor, pre, modo, pasta_dados)
        else:
            resultado = sel.executar(ctx, executor, pre, modo, pasta_dados, pedir_administrador)
    except Exception as erro:  # noqa: BLE001
        resultado = novo_resultado(pre, modo)
        resultado["situacao"], resultado["mensagem"] = "falhou", f"erro inesperado: {type(erro).__name__}: {erro}"
    for copia in ("exportacao", "anteriores"):
        local = (resultado.get(copia) or {}).get("no_pendrive")
        if local and local not in resultado["mensagem"]:
            resultado["mensagem"] += f" Cópia no pendrive: {local}."
    resultado["previa"] = pre.get("previa") or []
    resultado["fim"] = datetime.now().isoformat(timespec="seconds")
    return resultado


def prechecar_desfazer(resultado: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Pré-checagem do "Desfazer" de uma ação já executada (só leitura). Nunca levanta exceção."""
    entrada = CATALOGO.get(str(resultado.get("acao")))
    base = {"acao": resultado.get("acao"), "titulo": "Desfazer", "desfazer": True, "avisos": [], "previa": [], "antes": {},
            "nada_a_fazer": False, "pode_seguir": False}  # fmt: skip
    if entrada is None or entrada["modulo"] not in ("ntp", "idrac"):
        motivo = (resultado.get("desfazer") or {}).get("motivo") or "esta ação não pode ser desfeita"
        return {**base, "bloqueios": [motivo[0].upper() + motivo[1:] + "."]}
    try:
        if entrada["modulo"] == "idrac":
            return idrac.prechecar_desfazer(ctx, resultado)
        return ntp.prechecar_desfazer(ctx, resultado)
    except Exception as erro:  # noqa: BLE001
        return {**base, "bloqueios": [f"Erro inesperado na pré-checagem: {erro}"]}


def desfazer(pre: dict[str, Any], resultado: dict[str, Any], confirmacao: str, ctx: Any, modo: str = MODO_SIMULAR,
             pedir_administrador: Any = None, executor: Any = None, pasta_dados: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Desfaz uma ação pré-checada, com ``DESFAZER`` digitado. Mesmo modo e mesma trava da ação. Nunca levanta exceção."""
    motivo = conferir_confirmacao(confirmacao, CONFIRMACAO_DESFAZER)
    if motivo or not pre.get("pode_seguir"):
        registro = {"modo": modo, "situacao": "cancelada" if motivo else "bloqueada", "passos": [],
                    "mensagem": motivo or "; ".join(pre.get("bloqueios") or []) or "nada a desfazer"}  # fmt: skip
        return registro
    try:
        if (CATALOGO.get(str(pre.get("acao"))) or {}).get("modulo") == "idrac":
            return idrac.desfazer(ctx, executor if executor is not None else ctx.executor, pre, resultado, modo, pasta_dados)
        return ntp.desfazer(ctx, pre, resultado, modo, pedir_administrador)
    except Exception as erro:  # noqa: BLE001
        registro = {"modo": modo, "situacao": "falhou", "passos": [], "mensagem": f"erro inesperado: {type(erro).__name__}: {erro}"}
        resultado.setdefault("tentativas_desfazer", []).append(registro)
        return registro


__all__ = ["CONFIRMACAO", "CONFIRMACAO_DESFAZER", "desfazer", "executar", "prechecar", "prechecar_desfazer", "sugestoes_ntp"]

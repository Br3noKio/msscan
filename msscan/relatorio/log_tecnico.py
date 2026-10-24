"""Log técnico para o técnico mandar quando algo falhar (CLAUDE.md 18.6): sem senhas, chaves nem dados do cliente.

Leva o que explica uma falha: versão, modo de boot e local dos dados, os comandos e pedidos ao BMC com código de saída e
duração (o executor já mascara senhas e nunca registra tokens nem corpos), o estado da atualização (plano, caminhos
tentados, jobs, mudanças temporárias e se foram desfeitas, retomada) e o resumo do diagnóstico (componente, status e
mensagem). Não leva: chave da IA, conversa do chat, análise da IA, saídas brutas dos comandos, nem o conteúdo de arquivos.
Identificadores (serial, service tag, UUID, MAC, IP, hostname, WWN) viram marcadores, como no envio à IA.

Função pura: dados → texto JSON. Quem chama grava no pendrive (``MSSCAN/logs/``).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from msscan import textos
from msscan.ia.sanitizar import Sanitizador

PASTA_LOGS = "logs"
CHAVES_SECRETAS = re.compile(r"senha|password|passwd|token|chave|api[_-]?key|x-auth|secret|credencia", re.I)
SECOES_RELATORIO = ("atualizacoes", "aplicacao", "retomada", "jobs_idrac", "acoes", "temporarias_desfeitas", "bmc", "acesso_idrac")
MAX_HISTORICO = 2000


def _sem_segredos(dados: Any) -> Any:
    if isinstance(dados, dict):
        return {k: ("[REMOVIDO]" if CHAVES_SECRETAS.search(str(k)) else _sem_segredos(v)) for k, v in dados.items()}
    if isinstance(dados, list):
        return [_sem_segredos(v) for v in dados]
    return dados


def _equipamento(relatorio: dict[str, Any]) -> dict[str, Any]:
    equipamento = relatorio.get("equipamento") or {}
    return {k: equipamento.get(k) for k in ("fabricante", "modelo", "versao_bios", "bmc", "redfish", "ipmi") if k in equipamento}


def montar(versao: str, relatorio: dict[str, Any] | None, historico: list[dict[str, Any]], estado: dict[str, Any],
           nomes_host: list[str] | None = None, agora: datetime | None = None) -> str:  # fmt: skip
    """Texto JSON do log técnico, sanitizado."""
    relatorio = relatorio or {}
    conteudo = {
        "msscan": versao,
        "gerado": (agora or datetime.now()).isoformat(timespec="seconds"),
        "aviso": textos.t("py_rel_log_tecnico.log_tecnico_sanitizado_sem_senhas"),
        "equipamento": _equipamento(relatorio),
        "pendrive": {k: (relatorio.get("pendrive") or {}).get(k) for k in ("gravacao", "modo_boot", "persistente", "origem")},
        "diagnostico": [{"componente": m.get("componente"), "status": m.get("status"), "fonte": m.get("fonte"),
                         "mensagem": m.get("mensagem")} for m in relatorio.get("medido") or []],  # fmt: skip
        **{secao: relatorio.get(secao) for secao in SECOES_RELATORIO if relatorio.get(secao) is not None},
        "estado_da_interface": estado,
        "comandos": historico[-MAX_HISTORICO:],
    }
    sanitizador = Sanitizador({nome: "HOSTNAME" for nome in nomes_host or []})
    limpo = _sem_segredos(conteudo)
    sanitizador.coletar(limpo)
    return json.dumps(sanitizador.dados(limpo), ensure_ascii=False, indent=1, default=str) + "\n"


def nome_arquivo(agora: datetime | None = None) -> str:
    return f"log_tecnico_{(agora or datetime.now()):%Y-%m-%d_%H%M%S}.json"


__all__ = ["PASTA_LOGS", "montar", "nome_arquivo"]

"""Envio automático dos dados do servidor para o banco da MSServer (CLAUDE.md 33, pedido do usuário de 29/10/2026).

Sem botão: sempre que o MSscan tem internet e um relatório novo (diagnóstico concluído, atualizações verificadas,
instalação terminada), manda uma cópia ANONIMIZADA para o banco, para acumular dados para as próximas versões.
Anonimização: a mesma dos dados da IA (``msscan.ia.sanitizar``): sem serial, service tag, UUID, asset tag, MAC, IP,
hostname e WWN; sem histórico de comandos, pendrive, senhas nem chaves. O mesmo conteúdo não é enviado duas vezes.
Falhou (sem internet, banco fora do ar): tenta de novo quando a rede voltar ou o relatório mudar; nunca atrapalha o resto.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from msscan import __version__
from msscan.ia.sanitizar import Sanitizador, montar_dados_ia

FORMATO = "msscan-coleta-1"
TAMANHO_MAXIMO = 2_000_000
"""Bytes do JSON enviado (o receptor recusa acima disso)."""


def montar(relatorio: dict[str, Any], nomes_host: list[str] | None = None) -> dict[str, Any]:
    """Dados anonimizados do relatório. Função pura (sem data nem id: servem para a impressão digital)."""
    recorte = montar_dados_ia(relatorio)
    for chave in ("atualizacoes", "aplicacao", "configuracao"):
        if relatorio.get(chave):
            recorte[chave] = relatorio[chave]
    sanitizador = Sanitizador({nome: "HOSTNAME" for nome in nomes_host or []})
    sanitizador.coletar(recorte)
    return sanitizador.dados(recorte)


def impressao(dados: dict[str, Any]) -> str:
    """SHA-256 do conteúdo: o mesmo relatório não é enviado de novo."""
    return hashlib.sha256(json.dumps(dados, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def corpo(dados: dict[str, Any]) -> dict[str, Any]:
    """Envelope enviado: formato, versão do MSscan, horário (UTC) e um id aleatório do envio (não identifica o servidor)."""
    return {"formato": FORMATO, "versao_msscan": __version__, "enviado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "id": uuid.uuid4().hex, "dados": dados}  # fmt: skip


def enviar(executor: Any, relatorio: dict[str, Any], nomes_host: list[str] | None, ultima: str | None) -> tuple[str | None, str]:
    """Envia se o conteúdo mudou desde ``ultima``. Devolve ``(impressão enviada ou None, situação)``. Nunca levanta exceção."""
    try:
        dados = montar(relatorio, nomes_host)
        atual = impressao(dados)
        if atual == ultima:
            return ultima, "ja_enviado"
        envio = corpo(dados)
        if len(json.dumps(envio, ensure_ascii=False, default=str).encode("utf-8")) > TAMANHO_MAXIMO:
            return None, "grande_demais"
        resposta = executor.enviar_dados_servidor(envio)
    except Exception as erro:  # noqa: BLE001 — o envio nunca derruba a sessão
        return None, f"erro: {erro}"
    if resposta.status is not None and 200 <= resposta.status < 300:
        return atual, "enviado"
    return None, resposta.erro or f"HTTP {resposta.status}"

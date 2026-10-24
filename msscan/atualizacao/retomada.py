"""Retomada depois do reinício (CLAUDE.md 11: "reinício → retomada → antes × depois").

O estado ``estado/atualizacao_pendente.json`` é gravado pela aplicação antes de reiniciar. Na volta pelo pendrive,
o MSscan lê o estado, confere no inventário a versão de cada item enviado e mostra antes × depois. Os itens
seguintes do plano (já confirmado com ``ATUALIZAR``) continuam sozinhos, relendo o hardware antes
(``automatica.continuar``, CLAUDE.md 18.2).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from msscan.atualizacao.aplicacao import ARQUIVO_ESTADO
from msscan.core.executor import Executor

APLICADA, NAO_APLICADA, NAO_VERIFICADA = "aplicada", "nao_aplicada", "nao_verificada"
TEXTOS = {
    APLICADA: "aplicada (versão nova instalada)",
    NAO_APLICADA: "NÃO aplicada (versão anterior continua instalada)",
    NAO_VERIFICADA: "não foi possível verificar a versão instalada",
}


def ler_estado(executor: Executor, pasta_dados: str | None) -> dict[str, Any] | None:
    """Estado pendente, ou ``None`` (sem arquivo, ilegível ou de outro formato). Lido pelo executor (replay/coleta)."""
    if not pasta_dados:
        return None
    texto = executor.ler_arquivo(f"{pasta_dados}/{ARQUIVO_ESTADO}")
    if not texto:
        return None
    try:
        estado = json.loads(texto)
    except ValueError:
        return None
    if not isinstance(estado, dict) or estado.get("versao") != 1 or not isinstance(estado.get("enviados"), list):
        return None
    return estado


def conferir(estado: dict[str, Any], inventario: list[dict[str, Any]], fonte: str) -> dict[str, Any]:
    """Antes × depois de cada item enviado, pelo inventário lido agora (componentID; senão, nome)."""
    por_id = {str(i["component_id"]): i for i in inventario if i.get("component_id")}
    por_nome = {i["nome"]: i for i in inventario}
    itens = []
    for enviado in estado.get("enviados") or []:
        atual = por_id.get(str(enviado.get("component_id") or "")) or por_nome.get(enviado.get("componente"))
        versao = atual.get("versao") if atual else None
        if versao and versao == enviado.get("disponivel"):
            resultado = APLICADA
        elif versao and versao == enviado.get("instalada"):
            resultado = NAO_APLICADA
        else:
            resultado = NAO_VERIFICADA
        itens.append({"componente": enviado.get("componente"), "antes": enviado.get("instalada"),
                      "esperada": enviado.get("disponivel"), "agora": versao, "resultado": resultado,
                      "texto": TEXTOS[resultado]})  # fmt: skip
    return {"situacao": "conferida", "criado": estado.get("criado"), "fonte_inventario": fonte, "itens": itens,
            "pendentes": [p.get("componente") for p in estado.get("pendentes") or []]}  # fmt: skip


def arquivar_estado(pasta_dados: str) -> Path | None:
    """Tira o estado de "pendente" (renomeia com a data): a retomada não se repete no próximo boot."""
    origem = Path(pasta_dados) / ARQUIVO_ESTADO
    if not origem.is_file():
        return None
    destino = origem.with_name(f"atualizacao_concluida_{datetime.now():%Y-%m-%d_%H%M%S}.json")
    os.replace(origem, destino)
    return destino

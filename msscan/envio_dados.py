"""Envio automático dos dados do servidor para o banco da MSServer (CLAUDE.md 33, pedido do usuário de 29/10/2026).

Sem botão: sempre que o MSscan tem internet e um relatório novo (diagnóstico concluído, atualizações verificadas,
instalação terminada), manda uma cópia ANONIMIZADA para o banco, para acumular dados para as próximas versões.
Anonimização: a mesma dos dados da IA (``msscan.ia.sanitizar``): sem serial, service tag, UUID, asset tag, MAC, IP,
hostname e WWN; sem histórico de comandos, pendrive, senhas nem chaves. O mesmo conteúdo não é enviado duas vezes.
Falhou (sem internet, banco fora do ar): tenta de novo quando a rede voltar ou o relatório mudar; nunca atrapalha o resto.
A cópia que falhou fica no pendrive (``envios_pendentes/``) e vai no próximo envio que der certo, mesmo em outro boot.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from msscan import __version__
from msscan.ia.sanitizar import Sanitizador, montar_dados_ia

FORMATO = "msscan-coleta-1"
TAMANHO_MAXIMO = 2_000_000
"""Bytes do JSON enviado (o receptor recusa acima disso)."""
PASTA_PENDENTES = "envios_pendentes"
"""Subpasta da pasta de dados (pendrive) com os envios que falharam, um JSON por conteúdo (nome = impressão)."""


def _montar(relatorio: dict[str, Any], nomes_host: list[str] | None) -> tuple[dict[str, Any], dict[str, int]]:
    recorte = montar_dados_ia(relatorio)
    for chave in ("atualizacoes", "aplicacao", "configuracao"):
        if relatorio.get(chave):
            recorte[chave] = relatorio[chave]
    sanitizador = Sanitizador({nome: "HOSTNAME" for nome in nomes_host or []})
    sanitizador.coletar(recorte)
    return sanitizador.dados(recorte), sanitizador.contagem()


def montar(relatorio: dict[str, Any], nomes_host: list[str] | None = None) -> dict[str, Any]:
    """Dados anonimizados do relatório. Função pura (sem data nem id: servem para a impressão digital)."""
    return _montar(relatorio, nomes_host)[0]


def previa(relatorio: dict[str, Any], nomes_host: list[str] | None = None) -> dict[str, Any]:
    """"Ver o que é enviado" (tela Início): os dados exatos que vão ao banco e a contagem do que foi removido."""
    dados, removidos = _montar(relatorio, nomes_host)
    return {"dados": dados, "removidos": removidos}


def impressao(dados: dict[str, Any]) -> str:
    """SHA-256 do conteúdo: o mesmo relatório não é enviado de novo."""
    return hashlib.sha256(json.dumps(dados, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def corpo(dados: dict[str, Any]) -> dict[str, Any]:
    """Envelope enviado: formato, versão do MSscan, horário (UTC) e um id aleatório do envio (não identifica o servidor)."""
    return {"formato": FORMATO, "versao_msscan": __version__, "enviado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "id": uuid.uuid4().hex, "dados": dados}  # fmt: skip


def _aceito(resposta: Any) -> bool:
    return resposta.status is not None and 200 <= resposta.status < 300


def enviar(executor: Any, relatorio: dict[str, Any], nomes_host: list[str] | None, ultima: str | None,
           pasta: str | None = None) -> tuple[str | None, str]:
    """Envia se o conteúdo mudou desde ``ultima``. Devolve ``(impressão enviada ou None, situação)``. Nunca levanta exceção.

    Com ``pasta`` (pasta de dados do pendrive): se falhar, guarda o envio em ``envios_pendentes/``; se der certo, apaga a
    cópia guardada deste mesmo conteúdo (não vai duas vezes pela fila).
    """
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
    pendente = Path(pasta, PASTA_PENDENTES, f"{atual}.json") if pasta else None
    try:
        if _aceito(resposta):
            if pendente:
                pendente.unlink(missing_ok=True)
            return atual, "enviado"
        if pendente:
            pendente.parent.mkdir(exist_ok=True)
            pendente.write_text(json.dumps(envio, ensure_ascii=False, default=str), encoding="utf-8")
    except OSError:
        pass  # pendrive só leitura ou cheio: perde só a fila, nunca a sessão
    return None, resposta.erro or f"HTTP {resposta.status}"


def reenviar_pendentes(executor: Any, pasta: str | None) -> int:
    """Manda os envios guardados no pendrive e apaga cada um que o banco aceitar; para no primeiro que falhar.

    Devolve quantos foram aceitos. Nunca levanta exceção. Arquivo ilegível é descartado.
    """
    # ponytail: fila sem limite de tamanho (cada envio tem até 2 MB); limitar se um pendrive ficar sem rede por meses.
    if not pasta:
        return 0
    aceitos = 0
    try:
        for arquivo in sorted(Path(pasta, PASTA_PENDENTES).glob("*.json")):
            try:
                corpo_salvo = json.loads(arquivo.read_text(encoding="utf-8"))
            except ValueError:
                arquivo.unlink(missing_ok=True)
                continue
            if not _aceito(executor.enviar_dados_servidor(corpo_salvo)):
                break
            arquivo.unlink(missing_ok=True)
            aceitos += 1
    except Exception:  # noqa: BLE001 — a fila nunca derruba a sessão
        pass
    return aceitos

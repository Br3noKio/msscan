"""Banco de compatibilidade (``compat_db/*.yaml``): dicas por modelo e divergências com a detecção.

O banco é DICA, a detecção é a verdade: nada aqui muda o status de um componente.
Para atualização de firmware, o banco só pode RESTRINGIR (``bloqueios``), nunca liberar.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from msscan.deteccao.contexto import Contexto

PASTA_PADRAO = Path(__file__).resolve().parents[2] / "compat_db"
"""``<raiz do projeto>/compat_db`` (na ISO: ``/opt/msscan/compat_db``)."""
CAMPOS_MODELO = ("match", "nome", "bmc", "redfish", "controladoras", "ferramentas", "limitacoes", "atualizacao")


class ErroBanco(Exception):
    """Arquivo do banco ilegível ou fora do formato esperado."""


def _validar(dados: Any, arquivo: str) -> dict[str, Any]:
    if not isinstance(dados, dict) or not isinstance(dados.get("modelos"), list):
        raise ErroBanco(f"{arquivo}: esperado um mapa com 'fabricante', 'match_fabricante' e 'modelos'")
    for chave in ("fabricante", "match_fabricante"):
        if not isinstance(dados.get(chave), str):
            raise ErroBanco(f"{arquivo}: campo '{chave}' ausente")
    for i, modelo in enumerate(dados["modelos"]):
        faltando = [c for c in CAMPOS_MODELO if not isinstance(modelo, dict) or c not in modelo]
        if faltando:
            raise ErroBanco(f"{arquivo}: modelo #{i + 1} sem {', '.join(faltando)}")
        try:
            re.compile(modelo["match"])
        except re.error as erro:
            raise ErroBanco(f"{arquivo}: 'match' inválido no modelo #{i + 1}: {erro}") from erro
    return dados


def carregar_banco(pasta: str | Path = PASTA_PADRAO) -> list[dict[str, Any]]:
    """Lê todos os ``*.yaml`` da pasta. Levanta ``ErroBanco`` se algum for inválido."""
    try:
        import yaml
    except ImportError as erro:  # pragma: no cover - python3-yaml está na ISO
        raise ErroBanco("python3-yaml não instalado") from erro
    banco = []
    for arquivo in sorted(Path(pasta).glob("*.yaml")):
        try:
            dados = yaml.safe_load(arquivo.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as erro:
            raise ErroBanco(f"{arquivo.name}: {erro}") from erro
        banco.append({**_validar(dados, arquivo.name), "_arquivo": arquivo.name})
    return banco


def consultar(banco: list[dict[str, Any]], fabricante: str | None, modelo: str | None) -> tuple[dict[str, Any] | None, str]:
    """Entrada do modelo (comparação EXATA com o modelo inteiro) e o arquivo de origem."""
    if not fabricante or not modelo:
        return None, ""
    for dados in banco:
        if not re.fullmatch(dados["match_fabricante"], fabricante.strip(), re.IGNORECASE):
            continue
        for entrada in dados["modelos"]:
            if re.fullmatch(entrada["match"], modelo.strip(), re.IGNORECASE):
                return entrada, dados["_arquivo"]
    return None, ""


def divergencias(entrada: dict[str, Any], ctx: Contexto) -> list[str]:
    """Diferenças entre o que o banco espera e o que foi detectado (vão para o relatório)."""
    lista = []
    bmc = entrada.get("bmc") or "BMC"
    if entrada.get("redfish") is True and not ctx.redfish.confirmado:
        motivo = ctx.redfish.motivo or "não detectado"
        lista.append(f"O banco indica Redfish ({bmc}), mas ele não foi confirmado neste equipamento: {motivo}")
    if entrada.get("redfish") is False and ctx.redfish.confirmado:
        lista.append(f"Redfish detectado, mas o banco diz que o {bmc} não tem Redfish (banco desatualizado?)")
    if entrada.get("ipmi") and not ctx.ipmi.disponivel:
        lista.append(f"O banco indica IPMI {entrada['ipmi']}, mas ele não está disponível: {ctx.ipmi.motivo or 'não detectado'}")
    conhecidas = [str(c).lower() for c in entrada.get("controladoras") or []]
    for controladora in ctx.controladoras_raid_hardware:
        nome = controladora.nome.lower()
        if conhecidas and not any(c in nome for c in conhecidas):
            lista.append(f"Controladora '{controladora.nome}' não consta no banco para este modelo")
    return lista


def detectar_compatibilidade(ctx: Contexto, pasta: str | Path = PASTA_PADRAO) -> None:
    """Preenche ``ctx.compat``. Detector: roda por último, depois de tudo ter sido detectado."""
    try:
        banco = carregar_banco(pasta)
    except ErroBanco as erro:
        ctx.compat = {"encontrado": False, "erro": str(erro), "entrada": None, "divergencias": []}
        return
    from msscan.compat.genericos import gerar

    entrada, arquivo = consultar(banco, ctx.fabricante, ctx.modelo)
    # Modelo 3D/mapa genérico (19.4) para quem não tem posições conferidas no banco: só desenho, marcado "aproximado".
    generico = None if (entrada or {}).get("modelo3d") else gerar(ctx.fabricante, ctx.modelo)
    if entrada is None:
        ctx.compat = {"encontrado": False, "entrada": None, "divergencias": [], "generico": generico,
                      "mensagem": "modelo sem entrada no banco de compatibilidade (tudo foi detectado ao vivo)"}  # fmt: skip
        return
    ctx.compat = {"encontrado": True, "arquivo": arquivo, "entrada": entrada, "divergencias": divergencias(entrada, ctx),
                  "generico": generico}  # fmt: skip

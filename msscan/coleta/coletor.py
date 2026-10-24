"""Modo ``--coletar``: roda somente leitura e empacota as saídas brutas em ``.tar.gz``.

A coleta executa a mesma verificação de rede (uma vez, sem exigir internet), a
mesma detecção e o mesmo diagnóstico do modo normal (que são somente leitura)
por meio de um ``ExecutorGravador``, mais alguns comandos extras de leitura
úteis para fixtures futuras. O pacote extraído em
``tests/fixtures/`` vira ``<fabricante>/<modelo>/`` pronto para ``--replay``.

ATENÇÃO: saídas brutas contêm seriais, WWN, MAC, IP e afins. Antes de versionar, rode
``python3 -m msscan.coleta.anonimizar <pasta>``.
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from msscan import __version__
from msscan.core.executor import RACADM_LEITURA_EXATA, Executor, ExecutorGravador
from msscan.core.fallback import FalhaColeta
from msscan.deteccao.contexto import Contexto
from msscan.deteccao.detectores import detectar
from msscan.modulos.registro import registro_padrao
from msscan.parsers import ErroParser
from msscan.parsers.redfish import membros
from msscan.rede.verificacao import modo_sem_rede, verificar_rede
from msscan.redfish.acesso import AcessoBMC

# Comandos de leitura extras (não usados pelo diagnóstico atual).
COMANDOS_EXTRAS: list[list[str]] = [
    ["ipmitool", "sel", "info"],
    ["lsscsi", "-g"],
    ["dmesg"],  # por que uma controladora ficou sem driver (T620, 16/10/2026: PERC H310 sem megaraid_sas)
]

_FABRICANTES_CONHECIDOS = {
    "dell": "dell",
    "hpe": "hpe",
    "hp": "hpe",
    "hewlett": "hpe",
    "lenovo": "lenovo",
    "ibm": "ibm",
    "supermicro": "supermicro",
    "fujitsu": "fujitsu",
    "cisco": "cisco",
}


def _slug(texto: str | None, padrao: str) -> str:
    limpo = re.sub(r"[^a-z0-9]+", "_", (texto or "").lower()).strip("_")
    return limpo or padrao


def slug_fabricante(fabricante: str | None) -> str:
    """Nome curto da pasta do fabricante (ex.: "Dell Inc." → "dell")."""
    primeira = _slug(fabricante, "desconhecido").split("_")[0]
    return _FABRICANTES_CONHECIDOS.get(primeira, _slug(fabricante, "desconhecido"))


def slug_modelo(modelo: str | None) -> str:
    """Nome da pasta do modelo (ex.: "PowerEdge R640" → "poweredge_r640")."""
    return _slug(modelo, "modelo_desconhecido")


RECURSOS_REDFISH_EXTRAS = ("sistemas", "gerenciadores", "chassis")


def _coletar_redfish_extra(ctx: Contexto) -> None:
    """Recursos Redfish usados pelas próximas fases (inventário de firmware, sistema, BMC). Só GET."""
    cliente = ctx.bmc.cliente
    raiz = ctx.cache.get("redfish_raiz") or {}
    caminhos = [raiz.get(chave) for chave in RECURSOS_REDFISH_EXTRAS]
    caminhos.append("/redfish/v1/UpdateService/FirmwareInventory")
    for caminho in (c for c in caminhos if c):
        try:
            colecao = cliente.get(caminho)
            for membro in membros(colecao)[:MAX_MEMBROS_EXTRAS]:
                cliente.get(membro)
        except (FalhaColeta, ErroParser):
            continue


MAX_MEMBROS_EXTRAS = 64


def coletar(
    destino: str | Path,
    base: Executor | None = None,
    agora: datetime | None = None,
    pasta_dados: str | None = None,
    credenciais: Callable[[AcessoBMC], None] | None = None,
) -> Path:
    """Executa a coleta e devolve o caminho do ``.tar.gz`` criado em ``destino``.

    ``credenciais``: com Redfish confirmado, pergunta usuário/senha do BMC (mesmas regras do
    diagnóstico) e grava as respostas Redfish (GET) em ``redfish.json``; senha e token nunca.
    """
    agora = agora or datetime.now()
    destino = Path(destino)
    destino.mkdir(parents=True, exist_ok=True)
    temporario = Path(tempfile.mkdtemp(prefix="msscan_coleta_"))
    try:
        gravador = ExecutorGravador(temporario, base=base)
        verificar_rede(gravador)
        modo_sem_rede(gravador)
        ctx = detectar(gravador, pasta_dados)
        try:
            if ctx.bmc is not None and credenciais is not None:
                credenciais(ctx.bmc)
            registro_padrao().diagnosticar(ctx)
            if ctx.bmc is not None and ctx.bmc.validado:
                _coletar_redfish_extra(ctx)
        finally:
            if ctx.bmc is not None:
                ctx.bmc.encerrar()
        for comando in COMANDOS_EXTRAS:
            gravador.rodar(comando, timeout=60)
        racadm = (ctx.ferramentas_fabricante or {}).get("racadm")
        for args in RACADM_LEITURA_EXATA if racadm else ():
            gravador.rodar([racadm, *args], timeout=120)  # TODO(fixture): saídas reais para o parser da RAID pelo racadm

        fabricante, modelo = slug_fabricante(ctx.fabricante), slug_modelo(ctx.modelo)
        origem = {
            "fabricante": ctx.fabricante,
            "modelo": ctx.modelo,
            "origem": "real",
            "coletado_em": agora.strftime("%Y-%m-%d %H:%M"),
            "coletado_por": f"MSscan {__version__} --coletar",
            "anonimizado": False,
            "observacoes": ["Saídas brutas: contêm seriais, WWN, MAC e IP. Anonimize antes de versionar."],
        }
        (temporario / "origem.json").write_text(json.dumps(origem, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        arquivo = destino / f"coleta_{fabricante}_{modelo}_{agora:%Y%m%d_%H%M%S}.tar.gz"
        with tarfile.open(arquivo, "w:gz") as tar:
            tar.add(temporario, arcname=f"{fabricante}/{modelo}")
        return arquivo
    finally:
        shutil.rmtree(temporario, ignore_errors=True)

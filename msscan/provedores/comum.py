"""Funções compartilhadas pelos provedores."""

from __future__ import annotations

from typing import Any

from msscan.core.fallback import FalhaColeta
from msscan.core.modelo import Status
from msscan.deteccao.contexto import Contexto
from msscan.parsers.ipmitool import parse_sel_elist

# Coluna de status do ipmitool (sensor e sdr) → Status. "ns"/"na" = sem leitura (ignorado).
STATUS_LIMIAR_IPMI = {
    "ok": Status.OK,
    "nc": Status.ALERTA,
    "lnc": Status.ALERTA,
    "unc": Status.ALERTA,
    "cr": Status.FALHA,
    "lcr": Status.FALHA,
    "ucr": Status.FALHA,
    "nr": Status.FALHA,
    "lnr": Status.FALHA,
    "unr": Status.FALHA,
}
SEM_LEITURA_IPMI = {"ns", "na", ""}

# Palavras que, num evento "Asserted" do SEL, indicam problema de hardware.
PALAVRAS_PROBLEMA_SEL = (
    "fail",
    "error",
    " err",
    "uncorr",
    "correctable ecc",
    "fatal",
    "lost",
    "critical",
    "ierr",
    "thermal trip",
    "predictive",
    "degraded",
    "non-recoverable",
    "throttl",
    "machine check",
)


def rodar_ou_falhar(ctx: Contexto, comando: list[str], timeout: float) -> str:
    """Roda um comando e devolve o stdout; levanta ``FalhaColeta`` se falhar."""
    rc, saida, erro = ctx.executor.rodar(comando, timeout=timeout)
    if rc != 0:
        detalhe = erro.strip().splitlines()[-1] if erro.strip() else f"código de saída {rc}"
        raise FalhaColeta(f"'{' '.join(comando)}' falhou: {detalhe}")
    return saida


def obter_sel(ctx: Contexto) -> list[dict[str, str]]:
    """Eventos do SEL via ``ipmitool sel elist``, lidos uma única vez por diagnóstico.

    Levanta ``FalhaColeta`` se não for possível ler (o erro também fica em cache).
    """
    if "sel" not in ctx.cache:
        try:
            saida = rodar_ou_falhar(ctx, ["ipmitool", "sel", "elist"], timeout=120)
            ctx.cache["sel"] = parse_sel_elist(saida)
        except Exception as erro:  # noqa: BLE001
            ctx.cache["sel"] = erro
    valor = ctx.cache["sel"]
    if isinstance(valor, Exception):
        raise FalhaColeta(str(valor)) from valor
    return valor


def sel_disponivel(ctx: Contexto) -> list[dict[str, str]] | None:
    """Como ``obter_sel``, mas devolve ``None`` em vez de falhar (fonte complementar)."""
    if not ctx.ipmi.disponivel:
        return None
    try:
        return obter_sel(ctx)
    except FalhaColeta:
        return None


def evento_sel_e_problema(evento: dict[str, str]) -> bool:
    """Indica se um evento do SEL aponta problema de hardware.

    Regras: eventos "Deasserted" são recuperações, exceto "Presence detected"
    de processador deasserted (processador deixou de ser detectado). Nos
    "Asserted", procura palavras de falha no sensor e na descrição; bateria "Low" também conta (IPMI, tipo 29h,
    offset 00: "battery low (predictive failure)"). Referência real (R620, 19/10/2026): o iDRAC7 classifica pelo Redfish
    "The PERC1 battery is low" como Warning — o mesmo evento que o ``ipmitool`` mostra como "ROMB Battery | Low | Asserted".
    Disco removido ("Drive Present" deasserted) continua fora: sem saber se voltou à baia, é troca de disco tanto quanto
    defeito (o estado ATUAL das baias vem do sensor do BMC, ``provedores/raid.py``).
    """
    texto = f"{evento.get('sensor', '')} {evento.get('evento', '')}".lower()
    direcao = evento.get("direcao", "")
    if evento.get("tipo") == "Processor" and "presence detected" in texto and direcao == "Deasserted":
        return True
    if direcao == "Deasserted":
        return False
    if evento.get("tipo") == "Battery" and "low" in (evento.get("evento") or "").lower():
        return True
    return any(palavra in texto for palavra in PALAVRAS_PROBLEMA_SEL)


def descrever_evento_sel(evento: dict[str, str]) -> str:
    """Texto de uma linha para um evento do SEL."""
    descricao = evento.get("evento") or "(sem descrição)"
    direcao = f" ({evento['direcao']})" if evento.get("direcao") else ""
    return f"#{evento.get('id', '?')} {evento.get('data', '')} {evento.get('hora', '')}: {evento.get('sensor', '')} — {descricao}{direcao}"


def problemas_sel_por_tipo(eventos: list[dict[str, str]] | None, tipo: str) -> list[dict[str, Any]]:
    """Eventos-problema do SEL de um tipo IPMI (ex.: "Processor", "Memory")."""
    return [e for e in eventos or [] if e.get("tipo") == tipo and evento_sel_e_problema(e)]

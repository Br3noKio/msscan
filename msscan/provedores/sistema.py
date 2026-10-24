"""Provedores baseados no sistema operacional: CPU e memória.

Combinam várias fontes (SMBIOS, /proc, EDAC e, quando houver IPMI, o SEL).
Cada fonte é opcional; as que faltarem ficam registradas em ``observacoes``.
"""

from __future__ import annotations

from typing import Any

from msscan.core.fallback import Disponibilidade, Provedor
from msscan.core.modelo import Resultado, Status, pior_status
from msscan.deteccao.contexto import Contexto
from msscan.parsers import ErroParser
from msscan.parsers.dmidecode import parse_memorias, parse_processadores
from msscan.parsers.procfs import parse_cpuinfo
from msscan.historico import ATUAL, NOTA_DATAS, SUGESTAO_LIMPAR_SEL, classificar_cpu, classificar_memoria, resumo, slot_dimm
from msscan.provedores.comum import problemas_sel_por_tipo, sel_disponivel

PRIORIDADE_SISTEMA = 50
RAIZ_EDAC = "/sys/devices/system/edac/mc"


class _ProvedorSistema(Provedor):
    prioridade = PRIORIDADE_SISTEMA

    def disponivel(self, ctx: Contexto) -> Disponibilidade:
        return Disponibilidade(True)


def _dmidecode(ctx: Contexto, tipo: str, parser) -> list[dict[str, Any]] | None:
    rc, saida, _ = ctx.executor.rodar(["dmidecode", "-t", tipo], timeout=15)
    if rc != 0:
        return None
    try:
        return parser(saida) or None
    except ErroParser:
        return None


def _separar_sel(classificados: list[dict[str, Any]], situacoes: list[Status], problemas: list[str]) -> list[str]:
    """Eventos do SEL classificados (``msscan/historico.py``) → problemas e histórico.

    ATUAL (confirmado agora) e HISTÓRICO de peça presente viram problema com a classe no texto (ALERTA: o SEL sozinho
    nunca é FALHA); HISTÓRICO de peça que não está mais instalada vai só para ``historico`` (não muda o status).
    """
    historico = []
    for item in classificados:
        texto = f"log do BMC {item['texto']} ({item['nota']})"
        if item["classe"] != ATUAL and item["presente"] is False:
            historico.append(texto)
        elif item["classe"] != ATUAL and "presentes e habilitados agora" in item["nota"]:
            historico.append(texto)
        else:
            situacoes.append(Status.ALERTA)
            problemas.append(texto)
    return historico


def _dados_sel(classificados: list[dict[str, Any]], historico: list[str]) -> dict[str, Any]:
    if not classificados:
        return {}
    saida: dict[str, Any] = {"eventos_sel": classificados, "eventos_sel_resumo": resumo(classificados)}
    if historico:
        saida["historico"] = historico
    if any(c["classe"] != ATUAL for c in classificados):
        saida["explicacoes"] = [f"Eventos do log do BMC marcados como histórico não foram confirmados agora ({NOTA_DATAS}). "
                                + SUGESTAO_LIMPAR_SEL]  # fmt: skip
    return saida


# --- CPU -----------------------------------------------------------------------


def avaliar_cpu(
    processadores: list[dict[str, Any]] | None,
    cpuinfo: dict[str, Any] | None,
    eventos_sel: list[dict[str, str]] | None,
) -> Resultado | None:
    """Combina SMBIOS, /proc/cpuinfo e SEL. ``None`` se não houver SMBIOS nem cpuinfo."""
    if not processadores and not cpuinfo:
        return None
    fontes, problemas, observacoes = [], [], []
    situacoes: list[Status] = []
    classificados: list[dict[str, Any]] = []
    historico: list[str] = []

    habilitados = None
    if processadores:
        fontes.append("dmidecode -t 4")
        populados = [p for p in processadores if p["populado"]]
        habilitados = sum(p["habilitado"] for p in populados)
        # R620 (16/10/2026): CPU2 "Populated, Idle" no SMBIOS com as 24 threads das duas CPUs no /proc/cpuinfo. "Idle" =
        # esperando ser habilitado (SMBIOS); se o sistema operacional usa todos os soquetes populados, não é defeito.
        # "Disabled By BIOS/User" continua ALERTA.
        todos_em_uso = bool(cpuinfo) and cpuinfo.get("soquetes") == len(populados)
        ociosos = [p for p in populados if not p["habilitado"] and p["status"].lower().endswith("idle")]
        if todos_em_uso:
            habilitados += len(ociosos)
        for p in populados:
            if p in ociosos and todos_em_uso:
                observacoes.append(f"{p['soquete']}: o SMBIOS diz \"{p['status']}\", mas o sistema operacional usa este "
                                   "processador (informação do BIOS desatualizada; não é defeito)")
            elif not p["habilitado"]:
                situacoes.append(Status.ALERTA)
                problemas.append(f"{p['soquete']}: {p['status']}")
            if p["nucleos"] and p["nucleos_habilitados"] is not None and p["nucleos_habilitados"] < p["nucleos"]:
                observacoes.append(
                    f"{p['soquete']}: {p['nucleos_habilitados']} de {p['nucleos']} núcleos habilitados "
                    "(pode ser configuração do BIOS)"
                )
    else:
        observacoes.append("SMBIOS (dmidecode -t 4) indisponível: estado dos soquetes não verificado")

    if cpuinfo:
        fontes.append("/proc/cpuinfo")
        if habilitados is not None and cpuinfo["soquetes"] is not None and cpuinfo["soquetes"] != habilitados:
            situacoes.append(Status.ALERTA)
            problemas.append(
                f"o SMBIOS indica {habilitados} processador(es) habilitado(s), "
                f"mas o sistema operacional enxerga {cpuinfo['soquetes']}"
            )
    else:
        observacoes.append("/proc/cpuinfo indisponível")

    if eventos_sel is None:
        observacoes.append("log do BMC (SEL) indisponível: histórico de erros de processador não verificado")
    else:
        fontes.append("ipmitool sel elist")
        classificados = classificar_cpu(problemas_sel_por_tipo(eventos_sel, "Processor"), processadores)
        historico = _separar_sel(classificados, situacoes, problemas)

    modelos = (cpuinfo or {}).get("modelos") or sorted({p["modelo"] for p in processadores or [] if p["modelo"]})
    quantidade = habilitados if habilitados is not None else (cpuinfo or {}).get("soquetes")
    mensagem = f"{quantidade if quantidade is not None else '?'} processador(es)"
    if modelos:
        mensagem += f": {', '.join(modelos)}"
    if cpuinfo:
        mensagem += f"; {cpuinfo['processadores_logicos']} processadores lógicos"
    return Resultado(
        componente="cpu",
        status=pior_status(situacoes),
        fonte=" + ".join(fontes),
        dados={
            "processadores": processadores or [],
            "cpuinfo": cpuinfo,
            "problemas": problemas,
            "observacoes": observacoes,
            **_dados_sel(classificados, historico),
        },
        mensagem=mensagem,
    )


class ProvedorCPU(_ProvedorSistema):
    """Processadores: SMBIOS + /proc/cpuinfo + SEL."""

    nome = "sistema (dmidecode, /proc/cpuinfo, SEL)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        processadores = _dmidecode(ctx, "4", parse_processadores)
        texto = ctx.executor.ler_arquivo("/proc/cpuinfo")
        cpuinfo = None
        if texto:
            try:
                cpuinfo = parse_cpuinfo(texto)
            except ErroParser:
                cpuinfo = None
        return avaliar_cpu(processadores, cpuinfo, sel_disponivel(ctx))


# --- Memória -------------------------------------------------------------------


def _contador(texto: str | None) -> int | None:
    if texto is None:
        return None
    try:
        return int(texto.strip())
    except ValueError:
        return None


def ler_edac(ctx: Contexto) -> list[dict[str, Any]] | None:
    """Lê contadores EDAC (``/sys/devices/system/edac/mc``); ``None`` se não houver EDAC."""
    ex = ctx.executor
    controladores = [n for n in ex.listar(RAIZ_EDAC) if n.startswith("mc") and n[2:].isdigit()]
    if not controladores:
        return None
    resultado = []
    for mc in controladores:
        base = f"{RAIZ_EDAC}/{mc}"
        modulos = []
        for nome in ex.listar(base):
            if not (nome.startswith("dimm") or nome.startswith("rank")):
                continue
            modulos.append(
                {
                    "nome": nome,
                    "rotulo": (ex.ler_arquivo(f"{base}/{nome}/dimm_label") or "").strip(),
                    "corrigiveis": _contador(ex.ler_arquivo(f"{base}/{nome}/dimm_ce_count")),
                    "nao_corrigiveis": _contador(ex.ler_arquivo(f"{base}/{nome}/dimm_ue_count")),
                    "tamanho_mb": _contador(ex.ler_arquivo(f"{base}/{nome}/size")),
                }
            )
        resultado.append(
            {
                "controlador": mc,
                "nome": (ex.ler_arquivo(f"{base}/mc_name") or "").strip(),
                "corrigiveis": _contador(ex.ler_arquivo(f"{base}/ce_count")),
                "nao_corrigiveis": _contador(ex.ler_arquivo(f"{base}/ue_count")),
                "tamanho_mb": _contador(ex.ler_arquivo(f"{base}/size_mb")),
                "modulos": modulos,
            }
        )
    return resultado


def avaliar_memoria(
    memorias: list[dict[str, Any]] | None,
    edac: list[dict[str, Any]] | None,
    eventos_sel: list[dict[str, str]] | None,
) -> Resultado | None:
    """Combina SMBIOS, EDAC e SEL. ``None`` se não houver SMBIOS nem EDAC.

    EDAC não corrigível → FALHA; corrigível → ALERTA; eventos de memória no SEL
    → ALERTA (histórico).
    """
    if not memorias and not edac:
        return None
    fontes, problemas, observacoes = [], [], []
    situacoes: list[Status] = []
    classificados: list[dict[str, Any]] = []
    historico: list[str] = []
    instalados = [m for m in memorias or [] if m["instalado"]]
    total_smbios = sum(m["tamanho_mb"] for m in instalados)

    if memorias:
        fontes.append("dmidecode -t 17")
    else:
        observacoes.append("SMBIOS (dmidecode -t 17) indisponível: módulos instalados não listados")

    if edac:
        fontes.append("EDAC")
        ue = sum(mc["nao_corrigiveis"] or 0 for mc in edac)
        ce = sum(mc["corrigiveis"] or 0 for mc in edac)
        if ue:
            situacoes.append(Status.FALHA)
            problemas.append(f"EDAC: {ue} erro(s) de memória NÃO corrigível(is) desde a inicialização")
        if ce:
            situacoes.append(Status.ALERTA)
            problemas.append(f"EDAC: {ce} erro(s) de memória corrigível(is) desde a inicialização")
        for mc in edac:
            for modulo in mc["modulos"]:
                if modulo["corrigiveis"] or modulo["nao_corrigiveis"]:
                    problemas.append(
                        f"EDAC {modulo['rotulo'] or mc['controlador'] + '/' + modulo['nome']}: "
                        f"{modulo['corrigiveis'] or 0} corrigível(is), {modulo['nao_corrigiveis'] or 0} não corrigível(is)"
                    )
        total_edac = sum(mc["tamanho_mb"] or 0 for mc in edac)
        if instalados and total_edac and total_edac < total_smbios:
            situacoes.append(Status.ALERTA)
            problemas.append(
                f"memória instalada ({total_smbios // 1024} GB segundo o SMBIOS) maior que a reconhecida "
                f"pelos controladores de memória ({total_edac // 1024} GB): módulo pode estar desabilitado"
            )
        observacoes.append("contadores EDAC valem desde a inicialização do sistema")
    else:
        observacoes.append("EDAC indisponível: erros de memória em tempo real não verificados")

    if eventos_sel is None:
        observacoes.append("log do BMC (SEL) indisponível: histórico de erros de memória não verificado")
    else:
        fontes.append("ipmitool sel elist")
        # Mesmo normalizador do slot do evento ("DIMM_A1" do R620 virava "_A1" e o pente instalado parecia ausente).
        slots = {s for m in instalados if (s := slot_dimm(m.get("slot")))} if memorias else None
        erros_agora = sum((mc["corrigiveis"] or 0) + (mc["nao_corrigiveis"] or 0) for mc in edac or [])
        classificados = classificar_memoria(problemas_sel_por_tipo(eventos_sel, "Memory"), slots, {"*": erros_agora})
        historico = _separar_sel(classificados, situacoes, problemas)

    mensagem = (
        f"{len(instalados)} módulo(s) instalado(s), {total_smbios // 1024} GB"
        if memorias
        else "módulos não listados (sem SMBIOS)"
    )
    return Resultado(
        componente="memoria",
        status=pior_status(situacoes),
        fonte=" + ".join(fontes),
        dados={"modulos": instalados, "edac": edac, "problemas": problemas, "observacoes": observacoes,
               # Todos os soquetes que o SMBIOS lista (vazios inclusive): o modelo 3D genérico usa (CLAUDE.md 19.4).
               "soquetes": [m["slot"] for m in memorias or [] if m.get("slot")],
               **_dados_sel(classificados, historico)},
        mensagem=mensagem,
    )


class ProvedorMemoria(_ProvedorSistema):
    """Memória: SMBIOS + EDAC + SEL."""

    nome = "sistema (dmidecode, EDAC, SEL)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        return avaliar_memoria(_dmidecode(ctx, "17", parse_memorias), ler_edac(ctx), sel_disponivel(ctx))

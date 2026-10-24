"""Provedores baseados no sistema operacional: CPU e memória.

Combinam várias fontes (SMBIOS, /proc, EDAC e, quando houver IPMI, o SEL).
Cada fonte é opcional; as que faltarem ficam registradas em ``observacoes``.
"""

from __future__ import annotations

import re
from typing import Any

from msscan.core.fallback import Disponibilidade, Provedor
from msscan.core.modelo import Resultado, Status, pior_status
from msscan.deteccao.contexto import Contexto
from msscan.parsers import ErroParser
from msscan.parsers.dmidecode import parse_memorias, parse_processadores
from msscan.parsers.procfs import parse_cpuinfo
from msscan.historico import ATUAL, NOTA_DATAS, SUGESTAO_LIMPAR_SEL, classificar_cpu, classificar_memoria, resumo, slot_dimm
from msscan.localizacao import local_de
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


def _juntar_explicacoes(dados: dict[str, Any], extras: list[str]) -> dict[str, Any]:
    if extras:
        dados = {**dados, "explicacoes": [*extras, *(dados.get("explicacoes") or [])]}
    return dados


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


VAZIO, CONFERIR = "VAZIO", "CONFERIR"
"""Situações de uma posição além de OK/ALERTA/FALHA: vazia, ou vazia com indício fraco de peça (sem mudar o status)."""
_DESABILITADO = re.compile(r"disabled|configuration error|memory resize", re.IGNORECASE)
_SLOT_BANCO = re.compile(r"([A-Z])(\d{1,2})")
REENCAIXE = ("reencaixe o pente; se continuar, troque-o de slot com um pente que funciona: se o problema acompanhar o pente, "
             "é o pente; se ficar no slot, é o slot ou o processador")  # fmt: skip


def eventos_memoria(eventos_sel: list[dict[str, str]] | None) -> list[dict[str, str]] | None:
    """Problemas de memória do SEL mais "Memory Device Disabled" e "POST Memory Resize": a BIOS desabilitou um pente no
    POST, mas o texto não tem palavra de erro e o filtro geral do SEL o deixava passar (pedido de 22/10/2026)."""
    if eventos_sel is None:
        return None
    saida = problemas_sel_por_tipo(eventos_sel, "Memory")
    for evento in eventos_sel:
        texto = f"{evento.get('tipo', '')} {evento.get('evento', '')}"
        if (evento not in saida and evento.get("tipo") in ("Memory", "POST Memory Resize") and _DESABILITADO.search(texto)
                and "deasserted" not in (evento.get("direcao") or "").lower()):  # fmt: skip
            saida.append(evento)
    return saida


def _rotulo_slot(slot: str, dell: bool) -> str:
    local = local_de(f"DIMM {slot}", dell=dell) if _SLOT_BANCO.fullmatch(slot) else None
    return local[1] if local else slot


def mapa_memoria(memorias: list[dict[str, Any]] | None, classificados: list[dict[str, Any]], bmc: list[dict[str, Any]] | None,
                 dell: bool = False) -> tuple[list[dict[str, Any]], list[str], list[Status]]:  # fmt: skip
    """Cada slot com a situação e o local (pedido de 22/10/2026: "qual memória funciona e onde está a ruim").

    Pente desabilitado no POST aparece no SMBIOS como "No Module Installed", igual a slot vazio. Indício FORTE de pente
    não reconhecido (problema): o BMC vê um pente no slot; o SEL registra o pente do slot como desabilitado; slot vazio entre
    slots ocupados do mesmo processador (a Dell instala em ordem). Indício FRACO (``CONFERIR``, sem mudar o status — 17.1:
    pente retirado não é defeito): erro antigo no SEL de um slot vazio; outro processador com pente na mesma posição.
    Devolve ``(posicoes, problemas, situacoes)``.
    """
    smbios = {(slot_dimm(m["slot"]) or m["slot"].strip()): m for m in memorias or [] if (m.get("slot") or "").strip()}
    pelo_bmc = {(slot_dimm(p["slot"]) or p["slot"]): p for p in bmc or [] if p.get("slot")}
    eventos: dict[str, list[dict[str, Any]]] = {}
    for item in classificados:
        if item.get("slot"):
            eventos.setdefault(item["slot"], []).append(item)
    ocupados: dict[str, set[int]] = {}
    for slot, m in smbios.items():
        casado = _SLOT_BANCO.fullmatch(slot)
        if casado and m["instalado"]:
            ocupados.setdefault(casado.group(1), set()).add(int(casado.group(2)))
    posicoes, problemas, situacoes = [], [], []
    for slot in [*smbios, *(s for s in pelo_bmc if s not in smbios)]:
        m, p, evs = smbios.get(slot) or {}, pelo_bmc.get(slot), eventos.get(slot, [])
        casado = _SLOT_BANCO.fullmatch(slot)
        banco, numero = (casado.group(1), int(casado.group(2))) if casado else (None, None)
        saude_bmc = (p or {}).get("saude")
        if m.get("instalado"):
            detalhe = " ".join(str(x) for x in (f"{round(m['tamanho_mb'] / 1024)} GB", m.get("tipo"), m.get("velocidade")) if x)
            if saude_bmc in ("Critical", "Warning"):
                situacao = Status.FALHA.value if saude_bmc == "Critical" else Status.ALERTA.value
                situacoes.append(Status(situacao))
                problemas.append(f"DIMM {slot}: o BMC indica saúde {saude_bmc} para este pente; {REENCAIXE}")
                detalhe += f" — BMC: saúde {saude_bmc}"
            elif evs:
                situacao = Status.ALERTA.value
                detalhe += " — " + ("erro confirmado agora" if any(e["classe"] == ATUAL for e in evs) else "erro no log do BMC (histórico)")
            else:
                situacao = Status.OK.value
        else:
            fortes = []
            if p is not None and p.get("estado") != "Absent" and (p.get("estado") or p.get("capacidade_mb")):
                tamanho = f" de {round(p['capacidade_mb'] / 1024)} GB" if p.get("capacidade_mb") else ""
                fortes.append(f"o BMC vê um pente{tamanho} neste slot (estado {p.get('estado') or '?'}, saúde {saude_bmc or '?'})")
            if any(_DESABILITADO.search(e["texto"]) for e in evs):
                fortes.append("o log do BMC registra o pente deste slot como desabilitado")
            if banco and numero < max(ocupados.get(banco) or {0}):
                fortes.append("slot vazio entre slots ocupados do mesmo processador (a Dell instala os pentes em ordem)")
            if fortes:
                situacao = Status.FALHA.value if saude_bmc == "Critical" else Status.ALERTA.value
                situacoes.append(Status(situacao))
                problemas.append(f"DIMM {slot}: pente provavelmente instalado e NÃO reconhecido pelo sistema — {'; '.join(fortes)}. "
                                 f"Pente com defeito, mal encaixado ou incompatível: {REENCAIXE}")  # fmt: skip
                detalhe = "não reconhecido: " + "; ".join(fortes)
            elif evs:
                situacao, detalhe = CONFERIR, "vazio para o sistema, com erro antigo no log do BMC: se há um pente aqui, ele não foi reconhecido"
            elif banco and ocupados.get(banco) and any(numero in n for b, n in ocupados.items() if b != banco):
                situacao, detalhe = CONFERIR, "vazio, mas o outro processador tem pente nesta posição: se há um pente aqui, ele não foi reconhecido"
            else:
                situacao, detalhe = VAZIO, ""
        posicoes.append({"posicao": slot, "local": _rotulo_slot(slot, dell), "situacao": situacao, "detalhe": detalhe})
    return posicoes, problemas, situacoes


def avaliar_memoria(
    memorias: list[dict[str, Any]] | None,
    edac: list[dict[str, Any]] | None,
    eventos_sel: list[dict[str, str]] | None,
    bmc: list[dict[str, Any]] | None = None,
    dell: bool = False,
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
        classificados = classificar_memoria(eventos_memoria(eventos_sel) or [], slots, {"*": erros_agora})
        historico = _separar_sel(classificados, situacoes, problemas)

    if bmc is not None:
        fontes.append("Redfish (inventário de memória do BMC)")
    posicoes, problemas_slots, situacoes_slots = mapa_memoria(memorias, classificados, bmc, dell)
    problemas += problemas_slots
    situacoes += situacoes_slots
    nao_reconhecidos = [p["posicao"] for p in posicoes if p["situacao"] in ("ALERTA", "FALHA") and p["detalhe"].startswith("não reconhecido")]
    a_conferir = [p["posicao"] for p in posicoes if p["situacao"] in (VAZIO, CONFERIR)]

    mensagem = (
        f"{len(instalados)} de {len(posicoes)} slots com pente reconhecido, {total_smbios // 1024} GB"
        if memorias
        else "módulos não listados (sem SMBIOS)"
    )
    if nao_reconhecidos:
        mensagem += f"; pente provavelmente NÃO reconhecido em: {', '.join(nao_reconhecidos)}"
    explicacoes_slots = []
    indicio = nao_reconhecidos or any(p["situacao"] == CONFERIR for p in posicoes) or len(a_conferir) <= len(posicoes) // 4
    if a_conferir and memorias and indicio:  # servidor com poucos pentes de propósito: sem a lista enorme de vazios
        explicacoes_slots.append(f"{'Outros slots' if nao_reconhecidos else 'Slots'} que o sistema vê vazios: {', '.join(a_conferir)}. Se você instalou um pente em algum "
                                 "deles, ele não foi reconhecido (pente com defeito, mal encaixado, incompatível, ou slot/"
                                 "processador com problema): confira pela posição no mapa e " + REENCAIXE + ".")  # fmt: skip
    return Resultado(
        componente="memoria",
        status=pior_status(situacoes),
        fonte=" + ".join(fontes),
        dados={"modulos": instalados, "edac": edac, "problemas": problemas, "observacoes": observacoes,
               # Todos os soquetes que o SMBIOS lista (vazios inclusive): o modelo 3D genérico usa (CLAUDE.md 19.4).
               "soquetes": [m["slot"] for m in memorias or [] if m.get("slot")],
               "posicoes": posicoes, "pentes_bmc": bmc,
               **_juntar_explicacoes(_dados_sel(classificados, historico), explicacoes_slots)},
        mensagem=mensagem,
    )


class ProvedorMemoria(_ProvedorSistema):
    """Memória: SMBIOS + EDAC + SEL."""

    nome = "sistema (dmidecode, EDAC, SEL)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        from msscan.acesso_idrac import eh_dell
        from msscan.provedores.redfish import ler_memoria_redfish

        return avaliar_memoria(_dmidecode(ctx, "17", parse_memorias), ler_edac(ctx), sel_disponivel(ctx), ler_memoria_redfish(ctx),
                               eh_dell(ctx))  # fmt: skip

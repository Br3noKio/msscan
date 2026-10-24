"""Resumo de capacidades detectadas, exibido no terminal."""

from __future__ import annotations

from msscan.core.modelo import Status
from msscan.deteccao.contexto import Contexto
from msscan.geracao_idrac import geracao, idrac
from msscan.relatorio.simbolos import UNICODE, Simbolos

DESCRICAO_CATEGORIA = {
    "raid_hardware": "RAID de hardware",
    "hba_sas": "HBA SAS",
    "nvme": "NVMe",
    "raid_chipset": "RAID do chipset",
    "ahci": "SATA AHCI",
    "raid_desconhecido": "RAID (driver não reconhecido)",
    "sas_desconhecido": "SAS (driver não reconhecido)",
    "sem_driver": "sem driver carregado",
    "desconhecido": "tipo não reconhecido",
}


def formatar_resumo(ctx: Contexto, simbolos: Simbolos = UNICODE) -> str:
    """Monta o bloco "Servidor detectado / BMC / Redfish / RAID" para o terminal."""
    ok = simbolos[Status.OK]
    fabricante = ctx.fabricante or "fabricante desconhecido"
    modelo = ctx.modelo or "modelo desconhecido"
    linhas = [f"Servidor detectado: {fabricante} {modelo}"]
    numero = geracao(ctx.modelo) if fabricante.lower().startswith("dell") else None
    if numero:  # 19/10/2026: fora do banco de compatibilidade (ex.: R620) a geração não aparecia em lugar nenhum
        linhas[0] += f" ({numero}ª geração, {idrac(numero)})"

    if ctx.ipmi.disponivel:
        bmc = f"BMC: IPMI {ctx.ipmi.versao or '(versão desconhecida)'} disponível {ok}"
    else:
        bmc = "BMC: IPMI não disponível"

    if ctx.redfish.confirmado:
        redfish = f"Redfish {ctx.redfish.versao or ''}: disponível {ok}".replace("  ", " ")
    elif ctx.redfish.anunciado:
        redfish = f"Redfish: BMC em {ctx.redfish.endereco or '?'} não respondeu"
    else:
        redfish = "Redfish: não disponível"

    raid_hw = ctx.controladoras_raid_hardware
    if raid_hw:
        raid = "RAID: " + ", ".join(f"{c.nome} {ok}" for c in raid_hw)
    else:
        raid = "RAID: nenhuma controladora RAID de hardware"
    linhas.append("   ".join((bmc, redfish, raid)))

    if ctx.controladoras:
        linhas.append("Controladoras de armazenamento:")
        for c in ctx.controladoras:
            categoria = DESCRICAO_CATEGORIA.get(c.categoria, c.categoria)
            linhas.append(f"  {c.slot}  {categoria} (driver {c.driver or '-'}): {c.nome}")

    if ctx.sensores_kernel:
        linhas.append(f"Sensores do kernel: {len(ctx.sensores_kernel)} chip(s)")
    else:
        linhas.append("Sensores do kernel: nenhum")

    motivos = [m for m in (ctx.ipmi.motivo if not ctx.ipmi.disponivel else "", ctx.redfish.motivo) if m]
    for motivo in motivos:
        linhas.append(f"  · {motivo}")
    for nome, erro in ctx.erros_deteccao.items():
        linhas.append(f"  · falha na detecção ({nome}): {erro}")
    if ctx.redfish.confirmado:
        linhas.append(f"Redfish em {ctx.redfish.endereco} (endereço via {ctx.redfish.origem})")
    if ctx.compat and ctx.compat.get("encontrado"):
        entrada = ctx.compat["entrada"]
        linhas.append(f"Banco de compatibilidade: {entrada['nome']} — {entrada['bmc']}, {entrada.get('geracao') or ''}".rstrip(", "))
        for divergencia in ctx.compat["divergencias"]:
            linhas.append(f"  · divergência com o banco: {divergencia}")
    elif ctx.compat:
        linhas.append(f"Banco de compatibilidade: {ctx.compat.get('erro') or ctx.compat.get('mensagem')}")
    if ctx.pendrive:
        gravacao = ctx.pendrive["gravacao"]
        if ctx.pendrive["tipo"] != "manual":
            linhas.append(f"Pendrive: {gravacao['descricao']}; boot {gravacao['descricao_boot']}")
        linhas.append(f"Dados: {ctx.pendrive['mensagem']}")
        if ctx.pendrive.get("aviso"):
            linhas.append(f"  · {ctx.pendrive['aviso']}")
    return "\n".join(linhas)


def _item_compatibilidade(ctx: Contexto) -> list[tuple[str, str, bool, str]]:
    """Linha do banco de compatibilidade (dica; divergências contadas)."""
    if not ctx.compat:
        return []
    if not ctx.compat.get("encontrado"):
        return [("compatibilidade", "Banco de compatibilidade", False, ctx.compat.get("erro") or "modelo sem entrada no banco")]
    entrada = ctx.compat["entrada"]
    detalhe = f"{entrada['nome']} ({entrada['bmc']})"
    if ctx.compat["divergencias"]:
        detalhe += f"; {len(ctx.compat['divergencias'])} divergência(s) com a detecção"
    return [("compatibilidade", "Banco de compatibilidade", True, detalhe)]


def _itens_pendrive(ctx: Contexto) -> list[tuple[str, str, bool, str]]:
    """Linhas "Pendrive de boot" e "Dados do MSscan" das capacidades."""
    if not ctx.pendrive:
        return [("pendrive", "Dados do MSscan", bool(ctx.pasta_dados), ctx.pasta_dados or "nenhum local (replay)")]
    gravacao = ctx.pendrive["gravacao"]
    return [
        ("gravacao", "Pendrive de boot", gravacao["metodo"] != "desconhecido",
         f"{gravacao['descricao']}; boot {gravacao['descricao_boot']}"),
        ("pendrive", "Dados do MSscan", bool(ctx.pendrive["persistente"]),
         ctx.pendrive["aviso"] or ctx.pendrive["mensagem"]),
    ]  # fmt: skip


def capacidades(ctx: Contexto) -> list[dict[str, object]]:
    """Capacidades detectadas, uma por linha (✓ disponível / N/A), para a interface gráfica.

    ``disponivel`` só é verdadeiro com evidência da detecção; o banco de
    compatibilidade nunca entra aqui.
    """
    raid_hw = ctx.controladoras_raid_hardware
    if ctx.redfish.confirmado:
        redfish = (True, f"Redfish {ctx.redfish.versao or ''} em {ctx.redfish.endereco} (via {ctx.redfish.origem})")
    elif ctx.redfish.anunciado:
        redfish = (False, ctx.redfish.motivo or f"BMC em {ctx.redfish.endereco or '?'} não respondeu")
    else:
        redfish = (False, ctx.redfish.motivo or "não anunciado pelo SMBIOS")
    itens = [
        ("ipmi", "BMC / IPMI", ctx.ipmi.disponivel,
         f"IPMI {ctx.ipmi.versao or '(versão desconhecida)'}" if ctx.ipmi.disponivel else ctx.ipmi.motivo or "não disponível"),
        ("redfish", "Redfish", *redfish),
        ("raid", "RAID de hardware", bool(raid_hw),
         ", ".join(c.nome for c in raid_hw) if raid_hw else "nenhuma controladora RAID de hardware"),
        ("controladoras", "Controladoras de armazenamento", bool(ctx.controladoras),
         ", ".join(f"{DESCRICAO_CATEGORIA.get(c.categoria, c.categoria)} (driver {c.driver or '-'})" for c in ctx.controladoras)
         or "nenhuma"),
        ("sensores_kernel", "Sensores do kernel", bool(ctx.sensores_kernel),
         f"{len(ctx.sensores_kernel)} chip(s)" if ctx.sensores_kernel else "nenhum"),
        ("ferramentas", "Ferramentas do fabricante", bool(ctx.ferramentas_fabricante),
         ", ".join(sorted(ctx.ferramentas_fabricante)) or "nenhuma em vendor/ (opcionais)"),
        *_item_compatibilidade(ctx),
        *_itens_pendrive(ctx),
    ]  # fmt: skip
    return [{"chave": c, "nome": n, "disponivel": d, "detalhe": t} for c, n, d, t in itens]

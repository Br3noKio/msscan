"""Inventário do firmware instalado (somente leitura).

Fontes, por prioridade: Redfish ``FirmwareInventory`` (com credenciais do BMC; versões no formato
do fabricante) → sistema: BIOS (SMBIOS), BMC (racadm ``getversion`` com a versão completa; sem racadm, IPMI com a
versão truncada, ex. "7.00"), placas de rede
(``lspci`` + ``ethtool -i``; formato do fabricante da placa, em geral não comparável com o catálogo)
e controladoras RAID de hardware (sem versão sem Redfish nem ferramenta do fabricante).
"""

from __future__ import annotations

import re
from typing import Any

from msscan.core.fallback import FalhaColeta
from msscan.deteccao.contexto import Contexto
from msscan.parsers import ErroParser
from msscan.parsers.ethtool import parse_ethtool_i
from msscan.parsers.lspci import parse_lspci_nnk
from msscan.parsers.redfish import membros, parse_item_firmware
from msscan.rede.verificacao import listar_interfaces

CAMINHO_INVENTARIO = "/redfish/v1/UpdateService/FirmwareInventory"
MAX_ITENS = 200
FABRICANTE_DELL_IPMI = "674"


def _item(nome: str, versao: str | None, fonte: str, **chaves: Any) -> dict[str, Any]:
    return {"nome": nome, "versao": versao, "fonte": fonte, "truncada": False, "component_id": None,
            "tipo": None, "categoria": None, "pci": None, **chaves}  # fmt: skip


def inventario_redfish(ctx: Contexto) -> list[dict[str, Any]]:
    """Itens instalados segundo o BMC. Lista vazia sem sessão Redfish."""
    if ctx.bmc is None or not ctx.bmc.validado:
        return []
    cliente = ctx.bmc.cliente
    itens = []
    try:
        for membro in membros(cliente.get(CAMINHO_INVENTARIO))[:MAX_ITENS]:
            dados = parse_item_firmware(cliente.get(membro))
            if dados["instalado"] and dados["versao"]:
                itens.append(_item(dados["nome"], dados["versao"], "Redfish (FirmwareInventory)", component_id=dados["software_id"]))
    except (FalhaColeta, ErroParser):
        return []
    return itens


def _nome_placa(dispositivo: dict[str, Any]) -> str:
    """Subsistema ("Dell Ethernet 10G 4P X710/I350 rNDC"), ou a descrição se o subsistema for genérico."""
    subsistema = dispositivo.get("subsistema") or ""
    return subsistema if subsistema and not re.search(r"\bDevice\b", subsistema) else dispositivo["descricao"]


_RE_FAMILIA_INTEL = re.compile(r"(?:^|[\s,])(\d{1,3}\.\d{1,3}\.\d{1,3})\s*$")
_RE_FAMILIA_BROADCOM = re.compile(r"^FFV(\d{1,3}\.\d{1,3}\.\d{1,3})\b")


def versao_placa(fabricante_id: str, subfabricante_id: str, firmware: str) -> tuple[str | None, str, str | None]:
    """``(versão para comparar, fonte, versão bruta)`` de uma placa de rede pelo ``ethtool -i``.

    Placa Intel da Dell (8086, subsistema 1028): o ``ethtool`` informa "NVM, eetrack, versão da família"
    ("7.10 0x8000519f 19.0.12", "1.67, 0x80000fa8, 19.0.12"); o catálogo da Dell publica a versão da família
    ("Intel NIC Family Version 25.0.0 ... 25.0.4"). Compara-se só esse último campo (fixture real do R640).
    TODO(fixture): conferir com o FirmwareInventory do iDRAC9 do R640 (Redfish) que a versão mostrada é a mesma.
    Vale também para a placa Intel de subsistema Intel que o catálogo da Dell casa pelos IDs PCI (R430, 16/10/2026: I350-T4
    "1.67, 0x80000dd1, 17.0.12" × catálogo "19.5.12").
    Broadcom (14e4): "FFV22.31.6 bc 5720-v1.39" = versão da família (FFV) + bootcode; o catálogo publica a família
    ("22.00.6", "21.60.16"). R430/R620 (16/10/2026): "FFV7.10.64" e "FFV7.6.14" ficavam "não comparáveis".
    TODO(fixture): conferir com o FirmwareInventory do iDRAC (Redfish) de um servidor com Broadcom que a versão é a FFV.
    Outras placas: a string inteira (em geral não comparável → "não foi possível determinar").
    """
    fabricante = fabricante_id.lower()
    casado = _RE_FAMILIA_INTEL.search(firmware)
    if fabricante == "8086" and casado:
        return casado.group(1), "ethtool -i (versão da família Intel) + lspci", firmware
    casado = _RE_FAMILIA_BROADCOM.match(firmware.strip())
    if fabricante == "14e4" and casado:
        return casado.group(1), "ethtool -i (versão da família Broadcom, FFV) + lspci", firmware
    return firmware or None, "ethtool -i + lspci", None


def _placas_rede(ctx: Contexto) -> list[dict[str, Any]]:
    ex = ctx.executor
    rc, saida, _ = ex.rodar(["lspci", "-nnk"], timeout=20)
    if rc != 0:
        return []
    try:
        pci = {d["slot"]: d for d in parse_lspci_nnk(saida) if d["classe"] == "0200"}
    except ErroParser:
        return []
    por_slot: dict[tuple[str, Any], dict[str, Any]] = {}
    for interface in (i for i in listar_interfaces(ex) if i.fisica and not i.nic_bmc):
        rc, saida, _ = ex.rodar(["ethtool", "-i", interface.nome], timeout=15)
        if rc != 0:
            continue
        try:
            info = parse_ethtool_i(saida)
        except ErroParser:
            continue
        slot = info["barramento"].split(":", 1)[-1] if info["barramento"].count(":") == 2 else info["barramento"]
        dispositivo = pci.get(slot)
        if dispositivo is None:
            continue
        sub = (dispositivo.get("subsistema_ids") or ":").split(":")
        versao, fonte, bruta = versao_placa(dispositivo["fabricante_id"], sub[0], info["firmware"] or "")
        ids = {"vendorID": dispositivo["fabricante_id"].lower(), "deviceID": dispositivo["dispositivo_id"].lower(),
               "subVendorID": sub[0].lower(), "subDeviceID": sub[1].lower() if len(sub) > 1 else ""}
        # Funções do mesmo chip (mesmo barramento e dispositivo PCI, ex.: 18:00.0 e 18:00.1) têm um firmware só: uma placa
        # (CLAUDE.md 19.3). A 2ª função da X710 do R640 informa subsistema 1028:0000 (lspci real): vale o da outra função.
        chave = (slot.rsplit(".", 1)[0], versao)
        item = por_slot.get(chave)
        if item is None:
            item = por_slot[chave] = _item(_nome_placa(dispositivo), versao, fonte, versao_bruta=bruta, pci=ids, interfaces=[])
        elif item["pci"]["subDeviceID"] in ("", "0000") and ids["subDeviceID"] not in ("", "0000"):
            item.update(nome=_nome_placa(dispositivo), pci=ids)
        item["interfaces"].append(interface.nome)
    for item in por_slot.values():
        item["nome"] += f" ({', '.join(item['interfaces'])})"
    return list(por_slot.values())


def versoes_racadm(ctx: Contexto) -> dict[str, str] | None:
    """``racadm getversion`` (só leitura), lido uma vez por sessão; ``None`` sem racadm ou sem resposta."""
    from msscan.acoes.idrac import racadm
    from msscan.deteccao.idrac_tools import ler_versoes

    if "racadm_versoes" not in ctx.cache:
        preparado = (ctx.cache.get("idrac_tools") or {}).get("versoes")
        caminho = racadm(ctx)
        ctx.cache["racadm_versoes"] = preparado or (ler_versoes(ctx.executor, caminho) if caminho else None)
    return ctx.cache["racadm_versoes"]


def inventario_sistema(ctx: Contexto) -> list[dict[str, Any]]:
    """Itens que o sistema operacional e o IPMI conseguem informar."""
    itens = []
    if ctx.versao_bios:
        itens.append(_item("BIOS", ctx.versao_bios, "dmidecode -s bios-version", tipo="BIOS"))
    mc = ctx.ipmi.mc_info or {}
    completa = (versoes_racadm(ctx) or {}).get("iDRAC Version")
    if completa:
        # Versão completa (ex.: 7.00.00.173) pelo racadm local (CLAUDE.md 19.3): comparável com o catálogo.
        itens.append(_item("iDRAC (BMC)", completa, "racadm getversion", categoria="iDRAC with Lifecycle Controller"))
    elif ctx.ipmi.disponivel and mc.get("firmware") and mc.get("fabricante_id") == FABRICANTE_DELL_IPMI:
        itens.append(_item("iDRAC (BMC)", mc["firmware"], "ipmitool mc info", truncada=True,
                           categoria="iDRAC with Lifecycle Controller"))  # fmt: skip
    elif (ctx.fabricante or "").lower().startswith("dell"):
        # Sem racadm e sem IPMI (ex.: IPMI desligado no iDRAC): o iDRAC continua na lista, sem versão, e vira "não foi possível
        # determinar" com o motivo — nunca some da lista (achado da frota simulada, 14/10/2026). Sem versão nunca é oferecido.
        itens.append(_item("iDRAC (BMC)", None, "—", categoria="iDRAC with Lifecycle Controller",
                           observacao="versão não lida: IPMI indisponível e sem racadm; informe as credenciais do iDRAC "
                                      "(Redfish) ou coloque o iDRAC Tools for Linux em vendor/"))  # fmt: skip
    itens += _placas_rede(ctx)
    for controladora in ctx.controladoras_raid_hardware:
        itens.append(_item(controladora.nome, None, "lspci", pci={
            "vendorID": controladora.fabricante_id.lower(), "deviceID": controladora.dispositivo_id.lower(),
            "subVendorID": (controladora.subsistema_ids or ":").split(":")[0].lower(),
            "subDeviceID": (controladora.subsistema_ids or ":").split(":")[-1].lower(),
        }, observacao="versão só com credenciais do BMC (Redfish) ou ferramenta do fabricante em vendor/"))  # fmt: skip
    return itens


def coletar_inventario(ctx: Contexto) -> tuple[list[dict[str, Any]], str]:
    """``(itens, fonte)``: Redfish se houver sessão; senão o que o sistema informa."""
    itens = inventario_redfish(ctx)
    if itens:
        return itens, "Redfish (FirmwareInventory)"
    return inventario_sistema(ctx), "sistema (SMBIOS, IPMI, lspci, ethtool)"

"""Detectores de capacidades do equipamento.

Cada detector é isolado: se um falhar, o erro vai para ``ctx.erros_deteccao``
e os demais continuam. Todos são somente leitura, exceto a carga dos módulos
IPMI do kernel (``modprobe``), feita apenas quando não há dispositivo IPMI.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from msscan.core.executor import Executor
from msscan.deteccao.contexto import Contexto, Controladora
from msscan.parsers.dmidecode import (parse_dmidecode_s, parse_host_interface_tipo42, parse_ipmi_tipo38, parse_sku_dell,
                                      parse_system_id_dell_oem)
from msscan.core.fallback import FalhaColeta
from msscan.parsers import ErroParser
from msscan.parsers.ipmitool import parse_lan_print, parse_mc_info
from msscan.core import tls_bmc
from msscan.redfish.acesso import AcessoBMC
from msscan.redfish.cliente import ClienteRedfish
from msscan.parsers.lspci import parse_lspci_nnk
from msscan.parsers.sensors import parse_sensors_json

DISPOSITIVOS_IPMI = ("/dev/ipmi0", "/dev/ipmi/0", "/dev/ipmidev/0")
CLASSES_ARMAZENAMENTO = {"0104", "0107", "0108"}

# Driver do kernel → categoria da controladora. Driver define o que o hardware
# realmente é: classe PCI 0104 com driver "ahci" é RAID do chipset (firmware/software).
CATEGORIA_POR_DRIVER = {
    "megaraid_sas": "raid_hardware",
    "megaraid": "raid_hardware",
    "hpsa": "raid_hardware",
    "cciss": "raid_hardware",
    "smartpqi": "raid_hardware",
    "mpi3mr": "raid_hardware",  # PERC 12 (H965i, H765i, H365i; 16ª geração em diante) e HBA465
    "aacraid": "raid_hardware",
    "arcmsr": "raid_hardware",
    "3w-9xxx": "raid_hardware",
    "3w-sas": "raid_hardware",
    "3w-xxxx": "raid_hardware",
    "ips": "raid_hardware",
    "esas2r": "raid_hardware",
    "mpt3sas": "hba_sas",
    "mpt2sas": "hba_sas",
    "mptsas": "hba_sas",
    "mptspi": "hba_sas",
    "isci": "hba_sas",
    "pm80xx": "hba_sas",
    "mvsas": "hba_sas",
    "nvme": "nvme",
    "ahci": "raid_chipset",
    "vmd": "raid_chipset",
}
CATEGORIA_POR_CLASSE = {"0104": "raid_desconhecido", "0107": "sas_desconhecido", "0108": "nvme"}

_RE_SUBSISTEMA_GENERICO = re.compile(r"(^|\s)Device(\s+[0-9a-fA-F]{4})?$")


def classificar_controladora(classe: str, driver: str | None) -> str:
    """Classifica a controladora pela combinação classe PCI + driver em uso."""
    if driver and driver in CATEGORIA_POR_DRIVER:
        categoria = CATEGORIA_POR_DRIVER[driver]
        if categoria == "raid_chipset" and classe != "0104":
            return "ahci"
        return categoria
    if not driver:
        return "sem_driver"
    return CATEGORIA_POR_CLASSE.get(classe, "desconhecido")


def detectar_identificacao(ctx: Contexto) -> None:
    """Fabricante, modelo e versão do BIOS via ``dmidecode -s``."""
    ex = ctx.executor
    for atributo, chave in (
        ("fabricante", "system-manufacturer"),
        ("modelo", "system-product-name"),
        ("versao_bios", "bios-version"),
    ):
        rc, saida, erro = ex.rodar(["dmidecode", "-s", chave], timeout=15)
        if rc == 0:
            setattr(ctx, atributo, parse_dmidecode_s(saida))
        else:
            ctx.erros_deteccao[f"identificacao.{atributo}"] = erro.strip() or f"dmidecode retornou {rc}"
    # SKU (na Dell: "SKU=0716;ModelName=PowerEdge R640", o systemID do catálogo). Opcional: nem todo
    # fabricante preenche, e sem ele só a atualização de firmware fica sem confirmar o modelo.
    rc, saida, _ = ex.rodar(["dmidecode", "-s", "system-sku-number"], timeout=15)
    if rc == 0:
        ctx.sku = parse_dmidecode_s(saida)
    # Dell até a 13ª geração (caso real: R530): SKU vazio; o systemID fica na estrutura OEM tipo 208. Montado no mesmo formato
    # do SKU das gerações novas, para todo o resto (catálogo, pacotes manuais) seguir igual.
    if parse_sku_dell(ctx.sku) is None and (ctx.fabricante or "").lower().startswith("dell"):
        rc, saida, _ = ex.rodar(["dmidecode", "-t", "208"], timeout=15)
        system_id = parse_system_id_dell_oem(saida) if rc == 0 else None
        if system_id:
            ctx.sku = f"SKU={system_id};ModelName={ctx.modelo or ''}"


def _dispositivo_ipmi(ex: Executor) -> str | None:
    return next((d for d in DISPOSITIVOS_IPMI if ex.existe(d)), None)


def detectar_ipmi(ctx: Contexto, permitir_modprobe: bool = True) -> None:
    """IPMI: SMBIOS tipo 38, dispositivo do kernel e ``ipmitool mc info``."""
    ex = ctx.executor
    info = ctx.ipmi
    rc, saida, _ = ex.rodar(["dmidecode", "-t", "38"], timeout=15)
    if rc == 0:
        info.smbios = parse_ipmi_tipo38(saida)

    info.dispositivo = _dispositivo_ipmi(ex)
    if info.dispositivo is None and permitir_modprobe:
        info.modprobe_tentado = True
        ex.rodar(["modprobe", "ipmi_si"], timeout=30)
        ex.rodar(["modprobe", "ipmi_devintf"], timeout=15)
        ex.rodar(["udevadm", "settle", "--timeout=5"], timeout=10)
        info.dispositivo = _dispositivo_ipmi(ex)

    if info.dispositivo is None:
        info.motivo = (
            "IPMI anunciado no SMBIOS, mas o dispositivo do kernel não apareceu"
            if info.smbios
            else "nenhuma interface IPMI encontrada (SMBIOS tipo 38 ausente e sem /dev/ipmi0)"
        )
        return

    rc, saida, erro = ex.rodar(["ipmitool", "mc", "info"], timeout=20)
    if rc != 0:
        info.motivo = f"'ipmitool mc info' falhou: {erro.strip() or f'código {rc}'}"
        return
    info.mc_info = parse_mc_info(saida)
    info.versao = info.mc_info.get("versao_ipmi") or (info.smbios or {}).get("versao_especificacao") or None
    info.disponivel = True


CANAIS_LAN_IPMI = ("1", "2")
"""Canais LAN do BMC consultados (Dell/Lenovo/Supermicro usam o 1; HPE, em geral, o 2)."""


def _ip_bmc_pelo_ipmi(ctx: Contexto) -> tuple[str | None, str]:
    """IP do BMC na rede (``ipmitool lan print``, só leitura). ``(ip, origem)`` ou ``(None, motivo)``."""
    motivos = []
    for canal in CANAIS_LAN_IPMI:
        rc, saida, erro = ctx.executor.rodar(["ipmitool", "lan", "print", canal], timeout=20)
        if rc != 0:
            motivos.append(f"canal {canal}: {(erro.strip().splitlines() or [f'código {rc}'])[-1]}")
            continue
        try:
            ip = parse_lan_print(saida).get("IP Address", "")
        except ErroParser as falha:
            motivos.append(f"canal {canal}: {falha}")
            continue
        if ip and ip != "0.0.0.0":
            return ip, f"ipmitool lan print {canal}"
        motivos.append(f"canal {canal}: BMC sem endereço IP")
    return None, "; ".join(motivos)


def tentar_redfish(ctx: Contexto, endereco: str, porta: int | None, origem: str) -> bool:
    """GET sem autenticação na raiz ``/redfish/v1`` de um endereço; confirmado → ``ctx.redfish``/``ctx.bmc`` preenchidos.

    Usado na detecção e depois de ligar o OS to iDRAC Pass-through (endereço interno, pedido de 01/10/2026)."""
    info = ctx.redfish
    host = f"[{endereco}]" if ":" in endereco else endereco
    base = f"https://{host}" + (f":{porta}" if porta and porta != 443 else "")
    cliente = ClienteRedfish(ctx.executor, base)
    try:
        raiz = cliente.raiz()
    except FalhaColeta as falha:
        info.tentativas.append(f"{origem}: {falha}")
        info.respondeu_http = info.respondeu_http or getattr(falha, "status", None) is not None
        return False
    info.endereco, info.porta, info.origem = endereco, porta, origem
    info.confirmado = True
    info.modo_tls = getattr(ctx.executor, "modos_tls_bmc", {}).get(base.split("://", 1)[1].lower(), "")
    if info.modo_tls and info.modo_tls != tls_bmc.PADRAO:
        info.tentativas.append(f"{origem}: iDRAC acessível em modo de compatibilidade ({tls_bmc.DESCRICAO[info.modo_tls]})")
    info.versao, info.fabricante, info.produto = raiz["versao"], raiz["fabricante"], raiz["produto"]
    info.motivo = ""
    ctx.bmc = AcessoBMC(cliente, raiz["contas"])
    ctx.cache["redfish_raiz"] = raiz
    return True


def detectar_redfish(ctx: Contexto) -> None:
    """Redfish: endereço pelo SMBIOS tipo 42 ou pelo IP do BMC (IPMI); confirmado com GET na raiz.

    A raiz ``/redfish/v1`` é lida sem autenticação (permitido pela especificação).
    As credenciais são pedidas depois, só se o Redfish for confirmado.
    """
    info = ctx.redfish
    candidatos: list[tuple[str, int | None, str]] = []
    rc, saida, erro = ctx.executor.rodar(["dmidecode", "-t", "42"], timeout=15)
    if rc != 0:
        info.tentativas.append(f"SMBIOS tipo 42 ilegível: {erro.strip() or f'código {rc}'}")
    else:
        info.host_interface = parse_host_interface_tipo42(saida)
        redfish = [p for p in info.host_interface if p["redfish"] and p["endereco_servico"]]
        if redfish:
            candidatos += [(p["endereco_servico"], p["porta"], "SMBIOS tipo 42") for p in redfish]
        else:
            info.tentativas.append("Redfish Host Interface não anunciada pelo SMBIOS (tipo 42 ausente)")
    if ctx.ipmi.disponivel:
        ip, origem = _ip_bmc_pelo_ipmi(ctx)
        if ip:
            candidatos.append((ip, None, origem))
        else:
            info.tentativas.append(f"IP do BMC pelo IPMI não obtido ({origem})")
    else:
        info.tentativas.append("IP do BMC não consultado (IPMI indisponível)")

    if not candidatos:
        info.motivo = "; ".join(info.tentativas) or "nenhum endereço de Redfish encontrado"
        return
    info.anunciado = True
    for endereco, porta, origem in candidatos:
        if tentar_redfish(ctx, endereco, porta, origem):
            return
    info.endereco, info.porta, info.origem = candidatos[0][0], candidatos[0][1], candidatos[0][2]
    info.motivo = "endereço do BMC encontrado, mas o serviço Redfish não respondeu: " + "; ".join(info.tentativas[-len(candidatos):])


def _nome_controladora(dispositivo: dict) -> str:
    subsistema = dispositivo.get("subsistema")
    if subsistema and not _RE_SUBSISTEMA_GENERICO.search(subsistema):
        return subsistema
    return dispositivo["descricao"]


def detectar_controladoras(ctx: Contexto) -> None:
    """Controladoras de armazenamento (classes PCI 0104, 0107, 0108) via ``lspci -nnk``."""
    rc, saida, erro = ctx.executor.rodar(["lspci", "-nnk"], timeout=20)
    if rc != 0:
        ctx.erros_deteccao["controladoras"] = erro.strip() or f"lspci retornou {rc}"
        return
    for dispositivo in parse_lspci_nnk(saida):
        if dispositivo["classe"] not in CLASSES_ARMAZENAMENTO:
            continue
        ctx.controladoras.append(
            Controladora(
                slot=dispositivo["slot"],
                classe=dispositivo["classe"],
                nome=_nome_controladora(dispositivo),
                descricao=dispositivo["descricao"],
                driver=dispositivo["driver"],
                categoria=classificar_controladora(dispositivo["classe"], dispositivo["driver"]),
                fabricante_id=dispositivo["fabricante_id"],
                dispositivo_id=dispositivo["dispositivo_id"],
                subsistema_ids=dispositivo["subsistema_ids"],
            )
        )


def detectar_sensores(ctx: Contexto) -> None:
    """Sensores expostos pelo kernel (lm-sensors)."""
    rc, saida, erro = ctx.executor.rodar(["sensors", "-j"], timeout=20)
    if rc != 0:
        ctx.erros_deteccao["sensores_kernel"] = erro.strip() or f"sensors retornou {rc}"
        return
    ctx.sensores_kernel = parse_sensors_json(saida)


# Ferramentas proprietárias (fora da ISO, em <dados>/vendor ou no PATH), por família.
FERRAMENTAS_FABRICANTE = {
    "storcli": ("perccli64", "perccli", "storcli64", "storcli", "perccli2", "storcli2"),  # *2: PERC 12 (mpi3mr)
    "megacli": ("MegaCli64", "MegaCli", "megacli"),
    "ssacli": ("ssacli", "hpssacli", "hpacucli"),
    "arcconf": ("arcconf",),
    "racadm": ("racadm",),  # Dell iDRAC Tools for Linux: ações do iDRAC pelo acesso interno (CLAUDE.md 17.4)
}


def detectar_ferramentas(ctx: Contexto) -> None:
    """Procura as ferramentas proprietárias: primeiro em ``<dados>/vendor``, depois no PATH.

    Sem racadm: prepara-o a partir do pacote "iDRAC Tools for Linux" em ``vendor/`` (CLAUDE.md 19.1, opção A).
    """
    for nomes in FERRAMENTAS_FABRICANTE.values():
        for nome in nomes:
            caminho_vendor = f"{ctx.pasta_dados}/vendor/{nome}" if ctx.pasta_dados else None
            if caminho_vendor and ctx.executor.existe(caminho_vendor):
                ctx.ferramentas_fabricante[nome] = caminho_vendor
            elif ctx.executor.tem_comando(nome):
                ctx.ferramentas_fabricante[nome] = nome
    preparar_racadm(ctx)


def preparar_racadm(ctx: Contexto) -> None:
    """racadm do iDRAC Tools (já preparado nesta sessão ou a preparar agora). Resultado em ``ctx.cache["idrac_tools"]``."""
    from msscan.deteccao import idrac_tools

    if "racadm" in ctx.ferramentas_fabricante:
        return
    # Já preparado é só leitura (existe + getversion): vale também na coleta, que não prepara nada (bancada de 19/10/2026:
    # o racadm respondia na sessão do R620, mas a coleta pela interface não o via e o racadm da RAID nunca foi coletado).
    if ctx.executor.existe(idrac_tools.RACADM):
        versoes = idrac_tools.ler_versoes(ctx.executor, idrac_tools.RACADM)
        if versoes is not None:
            ctx.ferramentas_fabricante["racadm"] = idrac_tools.RACADM
            ctx.cache["idrac_tools"] = {"situacao": idrac_tools.PREPARADO, "caminho": idrac_tools.RACADM, "versoes": versoes,
                                        "mensagem": "racadm já preparado nesta sessão", "pacote": None, "passos": []}  # fmt: skip
            return
    info = idrac_tools.preparar(ctx.executor, ctx.pasta_dados)
    ctx.cache["idrac_tools"] = info
    if info["situacao"] == idrac_tools.PREPARADO:
        ctx.ferramentas_fabricante["racadm"] = info["caminho"]
    elif info["situacao"] == idrac_tools.FALHOU:
        ctx.erros_deteccao["racadm"] = info["mensagem"]


def detectar_compatibilidade(ctx: Contexto) -> None:
    """Banco de compatibilidade (dica) e divergências com o que foi detectado."""
    from msscan.compat.banco import detectar_compatibilidade as consultar_banco

    consultar_banco(ctx)


DETECTORES: list[tuple[str, Callable[[Contexto], None]]] = [
    ("identificacao", detectar_identificacao),
    ("ipmi", detectar_ipmi),
    ("redfish", detectar_redfish),
    ("controladoras", detectar_controladoras),
    ("sensores_kernel", detectar_sensores),
    ("ferramentas_fabricante", detectar_ferramentas),
    ("compatibilidade", detectar_compatibilidade),
]


def detectar(executor: Executor, pasta_dados: str | None = None, pendrive: dict[str, Any] | None = None) -> Contexto:
    """Roda todos os detectores, isolando falhas, e devolve o ``Contexto``."""
    ctx = Contexto(executor=executor, pasta_dados=pasta_dados, pendrive=pendrive)
    for nome, detector in DETECTORES:
        try:
            detector(ctx)
        except Exception as erro:  # noqa: BLE001 - um detector nunca derruba os outros
            ctx.erros_deteccao[nome] = f"{type(erro).__name__}: {erro}"
    return ctx

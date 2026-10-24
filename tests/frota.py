"""Frota simulada de servidores Dell (pedido de 14/10/2026): compatibilidade além do R640 e do R530.

Cada perfil parte da coleta REAL do R640 (``tests/fixtures/dell/poweredge_r640``) e troca só VALORES, sempre no formato das
saídas reais já gravadas (CLAUDE.md 14: formato desconhecido não se inventa):

- modelo e System ID: SKU (``SKU=0737;ModelName=...``, 14ª geração em diante) ou SKU vazio + estrutura OEM tipo 208 (até a
  13ª; mesmo layout do R640 real, ``FE`` + ID de 16 bits);
- firmware do iDRAC no ``ipmitool mc info`` (formato do R640 real);
- controladoras no ``lspci -nnk`` com os IDs da ``pci.ids`` da Debian; discos atrás da PERC como no log real do R530
  (``smartctl --scan-open`` → ``-d sat+megaraid,N /dev/bus/0``), volume da PERC no ``lsblk`` com o nome da controladora;
- placas de rede: as 4 funções do R640 (mesmos nomes de interface) com outro chip, subsistema e driver no ``ethtool -i``.

Fontes dos valores: System IDs do catálogo oficial da Dell 26.10.08 (14ª–17ª, blades, C, XR); R530 ``063A`` (log real de
13/10/2026); 11ª/12ª: o subsistema Dell da placa onboard na ``pci.ids`` ("PowerEdge R710 BCM5709" = ``1028:0235``,
"PowerEdge R320 server" = ``1028:04f7``), que na Dell é o System ID (conferido no R640 real: ``1028:0716``; e na ``pci.ids``:
"C6420 I350 LOM" = ``1028:0757``, igual ao catálogo). Onde o valor não tem fonte, o comentário diz.

Os perfis não simulam o comportamento do iDRAC pela rede (isso é ``tests/test_bmc_geracoes.py``, com o TLS de cada geração);
aqui é o lado do servidor: detecção, diagnóstico, relatórios, inventário e catálogo.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

from msscan.core.executor import nome_arquivo_comando

R640 = Path(__file__).parent / "fixtures/dell/poweredge_r640"
R620 = Path(__file__).parent / "fixtures/dell/poweredge_r620"  # coleta real da 12ª (iDRAC7 2.65.65.65, PERC H710), 19/10/2026
RECORTE_FROTA = Path(__file__).parent / "fixtures/dell/catalogo/Catalog_recorte_frota.xml.gz"
RECORTES_ANTIGOS = {
    # Catálogos antigos (catalogos_dell.yaml): todos os pacotes do R530 (13ª) e do R320 (12ª), sem edição (15/10/2026).
    "https://downloads.dell.com/FOLDER14611037M/1/Catalog.xml.gz": Path(__file__).parent / "fixtures/dell/catalogo/Catalog_recorte_frota_13g_260601.xml.gz",
    "https://downloads.dell.com/FOLDER09390002M/1/Catalog.xml.gz": Path(__file__).parent / "fixtures/dell/catalogo/Catalog_recorte_frota_12g_221230.xml.gz",
}



def interfaces(pasta: Path) -> dict[str, str]:
    """Interfaces da coleta base e o endereço PCI de cada uma (``ethtool -i``, bus-info). R640: eno3/eno4 (01:00.x) e
    eno1np0/eno2np1 (18:00.x); R620: eno1–eno4 (01:00.x e 02:00.x)."""
    saida = {}
    for arquivo in sorted((pasta / "comandos").glob("ethtool__-i__*.stdout")):
        barramento = re.search(r"^bus-info: (?:0000:)?(\S+)$", arquivo.read_text(encoding="utf-8"), re.MULTILINE)
        if barramento:
            saida[arquivo.name.removeprefix("ethtool__-i__").removesuffix(".stdout")] = barramento.group(1)
    return saida


@dataclass(frozen=True)
class Controladora:
    """Uma controladora de armazenamento acrescentada ao ``lspci -nnk`` (bloco no formato do R640 real)."""

    endereco: str
    classe: str
    nome: str
    subsistema: str
    driver: str
    volumes: tuple[str, ...] = ()  # modelo de cada volume que o sistema vê (lsblk), ex.: "PERC H730 Mini"
    discos_megaraid: tuple[int, ...] = ()  # discos físicos atrás da PERC que o smartctl enxerga (-d sat+megaraid,N)


@dataclass(frozen=True)
class Placa:
    """Chip das 4 portas de rede (substitui o I350/X710 do R640 nas mesmas interfaces)."""

    nome: str
    subsistema: str
    driver: str
    firmware: str  # campo firmware-version do ethtool -i; formato por driver, só para o inventário tentar ler


@dataclass(frozen=True)
class Perfil:
    """Um servidor da frota."""

    modelo: str
    system_id: str
    sku_preenchido: bool  # 14ª em diante: SKU; até a 13ª: SKU vazio e System ID só na estrutura OEM 208
    idrac_firmware: str
    geracao: int
    idrac: str
    no_catalogo: bool  # o modelo está no catálogo oficial atual (13ª e anteriores não estão)
    controladoras: tuple[Controladora, ...] = ()
    placa: Placa | None = None
    sem_ipmi: bool = False  # IPMI desligado no iDRAC / sem SMBIOS 38: tudo que depende dele vira "não verificado"
    sensores_do_chassi: bool = False  # blade/sled: ventoinhas e fontes são do chassi (saída vazia do ipmitool) — robustez
    bios: str = "2.21.2"  # dmidecode -s bios-version (o do R640 real); cada geração tem a sua faixa de versões
    notas: tuple[str, ...] = field(default_factory=tuple)
    base: Path = R640

    @property
    def tem_redfish(self) -> bool:
        """iDRAC6: nunca; iDRAC7/8: a partir da 2.40.40.40 (docs/compatibilidade.md; R620 real com a 1.40: 404)."""
        if self.idrac == "iDRAC6":
            return False
        if self.idrac in ("iDRAC7", "iDRAC8"):
            return tuple(int(x) for x in self.idrac_firmware.split(".")[:2]) >= (2, 40)
        return True
    """Coleta real de partida. 11ª–13ª: o R620 (iDRAC7), formatos do ipmitool, do SEL e do SMBIOS mais próximos de iDRAC6/7/8
    do que os do iDRAC9 (19/10/2026: slots "DIMM_A1" e o 2º layout da estrutura OEM 208 só apareceram com ele)."""


# Controladoras (IDs da pci.ids; classe e driver como o kernel reporta).
PERC_H700 = Controladora("03:00.0", "RAID bus controller [0104]", "Broadcom / LSI MegaRAID SAS 2108 [Liberator] [1000:0079] (rev 05)",
                         "Dell PERC H700 Integrated [1028:1f17]", "megaraid_sas", ("PERC H700",), (0,))  # fmt: skip
PERC_H710 = Controladora("03:00.0", "RAID bus controller [0104]", "Broadcom / LSI MegaRAID SAS 2208 [Thunderbolt] [1000:005b] (rev 05)",
                         "Dell PERC H710 Mini (for monolithics) [1028:1f38]", "megaraid_sas", ("PERC H710",), (0, 1))  # fmt: skip
PERC_H730 = Controladora("02:00.0", "RAID bus controller [0104]", "Broadcom / LSI MegaRAID SAS-3 3108 [Invader] [1000:005d] (rev 02)",
                         "Dell PERC H730 Mini [1028:1f49]", "megaraid_sas", ("PERC H730 Mini",), (3,))  # fmt: skip  # R530 real: megaraid,3
HBA330 = Controladora("02:00.0", "Serial Attached SCSI controller [0107]", "Broadcom / LSI SAS3008 PCI-Express Fusion-MPT SAS-3 [1000:0097] (rev 02)",
                      "Dell HBA330 Mini [1028:1f53]", "mpt3sas")  # fmt: skip
PERC_H740P = Controladora("3b:00.0", "RAID bus controller [0104]", "Broadcom / LSI MegaRAID Tri-Mode SAS3508 [1000:0016] (rev 01)",
                          "Dell PERC H740P Mini [1028:1fcf]", "megaraid_sas", ("PERC H740P Mini",), (0, 1))  # fmt: skip
BOSS_S1 = Controladora("5e:00.0", "SATA controller [0106]", "Marvell Technology Group Ltd. 88SE9230 PCIe 2.0 x2 4-port SATA 6 Gb/s RAID Controller [1b4b:9230] (rev 11)",
                       "Dell BOSS-S1 Adapter [1028:1fe2]", "ahci", ("DELLBOSS VD",))  # fmt: skip
PERC_H755 = Controladora("4b:00.0", "RAID bus controller [0104]", "Broadcom / LSI MegaRAID 12GSAS/PCIe Secure SAS39xx [1000:10e2]",
                         "Dell PERC H755 Front [1028:1ae1]", "megaraid_sas", ("PERC H755 Front",), (0,))  # fmt: skip
# PERC 12 (H965i, mpi3mr): a pci.ids traz o chip (1000:00a5) mas não o subsistema Dell — subsistema genérico aqui.
PERC_12 = Controladora("4b:00.0", "RAID bus controller [0104]", "Broadcom / LSI Fusion-MPT 24GSAS/PCIe SAS40xx/41xx [1000:00a5] (rev 01)",
                       "Dell Device [1028:0000]", "mpi3mr", ("PERC H965i Front",))  # fmt: skip
BOSS_N1 = Controladora("c1:00.0", "Non-Volatile memory controller [0108]", "Marvell Technology Group Ltd. 88NR2241 Non-Volatile memory controller [1b4b:2241] (rev 20)",
                       "Dell BOSS-N1 Monolithic [1028:2112]", "nvme")  # fmt: skip
PERC_H730P_MX = Controladora("3b:00.0", "RAID bus controller [0104]", "Broadcom / LSI MegaRAID SAS-3 3108 [Invader] [1000:005d] (rev 02)",
                             "Dell PERC H730P MX [1028:1fd1]", "megaraid_sas", ("PERC H730P MX",), (0,))  # fmt: skip

# Placas de rede (IDs da pci.ids; o subsistema onboard da Dell = System ID do servidor quando a pci.ids o lista).
BCM5709_R710 = Placa("Broadcom Inc. and subsidiaries NetXtreme II BCM5709 Gigabit Ethernet [14e4:1639] (rev 20)",
                     "Dell PowerEdge R710 BCM5709 Gigabit Ethernet [1028:0235]", "bnx2", "7.10.4 bc 5.2.3 NCSI 2.0.12")  # fmt: skip
BCM5720_R320 = Placa("Broadcom Inc. and subsidiaries NetXtreme BCM5720 Gigabit Ethernet PCIe [14e4:165f]",
                     "Dell PowerEdge R320 server [1028:04f7]", "tg3", "FFV7.10.64 bc 5720-v1.34")  # fmt: skip
# R530: placa onboard BCM5720 (4 portas eno1–eno4 no log real); subsistema sem fonte → genérico.
BCM5720_R530 = Placa("Broadcom Inc. and subsidiaries NetXtreme BCM5720 Gigabit Ethernet PCIe [14e4:165f]",
                     "Dell Device [1028:0000]", "tg3", "FFV21.60.16 bc 5720-v1.39")  # fmt: skip
BCM57416 = Placa("Broadcom Inc. and subsidiaries BCM57416 NetXtreme-E Dual-Media 10G RDMA Ethernet Controller [14e4:16d8] (rev 01)",
                 "Dell NetXtreme-E 10Gb SFP+ Adapter [1028:1feb]", "bnxt_en", "220.0.59.0/pkg 22.00.07.60")  # fmt: skip
E810 = Placa("Intel Corporation Ethernet Controller E810-XXV for SFP [8086:159b] (rev 02)", "Dell Device [1028:0000]", "ice",
             "4.40 0x8001c7d1 1.3534.0")  # fmt: skip
BCM57504_XR11 = Placa("Broadcom Inc. and subsidiaries BCM57504 NetXtreme-E 10Gb/25Gb/40Gb/50Gb/100Gb Ethernet [14e4:1751] (rev 11)",
                      "Dell PowerEdge XR11/XR12 LOM [1028:09d4]", "bnxt_en", "224.0.110.0/pkg 22.41.12.80")  # fmt: skip
I350_C6420 = Placa("Intel Corporation I350 Gigabit Network Connection [8086:1521] (rev 01)", "Dell Gigabit I350-t LOM [1028:0757]", "igb",
                   "1.67, 0x80000fa8, 19.0.12")  # fmt: skip

PERFIS: dict[str, Perfil] = {
    "r710_11g": Perfil("PowerEdge R710", "0235", False, "2.92", 11, "iDRAC6", False, (PERC_H700,), BCM5709_R710, bios="6.6.0",
                       base=R620),
    "r320_12g": Perfil("PowerEdge R320", "04F7", False, "2.65", 12, "iDRAC7", False, (PERC_H710,), BCM5720_R320, bios="2.9.0",
                       base=R620),
    "r530_13g": Perfil("PowerEdge R530", "063A", False, "2.30", 13, "iDRAC8", False, (PERC_H730,), BCM5720_R530, bios="2.16.0",
                       notas=("Como o R530 real de 13/10/2026: SKU vazio, PERC H730, iDRAC8 2.30.",), base=R620),
    "r530_13g_r640": Perfil("PowerEdge R530", "063A", False, "2.30", 13, "iDRAC8", False, (PERC_H730,), BCM5720_R530,
                            bios="2.16.0", notas=("Sobre a coleta do R640: disco em sat+megaraid,3 como no log do R530.",)),
    "r530_hba": Perfil("PowerEdge R530", "063A", False, "2.30", 13, "iDRAC8", False, (HBA330,), BCM5720_R530, bios="2.16.0"),
    "r740xd_14g": Perfil("PowerEdge R740xd", "0737", True, "7.00", 14, "iDRAC9", True, (PERC_H740P, BOSS_S1), BCM57416),
    "r440_14g": Perfil("PowerEdge R440", "07C9", True, "7.00", 14, "iDRAC9", True),  # S140 (SATA do chipset, ahci), como o R640
    "r750_15g": Perfil("PowerEdge R750", "090E", True, "7.30", 15, "iDRAC9", True, (PERC_H755,), E810, bios="1.16.2"),
    "r760_16g": Perfil("PowerEdge R760", "0A6B", True, "7.30", 16, "iDRAC9", True, (PERC_12, BOSS_N1), E810, bios="2.3.5"),
    "r770_17g": Perfil("PowerEdge R770", "0C65", True, "1.30", 17, "iDRAC10", True, (PERC_12,), E810, bios="1.4.2"),
    "mx740c_blade": Perfil("PowerEdge MX740c", "0765", True, "7.00", 14, "iDRAC9", True, (PERC_H730P_MX,), BCM57416,
                           sensores_do_chassi=True),
    "c6420_sled": Perfil("PowerEdge C6420", "0757", True, "7.00", 14, "iDRAC9", True, (), I350_C6420, sensores_do_chassi=True),
    "xr11_borda": Perfil("PowerEdge XR11", "09D4", True, "7.00", 15, "iDRAC9", True, (), BCM57504_XR11, bios="1.15.1"),
    "r640_sem_ipmi": Perfil("PowerEdge R640", "0716", True, "7.00", 14, "iDRAC9", True, sem_ipmi=True),
}


def _gravar(pasta: Path, comando: list[str], saida: str, rc: int = 0, stderr: str = "") -> None:
    """Grava (ou troca) a saída de um comando no formato da coleta (``.json`` + ``.stdout``)."""
    base = pasta / "comandos" / nome_arquivo_comando(comando)
    base.with_suffix(".json").write_text(json.dumps({"comando": comando, "rc": rc, "stderr": stderr, "duracao": 0.01}, indent=2),
                                         encoding="utf-8")  # fmt: skip
    base.with_suffix(".stdout").write_text(saida, encoding="utf-8")


def _ler(pasta: Path, comando: list[str]) -> str:
    return (pasta / "comandos" / (nome_arquivo_comando(comando) + ".stdout")).read_text(encoding="utf-8")


def _oem_208(pasta: Path, system_id: str) -> str:
    """Estrutura OEM 208 da própria coleta base (R640: ``D0 10 00 D0 03 0B FE 00 16 07 ...``; R620: ``... 02 00 FE 00 CE 04``)
    com outro System ID nos dois bytes depois do ``FE 00``."""
    valor = int(system_id, 16)
    return re.sub(r"(D0 10 00 D0 [0-9A-F]{2} [0-9A-F]{2} FE 00) [0-9A-F]{2} [0-9A-F]{2}",
                  lambda m: f"{m.group(1)} {valor & 0xFF:02X} {valor >> 8:02X}", _ler(pasta, ["dmidecode", "-t", "208"]), count=1)


def _blocos_lspci(texto: str) -> list[str]:
    blocos, atual = [], []
    for linha in texto.splitlines():
        if linha and not linha[0].isspace() and atual:
            blocos.append("\n".join(atual))
            atual = []
        atual.append(linha)
    if atual:
        blocos.append("\n".join(atual))
    return blocos


def _bloco(endereco: str, classe: str, nome: str, subsistema: str, driver: str) -> str:
    return f"{endereco} {classe}: {nome}\n\tSubsystem: {subsistema}\n\tKernel driver in use: {driver}\n\tKernel modules: {driver}"


def _lspci(pasta: Path, perfil: Perfil) -> None:
    blocos = _blocos_lspci(_ler(pasta, ["lspci", "-nnk"]))
    placas = set(interfaces(pasta).values())
    # Controladoras de armazenamento da base (PERC H710 do R620): as do perfil ficam no lugar delas. Uma controladora do
    # perfil cujo endereço já é de outra peça da base (02:00.0 é placa de rede no R620) usa o endereço da que sai.
    da_base = [b.split(" ", 1)[0] for b in blocos if "[0104]" in b.split("\n", 1)[0] or "[0107]" in b.split("\n", 1)[0]]
    ocupados = {b.split(" ", 1)[0] for b in blocos} - set(da_base)
    livres = iter(da_base)
    controladoras = [c if c.endereco not in ocupados else replace(c, endereco=next(livres)) for c in perfil.controladoras]
    saida = []
    for bloco in blocos:
        endereco = bloco.split(" ", 1)[0]
        if perfil.placa and endereco in placas:
            bloco = _bloco(endereco, "Ethernet controller [0200]", perfil.placa.nome, perfil.placa.subsistema, perfil.placa.driver)
        elif endereco in da_base and perfil.controladoras:
            continue
        saida.append(bloco)
    saida += [_bloco(c.endereco, c.classe, c.nome, c.subsistema, c.driver) for c in controladoras]
    saida.sort(key=lambda b: b.split(" ", 1)[0])
    _gravar(pasta, ["lspci", "-nnk"], "\n".join(saida) + "\n")


def _ethtool(pasta: Path, perfil: Perfil) -> None:
    if not perfil.placa:
        return
    for interface in interfaces(pasta):
        comando = ["ethtool", "-i", interface]
        linhas = []
        for linha in _ler(pasta, comando).splitlines():
            if linha.startswith("driver:"):
                linha = f"driver: {perfil.placa.driver}"
            elif linha.startswith("firmware-version:"):
                linha = f"firmware-version: {perfil.placa.firmware}"
            linhas.append(linha)
        _gravar(pasta, comando, "\n".join(linhas) + "\n")


def _discos(pasta: Path, perfil: Perfil) -> None:
    """Volumes das controladoras no lsblk e discos atrás da PERC no smartctl (formato do R640 e do log do R530)."""
    lsblk = json.loads(_ler(pasta, ["lsblk", "-J", "-d", "-o", "NAME,TRAN,TYPE,SIZE,MODEL"]))
    if not (pasta / "comandos" / (nome_arquivo_comando(["smartctl", "-a", "-j", "-d", "sat", "/dev/sda"]) + ".stdout")).exists():
        # Base sem disco SATA direto (R620): os discos SAS reais atrás da PERC ficam; só o nome do volume muda.
        volumes = [m for c in perfil.controladoras for m in c.volumes]
        for dispositivo in lsblk["blockdevices"]:
            if volumes and str(dispositivo.get("model") or "").startswith("PERC"):
                dispositivo["model"] = volumes[0]
        _gravar(pasta, ["lsblk", "-J", "-d", "-o", "NAME,TRAN,TYPE,SIZE,MODEL"], json.dumps(lsblk, indent=3))
        return
    varredura = json.loads(_ler(pasta, ["smartctl", "--scan-open", "-j"]))
    disco_r640 = json.loads(_ler(pasta, ["smartctl", "-a", "-j", "-d", "sat", "/dev/sda"]))
    letras = iter("bcdefghij")
    for controladora in perfil.controladoras:
        for modelo in controladora.volumes:
            lsblk["blockdevices"].append({"name": "sd" + next(letras), "tran": None, "type": "disk", "size": "558.4G", "model": modelo})
        for n in controladora.discos_megaraid:
            tipo = f"sat+megaraid,{n}"
            varredura["devices"].append({"name": "/dev/bus/0", "info_name": f"/dev/bus/0 [megaraid_disk_{n:02d}] [SAT]",
                                         "type": tipo, "protocol": "ATA"})  # fmt: skip
            disco = json.loads(json.dumps(disco_r640))
            disco["device"] = {"name": "/dev/bus/0", "info_name": f"/dev/bus/0 [megaraid_disk_{n:02d}] [SAT]", "type": tipo,
                               "protocol": "ATA"}  # fmt: skip
            disco["smartctl"]["argv"] = ["smartctl", "-a", "-j", "-d", tipo, "/dev/bus/0"]
            disco["serial_number"] = f"FROTA{n:04d}"
            _gravar(pasta, ["smartctl", "-a", "-j", "-d", tipo, "/dev/bus/0"], json.dumps(disco, indent=2))
    _gravar(pasta, ["lsblk", "-J", "-d", "-o", "NAME,TRAN,TYPE,SIZE,MODEL"], json.dumps(lsblk, indent=3))
    _gravar(pasta, ["smartctl", "--scan-open", "-j"], json.dumps(varredura, indent=2))


def _ipmi(pasta: Path, perfil: Perfil) -> None:
    if perfil.sem_ipmi:
        # IPMI desligado: sem SMBIOS 38, sem /dev/ipmi0; o ipmitool falha como no Debian sem o dispositivo.
        _gravar(pasta, ["dmidecode", "-t", "38"], "# dmidecode 3.6\nGetting SMBIOS data from sysfs.\nSMBIOS 3.2.0 present.\n\n")
        caminhos = json.loads((pasta / "caminhos.json").read_text(encoding="utf-8"))
        caminhos["/dev/ipmi0"] = False
        (pasta / "caminhos.json").write_text(json.dumps(caminhos, indent=2), encoding="utf-8")
        for arquivo in (pasta / "comandos").glob("ipmitool__*.json"):
            comando = json.loads(arquivo.read_text(encoding="utf-8"))["comando"]
            _gravar(pasta, comando, "", rc=1, stderr="Could not open device at /dev/ipmi0 or /dev/ipmi/0 or /dev/ipmidev/0: "
                                                     "No such file or directory\n")  # fmt: skip
        return
    info = re.sub(r"^(Firmware Revision\s*:) \S+$", lambda m: f"{m.group(1)} {perfil.idrac_firmware}",
                  _ler(pasta, ["ipmitool", "mc", "info"]), count=1, flags=re.MULTILINE)
    _gravar(pasta, ["ipmitool", "mc", "info"], info)
    if perfil.base == R620:
        # O R620 real tem um volume com falha ("Drive 0 | 0x41 | ... | 0x4180"); na frota, servidores sem falha no RAID:
        # só "Drive Present" (o valor do R640 real), para a regra "RAID de hardware sem credenciais = não verificado".
        sensores = _ler(pasta, ["ipmitool", "sensor"]).replace("Drive 0          | 0x41       | discrete   | 0x4180",
                                                               "Drive 0          | 0x1        | discrete   | 0x0180")
        assert "0x4180" not in sensores, "a linha do Drive 0 do R620 mudou: ajuste a frota"
        _gravar(pasta, ["ipmitool", "sensor"], sensores)
    if perfil.sensores_do_chassi:
        _gravar(pasta, ["ipmitool", "sdr", "type", "Fan"], "")
        _gravar(pasta, ["ipmitool", "sdr", "type", "Power Supply"], "")


def _redfish(pasta: Path, perfil: Perfil) -> None:
    """Base com Redfish (R620 2.65) e perfil sem ele (iDRAC6, iDRAC7/8 antes da 2.40): a raiz responde 404, como no R620 real
    com a 1.40 (bancada de 16/10/2026)."""
    arquivo = pasta / "redfish.json"
    if perfil.tem_redfish or not arquivo.exists():
        return
    gravado = {"GET /redfish/v1": {"status": 404, "corpo": None, "erro": "", "erro_tipo": ""}}
    arquivo.write_text(json.dumps(gravado, indent=2), encoding="utf-8")


def criar(nome: str, destino: Path) -> Path:
    """Monta a fixture do perfil ``nome`` em ``destino`` (cópia da coleta real do R640 com os valores do perfil)."""
    perfil = PERFIS[nome]
    pasta = destino / nome
    shutil.copytree(perfil.base, pasta)
    _gravar(pasta, ["dmidecode", "-s", "system-product-name"], perfil.modelo + "\n")
    _gravar(pasta, ["dmidecode", "-s", "bios-version"], perfil.bios + "\n")
    sku = f"SKU={perfil.system_id};ModelName={perfil.modelo}" if perfil.sku_preenchido else ""
    _gravar(pasta, ["dmidecode", "-s", "system-sku-number"], sku + "\n")
    if not perfil.sku_preenchido:
        _gravar(pasta, ["dmidecode", "-t", "208"], _oem_208(pasta, perfil.system_id))
    _lspci(pasta, perfil)
    _ethtool(pasta, perfil)
    _discos(pasta, perfil)
    _ipmi(pasta, perfil)
    _redfish(pasta, perfil)
    origem = json.loads((pasta / "origem.json").read_text(encoding="utf-8"))
    origem.update({"modelo": perfil.modelo, "origem": f"simulado (tests/frota.py) a partir da coleta real do {perfil.base.name}",
                   "observacoes": list(perfil.notas)})  # fmt: skip
    (pasta / "origem.json").write_text(json.dumps(origem, indent=2, ensure_ascii=False), encoding="utf-8")
    return pasta

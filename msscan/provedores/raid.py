"""Provedores do componente RAID.

Cadeia: ferramentas do fabricante (perccli/storcli → MegaCli → ssacli → arcconf)
→ sensores de baia e bateria do BMC (IPMI) → mdadm (RAID de software e do chipset). Redfish Storage entra na fase 6.

Estado atual (decisão do usuário): as ferramentas proprietárias são EXECUTADAS
(somente leitura) e suas saídas ficam gravadas no modo coleta, mas a
interpretação só será escrita quando houver saídas reais (Fase 4b). Até lá o
provedor devolve "não verificado" com o motivo, e o RAID de hardware nunca
aparece como OK sem evidência.
"""

from __future__ import annotations

import re
from typing import Any

from msscan.core.fallback import Disponibilidade, FalhaColeta, Provedor
from msscan.core.modelo import Resultado, Status, pior_status
from msscan.deteccao.contexto import Contexto
from msscan.deteccao.detectores import FERRAMENTAS_FABRICANTE
from msscan.parsers.ipmitool import parse_sensor
from msscan.parsers.mdadm import parse_examine_scan
from msscan.parsers.procfs import parse_mdstat
from msscan.provedores.comum import rodar_ou_falhar

# Comandos de leitura executados por família (todos passam por validar_comando_leitura).
COMANDOS_RAID: dict[str, list[list[str]]] = {
    "storcli": [
        ["/call", "show", "all", "J"],
        ["/call/vall", "show", "all", "J"],
        ["/call/eall/sall", "show", "all", "J"],
        ["/call/bbu", "show", "all", "J"],
        ["/call/cv", "show", "all", "J"],
    ],
    "megacli": [
        ["-AdpAllInfo", "-aALL", "-NoLog"],
        ["-LDInfo", "-Lall", "-aALL", "-NoLog"],
        ["-PDList", "-aALL", "-NoLog"],
        ["-AdpBbuCmd", "-GetBbuStatus", "-aALL", "-NoLog"],
    ],
    "ssacli": [
        ["ctrl", "all", "show", "config", "detail"],
        ["ctrl", "all", "show", "status"],
    ],
    "arcconf": [
        ["GETCONFIG", "1", "AL"],
    ],
}

# Palavras que indicam escrita em qualquer ferramenta de RAID: nunca permitidas.
PALAVRAS_ESCRITA = {
    "set", "add", "delete", "del", "create", "modify", "start", "stop", "import", "clear", "flash",
    "download", "rebuild", "init", "initialize", "reset", "reboot", "restart", "flush", "erase",
    "secureerase", "migrate", "expand", "transform", "learn", "-bbulearn", "-cfgclr", "-cfgldadd",
    "-cfglddel", "-pdmakegood", "-adpsetprop", "-pdrbld", "-ldinit", "-adpfwflash", "-cfgforeign",
    "romupdate", "setstate", "task", "uninstall", "insert", "remove", "replace", "spin", "online", "offline",
}  # fmt: skip

_VERBOS_PERMITIDOS = {
    "storcli": lambda args: "show" in args,
    "megacli": lambda args: args[0] in {"-AdpAllInfo", "-LDInfo", "-PDList", "-EncInfo"}
    or (args[0] == "-AdpBbuCmd" and "-GetBbuStatus" in args),
    "ssacli": lambda args: "show" in args,
    "arcconf": lambda args: args[0].upper() in {"GETCONFIG", "GETVERSION", "LIST", "GETSTATUS"},
}

DRIVERS_POR_FAMILIA = {
    "storcli": {"megaraid_sas"},
    "megacli": {"megaraid_sas", "megaraid"},
    "ssacli": {"hpsa", "cciss", "smartpqi"},
    "arcconf": {"aacraid", "smartpqi"},
}


def validar_comando_leitura(familia: str, args: list[str]) -> None:
    """Levanta ``ValueError`` se o comando não for claramente de leitura."""
    if not args:
        raise ValueError("comando vazio")
    minusculos = {a.lower() for a in args}
    proibidas = minusculos & PALAVRAS_ESCRITA
    if proibidas:
        raise ValueError(f"comando com palavra de escrita {sorted(proibidas)}: {args}")
    verificador = _VERBOS_PERMITIDOS.get(familia)
    if verificador is None or not verificador(args):
        raise ValueError(f"comando não está na lista de leitura da família {familia}: {args}")


class ProvedorRaidFabricante(Provedor):
    """Executa a ferramenta proprietária de uma família (somente leitura).

    Interpretação pendente de saídas reais (Fase 4b): devolve NAO_VERIFICADO
    explicando o motivo, e as saídas ficam registradas no histórico/coleta.
    """

    def __init__(self, familia: str, prioridade: int) -> None:
        self.familia = familia
        self.prioridade = prioridade
        self.nome = f"fabricante ({'/'.join(FERRAMENTAS_FABRICANTE[familia])})"

    def _ferramenta(self, ctx: Contexto) -> str | None:
        return next((ctx.ferramentas_fabricante[n] for n in FERRAMENTAS_FABRICANTE[self.familia] if n in ctx.ferramentas_fabricante), None)

    def disponivel(self, ctx: Contexto) -> Disponibilidade:
        drivers = DRIVERS_POR_FAMILIA[self.familia]
        if not any(c.driver in drivers for c in ctx.controladoras):
            return Disponibilidade(False, f"nenhuma controladora com driver {'/'.join(sorted(drivers))}")
        if self._ferramenta(ctx) is None:
            nomes = ", ".join(FERRAMENTAS_FABRICANTE[self.familia])
            return Disponibilidade(False, f"ferramenta não encontrada ({nomes}): copie-a para a pasta vendor/ do pendrive")
        return Disponibilidade(True)

    def coletar(self, ctx: Contexto) -> Resultado | None:
        ferramenta = self._ferramenta(ctx)
        executados, falhas = [], []
        for args in COMANDOS_RAID[self.familia]:
            validar_comando_leitura(self.familia, args)
            rc, _, _ = ctx.executor.rodar([ferramenta, *args], timeout=120)
            if rc == 0:
                executados.append(" ".join(args))
            else:
                falhas.append(f"{' '.join(args)} (rc {rc})")
        if not executados:
            raise FalhaColeta(f"{ferramenta} falhou em todos os comandos: {'; '.join(falhas)}")
        return Resultado(
            componente="raid",
            status=Status.NAO_VERIFICADO,
            fonte=ferramenta,
            dados={"comandos": executados, "falhas": falhas},
            mensagem=(
                f"saídas do {ferramenta.rsplit('/', 1)[-1]} coletadas, mas a interpretação ainda não foi "
                "implementada (aguardando saída real para validar o parser)"
            ),
        )


# --- Sensores do BMC: baias de disco e bateria da controladora (IPMI) --------------------------------------------------

# IPMI 2.0, tabela 42-3, tipo 0Dh "Drive Slot (Bay)": bit → (texto do ipmitool, situação). Conferido na tabela do próprio
# ipmitool da Debian (mesma ordem) e na leitura real do R640 ("Drive 0 | 0x1 | 0x0180" = "Drive Present" no ``sdr type``).
ESTADOS_BAIA: dict[int, tuple[str, Status | None]] = {
    0: ("Drive Present", None),
    1: ("Drive Fault", Status.FALHA),
    2: ("Predictive Failure", Status.ALERTA),
    3: ("Hot Spare", None),
    4: ("Parity Check In Progress", None),
    5: ("In Critical Array", Status.FALHA),  # volume degradado (como o mdadm: degradado = FALHA)
    6: ("In Failed Array", Status.FALHA),
    7: ("Rebuild In Progress", Status.ALERTA),
    8: ("Rebuild Aborted", Status.ALERTA),
}
# Tipo 29h "Battery": 0 low (predictive failure), 1 failed, 2 presence detected.
ESTADOS_BATERIA: dict[int, tuple[str, Status | None]] = {0: ("Low", Status.ALERTA), 1: ("Failed", Status.ALERTA),
                                                         2: ("Presence Detected", None)}  # fmt: skip
SENSOR_BAIA = re.compile(r"^Drive \d+$")
SENSOR_BATERIA_RAID = "ROMB Battery"  # bateria da controladora integrada (Dell; R620 e R640 reais)


def bits_discretos(status: str) -> set[int] | None:
    """Estados ativos de um sensor discreto do ``ipmitool sensor`` (4ª coluna, ``0xHHLL``: HH = estados 0–7, LL = 8–14).

    Leitura real do R620: "Drive 0 | 0x41 | discrete | 0x4180" → {0, 6}. Fora desse formato (``na``): ``None``.
    """
    if not re.fullmatch(r"0x[0-9a-fA-F]{4}", status or ""):
        return None
    valor = int(status, 16)
    alto, baixo = valor >> 8, valor & 0x7F  # bit 7 do 2º byte é reservado (vem 1 em todos os sensores lidos)
    return {b for b in range(8) if alto >> b & 1} | {8 + b for b in range(7) if baixo >> b & 1}


def avaliar_sensores_bmc_raid(sensores: list[dict[str, Any]]) -> Resultado:
    """RAID pelo que o BMC diz AGORA das baias de disco e da bateria da controladora (leitura ao vivo, não o SEL).

    Só a presença (nenhum estado de problema) não prova que os volumes estão bons: o sensor do BMC não descreve os
    volumes. Nesse caso o resultado é NAO_VERIFICADO com o que foi lido (o fallback registra como "sem dados").
    """
    problemas: list[str] = []
    situacoes: list[Status] = []
    lidos: list[dict[str, Any]] = []
    for sensor in sensores:
        nome = sensor.get("nome") or ""
        tabela = ESTADOS_BAIA if SENSOR_BAIA.match(nome) else ESTADOS_BATERIA if nome == SENSOR_BATERIA_RAID else None
        bits = bits_discretos(sensor.get("status") or "") if tabela is not None and sensor.get("discreto") else None
        if bits is None:
            continue
        estados = [tabela[b][0] for b in sorted(bits) if b in tabela]
        lidos.append({"sensor": nome, "estado": sensor["status"], "estados": estados})
        ruins = [(tabela[b][0], tabela[b][1]) for b in sorted(bits) if b in tabela and tabela[b][1] is not None]
        if ruins:
            peca = "bateria da controladora RAID (ROMB)" if tabela is ESTADOS_BATERIA else f"baia de disco ({nome})"
            problemas.append(f"{peca}: {', '.join(t for t, _ in ruins)} — leitura atual do BMC")
            situacoes += [s for _, s in ruins]
    fonte = "ipmitool sensor (baias de disco e bateria da controladora)"
    if not situacoes:
        detalhe = "; ".join(f"{s['sensor']}: {', '.join(s['estados']) or 'sem estado'}" for s in lidos) or "nenhum sensor de baia ou bateria"
        return Resultado(componente="raid", status=Status.NAO_VERIFICADO, fonte=fonte, dados={"sensores_bmc": lidos},
                         mensagem=f"o BMC não indica problema nas baias nem na bateria ({detalhe}), mas não informa o estado "
                                  "dos volumes")  # fmt: skip
    return Resultado(componente="raid", status=pior_status(situacoes), fonte=fonte,
                     dados={"sensores_bmc": lidos, "problemas": problemas, "volumes_verificados": False},
                     mensagem=f"o BMC indica: {'; '.join(problemas)}. Volumes e discos em detalhe só com as credenciais do "
                              "iDRAC (Redfish) ou com o perccli em vendor/")  # fmt: skip


class ProvedorRaidSensoresBMC(Provedor):
    """Estado atual das baias e da bateria da controladora pelo BMC (sem credenciais e sem ferramenta do fabricante).

    Bancada de 19/10/2026 (R620, PERC H710 sem perccli): o RAID ficava "não verificado" sem credenciais, enquanto o BMC
    marcava "Drive 0" com "In Failed Array"; com credenciais, o Redfish do mesmo iDRAC dizia "Virtual Disk 0: Critical".
    """

    nome = "IPMI (sensores de baia e bateria do BMC)"
    prioridade = 40

    def disponivel(self, ctx: Contexto) -> Disponibilidade:
        if not ctx.controladoras_raid_hardware:
            return Disponibilidade(False, "nenhuma controladora RAID de hardware")
        if not ctx.ipmi.disponivel:
            return Disponibilidade(False, ctx.ipmi.motivo or "IPMI não disponível")
        return Disponibilidade(True)

    def coletar(self, ctx: Contexto) -> Resultado | None:
        return avaliar_sensores_bmc_raid(parse_sensor(rodar_ou_falhar(ctx, ["ipmitool", "sensor"], timeout=120)))


# --- mdadm (RAID de software / chipset) ------------------------------------------


def avaliar_mdadm(
    arrays_ativos: list[dict[str, Any]],
    arrays_superbloco: list[dict[str, Any]],
    ha_raid_hardware: bool,
) -> Resultado | None:
    """Avalia RAID de software a partir de /proc/mdstat e ``mdadm --examine --scan``.

    - Com controladora RAID de hardware, devolve ``None``: o mdadm não enxerga
      os volumes dela, e o componente não pode parecer verificado.
    - Sem nenhum RAID: NAO_APLICAVEL.
    - Array degradado ou com disco falho → FALHA; reconstrução/sincronização ou
      array inativo → ALERTA.
    - Arrays só nos superblocos (não montados: a ISO não monta arrays para não
      provocar escrita) → NAO_VERIFICADO com explicação.

    TODO(fixture): avaliação de arrays só testada com estruturas parseadas;
    precisa de /proc/mdstat e ``mdadm --examine --scan`` reais com arrays.
    """
    if ha_raid_hardware:
        return None
    fonte = "/proc/mdstat + mdadm --examine --scan"
    if not arrays_ativos and not arrays_superbloco:
        return Resultado(
            componente="raid",
            status=Status.NAO_APLICAVEL,
            fonte=fonte,
            dados={"arrays": [], "superblocos": []},
            mensagem="nenhuma controladora RAID de hardware e nenhum volume RAID de software/chipset encontrado",
        )
    if not arrays_ativos:
        return Resultado(
            componente="raid",
            status=Status.NAO_VERIFICADO,
            fonte=fonte,
            dados={"arrays": [], "superblocos": arrays_superbloco},
            mensagem=(
                f"{len(arrays_superbloco)} volume(s) RAID de software/chipset encontrado(s) nos discos, mas não "
                "montado(s): o MSscan não monta arrays (evita escrita); estado não verificado"
            ),
        )
    problemas: list[str] = []
    situacoes: list[Status] = []
    for array in arrays_ativos:
        rotulo = f"{array['nome']} ({array['nivel'] or 'nível ?'})"
        if array["falhos"]:
            situacoes.append(Status.FALHA)
            problemas.append(f"{rotulo}: disco(s) com falha: {', '.join(array['falhos'])}")
        if array["degradado"]:
            situacoes.append(Status.FALHA)
            problemas.append(f"{rotulo}: DEGRADADO ({array['ativos']} de {array['total']} discos, mapa [{array['mapa']}])")
        if array["operacao"]:
            situacoes.append(Status.ALERTA)
            problemas.append(f"{rotulo}: {array['operacao']} em andamento ({array['progresso']:g}%)")
        if array["estado"] != "active":
            situacoes.append(Status.ALERTA)
            problemas.append(f"{rotulo}: estado {array['estado']}")
        situacoes.append(Status.OK)
    return Resultado(
        componente="raid",
        status=pior_status(situacoes),
        fonte=fonte,
        dados={"arrays": arrays_ativos, "superblocos": arrays_superbloco, "problemas": problemas},
        mensagem=f"{len(arrays_ativos)} volume(s) RAID de software ativo(s)",
    )


class ProvedorMdadm(Provedor):
    """RAID de software e de chipset (Intel RST/VROC) via /proc/mdstat e mdadm (somente leitura)."""

    nome = "sistema (mdadm)"
    prioridade = 50

    def disponivel(self, ctx: Contexto) -> Disponibilidade:
        return Disponibilidade(True)

    def coletar(self, ctx: Contexto) -> Resultado | None:
        sem_driver = [c for c in ctx.controladoras if c.categoria == "sem_driver" and c.classe == "0104"]
        if sem_driver:
            # T620 (16/10/2026): PERC H310 sem driver carregado virava "nenhuma controladora RAID" (NAO_APLICAVEL).
            raise FalhaColeta(controladora_sem_driver(sem_driver))
        mdstat = ctx.executor.ler_arquivo("/proc/mdstat")
        examinou = False
        arrays_superbloco: list[dict[str, Any]] = []
        if ctx.executor.tem_comando("mdadm"):
            rc, saida, _ = ctx.executor.rodar(["mdadm", "--examine", "--scan"], timeout=60)
            if rc == 0:
                examinou = True
                arrays_superbloco = parse_examine_scan(saida)
        if mdstat is None and not examinou:
            # Sem nenhuma das duas fontes não dá para afirmar que "não há RAID".
            return None
        return avaliar_mdadm(parse_mdstat(mdstat or ""), arrays_superbloco, bool(ctx.controladoras_raid_hardware))


def controladora_sem_driver(controladoras: list[Any]) -> str:
    """Motivo para controladora RAID (classe PCI 0104) que o kernel enxerga mas nenhum driver assumiu."""
    nomes = ", ".join(f"{c.nome or c.descricao} ({c.slot})" for c in controladoras)
    return (f"controladora RAID {nomes} presente, mas sem driver carregado: os discos e volumes atrás dela ficam "
            "invisíveis. Causas comuns: controladora em falha ou desativada no BIOS; confira os LEDs da controladora e "
            "a tela dela no boot (Ctrl+R)")


def provedores_raid() -> list[Provedor]:
    """Cadeia do componente RAID, em ordem de prioridade."""
    return [
        ProvedorRaidFabricante("storcli", 30),
        ProvedorRaidFabricante("megacli", 31),
        ProvedorRaidFabricante("ssacli", 32),
        ProvedorRaidFabricante("arcconf", 33),
        ProvedorRaidSensoresBMC(),
        ProvedorMdadm(),
    ]

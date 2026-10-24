"""Parsers da saída do ``ipmitool``."""

from __future__ import annotations

from typing import Any

from msscan.parsers import ErroParser


def parse_mc_info(texto: str) -> dict[str, Any]:
    """Interpreta ``ipmitool mc info``.

    Devolve ``{"campos": {...}, "versao_ipmi", "firmware", "fabricante",
    "fabricante_id"}``. Campos multilinha (ex.: "Additional Device Support")
    viram listas. Levanta ``ErroParser`` se não houver nenhum campo reconhecível.
    """
    campos: dict[str, Any] = {}
    chave_lista: str | None = None
    for linha in texto.splitlines():
        if not linha.strip():
            continue
        if linha.startswith((" ", "\t")) and chave_lista is not None:
            campos[chave_lista].append(linha.strip())
            continue
        if ":" not in linha:
            chave_lista = None
            continue
        chave, _, valor = linha.partition(":")
        chave, valor = chave.strip(), valor.strip()
        if valor:
            campos[chave] = valor
            chave_lista = None
        else:
            campos[chave] = []
            chave_lista = chave
    if "IPMI Version" not in campos and "Firmware Revision" not in campos:
        raise ErroParser("saída do 'ipmitool mc info' sem os campos esperados")
    return {
        "campos": campos,
        "versao_ipmi": campos.get("IPMI Version"),
        "firmware": campos.get("Firmware Revision"),
        "fabricante": campos.get("Manufacturer Name"),
        "fabricante_id": campos.get("Manufacturer ID"),
    }


def _numero(texto: str) -> float | None:
    """Converte "31.000" (ou "31,000" em locale pt_BR) em float; ``None`` se não for número."""
    try:
        return float(texto.strip().replace(",", "."))
    except ValueError:
        return None


LIMIARES_SENSOR = ("lnr", "lcr", "lnc", "unc", "ucr", "unr")


def parse_sensor(texto: str) -> list[dict[str, Any]]:
    """Interpreta ``ipmitool sensor`` (tabela com 10 colunas separadas por ``|``).

    Cada sensor: ``nome``, ``valor`` (float, ou ``None`` se "na"), ``valor_bruto``,
    ``unidade``, ``discreto``, ``status`` (ok/nc/cr/nr/na ou hexadecimal nos
    discretos) e ``limiares`` (lnr, lcr, lnc, unc, ucr, unr; ``None`` se "na").
    Nomes podem se repetir (ex.: dois sensores "Temp" num Dell R640).
    """
    sensores: list[dict[str, Any]] = []
    for linha in texto.splitlines():
        campos = [c.strip() for c in linha.split("|")]
        if len(campos) < 4 or not campos[0]:
            continue
        unidade = campos[2]
        discreto = unidade == "discrete"
        valor_bruto = campos[1]
        sensores.append(
            {
                "nome": campos[0],
                "valor": None if discreto or valor_bruto == "na" else _numero(valor_bruto),
                "valor_bruto": valor_bruto,
                "unidade": unidade,
                "discreto": discreto,
                "status": campos[3],
                "limiares": {
                    nome: (_numero(v) if v != "na" else None) for nome, v in zip(LIMIARES_SENSOR, campos[4:10])
                },
            }
        )
    if texto.strip() and not sensores:
        raise ErroParser("nenhum sensor reconhecido na saída do 'ipmitool sensor'")
    return sensores


def parse_sdr_tipo(texto: str) -> list[dict[str, Any]]:
    """Interpreta ``ipmitool sdr type <tipo>`` (5 colunas separadas por ``|``).

    Cada linha: ``nome``, ``id_sensor`` (ex. "85h"), ``status`` (ok, ns, nc,
    cr, nr...), ``entidade`` (ex. "10.1" = fonte 1), ``leitura`` (texto bruto),
    ``valor`` e ``unidade`` (se a leitura for numérica, ex. "6840 RPM") e
    ``estados`` (lista, se a leitura for textual, ex. ["Presence detected",
    "Power Supply AC lost"]).
    """
    linhas: list[dict[str, Any]] = []
    for linha in texto.splitlines():
        campos = [c.strip() for c in linha.split("|")]
        if len(campos) < 5 or not campos[0]:
            continue
        leitura = " | ".join(campos[4:]).strip()
        valor, unidade, estados = None, "", []
        partes = leitura.split(maxsplit=1)
        numero = _numero(partes[0]) if partes else None
        if numero is not None:
            valor = numero
            unidade = partes[1] if len(partes) > 1 else ""
        elif leitura:
            estados = [e.strip() for e in leitura.split(",") if e.strip()]
        linhas.append(
            {
                "nome": campos[0],
                "id_sensor": campos[1],
                "status": campos[2],
                "entidade": campos[3],
                "leitura": leitura,
                "valor": valor,
                "unidade": unidade,
                "estados": estados,
            }
        )
    if texto.strip() and not linhas:
        raise ErroParser("nenhum sensor reconhecido na saída do 'ipmitool sdr type'")
    return linhas


# Tipos de sensor da especificação IPMI, como o ipmitool os escreve. A coluna
# "sensor" do ``sel elist`` é "<tipo> <nome do sensor>".
TIPOS_SENSOR_IPMI = (
    "Temperature", "Voltage", "Current", "Fan", "Physical Security", "Platform Security",
    "Processor", "Power Supply", "Power Unit", "Cooling Device", "Other", "Memory",
    "Drive Slot / Bay", "POST Memory Resize", "System Firmwares", "System Firmware Progress",
    "Event Logging Disabled", "Watchdog1", "System Event", "Critical Interrupt", "Button",
    "Module / Board", "Microcontroller", "Add-in Card", "Chassis", "Chip Set", "Other FRU",
    "Cable / Interconnect", "Terminator", "System Boot Initiated", "Boot Error", "OS Boot",
    "OS Critical Stop", "Slot / Connector", "System ACPI Power State", "Watchdog2",
    "Platform Alert", "Entity Presence", "Monitor ASIC", "LAN", "Management Subsys Health",
    "Battery", "Session Audit", "Version Change", "FRU State",
)  # fmt: skip
_TIPOS_POR_TAMANHO = sorted(TIPOS_SENSOR_IPMI, key=len, reverse=True)


def tipo_do_sensor(sensor: str) -> str:
    """Extrai o tipo IPMI do começo da coluna "sensor" do SEL ("" se desconhecido)."""
    return next((t for t in _TIPOS_POR_TAMANHO if sensor == t or sensor.startswith(t + " ")), "")


def parse_sel_elist(texto: str) -> list[dict[str, str]]:
    """Interpreta ``ipmitool sel elist``.

    Colunas: id | data | hora | sensor | evento | direção. A descrição do evento
    pode conter ``|`` (ex.: "Uncorrectable ECC (UnCorrectable ECC |  DIMMA2)"),
    então a direção é a última coluna e o evento é tudo entre o sensor e ela.
    As datas são mantidas como texto: o relógio do BMC pode estar errado.
    """
    eventos: list[dict[str, str]] = []
    reconheceu = False
    for linha in texto.splitlines():
        if "no entries" in linha.lower():
            reconheceu = True
            continue
        campos = [c.strip() for c in linha.split("|")]
        if len(campos) < 5 or not campos[0]:
            continue
        if campos[-1] in ("Asserted", "Deasserted"):
            direcao, meio = campos[-1], campos[4:-1]
        else:
            direcao, meio = "", campos[4:]
        eventos.append(
            {
                "id": campos[0],
                "data": campos[1],
                "hora": campos[2],
                "sensor": campos[3],
                "tipo": tipo_do_sensor(campos[3]),
                "evento": " | ".join(meio).strip(" |"),
                "direcao": direcao,
            }
        )
    if texto.strip() and not eventos and not reconheceu:
        raise ErroParser("nenhum evento reconhecido na saída do 'ipmitool sel elist'")
    return eventos


def parse_lan_print(texto: str) -> dict[str, str]:
    """Interpreta ``ipmitool lan print <canal>``: ``{"IP Address": "...", "MAC Address": "...", ...}``.

    Linhas de continuação (começam com ``:``, ex.: "Auth Type Enable") são ignoradas.
    """
    campos: dict[str, str] = {}
    for linha in texto.splitlines():
        if ":" not in linha or linha.lstrip().startswith(":"):
            continue
        chave, _, valor = linha.partition(":")
        chave = chave.strip()
        if chave and chave not in campos:
            campos[chave] = valor.strip()
    if not campos:
        raise ErroParser("saída do 'ipmitool lan print' sem campos reconhecíveis")
    return campos


def parse_user_list(texto: str) -> list[dict[str, Any]]:
    """Interpreta ``ipmitool user list <canal>`` (colunas de largura fixa do ipmitool: ``%-4d%-17s``...).

    Um item por slot: ``{"slot", "nome", "callin", "link_auth", "ipmi_msg", "privilegio"}``; nome vazio = slot livre.
    Saída real: iDRAC9 do R640 (``tests/fixtures/dell/poweredge_r640_idrac``). Senhas nunca aparecem nesta saída.
    """
    usuarios = []
    for linha in texto.splitlines():
        if not linha[:4].strip().isdigit():
            continue
        resto = linha[21:].split()
        if len(resto) < 4:
            continue
        usuarios.append({"slot": int(linha[:4]), "nome": linha[4:21].strip(), "callin": resto[0] == "true",
                         "link_auth": resto[1] == "true", "ipmi_msg": resto[2] == "true", "privilegio": " ".join(resto[3:])})  # fmt: skip
    if texto.strip() and not usuarios:
        raise ErroParser("saída do 'ipmitool user list' sem usuários reconhecíveis")
    return usuarios


PORTAS_ATIVAS_DELL = {"none": "", "lom1": "LOM1", "lom2": "LOM2", "lom3": "LOM3", "lom4": "LOM4", "dedicated": "dedicada"}


def parse_delloem_active(texto: str) -> str:
    """``ipmitool delloem lan get active``: porta em que o iDRAC tem link — ``"dedicada"``, ``"LOM1"``…``"LOM4"`` ou ``""``
    (``None``: sem link na porta escolhida no NIC Selection).

    Saída real do iDRAC9 do R640 (``tests/fixtures/dell/poweredge_r640_idrac``): ``\\ndedicated\\n``. Os demais valores
    vêm da fonte do ipmitool (``AciveLOM_String`` em ``lib/ipmi_delloem.c``).
    """
    palavras = texto.split()
    if len(palavras) != 1 or palavras[0].lower() not in PORTAS_ATIVAS_DELL:
        raise ErroParser("saída do 'ipmitool delloem lan get active' não reconhecida")
    return PORTAS_ATIVAS_DELL[palavras[0].lower()]

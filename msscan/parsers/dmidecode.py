"""Parsers da saída do ``dmidecode``."""

from __future__ import annotations

import re
from typing import Any

# Valores que fabricantes deixam no SMBIOS quando o campo não foi preenchido.
VALORES_GENERICOS = {
    "",
    "not specified",
    "not present",
    "not available",
    "none",
    "to be filled by o.e.m.",
    "to be filled by oem",
    "default string",
    "system product name",
    "system manufacturer",
    "o.e.m.",
    "oem",
    "unknown",
    "invalid",
}

_RE_HANDLE = re.compile(r"^Handle (0x[0-9A-Fa-f]+), DMI type (\d+), (\d+) bytes")


def valor_util(valor: str | None) -> str | None:
    """Devolve o valor sem espaços extras, ou ``None`` se for um texto genérico de SMBIOS."""
    if valor is None:
        return None
    limpo = valor.strip()
    if limpo.lower() in VALORES_GENERICOS:
        return None
    return limpo


def parse_dmidecode_s(texto: str) -> str | None:
    """Interpreta a saída de ``dmidecode -s <palavra-chave>``.

    Ignora linhas de comentário (``#``) e devolve ``None`` para valores genéricos
    como "To Be Filled By O.E.M.".
    """
    for linha in texto.splitlines():
        if linha.startswith("#") or not linha.strip():
            continue
        return valor_util(linha)
    return None


def separar_estruturas(texto: str) -> list[dict[str, Any]]:
    """Separa a saída de ``dmidecode -t <tipo>`` em estruturas SMBIOS.

    Cada estrutura vira ``{"handle", "tipo", "titulo", "linhas"}``, onde
    ``linhas`` é uma lista de ``(profundidade, chave, valor)`` na ordem original.
    Linhas sem ``:`` (itens de lista) têm chave vazia.
    """
    estruturas: list[dict[str, Any]] = []
    atual: dict[str, Any] | None = None
    for linha in texto.splitlines():
        combinacao = _RE_HANDLE.match(linha)
        if combinacao:
            atual = {
                "handle": combinacao.group(1),
                "tipo": int(combinacao.group(2)),
                "titulo": "",
                "linhas": [],
            }
            estruturas.append(atual)
            continue
        if atual is None or not linha.strip():
            continue
        if not linha.startswith(("\t", " ")):
            if not atual["titulo"]:
                atual["titulo"] = linha.strip()
            continue
        profundidade = len(linha) - len(linha.lstrip("\t"))
        conteudo = linha.strip()
        if ":" in conteudo:
            chave, _, valor = conteudo.partition(":")
            atual["linhas"].append((profundidade, chave.strip(), valor.strip()))
        else:
            atual["linhas"].append((profundidade, "", conteudo))
    return estruturas


def _campos_nivel1(estrutura: dict[str, Any]) -> dict[str, str]:
    return {chave: valor for prof, chave, valor in estrutura["linhas"] if prof == 1 and chave}


def parse_ipmi_tipo38(texto: str) -> dict[str, str] | None:
    """Interpreta ``dmidecode -t 38`` (IPMI Device Information).

    Devolve ``None`` se o SMBIOS não anuncia dispositivo IPMI.
    """
    for estrutura in separar_estruturas(texto):
        if estrutura["tipo"] != 38:
            continue
        campos = _campos_nivel1(estrutura)
        return {
            "interface": campos.get("Interface Type", ""),
            "versao_especificacao": campos.get("Specification Version", ""),
            "endereco_base": campos.get("Base Address", ""),
            "endereco_i2c": campos.get("I2C Slave Address", ""),
        }
    return None


_RE_IPV4 = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def parse_host_interface_tipo42(texto: str) -> list[dict[str, Any]]:
    """Interpreta ``dmidecode -t 42`` (Management Controller Host Interface).

    Devolve uma lista de protocolos anunciados, cada um com
    ``{"interface", "dispositivo", "protocolo", "redfish", "endereco_servico", "porta", "campos"}``.

    TODO(fixture): precisa de saída real de ``dmidecode -t 42`` em servidor com
    Redfish Host Interface habilitada (ex.: Dell iDRAC9 com "OS to iDRAC Pass-through",
    HPE iLO 5/6). O R640 de desenvolvimento não anuncia tipo 42. O parser procura
    as chaves por palavras ("Protocol ID", "Redfish Service", "Address", "Port")
    para tolerar variações de versão do dmidecode.
    """
    protocolos: list[dict[str, Any]] = []
    for estrutura in separar_estruturas(texto):
        if estrutura["tipo"] != 42:
            continue
        campos1 = _campos_nivel1(estrutura)
        atual: dict[str, Any] | None = None
        for _prof, chave, valor in estrutura["linhas"]:
            if chave.lower().startswith("protocol id"):
                atual = {
                    "interface": campos1.get("Host Interface Type", campos1.get("Interface Type", "")),
                    "dispositivo": campos1.get("Device Type", ""),
                    "protocolo": valor,
                    "redfish": "redfish" in valor.lower(),
                    "endereco_servico": None,
                    "porta": None,
                    "campos": {},
                }
                protocolos.append(atual)
                continue
            if atual is None or not chave:
                continue
            atual["campos"][chave] = valor
            chave_min = chave.lower()
            if "redfish service" in chave_min and "address" in chave_min and "format" not in chave_min:
                ip = _RE_IPV4.search(valor)
                if ip and atual["endereco_servico"] is None:
                    atual["endereco_servico"] = ip.group(1)
            elif "redfish service port" in chave_min and valor.isdigit():
                atual["porta"] = int(valor)
    return protocolos


def _inteiro(valor: str | None) -> int | None:
    if valor is None:
        return None
    combinacao = re.match(r"^\s*(\d+)", valor)
    return int(combinacao.group(1)) if combinacao else None


def parse_processadores(texto: str) -> list[dict[str, Any]]:
    """Interpreta ``dmidecode -t 4`` (Processor Information).

    Cada soquete: ``soquete``, ``status`` (texto original), ``populado``,
    ``habilitado``, ``modelo``, ``fabricante``, ``nucleos``,
    ``nucleos_habilitados``, ``threads``. Não inclui ID, serial nem asset tag.
    """
    processadores: list[dict[str, Any]] = []
    for estrutura in separar_estruturas(texto):
        if estrutura["tipo"] != 4:
            continue
        campos = _campos_nivel1(estrutura)
        status = campos.get("Status", "")
        processadores.append(
            {
                "soquete": campos.get("Socket Designation", ""),
                "status": status,
                "populado": status.lower().startswith("populated"),
                "habilitado": status.lower() == "populated, enabled",
                "modelo": valor_util(campos.get("Version")),
                "fabricante": valor_util(campos.get("Manufacturer")),
                "nucleos": _inteiro(campos.get("Core Count")),
                "nucleos_habilitados": _inteiro(campos.get("Core Enabled")),
                "threads": _inteiro(campos.get("Thread Count")),
            }
        )
    return processadores


_UNIDADES_MB = {"kb": 1 / 1024, "mb": 1, "gb": 1024, "tb": 1024 * 1024}


def tamanho_em_mb(valor: str | None) -> int | None:
    """Converte "8 GB" / "16384 MB" / "1 TB" em MB; ``None`` para "No Module Installed" e afins."""
    if not valor:
        return None
    combinacao = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]B)\b", valor, re.IGNORECASE)
    if not combinacao:
        return None
    return int(float(combinacao.group(1)) * _UNIDADES_MB[combinacao.group(2).lower()])


def parse_memorias(texto: str) -> list[dict[str, Any]]:
    """Interpreta ``dmidecode -t 17`` (Memory Device): um item por slot.

    Cada slot: ``slot`` (Locator), ``banco``, ``instalado``, ``tamanho_mb``,
    ``tipo``, ``velocidade``, ``fabricante``, ``part_number``, ``serial``, ``rank``.
    """
    memorias: list[dict[str, Any]] = []
    for estrutura in separar_estruturas(texto):
        if estrutura["tipo"] != 17:
            continue
        campos = _campos_nivel1(estrutura)
        tamanho = tamanho_em_mb(campos.get("Size"))
        memorias.append(
            {
                "slot": campos.get("Locator", ""),
                "banco": valor_util(campos.get("Bank Locator")),
                "instalado": tamanho is not None and tamanho > 0,
                "tamanho_mb": tamanho,
                "tipo": valor_util(campos.get("Type")),
                "velocidade": valor_util(campos.get("Speed")),
                "fabricante": valor_util(campos.get("Manufacturer")),
                "part_number": valor_util(campos.get("Part Number")),
                "serial": valor_util(campos.get("Serial Number")),
                "rank": _inteiro(campos.get("Rank")),
            }
        )
    return memorias


def parse_sku_dell(sku: str | None) -> dict[str, str] | None:
    """SKU da Dell no SMBIOS: ``"SKU=0716;ModelName=PowerEdge R640"`` → ``{"system_id": "0716", "modelo": ...}``.

    ``None`` se não estiver nesse formato (outros fabricantes, ou "SKU=NotProvided").
    """
    if not sku:
        return None
    campos = {}
    for parte in sku.split(";"):
        chave, _, valor = parte.partition("=")
        campos[chave.strip().lower()] = valor.strip()
    system_id = campos.get("sku", "")
    if len(system_id) != 4 or any(c not in "0123456789abcdefABCDEF" for c in system_id):
        return None
    return {"system_id": system_id.upper(), "modelo": campos.get("modelname", "")}


def parse_system_id_dell_oem(texto: str) -> str | None:
    """System ID da Dell na estrutura OEM do SMBIOS (``dmidecode -t 208``), usada até a 13ª geração, que deixa o SKU vazio.

    Byte 0x06 = ID; ``FE`` = ID estendido de 16 bits (little-endian) nos bytes 0x08–0x09. Conferido no R640: ``D0 10 00 D0 03
    0B FE 00 16 07 ...`` → ``0716``, igual ao ``SKU=0716`` dele (o mesmo layout do ``libsmbios`` da Dell). ``None`` se não houver.
    """
    for bloco in texto.split("Handle ")[1:]:
        if "DMI type 208" not in bloco or "Header and Data:" not in bloco:
            continue
        dados = bloco.split("Header and Data:", 1)[1].split("Strings:", 1)[0]
        try:
            octetos = [int(b, 16) for b in dados.split()]
        except ValueError:
            continue
        if len(octetos) < 7 or octetos[0] != 0xD0:
            continue
        if octetos[6] == 0xFE:
            if len(octetos) < 10:
                continue
            identificador = octetos[8] | octetos[9] << 8
        else:
            identificador = octetos[6]
        if identificador not in (0, 0xFFFF):
            return f"{identificador:04X}"
    return None

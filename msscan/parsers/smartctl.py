"""Parsers da saída JSON do ``smartctl`` (smartmontools 7.x, opção ``-j``)."""

from __future__ import annotations

import json
from typing import Any

from msscan.parsers import ErroParser

# Bits do código de saída do smartctl (man smartctl, "EXIT STATUS").
BIT_LINHA_DE_COMANDO = 0
BIT_ABERTURA_FALHOU = 1
BIT_COMANDO_SMART_FALHOU = 2
BIT_DISCO_FALHANDO = 3
BIT_PREFALHA_ABAIXO_LIMITE = 4
BIT_ATRIBUTO_ABAIXO_LIMITE_NO_PASSADO = 5
BIT_LOG_DE_ERROS = 6
BIT_LOG_AUTOTESTE = 7


def _json(texto: str, comando: str) -> dict[str, Any]:
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError as erro:
        raise ErroParser(f"saída do '{comando}' não é JSON válido: {erro}") from erro
    if not isinstance(dados, dict):
        raise ErroParser(f"saída do '{comando}' não é um objeto JSON")
    return dados


def parse_scan_open(texto: str) -> list[dict[str, str]]:
    """Interpreta ``smartctl --scan-open -j``: lista de ``{"nome", "tipo", "protocolo", "erro"}``."""
    dados = _json(texto, "smartctl --scan-open -j")
    discos = []
    for dispositivo in dados.get("devices", []):
        if not isinstance(dispositivo, dict) or not dispositivo.get("name"):
            continue
        discos.append(
            {
                "nome": dispositivo["name"],
                "tipo": dispositivo.get("type", ""),
                "protocolo": dispositivo.get("protocol", ""),
                "erro": dispositivo.get("open_error", ""),
            }
        )
    return discos


def bits_saida(codigo: int) -> set[int]:
    """Bits ligados no código de saída do smartctl."""
    return {bit for bit in range(8) if codigo & (1 << bit)}


def parse_smart(texto: str) -> dict[str, Any]:
    """Interpreta ``smartctl -a -j``: dados normalizados de um disco.

    Campos: ``dispositivo``, ``tipo``, ``protocolo``, ``modelo``, ``serial``,
    ``firmware``, ``capacidade_bytes``, ``codigo_saida``, ``smart_disponivel``,
    ``smart_habilitado``, ``smart_passou`` (``None`` se não informado),
    ``temperatura``, ``horas_ligado``, ``atributos`` (ATA), ``nvme`` (log de
    saúde NVMe) e ``scsi_defeitos_crescidos``.
    """
    dados = _json(texto, "smartctl -a -j")
    dispositivo = dados.get("device", {}) or {}
    suporte = dados.get("smart_support", {}) or {}
    status = dados.get("smart_status")
    atributos = []
    for atributo in (dados.get("ata_smart_attributes", {}) or {}).get("table", []) or []:
        atributos.append(
            {
                "id": atributo.get("id"),
                "nome": atributo.get("name", ""),
                "valor": atributo.get("value"),
                "limite": atributo.get("thresh"),
                "falhou_quando": atributo.get("when_failed", ""),
                "bruto": (atributo.get("raw", {}) or {}).get("value"),
            }
        )
    return {
        "dispositivo": dispositivo.get("name", ""),
        "tipo": dispositivo.get("type", ""),
        "protocolo": dispositivo.get("protocol", ""),
        "modelo": dados.get("model_name") or dados.get("scsi_model_name") or dados.get("product"),
        "serial": dados.get("serial_number"),
        "firmware": dados.get("firmware_version"),
        "capacidade_bytes": (dados.get("user_capacity", {}) or {}).get("bytes"),
        "codigo_saida": (dados.get("smartctl", {}) or {}).get("exit_status"),
        "smart_disponivel": suporte.get("available"),
        "smart_habilitado": suporte.get("enabled"),
        "smart_passou": status.get("passed") if isinstance(status, dict) else None,
        "temperatura": (dados.get("temperature", {}) or {}).get("current"),
        "horas_ligado": (dados.get("power_on_time", {}) or {}).get("hours"),
        "atributos": atributos,
        "nvme": dados.get("nvme_smart_health_information_log"),
        "scsi_defeitos_crescidos": dados.get("scsi_grown_defect_list"),
    }

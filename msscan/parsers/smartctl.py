"""Parsers da saída JSON do ``smartctl`` (smartmontools 7.x, opção ``-j``)."""

from __future__ import annotations

import json
import re
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
                "pior": atributo.get("worst"),
                "bruto_texto": (atributo.get("raw", {}) or {}).get("string"),
                "prefalha": ((atributo.get("flags", {}) or {}).get("prefailure")),
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
        **_detalhes_disco(dados, atributos),
    }


# Atributos ATA cujo valor NORMALIZADO é a vida restante em % (nomes da base do smartctl; nome desconhecido não entra).
ATRIBUTOS_VIDA_RESTANTE = {"Wear_Leveling_Count", "Media_Wearout_Indicator", "SSD_Life_Left", "Percent_Lifetime_Remain",
                           "Remaining_Lifetime_Perc"}  # fmt: skip
_RE_MIN_MAX = re.compile(r"Min/Max\s+(-?\d+)/(-?\d+)")
RESULTADO_SCSI_EM_ANDAMENTO = 15
"""Código de resultado 0xF no log de autotestes SCSI: teste em andamento (SPC-4, "self-test results log")."""


def _int(valor: Any) -> int | None:
    return valor if isinstance(valor, int) and not isinstance(valor, bool) else None


def _temperatura(dados: dict[str, Any], atributos: list[dict[str, Any]]) -> dict[str, Any]:
    temp = dados.get("temperature", {}) or {}
    minima, maxima = _int(temp.get("lifetime_min")), _int(temp.get("lifetime_max"))
    if minima is None and maxima is None:
        # ATA 194 traz no texto bruto o mínimo e o máximo registrados pelo disco: "24 (Min/Max 15/38)".
        bruto = next((a["bruto_texto"] for a in atributos if a["id"] == 194 and a.get("bruto_texto")), "")
        achado = _RE_MIN_MAX.search(bruto or "")
        if achado:
            minima, maxima = int(achado.group(1)), int(achado.group(2))
    limite = next((_int(temp.get(c)) for c in ("drive_trip", "op_limit_max", "limit_max") if _int(temp.get(c))), None)
    return {"atual": _int(temp.get("current")), "minima": minima, "maxima": maxima, "limite": limite}


def _desgaste(dados: dict[str, Any], atributos: list[dict[str, Any]]) -> int | None:
    """% da vida útil estimada já consumida (SSD/NVMe), como o próprio disco informa."""
    nvme = dados.get("nvme_smart_health_information_log") or {}
    for valor in (nvme.get("percentage_used"), dados.get("scsi_percentage_used_endurance_indicator")):
        if _int(valor) is not None:
            return valor
    for atributo in atributos:
        if atributo["nome"] in ATRIBUTOS_VIDA_RESTANTE and _int(atributo["valor"]) is not None and 0 <= atributo["valor"] <= 100:
            return 100 - atributo["valor"]
    return None


def _autotestes(dados: dict[str, Any]) -> list[dict[str, Any]]:
    """Log de autotestes, mais recente primeiro: ``{"tipo", "resultado", "passou", "em_andamento", "horas", "lba"}``.

    TODO(fixture): só o SCSI tem saída real (R620); ATA com entradas e NVMe seguem o formato do smartctl 7.x.
    """
    saida: list[dict[str, Any]] = []
    ata = ((dados.get("ata_smart_self_test_log") or {}).get("standard") or {}).get("table") or []
    for item in ata:
        status = item.get("status") or {}
        saida.append({"tipo": (item.get("type") or {}).get("string"), "resultado": status.get("string"), "passou": status.get("passed"),
                      "em_andamento": (_int(status.get("value")) or 0) >> 4 == 0xF, "horas": _int(item.get("lifetime_hours")),
                      "lba": _int(item.get("lba"))})  # fmt: skip
    for indice in range(20):
        item = dados.get(f"scsi_self_test_{indice}")
        if not isinstance(item, dict):
            break
        valor = _int((item.get("result") or {}).get("value"))
        saida.append({"tipo": (item.get("code") or {}).get("string"), "resultado": (item.get("result") or {}).get("string"),
                      "passou": True if valor == 0 else False if valor is not None and 3 <= valor <= 7 else None,
                      "em_andamento": valor == RESULTADO_SCSI_EM_ANDAMENTO, "horas": _int((item.get("power_on_time") or {}).get("hours")),
                      "lba": _int((item.get("lba_first_failure") or {}).get("value"))})  # fmt: skip
    for item in (dados.get("nvme_self_test_log") or {}).get("table") or []:
        valor = _int((item.get("self_test_result") or {}).get("value"))
        saida.append({"tipo": (item.get("self_test_code") or {}).get("string"), "resultado": (item.get("self_test_result") or {}).get("string"),
                      "passou": True if valor == 0 else False if valor is not None and valor >= 3 else None, "em_andamento": False,
                      "horas": _int(item.get("power_on_hours")), "lba": _int(item.get("lba"))})  # fmt: skip
    return saida


def _autoteste_atual(dados: dict[str, Any], log: list[dict[str, Any]]) -> dict[str, Any]:
    """Autoteste em andamento (``em_andamento``, ``restante_pct``), suporte e duração recomendada (minutos)."""
    ata = dados.get("ata_smart_data") or {}
    teste = ata.get("self_test") or {}
    status = teste.get("status") or {}
    minutos = teste.get("polling_minutes") or {}
    nvme = dados.get("nvme_self_test_log") or {}
    em_andamento, restante = False, None
    if (_int(status.get("value")) or 0) >> 4 == 0xF:
        em_andamento, restante = True, _int(status.get("remaining_percent"))
    elif _int((nvme.get("current_self_test_operation") or {}).get("value")):
        concluido = _int(nvme.get("current_self_test_completion_percent"))
        em_andamento, restante = True, 100 - concluido if concluido is not None else None
    elif log and log[0]["em_andamento"]:
        em_andamento = True  # SCSI: sem percentual no log. TODO(fixture): saída real com teste rodando.
    estendido_s = _int(dados.get("scsi_extended_self_test_seconds"))
    capacidades = ata.get("capabilities") or {}
    if ata:
        suportado = bool(capacidades.get("self_tests_supported", True))
    else:
        suportado = bool(dados.get("scsi_self_test_0") is not None or estendido_s or nvme)
    return {"em_andamento": em_andamento, "restante_pct": restante, "suportado": suportado,
            "minutos_curto": _int(minutos.get("short")) or (2 if suportado else None),
            "minutos_estendido": _int(minutos.get("extended")) or (-(-estendido_s // 60) if estendido_s else None)}  # fmt: skip


def _detalhes_disco(dados: dict[str, Any], atributos: list[dict[str, Any]]) -> dict[str, Any]:
    """Campos da aba Discos (CLAUDE.md 31) além dos usados no diagnóstico."""
    ciclos_scsi = dados.get("scsi_start_stop_cycle_counter") or {}
    log = _autotestes(dados)
    erros_scsi = [_int((dados.get("scsi_error_counter_log") or {}).get(op, {}).get("total_uncorrected_errors")) for op in ("read", "write", "verify")]
    ano, semana = ciclos_scsi.get("year_of_manufacture"), ciclos_scsi.get("week_of_manufacture")
    return {
        "temperatura_detalhe": _temperatura(dados, atributos),
        "ciclos_energia": _int(dados.get("power_cycle_count")) or _int(ciclos_scsi.get("accumulated_start_stop_cycles")),
        "ciclos_especificados": _int(ciclos_scsi.get("specified_cycle_count_over_device_lifetime")),
        "rotacao_rpm": _int(dados.get("rotation_rate")),
        "formato": (dados.get("form_factor") or {}).get("name"),
        "interface": ((dados.get("interface_speed") or {}).get("current") or {}).get("string") or (dados.get("scsi_transport_protocol") or {}).get("name"),
        "fabricacao": f"{semana}/{ano}" if ano and semana else None,
        "desgaste_pct": _desgaste(dados, atributos),
        "scsi_erros_nao_corrigidos": sum(e for e in erros_scsi if e) if any(e is not None for e in erros_scsi) else None,
        "autotestes": log,
        "autoteste": _autoteste_atual(dados, log),
    }

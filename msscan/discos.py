"""Aba Discos, como o Hard Disk Sentinel (CLAUDE.md 31): saúde %, vida restante, temperatura e autotestes.

Funções puras sobre o que ``parse_smart`` já leu (o mesmo ``smartctl -a -j`` do diagnóstico). A saúde % é estimativa do
MSscan, não a do Hard Disk Sentinel (fórmula não pública): penalidades fixas, sempre mostradas junto do número. O status
do diagnóstico (OK/ALERTA/FALHA) continua sendo a avaliação oficial; a saúde só gradua.
"""

from __future__ import annotations

from typing import Any

from msscan import textos
from msscan.core.executor import RE_DISPOSITIVO_SMART, RE_TIPO_SMART
from msscan.core.modelo import Status
from msscan.parsers.smartctl import BIT_LOG_DE_ERROS, bits_saida

PENALIDADES: dict[str, tuple[int, int]] = {
    "realocados": (1, 60),
    "pendentes": (3, 60),
    "incorrigiveis": (3, 60),
    "erros_midia": (2, 60),
    "nao_corrigidos": (5, 60),
}
"""Contador → (pontos por unidade, máximo de pontos). Um setor realocado tira 1 ponto; setor pendente ou incorrigível, 3."""
TETO_SMART_REPROVADO = 5
TETO_ATRIBUTO_FALHANDO = 10
TETO_AUTOTESTE_REPROVADO = 20
PENALIDADE_ATRIBUTO_PASSADO = 10
PENALIDADE_LOG_DE_ERROS = 10
LIMITE_TEMPERATURA_HDD = 60
LIMITE_TEMPERATURA_SSD = 70
# ponytail: limites genéricos quando o disco não informa o dele (drive_trip/op_limit_max); calibrar por modelo se precisar.
FRACAO_ALERTA_TEMPERATURA = 0.85
TIPOS_AUTOTESTE = {"curto": "short", "estendido": "long"}


def identificador(disco: dict[str, Any]) -> str:
    """Id estável do disco na interface: tipo + dispositivo (atrás da PERC o ``/dev/bus/0`` é o mesmo para todos)."""
    return f"{disco.get('tipo') or ''}|{disco.get('dispositivo') or ''}"


def _bruto(disco: dict[str, Any], ids: tuple[int, ...]) -> int:
    return sum(a["bruto"] for a in disco.get("atributos") or [] if a.get("id") in ids and isinstance(a.get("bruto"), int) and a["bruto"] > 0)


def saude(disco: dict[str, Any]) -> dict[str, Any]:
    """``{"percentual", "motivos"}``. ``percentual`` é ``None`` sem leitura SMART utilizável (nunca 100% por falta de dado)."""
    if disco.get("status") in (None, Status.NAO_VERIFICADO.value):
        return {"percentual": None, "motivos": [textos.t("discos.sem_leitura_smart")]}
    nvme = disco.get("nvme") or {}
    contadores = {
        "realocados": _bruto(disco, (5,)) + max(disco.get("scsi_defeitos_crescidos") or 0, 0),
        "pendentes": _bruto(disco, (197,)),
        "incorrigiveis": _bruto(disco, (187, 198)),
        "erros_midia": max(nvme.get("media_errors") or 0, 0),
        "nao_corrigidos": disco.get("scsi_erros_nao_corrigidos") or 0,
    }
    tetos = [100]
    motivos: list[str] = []
    if disco.get("smart_passou") is False:
        tetos.append(TETO_SMART_REPROVADO)
        motivos.append(textos.t("discos.motivo_smart_reprovado", TETO_SMART_REPROVADO))
    atributos = disco.get("atributos") or []
    if any(a.get("falhou_quando") == "now" for a in atributos):
        tetos.append(TETO_ATRIBUTO_FALHANDO)
        motivos.append(textos.t("discos.motivo_atributo_falhando", TETO_ATRIBUTO_FALHANDO))
    ultimo = (disco.get("autotestes") or [{}])[0]
    if ultimo.get("passou") is False:
        tetos.append(TETO_AUTOTESTE_REPROVADO)
        motivos.append(textos.t("discos.motivo_autoteste_reprovado", TETO_AUTOTESTE_REPROVADO))
    desgaste = disco.get("desgaste_pct")
    if isinstance(desgaste, int) and desgaste > 0:
        tetos.append(max(0, 100 - desgaste))
        motivos.append(textos.t("discos.motivo_desgaste", desgaste, max(0, 100 - desgaste)))
    perdidos = 0
    if any(a.get("falhou_quando") == "past" for a in atributos):
        perdidos += PENALIDADE_ATRIBUTO_PASSADO
        motivos.append(textos.t("discos.motivo_atributo_passado", PENALIDADE_ATRIBUTO_PASSADO))
    if BIT_LOG_DE_ERROS in bits_saida(disco.get("codigo_saida") or 0):
        perdidos += PENALIDADE_LOG_DE_ERROS
        motivos.append(textos.t("discos.motivo_log_de_erros", PENALIDADE_LOG_DE_ERROS))
    for nome, quantidade in contadores.items():
        if quantidade > 0:
            por_unidade, maximo = PENALIDADES[nome]
            pontos = min(quantidade * por_unidade, maximo)
            perdidos += pontos
            motivos.append(textos.t(f"discos.motivo_{nome}", quantidade, pontos))
    return {"percentual": max(0, min(tetos) - perdidos), "motivos": motivos}


def vida_restante_dias(disco: dict[str, Any]) -> int | None:
    """Dias até 100% de desgaste no ritmo de uso até agora (só SSD/NVMe que informam o desgaste). ``None`` sem base."""
    desgaste, horas = disco.get("desgaste_pct"), disco.get("horas_ligado")
    if not isinstance(desgaste, int) or not isinstance(horas, int) or not 0 < desgaste < 100 or horas <= 0:
        return None
    return round(horas * (100 - desgaste) / desgaste / 24)


def limite_temperatura(disco: dict[str, Any]) -> int:
    """Limite informado pelo disco; senão, o genérico de HDD ou SSD."""
    limite = (disco.get("temperatura_detalhe") or {}).get("limite")
    if limite:
        return limite
    return LIMITE_TEMPERATURA_SSD if disco.get("rotacao_rpm") == 0 or disco.get("nvme") else LIMITE_TEMPERATURA_HDD


def nivel_temperatura(valor: Any, limite: int) -> str:
    """``ok``, ``alerta`` (a partir de 85% do limite) ou ``falha`` (no limite); ``""`` sem leitura."""
    if not isinstance(valor, (int, float)):
        return ""
    if valor >= limite:
        return "falha"
    return "alerta" if valor >= limite * FRACAO_ALERTA_TEMPERATURA else "ok"


def comando_autoteste(disco: dict[str, Any], tipo: str | None) -> list[str] | None:
    """``smartctl -t short|long`` (ou ``-X`` com ``tipo=None``, interromper) para um disco já diagnosticado; ``None`` se o tipo
    ou o dispositivo não forem dos aceitos (a validação final é a do executor)."""
    dispositivo, tipo_d = str(disco.get("dispositivo") or ""), str(disco.get("tipo") or "")
    if not RE_DISPOSITIVO_SMART.fullmatch(dispositivo) or (tipo_d and not RE_TIPO_SMART.fullmatch(tipo_d)):
        return None
    if tipo is not None and tipo not in TIPOS_AUTOTESTE:
        return None
    acao = ["-X"] if tipo is None else ["-t", TIPOS_AUTOTESTE[tipo]]
    return ["smartctl", *acao, *(["-d", tipo_d] if tipo_d else []), dispositivo]


def painel(disco: dict[str, Any], historico: list[list[Any]] | None = None) -> dict[str, Any]:
    """Cartão de um disco para a aba Discos."""
    temperatura = dict(disco.get("temperatura_detalhe") or {"atual": disco.get("temperatura")})
    limite = limite_temperatura(disco)
    return {
        "id": identificador(disco),
        "dispositivo": disco.get("dispositivo"),
        "tipo": disco.get("tipo"),
        "modelo": disco.get("modelo"),
        "serial": disco.get("serial"),
        "firmware": disco.get("firmware"),
        "capacidade_bytes": disco.get("capacidade_bytes"),
        "status": disco.get("status"),
        "problemas": disco.get("problemas") or [],
        "observacoes": disco.get("observacoes") or [],
        "saude": saude(disco),
        "temperatura": {**temperatura, "limite": limite, "nivel": nivel_temperatura(temperatura.get("atual"), limite),
                        "historico": historico or []},  # fmt: skip
        "horas_ligado": disco.get("horas_ligado"),
        "ciclos_energia": disco.get("ciclos_energia"),
        "ciclos_especificados": disco.get("ciclos_especificados"),
        "rotacao_rpm": disco.get("rotacao_rpm"),
        "formato": disco.get("formato"),
        "interface": disco.get("interface"),
        "fabricacao": disco.get("fabricacao"),
        "desgaste_pct": disco.get("desgaste_pct"),
        "scsi_defeitos_crescidos": disco.get("scsi_defeitos_crescidos"),
        "scsi_erros_nao_corrigidos": disco.get("scsi_erros_nao_corrigidos"),
        "vida_restante_dias": vida_restante_dias(disco),
        "atributos": disco.get("atributos") or [],
        "autotestes": disco.get("autotestes") or [],
        "autoteste": disco.get("autoteste") or {"suportado": False},
    }


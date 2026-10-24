"""Visão do servidor e sensores ao vivo da interface (nova interface da 1.0.0, pedido de 09/10/2026).

Funções puras: a visão é montada com o que a detecção e o diagnóstico JÁ leram (nenhum comando novo); os sensores ao
vivo vêm de um ``ipmitool sensor`` (só leitura), que a sessão relê no máximo a cada ``INTERVALO_SENSORES_S`` e nunca
durante a gravação de firmware ou uma ação no controlador.
"""

from __future__ import annotations

from typing import Any

from msscan import textos

INTERVALO_SENSORES_S = 5.0
"""Releitura dos sensores ao vivo (só enquanto a interface pede; ``ipmitool sensor`` leva ~2 s num iDRAC9)."""


def _dados(resultados: dict[str, Any], nome: str) -> dict[str, Any]:
    resultado = resultados.get(nome)
    return dict(getattr(resultado, "dados", None) or {}) if resultado is not None else {}


def _status(resultados: dict[str, Any], nome: str) -> str | None:
    resultado = resultados.get(nome)
    status = getattr(resultado, "status", None)
    return getattr(status, "value", status) if status is not None else None


def versao_idrac(ctx: Any) -> dict[str, Any] | None:
    """Versão do controlador já lida (racadm preparado ou ``ipmitool mc info``); nunca dispara leitura nova."""
    cache = getattr(ctx, "cache", {}) or {}
    completa = ((cache.get("idrac_tools") or {}).get("versoes") or cache.get("racadm_versoes") or {}).get("iDRAC Version")
    if completa:
        return {"versao": completa, "fonte": "racadm getversion"}
    mc = getattr(getattr(ctx, "ipmi", None), "mc_info", None) or {}
    if mc.get("firmware"):
        return {"versao": mc["firmware"], "fonte": "ipmitool mc info", "abreviada": True}
    return None


def montar(ctx: Any, resultados: dict[str, Any], configuracao: dict[str, Any] | None, tempo_ligado_s: float | None) -> dict[str, Any]:
    """Identidade, versões, processadores, memória por slot, discos, fontes e tempo ligado (só o que já foi lido)."""
    entrada = ((getattr(ctx, "compat", None) or {}).get("entrada") or {}) if ctx is not None else {}
    cpu = _dados(resultados, "cpu")
    memoria = _dados(resultados, "memoria")
    discos = _dados(resultados, "discos")
    ocupados = {m.get("slot"): m for m in memoria.get("modulos") or [] if m.get("instalado")}
    config = configuracao or {}
    return {
        "fabricante": getattr(ctx, "fabricante", None),
        "modelo": getattr(ctx, "modelo", None),
        "geracao": entrada.get("geracao"),
        "bmc": entrada.get("bmc"),
        "bios": getattr(ctx, "versao_bios", None),
        "idrac": versao_idrac(ctx) if ctx is not None else None,
        "processadores": [{"soquete": p.get("soquete"), "modelo": p.get("modelo"), "nucleos": p.get("nucleos"), "threads": p.get("threads")}
                          for p in cpu.get("processadores") or [] if p.get("populado")],  # fmt: skip
        "memoria": {
            "total_gb": config.get("memoria_gb"),
            "slots": [{"slot": s, "ocupado": s in ocupados, "gb": round((ocupados[s].get("tamanho_mb") or 0) / 1024) if s in ocupados else None,
                       "tipo": (ocupados.get(s) or {}).get("tipo"), "velocidade": (ocupados.get(s) or {}).get("velocidade")}
                      for s in (memoria.get("soquetes") or list(ocupados))],  # fmt: skip
        },
        "discos": [{"dispositivo": d.get("dispositivo"), "modelo": d.get("modelo"), "capacidade_bytes": d.get("capacidade_bytes"),
                    "temperatura": d.get("temperatura"), "smart_passou": d.get("smart_passou")} for d in discos.get("discos") or []],  # fmt: skip
        "fontes": config.get("fontes"),
        "status": {nome: _status(resultados, nome) for nome in ("sensores", "fontes", "ventoinhas", "memoria", "discos")},
        "tempo_ligado_s": int(tempo_ligado_s) if tempo_ligado_s is not None else None,
    }


def sensores(lista: list[dict[str, Any]]) -> dict[str, Any]:
    """Barras dos sensores ao vivo a partir do ``parse_sensor``: temperaturas, ventoinhas e consumo, cada leitura com o
    limite crítico do próprio controlador (quando informado) para a escala e a situação."""
    temperaturas, ventoinhas, consumo = [], [], []
    repetidos: dict[str, int] = {}
    for s in lista:
        if s.get("discreto") or s.get("valor") is None:
            continue
        unidade = str(s.get("unidade") or "").lower()
        limiares = s.get("limiares") or {}
        nome = str(s.get("nome") or "")
        repetidos[nome] = repetidos.get(nome, 0) + 1
        item = {"nome": nome, "valor": s["valor"], "status": s.get("status"),
                "limite": limiares.get("ucr") or limiares.get("unc"), "minimo": limiares.get("lcr") or limiares.get("lnc")}  # fmt: skip
        if "degrees" in unidade:
            temperaturas.append(item)
        elif unidade == "rpm":
            ventoinhas.append(item)
        elif unidade == "watts":
            consumo.append(item)
    # Nomes repetidos (dois "Temp" no R640): numerados na ordem do controlador, sem inventar a qual peça pertencem.
    contagem: dict[str, int] = {}
    for item in temperaturas + ventoinhas + consumo:
        if repetidos.get(item["nome"], 0) > 1:
            contagem[item["nome"]] = contagem.get(item["nome"], 0) + 1
            item["nome"] = f"{item['nome']} {contagem[item['nome']]}"
    maxima = max((t["valor"] for t in temperaturas), default=None)
    return {"temperaturas": temperaturas, "ventoinhas": ventoinhas, "consumo": consumo, "temperatura_maxima": maxima}


def tempo_ligado(texto: str | None) -> float | None:
    """Segundos desde a inicialização, do ``/proc/uptime``."""
    try:
        return float((texto or "").split()[0])
    except (IndexError, ValueError):
        return None


def rotulo_tempo(segundos: int | None) -> str:
    """Ex.: "3 d 4 h", "2 h 15 min", "12 min" (``—`` sem leitura)."""
    if segundos is None:
        return "—"
    dias, resto = divmod(int(segundos), 86400)
    horas, minutos = resto // 3600, (resto % 3600) // 60
    if dias:
        return textos.t("visao.tempo_dias", dias, horas)
    if horas:
        return textos.t("visao.tempo_horas", horas, minutos)
    return textos.t("visao.tempo_minutos", minutos)

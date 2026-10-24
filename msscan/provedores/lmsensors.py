"""Provedores baseados nos sensores do kernel (lm-sensors, ``sensors -j``).

Usam os chips já lidos na detecção (``ctx.sensores_kernel``): o comando não é
executado de novo.
"""

from __future__ import annotations

import re
from typing import Any

from msscan.core.fallback import Disponibilidade, Provedor
from msscan.core.modelo import Resultado, Status, pior_status
from msscan.deteccao.contexto import Contexto

PRIORIDADE_LMSENSORS = 40
_RE_ENTRADA = re.compile(r"^(temp|fan)(\d+)_input$")


def _leituras(chips: list[dict[str, Any]], tipo: str) -> list[dict[str, Any]]:
    """Extrai leituras ``temp`` ou ``fan`` com seus limites e alarmes."""
    leituras = []
    for chip in chips:
        for rotulo, sub in chip["leituras"].items():
            for chave, valor in sub.items():
                combinacao = _RE_ENTRADA.match(chave)
                if not combinacao or combinacao.group(1) != tipo:
                    continue
                prefixo = f"{tipo}{combinacao.group(2)}_"
                extras = {k[len(prefixo) :]: v for k, v in sub.items() if k.startswith(prefixo) and k != chave}
                leituras.append({"chip": chip["chip"], "rotulo": rotulo, "valor": valor, "limites": extras})
    return leituras


def avaliar_temperaturas(chips: list[dict[str, Any]]) -> Resultado | None:
    """Avalia temperaturas do kernel contra os limites ``max``/``crit`` e alarmes do próprio chip."""
    leituras = _leituras(chips, "temp")
    if not leituras:
        return None
    problemas: list[str] = []
    situacoes: list[Status] = []
    for leitura in leituras:
        limites, valor = leitura["limites"], leitura["valor"]
        nome = f"{leitura['chip']} / {leitura['rotulo']}"
        situacao = Status.OK
        if limites.get("crit_alarm") == 1 or ("crit" in limites and valor >= limites["crit"]):
            situacao = Status.FALHA
        elif any(limites.get(a) == 1 for a in ("max_alarm", "alarm")) or ("max" in limites and valor >= limites["max"]):
            situacao = Status.ALERTA
        if situacao != Status.OK:
            problemas.append(f"{nome}: {valor:g} °C (limites: {limites})")
        leitura["avaliacao"] = situacao.value
        situacoes.append(situacao)
    maior = max(leituras, key=lambda x: x["valor"])
    return Resultado(
        componente="sensores",
        status=pior_status(situacoes),
        fonte="lm-sensors (sensors -j)",
        dados={"leituras": leituras, "problemas": problemas},
        mensagem=(
            f"{len(leituras)} temperatura(s) lida(s) pelo kernel; máxima {maior['valor']:g} °C "
            f"({maior['chip']} / {maior['rotulo']}); tensões e fontes não são cobertas por esta fonte"
        ),
    )


def avaliar_ventoinhas(chips: list[dict[str, Any]]) -> Resultado | None:
    """Avalia ventoinhas do kernel.

    0 RPM sem limite mínimo é tratado como conector vazio e ignorado.

    TODO(fixture): precisa de saída real de ``sensors -j`` com ventoinhas
    (placas-mãe com chip Super I/O, ex.: servidores de torre / Supermicro).
    """
    leituras = _leituras(chips, "fan")
    avaliadas, ignoradas = [], []
    problemas: list[str] = []
    situacoes: list[Status] = []
    for leitura in leituras:
        limites, valor = leitura["limites"], leitura["valor"]
        nome = f"{leitura['chip']} / {leitura['rotulo']}"
        minimo = limites.get("min")
        if valor == 0 and not minimo and limites.get("fault") != 1:
            ignoradas.append(nome)
            continue
        situacao = Status.OK
        if limites.get("fault") == 1 or (minimo and valor < minimo):
            situacao = Status.FALHA
        elif limites.get("alarm") == 1:
            situacao = Status.ALERTA
        if situacao != Status.OK:
            problemas.append(f"{nome}: {valor:g} RPM (limites: {limites})")
        leitura["avaliacao"] = situacao.value
        avaliadas.append(leitura)
        situacoes.append(situacao)
    if not avaliadas:
        return None
    return Resultado(
        componente="ventoinhas",
        status=pior_status(situacoes),
        fonte="lm-sensors (sensors -j)",
        dados={"leituras": avaliadas, "ignoradas_sem_rotacao": ignoradas, "problemas": problemas},
        mensagem=f"{len(avaliadas)} ventoinha(s) lida(s) pelo kernel",
    )


class _ProvedorLmSensors(Provedor):
    prioridade = PRIORIDADE_LMSENSORS

    def disponivel(self, ctx: Contexto) -> Disponibilidade:
        if ctx.sensores_kernel:
            return Disponibilidade(True)
        erro = ctx.erros_deteccao.get("sensores_kernel")
        return Disponibilidade(False, f"lm-sensors sem dados ({erro})" if erro else "nenhum chip de sensores no kernel")


class ProvedorLmSensorsTemperatura(_ProvedorLmSensors):
    """Temperaturas via lm-sensors."""

    nome = "lm-sensors (temperaturas)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        return avaliar_temperaturas(ctx.sensores_kernel)


class ProvedorLmSensorsVentoinhas(_ProvedorLmSensors):
    """Ventoinhas via lm-sensors."""

    nome = "lm-sensors (ventoinhas)"

    def coletar(self, ctx: Contexto) -> Resultado | None:
        return avaliar_ventoinhas(ctx.sensores_kernel)

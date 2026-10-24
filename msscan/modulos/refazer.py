"""Refazer diagnóstico (CLAUDE.md 7b): tudo ou um componente, somente leitura.

O diagnóstico guarda leituras no ``Contexto`` (SEL, respostas do Redfish, ``sensors -j`` da detecção).
Antes de refazer, essas leituras são descartadas para que o resultado reflita o estado ATUAL do servidor
(ex.: pente de memória reencaixado). Os dados da detecção (modelo, BMC, controladoras) não mudam.
"""

from __future__ import annotations

from collections.abc import Collection

from msscan.deteccao.contexto import Contexto
from msscan.deteccao.detectores import detectar_sensores

USAM_SENSORES_KERNEL = frozenset({"sensores", "ventoinhas"})
"""Componentes cujo fallback (lm-sensors) lê os chips obtidos na detecção."""


def preparar_releitura(ctx: Contexto, somente: Collection[str] | None) -> None:
    """Descarta as leituras guardadas para que o diagnóstico refeito leia tudo de novo. Nunca levanta exceção."""
    ctx.cache.pop("sel", None)
    ctx.cache.pop("redfish", None)
    if somente is not None and not USAM_SENSORES_KERNEL.intersection(somente):
        return
    # Leitura antiga nunca passa por atual: se a nova falhar, o lm-sensors fica sem dados (e o fallback registra).
    ctx.sensores_kernel = []
    ctx.erros_deteccao.pop("sensores_kernel", None)
    try:
        detectar_sensores(ctx)
    except Exception as erro:  # noqa: BLE001 - refazer nunca quebra por causa do lm-sensors
        ctx.erros_deteccao["sensores_kernel"] = f"{type(erro).__name__}: {erro}"

"""Registro de componentes diagnosticáveis e de seus provedores.

Adicionar um método novo é só ``registrar_provedor("sensores", MeuProvedor())``;
o núcleo não muda.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, field

from msscan.core.fallback import Provedor, executar
from msscan.core.modelo import Resultado, resultado_nao_verificado
from msscan.deteccao.contexto import Contexto
from msscan.provedores.ipmi import (
    ProvedorIPMIFontes,
    ProvedorIPMIInternos,
    ProvedorIPMILogBMC,
    ProvedorIPMISensores,
    ProvedorIPMIVentoinhas,
)
from msscan.provedores.lmsensors import ProvedorLmSensorsTemperatura, ProvedorLmSensorsVentoinhas
from msscan.provedores.nic import ProvedorPlacasRede
from msscan.provedores.raid import provedores_raid
from msscan.provedores.redfish import provedores_redfish
from msscan.provedores.sistema import ProvedorCPU, ProvedorMemoria
from msscan.provedores.smart import ProvedorSmartctl


Progresso = Callable[[str, "Resultado | None"], None]
"""Callback de andamento: ``(nome do componente, resultado ou None se está começando)``."""


@dataclass
class Componente:
    """Componente do servidor e a cadeia de provedores que o diagnosticam."""

    nome: str
    titulo: str
    provedores: list[Provedor] = field(default_factory=list)


class Registro:
    """Coleção ordenada de componentes."""

    def __init__(self) -> None:
        self._componentes: dict[str, Componente] = {}

    def registrar_componente(self, nome: str, titulo: str) -> Componente:
        """Registra (ou devolve, se já existir) um componente."""
        if nome not in self._componentes:
            self._componentes[nome] = Componente(nome, titulo)
        return self._componentes[nome]

    def registrar_provedor(self, componente: str, provedor: Provedor) -> None:
        """Acrescenta um provedor à cadeia de um componente já registrado."""
        if componente not in self._componentes:
            raise KeyError(f"componente não registrado: {componente}")
        self._componentes[componente].provedores.append(provedor)

    def componentes(self) -> list[Componente]:
        """Componentes na ordem de registro."""
        return list(self._componentes.values())

    def titulo(self, nome: str) -> str:
        """Título legível de um componente (o próprio nome se não registrado)."""
        comp = self._componentes.get(nome)
        return comp.titulo if comp else nome

    def diagnosticar(
        self, ctx: Contexto, progresso: Progresso | None = None, somente: Collection[str] | None = None
    ) -> list[Resultado]:
        """Diagnostica todos os componentes; nunca levanta exceção.

        ``progresso(nome, resultado)`` é chamado antes de cada componente (com
        ``resultado=None``) e depois dele (com o resultado): a interface gráfica
        usa isso para mostrar o andamento ao vivo. Erro no callback é ignorado.
        ``somente``: refaz apenas estes componentes (ex.: os do Redfish, depois das credenciais).
        """
        resultados: list[Resultado] = []
        for comp in self._componentes.values():
            if somente is not None and comp.nome not in somente:
                continue
            _avisar(progresso, comp.nome, None)
            try:
                resultado = executar(comp.nome, comp.provedores, ctx)
            except Exception as erro:  # noqa: BLE001 - diagnóstico nunca para no meio
                resultado = resultado_nao_verificado(
                    comp.nome,
                    [{"provedor": "-", "resultado": "erro", "detalhe": f"{type(erro).__name__}: {erro}"}],
                )
            resultados.append(resultado)
            _avisar(progresso, comp.nome, resultado)
        return resultados



def _avisar(progresso: Progresso | None, nome: str, resultado: Resultado | None) -> None:
    if progresso is None:
        return
    try:
        progresso(nome, resultado)
    except Exception:  # noqa: BLE001 - quem acompanha o progresso não pode parar o diagnóstico
        pass


def registro_padrao() -> Registro:
    """Registro com todos os componentes do MSscan.

    Cadeias (Redfish só com credenciais validadas pelo técnico):
    sensores: Redfish → IPMI → lm-sensors · fontes: Redfish → IPMI → Redfish sem classificação ·
    ventoinhas: Redfish → IPMI → lm-sensors · cpu e memória: sistema (SMBIOS, /proc, EDAC, SEL) ·
    log_bmc: Redfish → IPMI · internos (backplane, cabos, slots): SEL · discos: smartctl ·
    raid: Redfish → perccli/storcli → MegaCli → ssacli → arcconf → sensores de baia/bateria do BMC → mdadm ·
    rede: Redfish (com ip + ethtool como complemento) → ip + ethtool.
    """
    registro = Registro()
    for nome, titulo in (
        ("sensores", "Sensores / temperatura"),
        ("fontes", "Fontes de alimentação"),
        ("ventoinhas", "Ventoinhas"),
        ("cpu", "Processadores"),
        ("memoria", "Memória"),
        ("log_bmc", "Log do BMC"),
        ("internos", "Componentes internos (backplane, cabos, slots)"),
        ("discos", "Discos"),
        ("raid", "RAID"),
        ("rede", "Placas de rede"),
    ):
        registro.registrar_componente(nome, titulo)
    for componente, provedor in (
        ("sensores", ProvedorIPMISensores()),
        ("sensores", ProvedorLmSensorsTemperatura()),
        ("fontes", ProvedorIPMIFontes()),
        ("ventoinhas", ProvedorIPMIVentoinhas()),
        ("ventoinhas", ProvedorLmSensorsVentoinhas()),
        ("cpu", ProvedorCPU()),
        ("memoria", ProvedorMemoria()),
        ("log_bmc", ProvedorIPMILogBMC()),
        ("internos", ProvedorIPMIInternos()),
        ("discos", ProvedorSmartctl()),
        *(("raid", provedor) for provedor in provedores_raid()),
        ("rede", ProvedorPlacasRede()),
        *provedores_redfish(),
    ):
        registro.registrar_provedor(componente, provedor)
    return registro

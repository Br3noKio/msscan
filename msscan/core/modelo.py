"""Modelo de dados comum a todo o MSscan."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

MENSAGEM_NAO_VERIFICADO = "Não foi possível verificar este componente neste equipamento"


class Status(str, Enum):
    """Situação de um componente após o diagnóstico."""

    OK = "OK"
    ALERTA = "ALERTA"
    FALHA = "FALHA"
    NAO_VERIFICADO = "NAO_VERIFICADO"
    NAO_APLICAVEL = "NAO_APLICAVEL"


_GRAVIDADE = {Status.OK: 0, Status.ALERTA: 1, Status.FALHA: 2}


def pior_status(situacoes: list[Status]) -> Status:
    """Devolve o status mais grave entre OK, ALERTA e FALHA (lista vazia → OK).

    Outros status (NAO_VERIFICADO, NAO_APLICAVEL) são ignorados: quem chama
    decide o que fazer quando nada foi verificado.
    """
    graves = [s for s in situacoes if s in _GRAVIDADE]
    return max(graves, key=_GRAVIDADE.__getitem__, default=Status.OK)


@dataclass
class Resultado:
    """Resultado do diagnóstico de um componente.

    Atributos:
        componente: identificador do componente (ex.: "sensores", "raid").
        status: situação apurada.
        fonte: método que gerou o dado (ex.: "ipmitool sensor"); vazio se nenhum.
        dados: dados medidos e metadados (inclui "tentativas" quando há fallback).
        mensagem: texto curto para o técnico.
    """

    componente: str
    status: Status
    fonte: str = ""
    dados: dict[str, Any] = field(default_factory=dict)
    mensagem: str = ""

    def para_dict(self) -> dict[str, Any]:
        """Converte para dicionário serializável em JSON."""
        return {
            "componente": self.componente,
            "status": self.status.value,
            "fonte": self.fonte,
            "dados": self.dados,
            "mensagem": self.mensagem,
        }


def resultado_nao_verificado(componente: str, tentativas: list[dict[str, str]]) -> Resultado:
    """Cria o resultado padrão para um componente que não pôde ser verificado."""
    return Resultado(
        componente=componente,
        status=Status.NAO_VERIFICADO,
        fonte="",
        dados={"tentativas": tentativas},
        mensagem=MENSAGEM_NAO_VERIFICADO,
    )

"""Fallback entre provedores de dados de um mesmo componente."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from msscan.core.modelo import Resultado, Status, resultado_nao_verificado

if TYPE_CHECKING:
    from msscan.deteccao.contexto import Contexto

INDISPONIVEL = "indisponível"
SEM_DADOS = "sem dados"
ERRO = "erro"
SUCESSO = "sucesso"


class FalhaColeta(Exception):
    """O método estava disponível, mas a coleta falhou (ex.: comando devolveu erro).

    A mensagem vai como está para as tentativas do relatório.
    """


@dataclass(frozen=True)
class Disponibilidade:
    """Resposta de ``Provedor.disponivel``: se pode ser usado e, se não, por quê."""

    ok: bool
    motivo: str = ""


class Provedor:
    """Método de obtenção de dados de um componente (ex.: IPMI para sensores).

    Subclasses definem ``nome``, ``prioridade`` (menor = tentado antes) e
    implementam ``disponivel`` e ``coletar``. ``coletar`` devolve ``None``
    quando o método funcionou mas não trouxe dados úteis.
    """

    nome: str = "provedor"
    prioridade: int = 100

    def disponivel(self, ctx: Contexto) -> Disponibilidade:
        """Verifica, sem efeitos colaterais, se este método pode ser usado."""
        raise NotImplementedError

    def coletar(self, ctx: Contexto) -> Resultado | None:
        """Coleta e interpreta os dados. Somente leitura."""
        raise NotImplementedError


def _descrever_erro(erro: Exception) -> str:
    if isinstance(erro, FalhaColeta):
        return str(erro)
    return f"{type(erro).__name__}: {erro}"


def executar(componente: str, provedores: list[Provedor], ctx: Contexto) -> Resultado:
    """Tenta os provedores por prioridade e devolve o primeiro resultado útil.

    Cada tentativa é registrada em ``dados["tentativas"]`` com o resultado
    (indisponível / sem dados / erro / sucesso). Um resultado ``NAO_VERIFICADO``
    vindo de um provedor conta como "sem dados" e o próximo é tentado. Se nenhum
    funcionar, devolve ``NAO_VERIFICADO`` com a mensagem padrão.
    """
    tentativas: list[dict[str, str]] = []
    if not provedores:
        tentativas.append(
            {"provedor": "-", "resultado": INDISPONIVEL, "detalhe": "nenhum método implementado para este componente"}
        )
    for provedor in sorted(provedores, key=lambda p: p.prioridade):
        nome = getattr(provedor, "nome", type(provedor).__name__)
        try:
            disp = provedor.disponivel(ctx)
        except Exception as erro:  # noqa: BLE001 - falha de um provedor não derruba os outros
            tentativas.append({"provedor": nome, "resultado": ERRO, "detalhe": _descrever_erro(erro)})
            continue
        if not isinstance(disp, Disponibilidade):
            disp = Disponibilidade(bool(disp), "" if disp else "indisponível")
        if not disp.ok:
            tentativas.append({"provedor": nome, "resultado": INDISPONIVEL, "detalhe": disp.motivo})
            continue
        try:
            resultado = provedor.coletar(ctx)
        except Exception as erro:  # noqa: BLE001
            tentativas.append({"provedor": nome, "resultado": ERRO, "detalhe": _descrever_erro(erro)})
            continue
        if resultado is None or resultado.status == Status.NAO_VERIFICADO:
            detalhe = resultado.mensagem if resultado is not None else ""
            tentativas.append({"provedor": nome, "resultado": SEM_DADOS, "detalhe": detalhe})
            continue
        tentativas.append({"provedor": nome, "resultado": SUCESSO, "detalhe": ""})
        resultado.componente = componente
        resultado.fonte = resultado.fonte or nome
        resultado.dados["tentativas"] = tentativas
        return resultado
    return resultado_nao_verificado(componente, tentativas)

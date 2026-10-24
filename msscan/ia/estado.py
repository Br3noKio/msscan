"""Estado da IA para a tela inicial e o botão "Testar" (CLAUDE.md 10c).

Indicador: ``disponivel`` / ``sem_chave`` / ``sem_acesso`` (chave recusada) / ``limite`` (limite atingido),
mais ``indisponivel`` quando o contexto não permite IA (replay, ``--sem-ia``, ``semrede=1``, sem rede).
A situação de cada provedor vem do último "Testar" ou da última análise real; nunca da chave em si.

"Testar" faz uma chamada mínima SEM nenhum dado do servidor: só a palavra "teste" e uma instrução curta.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from msscan.core.executor import Executor
from msscan.ia.provedores import (
    ERRO,
    LIMITE,
    OK,
    SEM_ACESSO,
    SEM_REDE,
    Provedor,
    _mensagem_http,
    classificar,
    montar,
    precisa_chave,
)

DISPONIVEL, SEM_CHAVE, INDISPONIVEL = "disponivel", "sem_chave", "indisponivel"
TEXTOS = {
    DISPONIVEL: "IA disponível",
    SEM_CHAVE: "IA sem chave",
    SEM_ACESSO: "IA sem acesso (chave recusada)",
    LIMITE: "IA com limite atingido",
    INDISPONIVEL: "IA indisponível",
}
INSTRUCOES_TESTE = 'Teste de conexão do MSscan. Responda somente com o JSON {"ok": true}.'
MENSAGEM_TESTE = "teste"
"""Única coisa enviada no "Testar": nenhum dado do servidor."""
TIMEOUT_TESTE_S = 30
MENSAGENS_TESTE = {
    OK: "Chave aceita: a IA respondeu.",
    SEM_ACESSO: "Chave recusada (sem acesso). Confira se foi copiada inteira e se está ativa.",
    LIMITE: "A chave funciona, mas o limite de uso foi atingido agora (429). Tente mais tarde ou use a reserva.",
    SEM_REDE: "Sem conexão com a IA (rede, DNS ou relógio). O teste será possível quando a rede voltar.",
}


@dataclass
class ResultadoTeste:
    """Resultado do "Testar": situação, mensagem para o técnico e o código HTTP (nunca a chave)."""

    situacao: str
    mensagem: str
    status: int | None = None

    def para_dict(self) -> dict[str, Any]:
        return {"situacao": self.situacao, "mensagem": self.mensagem, "status": self.status}


def testar_chave(executor: Executor, provedor: Provedor) -> ResultadoTeste:
    """Chamada mínima para validar a chave. Nunca levanta exceção; nada do servidor é enviado."""
    if precisa_chave(provedor) and not provedor.chave:
        return ResultadoTeste(SEM_CHAVE, "Informe a chave antes de testar.")
    cabecalhos, corpo = montar(provedor, INSTRUCOES_TESTE, MENSAGEM_TESTE, None, max_tokens=256)
    resposta = executor.post_ia(cabecalhos, corpo, timeout=TIMEOUT_TESTE_S, url=provedor.url)
    del cabecalhos
    situacao = classificar(resposta)
    if situacao in MENSAGENS_TESTE:
        return ResultadoTeste(situacao, MENSAGENS_TESTE[situacao], resposta.status)
    return ResultadoTeste(ERRO, f"A IA respondeu com erro: {_mensagem_http(resposta)}", resposta.status)


class EstadoIA:
    """Última situação conhecida de cada provedor (por id), segura para várias threads."""

    def __init__(self) -> None:
        self._trava = threading.Lock()
        self._situacoes: dict[str, tuple[str, float]] = {}

    def registrar(self, identificador: str, situacao: str) -> None:
        with self._trava:
            self._situacoes[identificador] = (situacao, time.time())

    def registrar_tentativas(self, tentativas: list[dict[str, Any]]) -> None:
        """Atualiza a partir das tentativas de uma análise real (``solicitar_analise``)."""
        for tentativa in tentativas:
            if tentativa.get("nome") and tentativa.get("situacao"):
                self.registrar(str(tentativa["nome"]), str(tentativa["situacao"]))

    def situacao(self, identificador: str) -> str:
        with self._trava:
            return self._situacoes.get(identificador, ("", 0.0))[0]

    def esquecer(self, identificador: str) -> None:
        """Chave trocada: a situação antiga não vale mais."""
        with self._trava:
            self._situacoes.pop(identificador, None)


def _utilizavel(provedor: Provedor, estado: EstadoIA) -> bool:
    return (not precisa_chave(provedor) or bool(provedor.chave)) and estado.situacao(provedor.nome) not in (SEM_ACESSO, LIMITE)


def indicador(configurados: list[Provedor], estado: EstadoIA, motivo_contexto: str = "") -> dict[str, Any]:
    """Indicador da tela inicial a partir de principal/reserva configuradas (com ou sem chave)."""
    if motivo_contexto:
        return {"situacao": INDISPONIVEL, "texto": TEXTOS[INDISPONIVEL], "detalhe": motivo_contexto}
    com_chave = [p for p in configurados if not precisa_chave(p) or p.chave]
    if not com_chave:
        detalhe = "Escolha uma IA e informe a chave na tela \"Configurar IA\" (ou no config/ia.conf)."
        return {"situacao": SEM_CHAVE, "texto": TEXTOS[SEM_CHAVE], "detalhe": detalhe}
    primeira = com_chave[0]
    situacao = estado.situacao(primeira.nome)
    rotulo = primeira.titulo or primeira.nome
    if situacao in (SEM_ACESSO, LIMITE):
        reserva = next((p for p in com_chave[1:] if _utilizavel(p, estado)), None)
        if reserva is None:
            return {"situacao": situacao, "texto": TEXTOS[situacao], "detalhe": f"{rotulo}: {MENSAGENS_TESTE[situacao]}"}
        detalhe = f"{rotulo}: {TEXTOS[situacao].removeprefix('IA ')}; a reserva ({reserva.titulo or reserva.nome}) será usada."
        return {"situacao": DISPONIVEL, "texto": TEXTOS[DISPONIVEL], "detalhe": detalhe}
    detalhe = f"{rotulo} ({primeira.modelo})" + (" — chave testada" if situacao == OK else "")
    return {"situacao": DISPONIVEL, "texto": TEXTOS[DISPONIVEL], "detalhe": detalhe}

"""Testes do fallback entre provedores."""

from __future__ import annotations

from msscan.core.fallback import Disponibilidade, Provedor, executar
from msscan.core.modelo import MENSAGEM_NAO_VERIFICADO, Resultado, Status
from msscan.deteccao.contexto import Contexto
from tests.conftest import ExecutorFalso


class ProvedorTeste(Provedor):
    def __init__(self, nome, prioridade, disponivel=True, resultado=None, erro_disp=None, erro_coleta=None):
        self.nome = nome
        self.prioridade = prioridade
        self._disp = disponivel
        self._resultado = resultado
        self._erro_disp = erro_disp
        self._erro_coleta = erro_coleta
        self.coletou = False

    def disponivel(self, ctx):
        if self._erro_disp:
            raise self._erro_disp
        return Disponibilidade(self._disp, "" if self._disp else "ferramenta ausente")

    def coletar(self, ctx):
        self.coletou = True
        if self._erro_coleta:
            raise self._erro_coleta
        return self._resultado


def ctx():
    return Contexto(executor=ExecutorFalso())


def ok(fonte=""):
    return Resultado("x", Status.OK, fonte=fonte, dados={"valor": 1})


def test_usa_o_de_maior_prioridade_disponivel():
    redfish = ProvedorTeste("redfish", 10, disponivel=False)
    ipmi = ProvedorTeste("ipmi", 20, resultado=ok())
    sensores = ProvedorTeste("lm-sensors", 30, resultado=ok())
    r = executar("sensores", [sensores, ipmi, redfish], ctx())
    assert r.status == Status.OK
    assert r.fonte == "ipmi"
    assert r.componente == "sensores"
    assert not sensores.coletou
    assert [t["resultado"] for t in r.dados["tentativas"]] == ["indisponível", "sucesso"]
    assert r.dados["tentativas"][0]["detalhe"] == "ferramenta ausente"


def test_sem_dados_passa_para_o_proximo():
    r = executar("x", [ProvedorTeste("a", 1, resultado=None), ProvedorTeste("b", 2, resultado=ok("b"))], ctx())
    assert r.fonte == "b"
    assert r.dados["tentativas"][0]["resultado"] == "sem dados"


def test_nao_verificado_de_provedor_conta_como_sem_dados():
    nv = Resultado("x", Status.NAO_VERIFICADO, mensagem="sensor na")
    r = executar("x", [ProvedorTeste("a", 1, resultado=nv), ProvedorTeste("b", 2, resultado=ok())], ctx())
    assert r.status == Status.OK
    assert r.dados["tentativas"][0] == {"provedor": "a", "resultado": "sem dados", "detalhe": "sensor na"}


def test_excecoes_sao_registradas_e_nao_propagam():
    r = executar(
        "x",
        [
            ProvedorTeste("a", 1, erro_disp=RuntimeError("quebrou em disponivel")),
            ProvedorTeste("b", 2, erro_coleta=ValueError("quebrou em coletar")),
        ],
        ctx(),
    )
    assert r.status == Status.NAO_VERIFICADO
    detalhes = [t["detalhe"] for t in r.dados["tentativas"]]
    assert "RuntimeError: quebrou em disponivel" in detalhes
    assert "ValueError: quebrou em coletar" in detalhes


def test_todos_falham_devolve_nao_verificado_com_mensagem_exata():
    r = executar("raid", [ProvedorTeste("a", 1, disponivel=False), ProvedorTeste("b", 2, resultado=None)], ctx())
    assert r.status == Status.NAO_VERIFICADO
    assert r.mensagem == MENSAGEM_NAO_VERIFICADO == "Não foi possível verificar este componente neste equipamento"
    assert r.fonte == ""
    assert len(r.dados["tentativas"]) == 2


def test_sem_provedores():
    r = executar("raid", [], ctx())
    assert r.status == Status.NAO_VERIFICADO
    assert r.dados["tentativas"][0]["resultado"] == "indisponível"


def test_fonte_explicita_do_provedor_e_mantida():
    r = executar("x", [ProvedorTeste("ipmi", 1, resultado=ok("ipmitool sensor"))], ctx())
    assert r.fonte == "ipmitool sensor"

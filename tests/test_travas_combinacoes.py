"""Prova das travas da rodada final (ajuste da fase 10, pedido de 28/09/2026, item A4).

Com ``ACOES_REAIS_LIBERADAS = False`` e ``GRAVACAO_REAL_LIBERADA = False``, NENHUMA combinação de flags da linha de
comando — inclusive ``--aplicar`` — chega a executar uma escrita no BMC nem o reinício para aplicar firmware.

Como se prova:
1. Todas as combinações de ``--simular``/``--aplicar`` (e nenhum) com ``--sem-ia``, ``--sem-credenciais``, ``--ui``,
   ``--replay`` e ``--coletar`` passam pelo parser real (``main.criar_parser``) e pelo ``main.criar_executor``: o
   executor criado recusa escrita no BMC e ações.
2. Para cada combinação, os fluxos reais do modo texto (``etapa_acoes`` com NTP, SEL pelo Redfish, SEL pelo IPMI e o
   "Desfazer"; ``etapa_aplicacao`` do firmware) e da interface (``main.executar_ui`` → ``Sessao`` → ações, desfazer e
   aplicação) rodam até o fim, com o técnico confirmando tudo (``CORRIGIR``, ``DESFAZER``, ``ATUALIZAR``, ``SEM
   REDUNDANCIA`` e um usuário administrador), contra um BMC espião com origem "real" — inclusive disfarçado de alvo
   simulado. O espião registra qualquer chamada que chegue às implementações de escrita (``_acao_bmc``,
   ``_bmc_escrita``, ``ipmitool sel clear`` e ``systemctl reboot``): a lista precisa terminar vazia.
3. ``requests`` e ``subprocess`` ficam proibidos durante o teste: nada sai para a rede nem roda na máquina.
"""

from __future__ import annotations

import argparse
import itertools
import re
from pathlib import Path

import pytest

import main
from msscan.acoes.comum import CONFIRMACAO, CONFIRMACAO_DESFAZER, MODO_APLICAR, MODO_SIMULAR
from msscan.atualizacao import aplicacao as mod_aplicacao
from msscan.atualizacao.aplicacao import CONFIRMACAO as ATUALIZAR, CONFIRMACAO_REDUNDANCIA
from msscan.core import executor as modulo_executor
from msscan.core.executor import ExecutorReal
from msscan.pendrive.local import local_manual
from msscan.relatorio.completo import Gravacao
from tests import test_acoes as ta
from tests import test_aplicacao as tf
from tests.conftest import R640, hardware_saudavel

FLAGS_MODO = ([], ["--simular"], ["--aplicar"])
FLAGS_OPCIONAIS = (["--sem-ia"], ["--sem-credenciais"], ["--ui"], ["--replay", str(R640)], ["--coletar"])


def combinacoes() -> list[list[str]]:
    """Todas as combinações de flags que mudam o fluxo (3 modos × 2^5 opcionais = 96)."""
    saida = []
    for modo in FLAGS_MODO:
        for n in range(len(FLAGS_OPCIONAIS) + 1):
            for escolha in itertools.combinations(FLAGS_OPCIONAIS, n):
                saida.append(modo + [a for flag in escolha for a in flag])
    return saida


COMBINACOES = combinacoes()


class Espiao:
    """Mistura: executor com origem "real" que registra toda chamada que chegue a uma implementação de escrita."""

    origem = "real"
    alvo_simulado = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.chegou: list[tuple] = []

    def _acao_bmc(self, metodo, url, token, corpo, timeout):
        self.chegou.append(("acao_bmc", metodo, url))
        return super()._acao_bmc(metodo, url, token, corpo, timeout)

    def _bmc_escrita(self, operacao, metodo, url, token, corpo, arquivo, timeout):
        self.chegou.append(("bmc_escrita", operacao, url))
        return super()._bmc_escrita(operacao, metodo, url, token, corpo, arquivo, timeout)

    def _rodar(self, comando, timeout):
        if tuple(comando) in (("ipmitool", "sel", "clear"), ("systemctl", "reboot")):
            self.chegou.append(("comando", *comando))
        return super()._rodar(comando, timeout)


class BMCAcoesReal(Espiao, ta.BMCFalso):
    """BMC das ações (NTP e SEL), com origem "real"."""


class BMCAcoesDisfarcado(BMCAcoesReal):
    alvo_simulado = True  # marcar um executor real como simulado não pode liberar nada


class BMCFirmwareReal(Espiao, tf.BMCFalso):
    """BMC da atualização de firmware, com origem "real"."""


class BMCFirmwareDisfarcado(BMCFirmwareReal):
    alvo_simulado = True


@pytest.fixture(autouse=True)
def sem_rede_nem_comandos(monkeypatch):
    import subprocess

    import requests

    monkeypatch.setattr(requests.Session, "request", lambda *a, **k: pytest.fail("chegou à rede"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("rodaria um comando na máquina"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("rodaria um comando na máquina"))
    monkeypatch.setattr(mod_aplicacao.shutil, "disk_usage", lambda _: type("U", (), {"free": 10 * 2**30})())


def test_travas_desligadas_nos_testes():
    """No código as travas foram liberadas (30/09/2026, 15.6); aqui elas estão SEMPRE desligadas (conftest), e é com
    elas desligadas que as combinações abaixo são provadas."""
    assert modulo_executor.ACOES_REAIS_LIBERADAS is False and modulo_executor.GRAVACAO_REAL_LIBERADA is False
    fonte = Path(modulo_executor.__file__).read_text(encoding="utf-8")
    assert re.search(r"^ACOES_REAIS_LIBERADAS = True$", fonte, re.M) and re.search(r"^GRAVACAO_REAL_LIBERADA = True$", fonte, re.M)


def test_combinacoes_cobrem_todas_as_flags_de_modo():
    parser = main.criar_parser()
    flags = {a for acao in parser._actions for a in acao.option_strings}
    assert {"--simular", "--aplicar", "--sem-ia", "--sem-credenciais", "--ui", "--replay", "--coletar"} <= flags
    assert len(COMBINACOES) == 96 and sum("--aplicar" in c for c in COMBINACOES) == 32
    with pytest.raises(SystemExit):
        parser.parse_args(["--simular", "--aplicar"])  # mutuamente exclusivas


def _entradas(respostas: list[str]):
    fila = iter(respostas)
    return lambda _pergunta="": next(fila, "")


def _fluxos_modo_texto(args: argparse.Namespace, tmp_path: Path, espioes: list) -> list[dict]:
    """Roda os fluxos de escrita do modo texto confirmando tudo; devolve os resultados."""
    resultados = []
    for classe in (BMCAcoesReal, BMCAcoesDisfarcado):
        # NTP (item 1 do catálogo, servidor digitado) + Desfazer.
        executor = classe()
        espioes.append(executor)
        relatorio: dict = {}
        ctx = ta.contexto(executor)
        main.etapa_acoes(args, executor, ctx, relatorio, "t", local_manual(str(tmp_path)), Gravacao(None, False, ""),
                         entrada=_entradas(["s", "1", "a.ntp.br", CONFIRMACAO, "s", CONFIRMACAO_DESFAZER]))  # fmt: skip
        resultados += relatorio.get("acoes", [])
        # SEL pelo Redfish (item 2).
        executor = classe()
        espioes.append(executor)
        relatorio = {}
        main.etapa_acoes(args, executor, ta.contexto(executor), relatorio, "t", local_manual(str(tmp_path)),
                         Gravacao(None, False, ""), entrada=_entradas(["s", "2", CONFIRMACAO]))  # fmt: skip
        resultados += relatorio.get("acoes", [])
        # SEL pelo IPMI local (sem credenciais do BMC).
        executor = classe(ipmi=True)
        espioes.append(executor)
        relatorio = {}
        main.etapa_acoes(args, executor, ta.contexto(executor, credenciais=False, ipmi=True), relatorio, "t",
                         local_manual(str(tmp_path)), Gravacao(None, False, ""), entrada=_entradas(["s", "2", CONFIRMACAO]))  # fmt: skip
        resultados += relatorio.get("acoes", [])
    for classe in (BMCFirmwareReal, BMCFirmwareDisfarcado):
        # Firmware: os dois itens, redundância perdida confirmada e ATUALIZAR; administrador já validado.
        executor = classe(com_exportacao=True)
        espioes.append(executor)
        relatorio = {"atualizacoes": tf.verificacao(tf.NIC, tf.BIOS, redundancia=True)}
        main.etapa_aplicacao(args, executor, tf.contexto(executor), relatorio, "t", local_manual(str(tmp_path)),
                             Gravacao(None, False, ""),
                             entrada=_entradas(["s", "1,2", CONFIRMACAO_REDUNDANCIA, ATUALIZAR]))  # fmt: skip  # plano + ATUALIZAR (18.2)
        resultados.append(relatorio.get("aplicacao") or {})
    return resultados


def _modo_da_interface(args: argparse.Namespace, monkeypatch) -> str:
    """Modo com que ``main.executar_ui`` cria a sessão da interface para estes argumentos (sem abrir porta)."""
    capturado: dict = {}

    class SessaoCaptura:
        def __init__(self, executor, pasta, **kwargs):
            capturado.update(kwargs, executor=executor)

        def iniciar(self):
            pass

        def encerrar(self):
            pass

    class ServidorFalso:
        url = "http://127.0.0.1:0/"

        def __init__(self, sessao, porta, terminal=None):
            self.terminal = terminal

        def serve_forever(self, poll_interval=0.5):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(main, "Sessao", SessaoCaptura)
    monkeypatch.setattr(main, "ServidorUI", ServidorFalso)
    monkeypatch.setattr(main, "ControleTela", lambda *a, **k: None)
    executor = main.criar_executor(args)
    assert main.executar_ui(args, executor, None) == 0
    assert not capturado["executor"].acoes_permitidas() and not capturado["executor"].escrita_permitida()
    return capturado["modo_aplicacao"]


_SESSOES_POR_MODO: dict[str, list] = {}


def _fluxos_interface(modo: str, tmp_path: Path) -> list:
    """Ações, desfazer e firmware pela sessão da interface (uma vez por modo; todas as combinações caem num dos dois)."""
    if modo in _SESSOES_POR_MODO:
        return _SESSOES_POR_MODO[modo]
    espioes: list = []
    for classe in (BMCAcoesReal, BMCAcoesDisfarcado):
        executor = classe()
        espioes.append(executor)
        sessao = ta.sessao_pronta(executor, tmp_path / f"ui_{modo}_{classe.__name__}", modo)
        for identificador, parametros in (("bmc.relogio_ntp", {"servidores": "a.ntp.br"}), ("bmc.sel_exportar_limpar", {})):
            assert sessao.prechecar_acao(identificador, parametros)[0]
            sessao.aguardar(30)
            assert sessao.executar_acao(identificador, CONFIRMACAO)[0]
            sessao.aguardar(30)
        if sessao.prechecar_desfazer(0)[0]:
            sessao.aguardar(30)
            sessao.desfazer_acao(0, CONFIRMACAO_DESFAZER)
            sessao.aguardar(30)
        sessao.encerrar()
    for classe in (BMCFirmwareReal, BMCFirmwareDisfarcado):
        executor = classe(com_exportacao=True)
        espioes.append(executor)
        sessao = tf.sessao_pronta(executor, tmp_path / f"uif_{modo}_{classe.__name__}", modo)
        # Plano de atualização (18.2) antes: a instalação precisa chegar ao caminho que grava para a trava ser posta à prova.
        assert sessao.preparar_aviso("catalogo", ["item1", "item2"])[0]
        sessao.aguardar(30)
        assert sessao.aplicar_atualizacoes(["item1", "item2"], ATUALIZAR, CONFIRMACAO_REDUNDANCIA)[0]
        sessao.aguardar(60)
        sessao.encerrar()
    _SESSOES_POR_MODO[modo] = espioes
    return espioes


@pytest.mark.parametrize("argv", COMBINACOES, ids=lambda a: " ".join(a).replace(str(R640), "R640") or "(sem flags)")
def test_nenhuma_combinacao_escreve_no_bmc(argv, tmp_path, monkeypatch):
    args = main.criar_parser().parse_args(argv)
    executor = main.criar_executor(args)
    assert not executor.acoes_permitidas() and not executor.escrita_permitida()

    espioes: list = []
    resultados = _fluxos_modo_texto(args, tmp_path, espioes)
    modo = _modo_da_interface(args, monkeypatch)
    # 15.6: o padrão é REAL; só --simular simula. Toda combinação sem --simular chega ao caminho que grava.
    assert modo == (MODO_SIMULAR if "--simular" in argv else MODO_APLICAR)
    espioes += _fluxos_interface(modo, tmp_path)

    chegaram = [c for e in espioes for c in e.chegou]
    assert chegaram == [], f"escrita chegou ao BMC com {argv}: {chegaram}"
    assert not any(isinstance(e, ta.BMCFalso) and e.escritas for e in espioes)
    assert not any(isinstance(e, tf.BMCFalso) and e.escritas for e in espioes)
    situacoes = {r.get("situacao") for r in resultados}
    if "--simular" not in argv:
        # O fluxo foi até o primeiro pedido ao BMC e a trava o recusou.
        assert "bloqueada_trava" in situacoes, situacoes
    else:
        assert situacoes <= {"simulada"}, situacoes


def test_sem_as_travas_o_espiao_detectaria(tmp_path, monkeypatch):
    """Controle: com as travas liberadas (só dentro deste teste), o mesmo fluxo CHEGA ao espião — o teste enxerga."""
    monkeypatch.setattr(modulo_executor, "ACOES_REAIS_LIBERADAS", True)
    monkeypatch.setattr(modulo_executor, "GRAVACAO_REAL_LIBERADA", True)
    args = main.criar_parser().parse_args(["--aplicar"])
    espioes: list = []
    _fluxos_modo_texto(args, tmp_path, espioes)
    tipos = {c[0] for e in espioes for c in e.chegou}
    assert {"acao_bmc", "bmc_escrita", "comando"} <= tipos


def test_executor_real_puro_recusa_todas_as_operacoes():
    executor = ExecutorReal()
    for operacao in (modulo_executor.NTP_DELL, modulo_executor.NTP_DMTF, modulo_executor.LIMPAR_SEL,
                     modulo_executor.NTP_DELL_RESTAURAR, modulo_executor.NTP_DMTF_RESTAURAR):  # fmt: skip
        assert executor.acao_bmc(operacao, "https://bmc/redfish/v1/Managers/1/Attributes", "t", {}).status is None
    for operacao in (modulo_executor.ENVIAR_FIRMWARE, modulo_executor.BOOT_UNICO_USB, modulo_executor.EXPORTAR_CONFIGURACAO):
        assert executor.bmc_escrita(operacao, "https://bmc/redfish/v1/UpdateService/x", "t", {}).status is None
    assert executor.acao_comando(["ipmitool", "sel", "clear"])[0] == -1
    # Configuração do iDRAC (CLAUDE.md 17.4): mesma trava das ações.
    for comando in (["ipmitool", "user", "set", "name", "5", "msscan1a2b"], ["racadm", "set", "iDRAC.OS-BMC.AdminState", "1"]):
        assert executor.acao_comando(comando) == (-1, "", modulo_executor.MOTIVO_TRAVA_ACOES)
    # 18.4: a rede do iDRAC é recusada antes mesmo da trava (nunca é alterada); o racadm update fica atrás da trava.
    assert "nunca é alterada" in executor.acao_comando(["ipmitool", "lan", "set", "1", "ipsrc", "dhcp"])[2]
    assert executor.atualizar_racadm("/opt/vendor/racadm", "/m/MSSCAN/firmware/pacotes/x.EXE") == (-1, "", modulo_executor.MOTIVO_TRAVA)
    assert executor.slots_temporarios == {}
    assert executor.reiniciar_para_aplicar() == (False, modulo_executor.MOTIVO_TRAVA)


@pytest.fixture(autouse=True)
def _hardware_saudavel(monkeypatch):
    """Tela de aviso: hardware do BMC falso tratado como OK (a lógica do aviso é testada em test_aviso.py)."""
    hardware_saudavel(monkeypatch)

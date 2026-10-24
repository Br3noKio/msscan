"""Fase 6: Redfish (executor, parsers, cliente, credenciais, provedores, relatório, interface).

Dados Redfish: respostas reais do DMTF Redfish Mockup Server (mockups públicos da DMTF),
gravadas pelo ExecutorGravador. Login com senha conferida: ``tests/redfish_falso.py``.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from msscan.core.executor import (
    ExecutorGravador,
    ExecutorReal,
    ExecutorReplay,
    RespostaRedfish,
    sem_segredos,
    validar_redfish,
)
from msscan.core.modelo import Status
from msscan.deteccao.contexto import Contexto
from msscan.deteccao.detectores import detectar_redfish
from msscan.interface.servidor import ServidorUI
from msscan.interface.sessao import Sessao
from msscan.modulos.registro import registro_padrao
from msscan.parsers import ErroParser
from msscan.parsers.ipmitool import parse_lan_print
from msscan.parsers.redfish import (
    membros,
    parse_conta,
    parse_disco,
    parse_entradas_log,
    parse_power,
    parse_raiz,
    parse_storage,
    parse_thermal,
    parse_volume,
)
from msscan.provedores.redfish import (
    COMPONENTES_REDFISH,
    ProvedorRedfishArmazenamento,
    ProvedorRedfishFontes,
    ProvedorRedfishFontesReserva,
    ProvedorRedfishLogBMC,
    ProvedorRedfishSensores,
    ProvedorRedfishVentoinhas,
    avaliar_armazenamento,
    avaliar_fontes_redfish,
    avaliar_leitura,
)
from msscan.redfish.acesso import (
    AVISO_BLOQUEIO,
    ESGOTADO,
    MAX_TENTATIVAS,
    NAO_PERGUNTADO,
    PULADO,
    VALIDADO,
    AcessoBMC,
    descrever_perfil,
    perguntar_credenciais,
)
from msscan.redfish.cliente import ClienteRedfish, FalhaRedfish
from msscan.relatorio.bmc import formatar_acesso_bmc, resumo_acesso_bmc
from msscan.relatorio.html import gerar_html
from tests.conftest import PASTA_FIXTURES, R640, ExecutorFalso, com_textos
from tests.redfish_falso import ServidorRedfishFalso

DMTF = PASTA_FIXTURES / "redfish" / "dmtf"
LOCALSTORAGE = DMTF / "public_localstorage"
RACKMOUNT = DMTF / "public_rackmount1"
IPMITOOL = PASTA_FIXTURES / "avulsas" / "ipmitool"
SENHA = "senha-certa"


def corpo(fixture: Path, caminho: str):
    """Corpo gravado de ``GET <caminho>`` numa fixture Redfish."""
    return json.loads((fixture / "redfish.json").read_text())[f"GET {caminho}"]["corpo"]


def contexto_replay(fixture: Path) -> Contexto:
    """Contexto com sessão Redfish aberta sobre as respostas gravadas (replay)."""
    executor = ExecutorReplay(fixture)
    cliente = ClienteRedfish(executor, "https://bmc.exemplo")
    raiz = cliente.raiz()
    ctx = Contexto(executor=executor)
    ctx.redfish.confirmado = True
    ctx.cache["redfish_raiz"] = raiz
    ctx.bmc = AcessoBMC(cliente, raiz["contas"])
    ctx.bmc.tentar("qualquer", "qualquer")
    assert ctx.bmc.validado
    return ctx


class ExecutorReplayComBMC(ExecutorReplay):
    """Replay do R640 com o Redfish de verdade (HTTP) num BMC falso."""

    _redfish = ExecutorReal._redfish


@pytest.fixture
def bmc():
    with ServidorRedfishFalso(LOCALSTORAGE, papeis=["ReadOnly"]) as servidor:
        yield servidor


def acesso_real(servidor: ServidorRedfishFalso) -> tuple[AcessoBMC, ExecutorReal]:
    executor = ExecutorReal()
    cliente = ClienteRedfish(executor, servidor.base)
    raiz = cliente.raiz()
    return AcessoBMC(cliente, raiz["contas"]), executor


# --- Executor: salvaguardas e segredos -------------------------------------------------------


class TestExecutorRedfish:
    @pytest.mark.parametrize(
        ("metodo", "url", "permitido"),
        [
            ("GET", "https://b/redfish/v1/Systems", True),
            ("POST", "https://b/redfish/v1/SessionService/Sessions", True),
            ("POST", "https://b/redfish/v1/SessionService/Sessions/", True),
            ("DELETE", "https://b/redfish/v1/SessionService/Sessions/42", True),
            ("POST", "https://b/redfish/v1/Systems/1/Actions/ComputerSystem.Reset", False),
            ("POST", "https://b/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate", False),
            ("DELETE", "https://b/redfish/v1/AccountService/Accounts/2", False),
            ("DELETE", "https://b/redfish/v1/SessionService/Sessions", False),
            ("POST", "https://b/redfish/v1/Sessions", True),
            ("DELETE", "https://b/redfish/v1/Sessions/7", True),
            ("DELETE", "https://b/redfish/v1/Sessions", False),
            ("DELETE", "https://b/redfish/v1/SessionService/Sessions/1/x", False),
            ("PATCH", "https://b/redfish/v1/Systems/1", False),
            ("PUT", "https://b/redfish/v1/Systems/1", False),
            ("GET", "ftp://b/redfish/v1", False),
        ],
    )
    def test_salvaguardas(self, metodo, url, permitido):
        assert (validar_redfish(metodo, url) == "") is permitido

    def test_bloqueado_nem_chega_ao_bmc(self):
        executor = ExecutorFalso()
        resposta = executor.redfish("PATCH", "https://b/redfish/v1/Systems/1")
        assert resposta.status is None and resposta.erro_tipo == "bloqueado"
        assert executor.historico[-1].comando == ["PATCH", "https://b/redfish/v1/Systems/1"]

    def test_historico_nunca_tem_senha_nem_token(self):
        executor = ExecutorFalso()
        url = "https://b/redfish/v1/SessionService/Sessions"
        executor.respostas_redfish[("POST", url)] = RespostaRedfish(url, 201, token="TOKEN-SECRETO", local="/x")
        executor.redfish("POST", url, corpo={"UserName": "u", "Password": "SENHA-SECRETA"})
        texto = json.dumps(executor.historico_dict())
        assert "SENHA-SECRETA" not in texto and "TOKEN-SECRETO" not in texto
        assert executor.historico[-1].comando == ["POST", url, "(credenciais omitidas)"]

    def test_respostas_sem_senha(self):
        # O mockup da DMTF ecoa o corpo do login no GET da sessão: "Password" nunca passa.
        assert sem_segredos({"UserName": "u", "Password": "x", "a": [{"Token": "t"}]}) == {
            "UserName": "u", "Password": "REMOVIDO", "a": [{"Token": "REMOVIDO"}]}  # fmt: skip
        assert "senha-de-teste" not in (LOCALSTORAGE / "redfish.json").read_text()

    def test_replay_login_com_token_ficticio(self):
        executor = ExecutorReplay(LOCALSTORAGE)
        resposta = executor.redfish("POST", "https://outro/redfish/v1/SessionService/Sessions", corpo={})
        assert resposta.ok and resposta.token == "token-replay"

    def test_gravador_nao_grava_token_nem_credenciais(self, tmp_path, bmc):
        gravador = ExecutorGravador(tmp_path, base=ExecutorReal())
        cliente = ClienteRedfish(gravador, bmc.base)
        cliente.raiz()
        assert cliente.login("tecnico", SENHA).ok
        cliente.get("/redfish/v1/Systems")
        assert cliente.logout()
        texto = (tmp_path / "redfish.json").read_text()
        assert SENHA not in texto and "tecnico" not in texto
        gravado = json.loads(texto)
        assert set(gravado["POST /redfish/v1/SessionService/Sessions"]) == {"status", "erro_tipo", "erro", "local"}
        assert "GET /redfish/v1/Systems" in gravado


# --- Parsers ---------------------------------------------------------------------------------


class TestParsersRedfish:
    def test_raiz(self):
        raiz = parse_raiz(corpo(LOCALSTORAGE, "/redfish/v1"))
        assert raiz["versao"] == "1.15.0"
        assert raiz["sessoes"] == "/redfish/v1/SessionService/Sessions" and raiz["contas"] == "/redfish/v1/AccountService"

    def test_raiz_invalida(self):
        with pytest.raises(ErroParser):
            parse_raiz({"qualquer": 1})
        with pytest.raises(ErroParser):
            parse_raiz([1])

    def test_power(self):
        power = parse_power(corpo(RACKMOUNT, "/redfish/v1/Chassis/1U/Power"))
        fonte = power["fontes"][0]
        assert (fonte["saude"], fonte["estado"], fonte["tensao_entrada"]) == ("Warning", "Enabled", 120.0)
        assert power["tensoes"][0]["leitura"] == 12.0 and power["tensoes"][0]["limite_critico"] == 13.0

    def test_thermal(self):
        thermal = parse_thermal(corpo(RACKMOUNT, "/redfish/v1/Chassis/1U/Thermal"))
        cpu1, cpu2 = thermal["temperaturas"][:2]
        assert cpu1["leitura"] == 41.0 and cpu1["limite_alerta"] == 42.0
        assert cpu2["estado"] == "Disabled" and cpu2["leitura"] is None
        assert thermal["ventoinhas"][0]["leitura"] == 2100.0 and thermal["ventoinhas"][0]["unidade"] == "RPM"
        assert thermal["redundancia"][0]["modo"] == "N+m"

    def test_storage_volume_disco(self):
        storage = parse_storage(corpo(LOCALSTORAGE, "/redfish/v1/Systems/437XR1138R2/Storage/1"))
        assert storage["controladoras"][0]["modelo"] == "12Gbs Integrated RAID" and len(storage["discos"]) == 4
        volume = parse_volume(corpo(LOCALSTORAGE, "/redfish/v1/Systems/437XR1138R2/Storage/1/Volumes/1"))
        assert volume["raid"] == "RAID1" and volume["saude"] == "OK"
        disco = parse_disco(corpo(LOCALSTORAGE, storage["discos"][0]))
        assert disco["falha_prevista"] is False and disco["protocolo"] == "SAS" and disco["capacidade_gb"] == 900

    def test_log_e_conta(self):
        entradas = parse_entradas_log(corpo(LOCALSTORAGE, "/redfish/v1/Managers/BMC/LogServices/Log/Entries"))
        assert entradas[0]["severidade"] == "Critical" and entradas[0]["mensagem"] == "System May be Melting"
        conta = parse_conta(corpo(LOCALSTORAGE, "/redfish/v1/AccountService/Accounts/1"))
        assert conta == {"usuario": "Administrator", "papel": "Administrator"}

    def test_colecao_sem_members(self):
        with pytest.raises(ErroParser):
            membros({"Name": "x"})

    def test_lan_print_real(self):
        campos = parse_lan_print((IPMITOOL / "lan_print_1.txt").read_text())
        assert campos["IP Address"] == "192.0.2.10" and campos["IP Address Source"] == "DHCP Address"
        assert campos["Auth Type Enable"] == "Callback : MD5"

    def test_lan_print_vazio(self):
        with pytest.raises(ErroParser):
            parse_lan_print("")


# --- Detecção --------------------------------------------------------------------------------


def executor_deteccao(lan1: tuple[int, str, str], raiz_ok: bool = True) -> ExecutorFalso:
    tipo42 = (R640 / "comandos" / "dmidecode__-t__42.stdout").read_text()
    executor = ExecutorFalso(comandos={
        ("dmidecode", "-t", "42"): (0, tipo42, ""),
        ("ipmitool", "lan", "print", "1"): lan1,
        ("ipmitool", "lan", "print", "2"): (1, "", (IPMITOOL / "lan_print_2.stderr").read_text()),
    })  # fmt: skip
    if raiz_ok:
        url = "https://192.0.2.10/redfish/v1"
        executor.respostas_redfish[("GET", url)] = RespostaRedfish(url, 200, corpo=corpo(LOCALSTORAGE, "/redfish/v1"))
    return executor


class TestDeteccao:
    def test_ip_do_bmc_pelo_ipmi_e_raiz_confirmada(self):
        executor = executor_deteccao((0, (IPMITOOL / "lan_print_1.txt").read_text(), ""))
        ctx = Contexto(executor=executor)
        ctx.ipmi.disponivel = True
        detectar_redfish(ctx)
        info = ctx.redfish
        assert info.confirmado and info.endereco == "192.0.2.10" and info.origem == "ipmitool lan print 1"
        assert info.versao == "1.15.0"
        assert ctx.bmc.situacao == NAO_PERGUNTADO and ctx.bmc.cliente.base == "https://192.0.2.10"
        # Detectar nunca tenta login: credenciais só com o técnico.
        assert not any(c[0] in ("POST", "DELETE") for c in executor.comandos_rodados())

    def test_bmc_nao_responde(self):
        executor = executor_deteccao((0, (IPMITOOL / "lan_print_1.txt").read_text(), ""), raiz_ok=False)
        ctx = Contexto(executor=executor)
        ctx.ipmi.disponivel = True
        detectar_redfish(ctx)
        assert ctx.redfish.anunciado and not ctx.redfish.confirmado and ctx.bmc is None
        assert "não respondeu" in ctx.redfish.motivo

    def test_sem_ip_nos_canais(self):
        executor = executor_deteccao((1, "", "Invalid channel: 1"))
        ctx = Contexto(executor=executor)
        ctx.ipmi.disponivel = True
        detectar_redfish(ctx)
        assert not ctx.redfish.anunciado and "Invalid channel: 2" in ctx.redfish.motivo

    def test_sem_ipmi(self):
        ctx = Contexto(executor=executor_deteccao((0, "", "")))
        detectar_redfish(ctx)
        assert "IPMI indisponível" in ctx.redfish.motivo and ctx.bmc is None


# --- Cliente e credenciais (HTTP de verdade no BMC falso) -------------------------------------


class TestCredenciais:
    def test_login_perfil_e_logout(self, bmc):
        acesso, executor = acesso_real(bmc)
        mensagem = acesso.tentar("tecnico", SENHA)
        assert acesso.validado and acesso.perfil == "somente leitura" and acesso.administrador is False
        assert "somente leitura" in mensagem
        assert acesso.cliente.get("/redfish/v1/Systems")["Members"]
        assert acesso.encerrar() and bmc.logouts == 1 and not bmc.sessoes
        with pytest.raises(FalhaRedfish):
            acesso.cliente.get("/redfish/v1/Systems")
        assert SENHA not in json.dumps(executor.historico_dict()) and SENHA not in json.dumps(acesso.para_dict())

    def test_tres_tentativas_no_maximo(self, bmc):
        acesso, _ = acesso_real(bmc)
        for n in range(1, MAX_TENTATIVAS + 1):
            mensagem = acesso.tentar("tecnico", "errada")
            assert f"Tentativa {n} de 3" in mensagem
        assert acesso.situacao == ESGOTADO and not acesso.pode_tentar and AVISO_BLOQUEIO in acesso.mensagem
        assert acesso.tentar("tecnico", SENHA) == AVISO_BLOQUEIO  # a 4ª nem chega ao BMC
        assert bmc.logins == [("tecnico", 401)] * 3

    def test_erro_e_depois_acerto(self, bmc):
        acesso, _ = acesso_real(bmc)
        assert "Restam 2" in acesso.tentar("tecnico", "errada")
        acesso.tentar("tecnico", SENHA)
        assert acesso.validado and acesso.tentativas == 2

    def test_perfil_pelas_contas_quando_a_sessao_nao_informa(self):
        with ServidorRedfishFalso(LOCALSTORAGE, usuario="Administrator") as servidor:
            acesso, _ = acesso_real(servidor)
            acesso.tentar("Administrator", SENHA)
            assert acesso.perfil == "administrador" and acesso.administrador is True

    def test_perfil_nao_informado(self, bmc):
        bmc.papeis = None  # sessão sem Roles e usuário fora da lista de contas
        acesso, _ = acesso_real(bmc)
        acesso.tentar("tecnico", SENHA)
        assert acesso.perfil == "perfil não informado pelo BMC" and acesso.administrador is None

    @pytest.mark.parametrize(
        ("papel", "privilegios", "esperado"),
        [
            ("Administrator", [], ("administrador", True)),
            ("ReadOnly", [], ("somente leitura", False)),
            ("Operator", [], ("operador (sem permissão de administrador)", False)),
            ("Custom", ["Login"], ("somente leitura (papel 'Custom')", False)),
            ("Custom", ["Login", "ConfigureManager", "ConfigureComponents", "ConfigureUsers"], ("administrador (papel 'Custom')", True)),
            (None, [], ("perfil não informado pelo BMC", None)),
        ],
    )
    def test_descrever_perfil(self, papel, privilegios, esperado):
        assert descrever_perfil(papel, privilegios) == esperado

    def test_pular_sem_redfish_nao_pergunta(self):
        acesso = AcessoBMC(None)
        saidas: list[str] = []
        perguntar_credenciais(acesso, entrada=lambda _: pytest.fail("não devia perguntar"), saida=saidas.append)
        assert saidas == [] and acesso.situacao == "sem_redfish"


class TestModoTexto:
    def test_usuario_em_branco_pula(self, bmc):
        acesso, _ = acesso_real(bmc)
        saidas: list[str] = []
        perguntar_credenciais(acesso, entrada=lambda _: "", ler_senha=lambda _: pytest.fail("não pede senha"), saida=saidas.append)
        assert acesso.situacao == PULADO and bmc.logins == []
        texto = "\n".join(saidas)
        assert "O Redfish traz informações mais completas" in texto and "PULAR" in texto

    def test_erro_depois_acerto(self, bmc):
        acesso, _ = acesso_real(bmc)
        senhas = iter(["errada", SENHA])
        perguntas: list[str] = []

        def entrada(prompt: str) -> str:
            perguntas.append(prompt)
            return "tecnico"

        saidas: list[str] = []
        perguntar_credenciais(acesso, entrada=entrada, ler_senha=lambda _: next(senhas), saida=saidas.append)
        assert acesso.validado and len(perguntas) == 2 and "tentativa 2 de 3" in perguntas[1]
        assert "Enter = Pular" in perguntas[0]
        assert SENHA not in "\n".join(saidas)

    def test_tres_erros_seguem_sem_redfish(self, bmc):
        acesso, _ = acesso_real(bmc)
        saidas: list[str] = []
        perguntar_credenciais(acesso, entrada=lambda _: "tecnico", ler_senha=lambda _: "errada", saida=saidas.append)
        assert acesso.situacao == ESGOTADO and len(bmc.logins) == 3
        assert "podem bloquear o acesso ao BMC" in "\n".join(saidas)

    def test_sem_terminal_pula(self, bmc):
        acesso, _ = acesso_real(bmc)

        def fim_de_arquivo(_: str) -> str:
            raise EOFError

        perguntar_credenciais(acesso, entrada=fim_de_arquivo, saida=lambda _: None)
        assert acesso.situacao == PULADO

    def test_main_nao_pergunta_em_replay_nem_com_flag(self, bmc):
        import argparse

        from main import etapa_credenciais_bmc

        for args in (argparse.Namespace(replay="x", sem_credenciais=False), argparse.Namespace(replay=None, sem_credenciais=True)):
            acesso, _ = acesso_real(bmc)
            ctx = Contexto(executor=ExecutorFalso())
            ctx.bmc = acesso
            etapa_credenciais_bmc(args, ctx)
            assert acesso.situacao == PULADO
        assert bmc.logins == []


# --- Provedores ------------------------------------------------------------------------------


class TestProvedoresDMTF:
    def test_localstorage(self):
        ctx = contexto_replay(LOCALSTORAGE)
        raid = ProvedorRedfishArmazenamento().coletar(ctx)
        assert raid.status == Status.OK and raid.mensagem.startswith("1 controladora(s), 3 volume(s), 4 disco(s)")
        log = ProvedorRedfishLogBMC().coletar(ctx)
        assert log.status == Status.ALERTA and "System May be Melting" in log.dados["problemas"][0]
        sensores = ProvedorRedfishSensores().coletar(ctx)
        assert sensores.status == Status.OK and "temperatura máxima 41 °C (CPU1 Temp)" in sensores.mensagem
        assert ProvedorRedfishVentoinhas().coletar(ctx).status == Status.OK

    def test_fonte_warning_fica_para_o_ipmi(self):
        ctx = contexto_replay(RACKMOUNT)
        principal = ProvedorRedfishFontes().coletar(ctx)
        assert principal.status == Status.NAO_VERIFICADO and "classificação fica com o IPMI" in principal.mensagem
        reserva = ProvedorRedfishFontesReserva().coletar(ctx)
        assert reserva.status == Status.ALERTA and reserva.dados["classificacao_incerta"] and reserva.dados["defeito"] is False

    def test_rackmount_sem_storage(self):
        assert ProvedorRedfishArmazenamento().coletar(contexto_replay(RACKMOUNT)).status == Status.NAO_VERIFICADO

    def test_indisponivel_sem_credenciais(self):
        ctx = contexto_replay(LOCALSTORAGE)
        ctx.bmc.encerrar()
        ctx.bmc.situacao = PULADO
        ctx.bmc.mensagem = "Credenciais do BMC não informadas pelo técnico"
        disp = ProvedorRedfishSensores().disponivel(ctx)
        assert not disp.ok and "não informadas pelo técnico" in disp.motivo
        assert not ProvedorRedfishSensores().disponivel(Contexto(executor=ExecutorFalso())).ok

    def test_cadeia_com_redfish_na_frente(self):
        ctx = contexto_replay(LOCALSTORAGE)
        resultados = {r.componente: r for r in registro_padrao().diagnosticar(ctx, somente=COMPONENTES_REDFISH)}
        assert set(resultados) == set(COMPONENTES_REDFISH)
        assert resultados["raid"].fonte == "Redfish (Storage)"
        assert resultados["raid"].dados["tentativas"][0] == {"provedor": "Redfish (Storage)", "resultado": "sucesso", "detalhe": ""}


def fonte(**campos):
    return {"nome": "PSU1", "saude": "OK", "estado": "Enabled", "entrada_status": None, **campos}


class TestAvaliacao:
    def test_fonte_ausente_e_sem_energia_sao_redundancia(self):
        r = avaliar_fontes_redfish([fonte(), fonte(nome="PSU2", estado="Absent", saude=None)], [], reserva=False)
        assert r.status == Status.ALERTA and r.dados["redundancia_perdida"] and not r.dados["defeito"]
        r = avaliar_fontes_redfish([fonte(), fonte(nome="PSU2", saude="Critical", entrada_status="NoPower")], [], reserva=False)
        assert r.status == Status.ALERTA and "sem energia de entrada" in r.dados["problemas"][0]

    def test_fontes_ok(self):
        r = avaliar_fontes_redfish([fonte(), fonte(nome="PSU2")], [], reserva=False)
        assert r.status == Status.OK and not r.dados["redundancia_perdida"]

    def test_critical_nunca_vira_defeito_inventado(self):
        r = avaliar_fontes_redfish([fonte(saude="Critical")], [], reserva=True)
        assert r.status == Status.ALERTA and r.dados["classificacao_incerta"] and r.dados["defeito"] is False

    def test_leitura_pelos_limiares_quando_nao_ha_saude(self):
        base = {"nome": "T", "saude": None, "estado": "Enabled", "limite_alerta": 42, "limite_critico": 45}
        assert avaliar_leitura({**base, "leitura": 40}) is None
        assert avaliar_leitura({**base, "leitura": 43}) == Status.ALERTA
        assert avaliar_leitura({**base, "leitura": 46}) == Status.FALHA
        assert avaliar_leitura({**base, "leitura": 46, "estado": "Absent"}) is None

    def test_rebuild_e_falha_prevista(self):
        conjunto = {
            "controladoras": [{"nome": "PERC", "saude": "OK", "estado": "Enabled"}],
            "volumes_lidos": [{"nome": "VD0", "saude": "Warning", "estado": "Enabled", "raid": "RAID1",
                               "operacoes": [{"nome": "Rebuild", "progresso": 37.0}]}],
            "discos_lidos": [{"nome": "Disk 0", "saude": "OK", "estado": "Enabled", "falha_prevista": True},
                             {"nome": "Disk 1", "saude": None, "estado": "Absent", "falha_prevista": None}],
        }  # fmt: skip
        r = avaliar_armazenamento([conjunto])
        assert r.status == Status.FALHA
        assert "Volume VD0: Rebuild em andamento (37%)" in r.dados["problemas"]
        assert "Disco Disk 0: falha prevista (predictive failure)" in r.dados["problemas"]
        assert r.mensagem.startswith("1 controladora(s), 1 volume(s), 1 disco(s)")


# --- Relatório -------------------------------------------------------------------------------


class TestRelatorio:
    def test_sem_credenciais_lista_os_itens(self):
        ctx = Contexto(executor=ExecutorReplay(R640))
        ctx.bmc = AcessoBMC(ClienteRedfish(ExecutorFalso(), "https://192.0.2.10"))
        ctx.bmc.pular()
        from msscan.deteccao.detectores import detectar

        resultados = registro_padrao().diagnosticar(detectar(ExecutorReplay(R640)))
        secao = resumo_acesso_bmc(ctx, resultados, registro_padrao().titulo)
        assert secao["titulo"] == "Credenciais do BMC não informadas pelo técnico"
        como = {c["componente"]: c["como"] for c in secao["componentes"]}
        assert como["sensores"] == "verificado por outro método (ipmitool sensor)"
        assert como["raid"].startswith("verificado por outro método") or como["raid"] == "sem verificação"
        texto = formatar_acesso_bmc(secao)
        assert "Itens que teriam o Redfish como primeira fonte" in texto
        html = gerar_html({"bmc": secao, "medido": []})
        assert "Credenciais do BMC não informadas pelo técnico" in html

    def test_sem_redfish_sem_secao(self):
        assert resumo_acesso_bmc(Contexto(executor=ExecutorFalso()), []) is None


# --- Interface: credenciais tardias refazem só o Redfish ----------------------------------------


def sessao_com_bmc(servidor: ServidorRedfishFalso, monkeypatch) -> Sessao:
    from msscan.interface import sessao as mod

    original = mod.detectar

    def detectar(executor, pasta, pendrive=None):
        ctx = original(executor, pasta, pendrive)
        cliente = ClienteRedfish(executor, servidor.base)
        raiz = cliente.raiz()
        ctx.redfish.confirmado = True
        ctx.cache["redfish_raiz"] = raiz
        ctx.bmc = AcessoBMC(cliente, raiz["contas"])
        return ctx

    monkeypatch.setattr(mod, "detectar", detectar)
    sessao = Sessao(ExecutorReplayComBMC(R640), None, replay=True)
    sessao.iniciar()
    sessao.aguardar(30)
    return sessao


class TestSessaoUI:
    def test_pular_diagnosticar_e_informar_depois(self, bmc, monkeypatch, tmp_path):
        sessao = sessao_com_bmc(bmc, monkeypatch)
        sessao.pasta_dados = str(tmp_path)
        assert sessao.estado()["bmc"]["situacao"] == NAO_PERGUNTADO
        assert sessao.pular_credenciais()
        assert sessao.iniciar_diagnostico()
        sessao.aguardar(30)
        assert sessao.estado()["bmc"]["situacao"] == PULADO
        assert not sessao.relatorio["medido"][-1]["fonte"].startswith("Redfish")
        pasta = sessao.gravacao["pasta"]
        historico_antes = len(sessao.executor.historico)

        assert sessao.informar_credenciais("tecnico", SENHA)
        sessao.aguardar(30)
        estado = sessao.estado()
        assert estado["etapa"] == "concluido" and estado["bmc"]["situacao"] == VALIDADO
        fontes = {c["componente"]: c["fonte"] for c in estado["diagnostico"]["componentes"]}
        assert fontes["raid"] == "Redfish (Storage)" and fontes["cpu"].startswith("dmidecode")
        # Só os itens do Redfish rodaram de novo (nada de dmidecode/smartctl na segunda passada);
        # placas de rede: sem NetworkAdapters neste mockup, ip/ethtool (fallback e complemento).
        segunda = [e.comando[0] for e in sessao.executor.historico[historico_antes:]]
        assert set(segunda) <= {"GET", "POST", "ipmitool", "ip", "ethtool"} and "GET" in segunda
        assert sessao.gravacao["pasta"] == pasta  # relatório regravado na mesma pasta
        assert sessao.relatorio["bmc"]["situacao"] == VALIDADO
        gravado = "".join(p.read_text() for p in Path(pasta).iterdir())
        assert SENHA not in gravado and SENHA not in json.dumps(sessao.relatorio, default=str)
        sessao.encerrar()
        assert bmc.logouts == 1

    def test_limite_de_tentativas_na_interface(self, bmc, monkeypatch):
        sessao = sessao_com_bmc(bmc, monkeypatch)
        for _ in range(MAX_TENTATIVAS):
            assert sessao.informar_credenciais("tecnico", "errada")
            sessao.aguardar(30)
        assert sessao.estado()["bmc"]["situacao"] == ESGOTADO
        assert not sessao.informar_credenciais("tecnico", SENHA)
        assert len(bmc.logins) == 3

    def test_api_de_credenciais(self, bmc, monkeypatch):
        sessao = sessao_com_bmc(bmc, monkeypatch)
        srv = ServidorUI(sessao, porta=0)
        thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            def post(rota, dados, token=srv.token):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{srv.porta}/api/{rota}", data=json.dumps(dados).encode(), method="POST",
                    headers={"X-MSscan-Token": token, "Content-Type": "application/json"},
                )  # fmt: skip
                try:
                    with urllib.request.urlopen(req, timeout=10) as resposta:
                        return resposta.status
                except urllib.error.HTTPError as erro:
                    return erro.code

            assert post("bmc/credenciais", {"usuario": "tecnico", "senha": SENHA}, token="errado") == 403
            assert post("bmc/credenciais", {"usuario": "tecnico"}) == 400
            assert bmc.logins == []
            assert post("bmc/credenciais", {"usuario": "tecnico", "senha": SENHA}) == 200
            sessao.aguardar(30)
            assert sessao.estado()["bmc"]["situacao"] == VALIDADO
            assert SENHA not in json.dumps(sessao.estado())
            assert post("bmc/pular", {}) == 200  # depois de validado, pular não desfaz
            assert sessao.estado()["bmc"]["situacao"] == VALIDADO
        finally:
            srv.shutdown()
            srv.server_close()
            sessao.encerrar()


def test_interface_tem_campos_e_pular():
    html = com_textos((Path(__file__).parent.parent / "msscan/interface/estaticos/index.html").read_text())
    assert 'type="password" id="bmc-senha"' in html and 'id="bmc-pular"' in html and "Continuar sem credenciais" in html
    assert 'id="diag-bmc-informar"' in html and "Informar credenciais" in html
    js = com_textos((Path(__file__).parent.parent / "msscan/interface/estaticos/app.js").read_text())
    assert 'senhaCampo.value = ""' in js  # a senha sai do campo assim que é enviada


# --- Placas de rede (Redfish primeiro; sistema operacional como complemento) --------------------

TOWER = DMTF / "public_tower"


class TestPlacasRedeRedfish:
    def test_parsers_dmtf(self):
        from msscan.parsers.redfish import parse_adaptador_rede, parse_porta_rede

        adaptador = parse_adaptador_rede(corpo(TOWER, "/redfish/v1/Chassis/Tower/NetworkAdapters/NIC1"))
        assert (adaptador["modelo"], adaptador["firmware"]) == ("2PNIC 2000", "7.4.10")
        assert adaptador["portas"] == "/redfish/v1/Chassis/Tower/NetworkAdapters/NIC1/Ports"
        porta = parse_porta_rede(corpo(TOWER, "/redfish/v1/Chassis/Tower/NetworkAdapters/NIC1/Ports/1"))
        assert porta["velocidade_mbps"] == 10000 and porta["velocidade_maxima_mbps"] == 10000 and porta["link"] is None

    def test_porta_legada_networkport(self):
        from msscan.parsers.redfish import parse_porta_rede

        # Esquema NetworkPort (antigo): mesmos nomes de campo da especificação DMTF.
        porta = parse_porta_rede({"Id": "1", "LinkStatus": "Up", "CurrentLinkSpeedMbps": 1000,
                                  "SupportedLinkCapabilities": [{"LinkSpeedMbps": 1000}, {"LinkSpeedMbps": 10000}],
                                  "Status": {"Health": "OK", "State": "Enabled"}})  # fmt: skip
        assert (porta["link"], porta["velocidade_mbps"], porta["velocidade_maxima_mbps"]) == ("Up", 1000, 10000)

    def test_tower_sem_sistema(self, monkeypatch):
        ctx = contexto_replay(TOWER)
        from msscan.provedores import redfish as mod

        r = mod.ProvedorRedfishPlacasRede().coletar(ctx)  # replay sem ip: sem complemento
        assert r.status == Status.OK and r.fonte == "Redfish (NetworkAdapters)"
        assert r.mensagem == "1 placa(s), 2 porta(s) no BMC; 0 com link, 2 com link não informado pelo BMC"

    def test_sem_networkadapters_cai_para_o_sistema(self):
        ctx = contexto_replay(LOCALSTORAGE)
        from msscan.provedores.redfish import ProvedorRedfishPlacasRede

        assert ProvedorRedfishPlacasRede().coletar(ctx).status == Status.NAO_VERIFICADO

    def test_complemento_do_sistema_entra_no_resultado(self):
        from msscan.provedores.redfish import ProvedorRedfishPlacasRede

        ctx = contexto_replay(TOWER)
        # Mesmo contexto, com as saídas reais de ip/ethtool do R640 (replay) como complemento.
        replay_r640 = ExecutorReplay(R640)
        ctx.executor.tem_comando = replay_r640.tem_comando
        ctx.executor._rodar = replay_r640._rodar
        ctx.executor._listar = replay_r640._listar
        ctx.executor._ler_arquivo = replay_r640._ler_arquivo
        ctx.executor._existe = replay_r640._existe
        r = ProvedorRedfishPlacasRede().coletar(ctx)
        assert r.fonte == "Redfish (NetworkAdapters) + ip -s link + ethtool"
        assert "sistema" in r.dados and "; sistema: " in r.mensagem

    def test_avaliacao(self):
        from msscan.provedores.redfish import avaliar_adaptadores_rede

        def porta(**campos):
            return {"nome": "P1", "saude": "OK", "estado": "Enabled", "link": "LinkUp",
                    "velocidade_mbps": 10000, "velocidade_maxima_mbps": 10000, "meio": None, **campos}  # fmt: skip

        def adaptador(*portas, saude="OK"):
            return {"nome": "NIC", "fabricante": "Broadcom", "modelo": "57416", "saude": saude, "estado": "Enabled",
                    "portas_lidas": list(portas)}  # fmt: skip

        situacoes, problemas, obs, links = avaliar_adaptadores_rede([adaptador(porta(), porta(nome="P2", link="LinkDown", saude="Critical"))])
        assert situacoes == [Status.OK, Status.OK] and links["sem_link"] == 1 and "Broadcom 57416 / P2: sem link" in obs
        situacoes, problemas, _, _ = avaliar_adaptadores_rede([adaptador(porta(velocidade_mbps=1000, meio="Electrical"))])
        assert Status.ALERTA in situacoes and "cabo ruim" in problemas[0]
        _, problemas, obs, _ = avaliar_adaptadores_rede([adaptador(porta(velocidade_mbps=1000, meio="Optical"))])
        assert not problemas and "depende do módulo" in obs[0]
        situacoes, problemas, _, _ = avaliar_adaptadores_rede([adaptador(porta(saude="Critical"), saude="Warning")])
        assert Status.FALHA in situacoes and len(problemas) == 2

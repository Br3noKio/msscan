"""Fase 10: ações corretivas — trava, salvaguardas, parsers, pré-checagens, prévia, confirmação, simulação e aplicação.

Nenhum teste altera um BMC: o caminho real só roda contra o BMC falso deste arquivo (``alvo_simulado = True``).
Respostas: mockup oficial da DMTF (NTP do ``public-telemetry``, SEL do ``public-rackmount1``), MOCKUP(rodada final) no
formato da Dell (ver LEIAME da fixture) e a saída real do ``ipmitool sel elist`` do R640.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from msscan.acoes import execucao
from msscan.acoes.catalogo import CATALOGO, DISPONIVEL, recomendadas
from msscan.acoes.comum import CONFIRMACAO, MODO_APLICAR, MODO_SIMULAR
from msscan.acoes.ntp import SERVIDOR_PADRAO, servidores_de
from msscan.core import executor as modulo_executor
from msscan.core.executor import (
    LIMPAR_SEL,
    MOTIVO_TRAVA_ACOES,
    NTP_DELL,
    NTP_DMTF,
    ExecutorReal,
    RespostaRedfish,
    servidor_ntp_valido,
    validar_acao_bmc,
)
from msscan.deteccao.contexto import Contexto
from msscan.parsers.ipmitool import parse_sel_elist
from msscan.parsers.redfish import parse_gerenciador, parse_ntp_dell, parse_ntp_protocolo, parse_servico_log
from msscan.redfish.acesso import AcessoBMC
from msscan.redfish.cliente import ClienteRedfish
from msscan.relatorio.acoes import formatar_acoes, html_acoes
from tests.conftest import PASTA_FIXTURES, ExecutorFalso

BASE = "https://bmc.teste"
DELL = PASTA_FIXTURES / "redfish" / "dell_idrac9_mockup"
DMTF = json.loads((PASTA_FIXTURES / "redfish" / "dmtf" / "public_rackmount1" / "redfish.json").read_text())
NTP_TELEMETRIA = json.loads((PASTA_FIXTURES / "redfish" / "dmtf" / "public_telemetry_ntp" / "NetworkProtocol.json").read_text())
SEL_R640 = (PASTA_FIXTURES / "dell" / "poweredge_r640" / "comandos" / "ipmitool__sel__elist.stdout").read_text()
GERENTE = "/redfish/v1/Managers/iDRAC.Embedded.1"
ATRIBUTOS, PROTOCOLOS = GERENTE + "/Attributes", GERENTE + "/NetworkProtocol"
SEL, ENTRADAS = GERENTE + "/LogServices/Sel", GERENTE + "/LogServices/Sel/Entries"
LIMPAR = SEL + "/Actions/LogService.ClearLog"


def ler(nome: str):
    return json.loads((DELL / nome).read_text())


def corpo_dmtf(caminho: str):
    return DMTF[f"GET {caminho}"]["corpo"]


class BMCFalso(ExecutorFalso):
    """GETs do Redfish a partir das fixtures; registra as escritas e muda o estado como um BMC faria."""

    alvo_simulado = True

    def __init__(self, dell=True, com_ntp=True, com_sel=True, ipmi=False):
        comandos = {("ipmitool", "sel", "elist"): (0, SEL_R640, ""), ("ipmitool", "sel", "clear"): (0, "Clearing SEL.  Please allow a few seconds to erase.\n", "")} if ipmi else {}
        super().__init__(comandos=comandos)
        self.escritas: list[tuple[str, str, object]] = []
        gerente = {**corpo_dmtf("/redfish/v1/Managers/BMC"), "@odata.id": GERENTE, "Id": "iDRAC.Embedded.1",
                   "LogServices": {"@odata.id": GERENTE + "/LogServices"}}  # fmt: skip
        gerente["NetworkProtocol"] = {"@odata.id": PROTOCOLOS}
        self.get = {
            "/redfish/v1/Managers": {"Members": [{"@odata.id": GERENTE}]},
            GERENTE: gerente,
            GERENTE + "/LogServices": {"Members": [{"@odata.id": SEL}] if com_sel else []},
            SEL: ler("LogService_Sel.json"),
            ENTRADAS: corpo_dmtf("/redfish/v1/Managers/BMC/LogServices/Log/Entries"),
            PROTOCOLOS: NTP_TELEMETRIA if com_ntp else {"Id": "NetworkProtocol", "HTTPS": {"ProtocolEnabled": True}},
        }
        if dell:
            self.get[ATRIBUTOS] = ler("Attributes_ntp.json")
        sessao = BASE + "/redfish/v1/SessionService/Sessions"
        self.respostas_redfish[("POST", sessao)] = RespostaRedfish(sessao, 201, token="tok", local="/redfish/v1/SessionService/Sessions/1")

    def _redfish(self, metodo, url, token, corpo, timeout):
        caminho = url[len(BASE):]
        if metodo == "GET" and caminho in self.get:
            return RespostaRedfish(url, 200, corpo=json.loads(json.dumps(self.get[caminho])))
        return super()._redfish(metodo, url, token, corpo, timeout)

    def _acao_bmc(self, metodo, url, token, corpo, timeout):
        caminho = url[len(BASE):]
        self.escritas.append((metodo, caminho, corpo))
        if caminho == ATRIBUTOS:
            self.get[ATRIBUTOS]["Attributes"].update(corpo["Attributes"])
        elif caminho == PROTOCOLOS:
            self.get[PROTOCOLOS] = {**self.get[PROTOCOLOS], "NTP": corpo["NTP"]}
        elif caminho == LIMPAR:
            self.get[ENTRADAS] = {**self.get[ENTRADAS], "Members": [], "Members@odata.count": 0}
            return RespostaRedfish(url, 204)
        return RespostaRedfish(url, 200)


class BMCReal(BMCFalso):
    """Mesmo BMC como executor real: a trava das ações precisa segurar."""

    alvo_simulado = False


def contexto(executor, administrador=True, fabricante="Dell Inc.", credenciais=True, ipmi=False):
    ctx = Contexto(executor=executor, fabricante=fabricante, modelo="PowerEdge R640")
    ctx.bmc = AcessoBMC(ClienteRedfish(executor, BASE))
    if credenciais:
        ctx.bmc.tentar("tecnico", "senha")
        ctx.bmc.administrador = administrador
    ctx.ipmi.disponivel = ipmi
    return ctx


# --- Trava e salvaguardas ---------------------------------------------------------------------------


class TestTrava:
    def test_trava_liberada_no_codigo_e_desligada_nos_testes(self):
        """Liberada em 30/09/2026 por ordem do usuário (15.6); nos testes, sempre forçada a False (conftest)."""
        assert modulo_executor.ACOES_REAIS_LIBERADAS is False
        fonte = Path(modulo_executor.__file__).read_text(encoding="utf-8")
        assert re.search(r"^ACOES_REAIS_LIBERADAS = True$", fonte, re.M)

    def test_executor_real_nunca_altera(self, monkeypatch):
        import subprocess

        import requests

        monkeypatch.setattr(requests.Session, "request", lambda *a, **k: pytest.fail("chegou à rede"))
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("rodaria o comando"))
        executor = ExecutorReal()
        for operacao, caminho, corpo in (
            (NTP_DELL, ATRIBUTOS, {"Attributes": {"NTPConfigGroup.1.NTPEnable": "Enabled", "NTPConfigGroup.1.NTP1": "pool.ntp.org"}}),
            (NTP_DMTF, PROTOCOLOS, {"NTP": {"ProtocolEnabled": True, "NTPServers": ["pool.ntp.org"]}}),
            (LIMPAR_SEL, LIMPAR, {}),
        ):
            resposta = executor.acao_bmc(operacao, BASE + caminho, "tok", corpo)
            assert resposta.erro_tipo == "trava" and resposta.erro == MOTIVO_TRAVA_ACOES and resposta.status is None
        assert executor.acao_comando(["ipmitool", "sel", "clear"]) == (-1, "", MOTIVO_TRAVA_ACOES)
        assert "tok" not in json.dumps(executor.historico_dict())

    def test_marcar_executor_real_como_simulado_nao_libera(self):
        class Disfarcado(ExecutorReal):
            alvo_simulado = True

        assert not Disfarcado().acoes_permitidas()
        assert BMCFalso().acoes_permitidas() and not BMCReal().acoes_permitidas()

    def test_trava_do_firmware_independente(self):
        assert modulo_executor.GRAVACAO_REAL_LIBERADA is False and not ExecutorReal().escrita_permitida()

    @pytest.mark.parametrize(("operacao", "caminho", "corpo", "trecho"), [
        (NTP_DELL, ATRIBUTOS, {"Attributes": {"NTPConfigGroup.1.NTPEnable": "Enabled", "NTPConfigGroup.1.NTP1": "a.b", "IPv4.1.Address": "1.2.3.4"}}, "NTPConfigGroup.1"),
        (NTP_DELL, ATRIBUTOS, {"Attributes": {"NTPConfigGroup.1.NTPEnable": "Disabled", "NTPConfigGroup.1.NTP1": "a.b"}}, "Enabled"),
        (NTP_DELL, ATRIBUTOS, {"Attributes": {"NTPConfigGroup.1.NTPEnable": "Enabled"}}, "inválido"),
        (NTP_DELL, ATRIBUTOS, {"Attributes": {"NTPConfigGroup.1.NTPEnable": "Enabled", "NTPConfigGroup.1.NTP1": "http://x"}}, "inválido"),
        (NTP_DELL, "/redfish/v1/Systems/1/Bios/Settings", {"Attributes": {"NTPConfigGroup.1.NTPEnable": "Enabled"}}, "Attributes"),
        (NTP_DELL, ATRIBUTOS, {"Attributes": {"NTPConfigGroup.1.NTPEnable": "Enabled", "NTPConfigGroup.1.NTP1": "a.b"}, "Extra": 1}, "NTPConfigGroup.1"),
        (NTP_DMTF, PROTOCOLOS, {"NTP": {"ProtocolEnabled": True, "NTPServers": ["a.b"]}, "IPMI": {"ProtocolEnabled": False}}, "NTP"),
        (NTP_DMTF, PROTOCOLOS, {"NTP": {"ProtocolEnabled": False, "NTPServers": ["a.b"]}}, "ProtocolEnabled"),
        (NTP_DMTF, PROTOCOLOS, {"NTP": {"ProtocolEnabled": True, "NTPServers": ["a", "b", "c", "d"]}}, "1 a 3"),
        (NTP_DMTF, PROTOCOLOS, {"NTP": {"ProtocolEnabled": True, "NTPServers": ["a.b; rm"]}}, "1 a 3"),
        (NTP_DMTF, "/redfish/v1/AccountService", {"NTP": {"ProtocolEnabled": True, "NTPServers": ["a.b"]}}, "NetworkProtocol"),
        (LIMPAR_SEL, GERENTE + "/LogServices/Lclog/Actions/LogService.ClearLog", {}, "SEL"),
        (LIMPAR_SEL, "/redfish/v1/Systems/1/LogServices/Sel/Actions/LogService.ClearLog", {}, "SEL"),
        (LIMPAR_SEL, LIMPAR, {"OverWritePolicy": "Never"}, "vazio"),
        ("resetar_bmc", GERENTE + "/Actions/Manager.Reset", {}, "não permitida"),
    ])  # fmt: skip
    def test_salvaguardas(self, operacao, caminho, corpo, trecho):
        assert trecho in validar_acao_bmc(operacao, BASE + caminho, corpo)
        executor = BMCFalso()
        assert executor.acao_bmc(operacao, BASE + caminho, "tok", corpo).erro_tipo == "bloqueado"
        assert executor.escritas == []

    def test_so_https(self):
        assert "HTTPS" in validar_acao_bmc(LIMPAR_SEL, "http://bmc" + LIMPAR, {})

    def test_acao_comando_so_sel_clear(self):
        executor = BMCFalso(ipmi=True)
        for comando in (["ipmitool", "raw", "0x0a", "0x47"], ["ipmitool", "sel", "delete", "1"], ["sh", "-c", "ipmitool sel clear"]):
            rc, _, erro = executor.acao_comando(comando)
            assert rc == -1 and "não permitido" in erro

    @pytest.mark.parametrize("comando", [
        ["ipmitool", "sel", "clear"], ["ipmitool", "raw", "0x06", "0x02"], ["ipmitool", "lan", "set", "1", "ipaddr", "1.2.3.4"],
        ["ipmitool", "mc", "reset", "cold"], ["ipmitool", "chassis", "power", "off"], ["/usr/bin/ipmitool", "SEL", "CLEAR"],
    ])  # fmt: skip
    def test_rodar_recusa_escrita_do_ipmitool(self, comando, monkeypatch):
        import subprocess

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("rodaria o comando"))
        rc, _, erro = ExecutorReal().rodar(comando)
        assert rc == -1 and "recusado" in erro

    def test_rodar_mantem_leituras_do_ipmitool(self):
        executor = ExecutorFalso(comandos={("ipmitool", "sdr", "type", "Power Supply"): (0, "ok", ""), ("ipmitool", "lan", "print", "1"): (0, "ok", "")})
        assert executor.rodar(["ipmitool", "sdr", "type", "Power Supply"])[0] == 0
        assert executor.rodar(["ipmitool", "lan", "print", "1"])[0] == 0

    @pytest.mark.parametrize(("texto", "valido"), [
        ("pool.ntp.org", True), ("10.0.0.1", True), ("2001:db8::1", True), ("ntp", True),
        ("http://pool.ntp.org", False), ("pool.ntp.org:123", False), ("a b", False), ("", False), ("-x.org", False), (None, False),
    ])  # fmt: skip
    def test_servidor_ntp(self, texto, valido):
        assert servidor_ntp_valido(texto) is valido


# --- Parsers -------------------------------------------------------------------------------------


class TestParsers:
    def test_ntp_dmtf(self):
        ntp = parse_ntp_protocolo(NTP_TELEMETRIA)
        assert {k: ntp[k] for k in ("ativo", "servidores")} == {"ativo": True, "servidores": ["0.pool.ntp.org", "1.pool.ntp.org", "time-a.nist.gov"]}
        assert ntp["bruto"] == {"ProtocolEnabled": True, "NTPServers": NTP_TELEMETRIA["NTP"]["NTPServers"]}

    def test_ntp_dmtf_ausente(self):
        assert parse_ntp_protocolo({"Id": "NetworkProtocol", "HTTP": {}}) is None

    def test_ntp_dell(self):
        assert parse_ntp_dell(ler("Attributes_ntp.json")) == {"ativo": False, "servidores": [], "bruto": {
            "NTPConfigGroup.1.NTPEnable": "Disabled", "NTPConfigGroup.1.NTP1": "", "NTPConfigGroup.1.NTP2": "", "NTPConfigGroup.1.NTP3": ""}}
        assert parse_ntp_dell({"Attributes": {"NTPConfigGroup.1.NTPEnable": "Enabled", "NTPConfigGroup.1.NTP2": " a.b "}}) == {
            "ativo": True, "servidores": ["a.b"], "bruto": {"NTPConfigGroup.1.NTPEnable": "Enabled", "NTPConfigGroup.1.NTP2": " a.b "}}
        assert parse_ntp_dell({"Attributes": {"IPv4.1.Enable": "Enabled"}}) is None

    def test_gerenciador_dmtf(self):
        gerente = parse_gerenciador(corpo_dmtf("/redfish/v1/Managers/BMC"))
        assert gerente["data_hora"] == "2015-03-13T04:14:33+06:00" and gerente["protocolos"] == "/redfish/v1/Managers/BMC/NetworkProtocol"
        assert gerente["logs"] == "/redfish/v1/Managers/BMC/LogServices"

    def test_servico_log_com_clearlog(self):
        assert parse_servico_log(ler("LogService_Sel.json"))["limpar"] == LIMPAR
        dmtf = parse_servico_log(corpo_dmtf("/redfish/v1/Managers/BMC/LogServices/Log"))
        assert dmtf["limpar"] == "/redfish/v1/Managers/BMC/LogServices/Log/Actions/LogService.ClearLog"


# --- Catálogo e IA -------------------------------------------------------------------------------


class TestCatalogo:
    def test_acoes_disponiveis(self):
        # 18.4: pass-through e usuário temporário só pela atualização automática (temporários). Rede do iDRAC: só a
        # exceção AVULSA pedida pelo usuário em 05/10/2026 (21.5), idrac.ip_fixo com CORRIGIR.
        assert set(CATALOGO) == {"bmc.relogio_ntp", "bmc.sel_exportar_limpar", "idrac.ver", "idrac.ip_fixo"}
        assert all(a["situacao"] == DISPONIVEL and a["modulo"] in ("ntp", "sel", "idrac") for a in CATALOGO.values())

    def test_recomendadas_so_do_catalogo(self):
        relatorio = {"interpretacao_ia": {"problemas": [
            {"titulo": "Relógio do BMC errado", "plano": {"acao_catalogo": "bmc.relogio_ntp"}},
            {"titulo": "SEL cheio", "plano": {"acao_catalogo": "bmc.sel_exportar_limpar"}},
            {"titulo": "Outra", "plano": {"acao_catalogo": None, "acao_ignorada": "bmc.formatar"}},
        ]}}  # fmt: skip
        assert recomendadas(relatorio) == {"bmc.relogio_ntp": ["Relógio do BMC errado"], "bmc.sel_exportar_limpar": ["SEL cheio"]}
        assert recomendadas({}) == {} and recomendadas(None) == {}

    def test_identificador_inexistente(self):
        pre = execucao.prechecar("bmc.formatar", contexto(BMCFalso()), BMCFalso())
        assert not pre["pode_seguir"] and "não existe" in pre["bloqueios"][0]


# --- NTP -----------------------------------------------------------------------------------------


class TestNTP:
    def test_servidores(self):
        assert servidores_de("") == ([SERVIDOR_PADRAO], "")
        assert servidores_de("a.br, b.br a.br") == (["a.br", "b.br"], "")
        assert "no máximo" in servidores_de("a b c d")[1]
        assert "inválido" in servidores_de("http://a.br")[1]

    def test_prechecagem_dell(self):
        executor = BMCFalso()
        pre = execucao.prechecar("bmc.relogio_ntp", contexto(executor), executor, {"servidores": "a.ntp.br b.ntp.br"}, persistente=True)
        assert pre["pode_seguir"] and pre["metodo"] == "dell" and pre["alvo"] == ATRIBUTOS
        assert pre["corpo"] == {"Attributes": {"NTPConfigGroup.1.NTPEnable": "Enabled", "NTPConfigGroup.1.NTP1": "a.ntp.br",
                                               "NTPConfigGroup.1.NTP2": "b.ntp.br"}}  # fmt: skip
        assert pre["antes"]["ntp_ligado"] is False and pre["antes"]["data_hora_bmc"] == "2015-03-13T04:14:33+06:00"
        assert pre["antes"]["diferenca_s"] < 0 and any("atrasado" in a for a in pre["avisos"])
        assert "desligado → ligado" in pre["previa"][0] and "(nenhum) → a.ntp.br, b.ntp.br" in pre["previa"][1]
        assert pre["previa"][2].startswith(f"Pedido: PATCH {ATRIBUTOS}")
        assert validar_acao_bmc(NTP_DELL, BASE + pre["alvo"], pre["corpo"]) == ""
        assert executor.escritas == []

    def test_prechecagem_dmtf_sem_dell(self):
        executor = BMCFalso(dell=False)
        pre = execucao.prechecar("bmc.relogio_ntp", contexto(executor, fabricante="Contoso"), executor, {}, persistente=True)
        assert pre["metodo"] == "dmtf" and pre["alvo"] == PROTOCOLOS and pre["parametros"]["servidores"] == [SERVIDOR_PADRAO]
        assert pre["corpo"] == {"NTP": {"ProtocolEnabled": True, "NTPServers": [SERVIDOR_PADRAO]}}
        assert validar_acao_bmc(NTP_DMTF, BASE + pre["alvo"], pre["corpo"]) == ""

    def test_ja_configurado(self):
        executor = BMCFalso(dell=False)
        pre = execucao.prechecar("bmc.relogio_ntp", contexto(executor, fabricante="Contoso"), executor,
                                 {"servidores": ["0.pool.ntp.org", "1.pool.ntp.org"]}, persistente=True)  # fmt: skip
        assert pre["nada_a_fazer"] and not pre["pode_seguir"]

    def test_sem_ntp_no_bmc(self):
        executor = BMCFalso(dell=False, com_ntp=False)
        pre = execucao.prechecar("bmc.relogio_ntp", contexto(executor, fabricante="Contoso"), executor, {}, persistente=True)
        assert not pre["pode_seguir"] and "não publica" in pre["bloqueios"][0]

    def test_sem_credenciais(self):
        executor = BMCFalso()
        pre = execucao.prechecar("bmc.relogio_ntp", contexto(executor, credenciais=False), executor, {}, persistente=True)
        assert not pre["pode_seguir"] and "credenciais" in pre["bloqueios"][0]

    def test_servidor_invalido_bloqueia(self):
        executor = BMCFalso()
        pre = execucao.prechecar("bmc.relogio_ntp", contexto(executor), executor, {"servidores": "ntp.br:123"}, persistente=True)
        assert not pre["pode_seguir"] and "inválido" in pre["bloqueios"][0]

    def test_simulacao_nao_escreve(self, tmp_path):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.relogio_ntp", ctx, executor, {}, persistente=True)
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path))
        assert resultado["situacao"] == "simulada" and resultado["modo"] == MODO_SIMULAR and executor.escritas == []
        assert resultado["passos"][-1]["situacao"] == "simulado" and resultado["depois"] is None
        assert resultado["passos"][0]["acao"] == "guardar valores atuais" and not resultado["desfazer"]["disponivel"]
        assert Path(resultado["desfazer"]["arquivo"]).is_file() and "simulação" in resultado["desfazer"]["motivo"]

    @pytest.mark.parametrize("confirmacao", ["", "corrigir", "ATUALIZAR", "CORRIGIR!"])
    def test_confirmacao_errada(self, confirmacao, tmp_path):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.relogio_ntp", ctx, executor, {}, persistente=True)
        resultado = execucao.executar(pre, confirmacao, ctx, executor, str(tmp_path), MODO_APLICAR)
        assert resultado["situacao"] == "cancelada" and executor.escritas == []

    def test_aplicar_dell_antes_depois(self, tmp_path):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.relogio_ntp", ctx, executor, {"servidores": "a.ntp.br"}, persistente=True)
        resultado = execucao.executar(pre, "  CORRIGIR ", ctx, executor, str(tmp_path), MODO_APLICAR)
        assert resultado["situacao"] == "aplicada"
        assert executor.escritas == [("PATCH", ATRIBUTOS, pre["corpo"])]
        assert resultado["antes"]["ntp_ligado"] is False and resultado["depois"]["ntp_ligado"] is True
        assert resultado["depois"]["servidores"] == ["a.ntp.br"]

    def test_aplicar_dmtf(self, tmp_path):
        executor = BMCFalso(dell=False, com_ntp=True)
        executor.get[PROTOCOLOS] = {**NTP_TELEMETRIA, "NTP": {"ProtocolEnabled": False, "NTPServers": []}}
        ctx = contexto(executor, fabricante="Contoso")
        pre = execucao.prechecar("bmc.relogio_ntp", ctx, executor, {}, persistente=True)
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path), MODO_APLICAR)
        assert resultado["situacao"] == "aplicada" and executor.escritas[0][:2] == ("PATCH", PROTOCOLOS)

    def test_aplicar_em_executor_real_para_na_trava(self, tmp_path):
        executor = BMCReal()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.relogio_ntp", ctx, executor, {}, persistente=True)
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path), MODO_APLICAR)
        assert resultado["situacao"] == "bloqueada_trava" and resultado["mensagem"].startswith(MOTIVO_TRAVA_ACOES)
        assert executor.escritas == []

    def test_somente_leitura_pede_administrador(self, tmp_path):
        executor = BMCFalso()
        ctx = contexto(executor, administrador=False)
        pre = execucao.prechecar("bmc.relogio_ntp", ctx, executor, {}, persistente=True)
        pedidos = []
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path), MODO_APLICAR, pedir_administrador=lambda: pedidos.append(1) or False)
        assert pedidos == [1] and resultado["situacao"] == "so_informativo" and executor.escritas == []


# --- SEL -----------------------------------------------------------------------------------------


class TestSEL:
    def test_prechecagem_redfish(self):
        executor = BMCFalso()
        pre = execucao.prechecar("bmc.sel_exportar_limpar", contexto(executor), executor, persistente=True)
        quantidade = len(corpo_dmtf("/redfish/v1/Managers/BMC/LogServices/Log/Entries")["Members"])
        assert pre["pode_seguir"] and pre["metodo"] == "redfish" and pre["alvo"] == LIMPAR
        assert pre["antes"] == {"eventos": quantidade} and quantidade > 0
        assert any(f"POST {LIMPAR} {{}}" in p for p in pre["previa"]) and executor.escritas == []

    def test_sem_pendrive_gravavel_bloqueia(self):
        executor = BMCFalso()
        pre = execucao.prechecar("bmc.sel_exportar_limpar", contexto(executor), executor, persistente=False)
        assert not pre["pode_seguir"] and "pendrive" in pre["bloqueios"][0]

    def test_prechecagem_ipmi_sem_credenciais(self):
        executor = BMCFalso(ipmi=True)
        pre = execucao.prechecar("bmc.sel_exportar_limpar", contexto(executor, credenciais=False, ipmi=True), executor, persistente=True)
        assert pre["metodo"] == "ipmi" and pre["antes"]["eventos"] == len(parse_sel_elist(SEL_R640)) > 0
        assert any("ipmitool sel clear" in p for p in pre["previa"])

    def test_sem_metodo(self):
        executor = BMCFalso(com_sel=False)
        pre = execucao.prechecar("bmc.sel_exportar_limpar", contexto(executor), executor, persistente=True)
        assert not pre["pode_seguir"] and "IPMI" in pre["bloqueios"][0]

    def test_sel_vazio(self):
        executor = BMCFalso()
        executor.get[ENTRADAS] = {**executor.get[ENTRADAS], "Members": []}
        pre = execucao.prechecar("bmc.sel_exportar_limpar", contexto(executor), executor, persistente=True)
        assert pre["nada_a_fazer"] and not pre["pode_seguir"]

    def test_simulacao_exporta_e_nao_limpa(self, tmp_path):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.sel_exportar_limpar", ctx, executor, persistente=True)
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path))
        assert resultado["situacao"] == "simulada" and executor.escritas == []
        arquivo = Path(resultado["exportacao"]["arquivo"])
        assert arquivo.parent == tmp_path / "acoes" and json.loads(arquivo.read_text())["quantidade"] == pre["antes"]["eventos"]

    def test_aplicar_redfish(self, tmp_path):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.sel_exportar_limpar", ctx, executor, persistente=True)
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path), MODO_APLICAR)
        assert resultado["situacao"] == "aplicada" and executor.escritas == [("POST", LIMPAR, {})]
        assert resultado["depois"] == {"eventos": 0} and resultado["exportacao"]["eventos"] == pre["antes"]["eventos"]

    def test_exportacao_falha_nao_limpa(self, tmp_path):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.sel_exportar_limpar", ctx, executor, persistente=True)
        bloqueado = tmp_path / "somente_leitura"
        bloqueado.write_text("é um arquivo, não pasta")
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(bloqueado), MODO_APLICAR)
        assert resultado["situacao"] == "falhou" and "NÃO foi limpo" in resultado["mensagem"] and executor.escritas == []

    def test_aplicar_ipmi(self, tmp_path):
        executor = BMCFalso(ipmi=True)
        ctx = contexto(executor, credenciais=False, ipmi=True)
        pre = execucao.prechecar("bmc.sel_exportar_limpar", ctx, executor, persistente=True)
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path), MODO_APLICAR)
        assert ["ipmitool", "sel", "clear"] in executor.comandos_rodados()
        exportado = json.loads(Path(resultado["exportacao"]["arquivo"]).read_text())
        assert exportado["ipmitool_sel_elist"] == SEL_R640
        # O BMC falso devolve o mesmo SEL depois de limpar: sem confirmação na releitura.
        assert resultado["situacao"] == "aplicada_sem_confirmacao"

    def test_ipmi_em_executor_real_para_na_trava(self, tmp_path):
        executor = BMCReal(ipmi=True)
        ctx = contexto(executor, credenciais=False, ipmi=True)
        pre = execucao.prechecar("bmc.sel_exportar_limpar", ctx, executor, persistente=True)
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path), MODO_APLICAR)
        assert resultado["situacao"] == "bloqueada_trava"
        assert ["ipmitool", "sel", "clear"] not in [e.comando for e in executor.historico if e.rc == 0]


# --- Relatório -----------------------------------------------------------------------------------


class TestRelatorio:
    def test_texto_e_html(self, tmp_path):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.relogio_ntp", ctx, executor, {"servidores": "a.ntp.br"}, persistente=True)
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path), MODO_APLICAR)
        texto = formatar_acoes([resultado])
        assert "Ações corretivas" in texto and "NTP ligado: não -> sim" in texto and "Servidores NTP: (nenhum) -> a.ntp.br" in texto
        html = html_acoes([resultado])
        assert "<h2>Ações corretivas</h2>" in html and "Prévia" in html and "a.ntp.br" in html
        assert formatar_acoes([]) == "" and html_acoes(None) == ""

    def test_simulacao_mostra_depois_vazio(self, tmp_path):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.relogio_ntp", ctx, executor, {}, persistente=True)
        texto = formatar_acoes([execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path))])
        assert "simulação (nada foi alterado no controlador de gerenciamento)" in texto and "NTP ligado: não -> —" in texto


# --- Modo texto (main.py) ------------------------------------------------------------------------


class TestModoTexto:
    def rodar(self, tmp_path, respostas, executor=None, aplicar=False, relatorio=None):
        import argparse

        from main import etapa_acoes
        from msscan.pendrive.local import local_manual
        from msscan.relatorio.completo import Gravacao

        executor = executor or BMCFalso()
        ctx = contexto(executor)
        relatorio = relatorio if relatorio is not None else {}
        fila = iter(respostas)
        texto = etapa_acoes(argparse.Namespace(replay=None, aplicar=aplicar, simular=not aplicar), executor, ctx, relatorio, "t",
                            local_manual(str(tmp_path)), Gravacao(None, False, ""), entrada=lambda _: next(fila))  # fmt: skip
        return executor, relatorio, texto

    def test_padrao_nao(self, tmp_path):
        executor, relatorio, texto = self.rodar(tmp_path, [""])
        assert "acoes" not in relatorio and texto == "t" and executor.escritas == []

    def test_nenhuma_e_invalida(self, tmp_path, capsys):
        _, relatorio, _ = self.rodar(tmp_path, ["s", ""])
        assert "acoes" not in relatorio and "Nenhuma ação escolhida" in capsys.readouterr().out
        _, relatorio, _ = self.rodar(tmp_path, ["s", "1,2"])
        assert "acoes" not in relatorio and "uma ação por vez" in capsys.readouterr().out

    def test_confirmacao_errada(self, tmp_path, capsys):
        executor, relatorio, _ = self.rodar(tmp_path, ["s", "1", "", "corrigir"], aplicar=True)
        assert "acoes" not in relatorio and executor.escritas == [] and "Nada foi feito" in capsys.readouterr().out

    def test_simulacao_ntp(self, tmp_path, capsys):
        executor, relatorio, texto = self.rodar(tmp_path, ["s", "1", "a.ntp.br", CONFIRMACAO, ""])
        assert relatorio["acoes"][0]["situacao"] == "simulada" and executor.escritas == []
        saida = capsys.readouterr().out
        assert "nenhuma vem escolhida" in saida and "O que será alterado" in saida and "Ações corretivas" in texto

    def test_recomendacao_da_ia_aparece(self, tmp_path, capsys):
        relatorio = {"interpretacao_ia": {"problemas": [{"titulo": "SEL cheio", "plano": {"acao_catalogo": "bmc.sel_exportar_limpar"}}]}}
        self.rodar(tmp_path, ["s", ""], relatorio=relatorio)
        assert "RECOMENDADA pela IA (SEL cheio)" in capsys.readouterr().out

    def test_aplicar_avisa_a_trava(self, tmp_path, capsys):
        executor, relatorio, _ = self.rodar(tmp_path, ["s", "2", CONFIRMACAO], executor=BMCReal(), aplicar=True)
        saida = capsys.readouterr().out
        assert "ACOES_REAIS_LIBERADAS" in saida and relatorio["acoes"][0]["situacao"] == "bloqueada_trava"
        assert Path(relatorio["acoes"][0]["exportacao"]["arquivo"]).is_file()


# --- Interface: sessão e API ---------------------------------------------------------------------


def sessao_pronta(executor, pasta, modo=MODO_SIMULAR):
    from msscan.interface.sessao import CONCLUIDO, Sessao
    from msscan.pendrive.local import local_manual

    sessao = Sessao(executor, str(pasta), replay=True, local=local_manual(str(pasta)), modo_aplicacao=modo)
    sessao.iniciar()
    sessao.aguardar(30)
    assert sessao.iniciar_diagnostico()
    sessao.aguardar(30)
    assert sessao.etapa == CONCLUIDO
    sessao.ctx.bmc = contexto(executor).bmc
    return sessao


class TestInterface:
    def test_estado_inicial(self, tmp_path):
        sessao = sessao_pronta(BMCFalso(), tmp_path)
        estado = sessao.estado()["acoes"]
        assert estado["disponivel"] and estado["modo"] == "simular" and estado["prechecagem"] is None
        assert [a["id"] for a in estado["catalogo"]] == list(CATALOGO) and estado["executadas"] == []

    def test_execucao_exige_prechecagem_e_confirmacao(self, tmp_path):
        executor = BMCFalso()
        sessao = sessao_pronta(executor, tmp_path)
        ok, motivo = sessao.executar_acao("bmc.sel_exportar_limpar", CONFIRMACAO)
        assert not ok and "verificação prévia" in motivo
        assert sessao.prechecar_acao("bmc.formatar", {})[0] is False
        assert sessao.prechecar_acao("bmc.sel_exportar_limpar", {}) == (True, "iniciado")
        sessao.aguardar(30)
        assert sessao.estado()["acoes"]["prechecagem"]["pode_seguir"]
        assert sessao.executar_acao("bmc.relogio_ntp", CONFIRMACAO)[0] is False  # pré-checagem é de outra ação
        ok, motivo = sessao.executar_acao("bmc.sel_exportar_limpar", "corrigir")
        assert not ok and "CORRIGIR" in motivo
        assert sessao.executar_acao("bmc.sel_exportar_limpar", CONFIRMACAO) == (True, "iniciado")
        sessao.aguardar(30)
        estado = sessao.estado()["acoes"]
        assert estado["situacao"] == "simulada" and estado["executadas"][0]["acao"] == "bmc.sel_exportar_limpar"
        assert executor.escritas == [] and sessao.relatorio["acoes"][0]["situacao"] == "simulada"
        assert sessao.executar_acao("bmc.sel_exportar_limpar", CONFIRMACAO)[0] is False  # uma execução por pré-checagem

    def test_modo_aplicar_para_na_trava(self, tmp_path):
        executor = BMCReal()
        sessao = sessao_pronta(executor, tmp_path, MODO_APLICAR)
        assert sessao.estado()["acoes"]["trava"] == MOTIVO_TRAVA_ACOES
        sessao.prechecar_acao("bmc.relogio_ntp", {"servidores": "a.ntp.br"})
        sessao.aguardar(30)
        assert sessao.executar_acao("bmc.relogio_ntp", CONFIRMACAO)[0]
        sessao.aguardar(30)
        assert sessao.acao["situacao"] == "bloqueada_trava" and executor.escritas == []

    def test_sem_simulacao_na_interface(self, tmp_path):
        """18.2: a interface não tem mais "Simular sem gravar": a API não aceita o campo e a ação vai à trava."""
        executor = BMCReal()
        sessao = sessao_pronta(executor, tmp_path, MODO_APLICAR)
        sessao.prechecar_acao("bmc.relogio_ntp", {"servidores": "a.ntp.br"})
        sessao.aguardar(30)
        with pytest.raises(TypeError):
            sessao.executar_acao("bmc.relogio_ntp", CONFIRMACAO, simular=True)  # type: ignore[call-arg]
        assert sessao.executar_acao("bmc.relogio_ntp", CONFIRMACAO)[0]
        sessao.aguardar(30)
        assert sessao.acao["situacao"] == "bloqueada_trava" and executor.escritas == []

    def test_api(self, tmp_path):
        import threading
        import urllib.error
        import urllib.request

        from msscan.interface.servidor import ServidorUI

        sessao = sessao_pronta(BMCFalso(), tmp_path)
        srv = ServidorUI(sessao, porta=0)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            def post(rota: str, corpo: bytes) -> tuple[int, str]:
                req = urllib.request.Request(f"http://127.0.0.1:{srv.porta}/api/acoes/{rota}", data=corpo, method="POST",
                                             headers={"X-MSscan-Token": srv.token})  # fmt: skip
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        return r.status, r.read().decode()
                except urllib.error.HTTPError as erro:
                    return erro.code, erro.read().decode()

            assert post("prechecar", b'{"id": 1}')[0] == 400
            assert post("prechecar", b'{"id": "bmc.relogio_ntp", "parametros": {"servidores": ["a"]}}')[0] == 400
            assert post("prechecar", b'{"id": "bmc.formatar"}')[0] == 409
            codigo, corpo = post("executar", b'{"id": "bmc.relogio_ntp", "confirmacao": "CORRIGIR"}')
            assert codigo == 409 and "verificação prévia" in corpo
            assert post("prechecar", b'{"id": "bmc.relogio_ntp", "parametros": {"servidores": "a.ntp.br"}}')[0] == 200
        finally:
            srv.shutdown()
            srv.server_close()

    def test_frontend_regras(self):
        from tests.test_interface import ESTATICOS_TEXTO

        html, js = ESTATICOS_TEXTO["index.html"], ESTATICOS_TEXTO["app.js"]
        assert 'id="acoes-nao" value="nao" checked' in html and 'id="acoes-sim" value="sim" disabled>' in html
        assert '$("acoes-confirmacao").value.trim() !== "CORRIGIR"' in js  # botão só com CORRIGIR digitado
        criacao = js.split('name: "acoes-escolha"')[1].split("}),")[0]
        assert "checked: a.id === escolhida" in criacao  # só preserva a escolha do técnico; nada vem marcado
        assert not re.search(r"acoes-escolha[^\n]*\.checked\s*=\s*true", js)
        assert 'class="botao botao-perigo" id="acoes-executar" disabled' in html


# --- Ajustes de 28/09/2026: cópia conferida, NTP do DHCP, Desfazer, relatório -------------------

NMCLI_R640 = (PASTA_FIXTURES / "dell" / "poweredge_r640" / "comandos" / "nmcli__-t__-f__DHCP4__device__show__eno3.stdout").read_text()


class TestDHCP:
    def test_parser_saida_real_sem_ntp(self):
        from msscan.parsers.nmcli import parse_dhcp4_opcoes, servidores_ntp_dhcp

        opcoes = parse_dhcp4_opcoes(NMCLI_R640)
        assert opcoes["requested_ntp_servers"] == "1" and opcoes["routers"] == "192.0.2.1"
        assert servidores_ntp_dhcp(opcoes) == []

    def test_parser_com_ntp(self):
        from msscan.parsers.nmcli import parse_dhcp4_opcoes, servidores_ntp_dhcp

        texto = NMCLI_R640 + "DHCP4.OPTION[28]:ntp_servers = 192.0.2.10 192.0.2.11 192.0.2.10\n"
        assert servidores_ntp_dhcp(parse_dhcp4_opcoes(texto)) == ["192.0.2.10", "192.0.2.11"]
        assert parse_dhcp4_opcoes("lixo\nDHCP4.OPTION:x = 1") == {}

    def _replay(self, ntp: str = ""):
        from msscan.core.executor import ExecutorReplay
        from tests.conftest import R640

        executor = ExecutorReplay(str(R640))
        if ntp:
            original = executor._rodar

            def rodar(comando, timeout):
                if comando[:2] == ["nmcli", "-t"]:
                    return 0, NMCLI_R640 + f"DHCP4.OPTION[28]:ntp_servers = {ntp}\n", "", 0.0
                return original(comando, timeout)

            executor._rodar = rodar
        return executor

    def test_sugestoes_sem_ntp_no_dhcp(self):
        sugestoes = execucao.sugestoes_ntp(self._replay())
        assert sugestoes == [{"servidor": SERVIDOR_PADRAO, "origem": "público (NTP Pool Project)"}]

    def test_sugestoes_com_ntp_do_dhcp(self):
        executor = self._replay("192.0.2.10 ntp.cliente.example bad/host")
        sugestoes = execucao.sugestoes_ntp(executor)
        assert [s["servidor"] for s in sugestoes] == ["192.0.2.10", "ntp.cliente.example", SERVIDOR_PADRAO]
        assert "DHCP da rede do cliente (eno3)" in sugestoes[0]["origem"]
        assert ["nmcli", "-t", "-f", "DHCP4", "device", "show", "eno3"] in [e.comando for e in executor.historico]

    def test_campo_vazio_usa_a_primeira_sugestao(self):
        executor = BMCFalso()
        sugeridos = [{"servidor": "192.0.2.10", "origem": "DHCP"}, {"servidor": SERVIDOR_PADRAO, "origem": "público"}]
        pre = execucao.prechecar("bmc.relogio_ntp", contexto(executor), executor, {"servidores": ""}, persistente=True, sugeridos=sugeridos)
        assert pre["parametros"]["servidores"] == ["192.0.2.10"] and pre["sugestoes"] == sugeridos


class TestCopiaConferida:
    def test_gravar_conferido(self, tmp_path):
        import hashlib

        from msscan.acoes.comum import gravar_conferido, no_pendrive

        destino = tmp_path / "MSSCAN" / "acoes" / "x.json"
        assert gravar_conferido(destino, b"abc") == hashlib.sha256(b"abc").hexdigest() and destino.read_bytes() == b"abc"
        assert no_pendrive(destino, str(tmp_path / "MSSCAN")) == "MSSCAN/acoes/x.json"

    def test_releitura_divergente_levanta(self, tmp_path, monkeypatch):
        from msscan.acoes.comum import gravar_conferido

        monkeypatch.setattr(Path, "read_bytes", lambda self: b"corrompido")
        with pytest.raises(ValueError, match="não confere"):
            gravar_conferido(tmp_path / "x.json", b"abc")

    def test_sel_com_releitura_divergente_nao_limpa(self, tmp_path, monkeypatch):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.sel_exportar_limpar", ctx, executor, persistente=True)
        monkeypatch.setattr(Path, "read_bytes", lambda self: b"{}")
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path), MODO_APLICAR)
        assert resultado["situacao"] == "falhou" and "NÃO foi limpo" in resultado["mensagem"] and executor.escritas == []

    def test_sel_sem_pasta_nao_limpa(self):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.sel_exportar_limpar", ctx, executor, persistente=True)
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, None, MODO_APLICAR)
        assert resultado["situacao"] == "falhou" and executor.escritas == []

    def test_sel_mostra_onde_ficou(self, tmp_path):
        executor = BMCFalso()
        ctx = contexto(executor)
        pasta = tmp_path / "MSSCAN"
        pre = execucao.prechecar("bmc.sel_exportar_limpar", ctx, executor, persistente=True)
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(pasta), MODO_APLICAR)
        local = resultado["exportacao"]["no_pendrive"]
        assert local.startswith("MSSCAN/acoes/sel_") and local in resultado["mensagem"]
        assert resultado["passos"][0]["acao"] == "exportar" and "conferido" in resultado["passos"][0]["detalhe"]
        # A exportação vem antes da limpeza, e só a limpeza escreve no BMC.
        assert [p["acao"] for p in resultado["passos"]][:2] == ["exportar", "limpar"]

    def test_trava_ainda_mostra_a_copia(self, tmp_path):
        executor = BMCReal()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.sel_exportar_limpar", ctx, executor, persistente=True)
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path / "MSSCAN"), MODO_APLICAR)
        assert resultado["situacao"] == "bloqueada_trava" and "MSSCAN/acoes/sel_" in resultado["mensagem"]

    def test_ntp_sem_pendrive_gravavel_bloqueia(self):
        executor = BMCFalso()
        pre = execucao.prechecar("bmc.relogio_ntp", contexto(executor), executor, {}, persistente=False)
        assert not pre["pode_seguir"] and any("pendrive" in b for b in pre["bloqueios"])

    def test_ntp_guarda_os_valores_antes(self, tmp_path):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.relogio_ntp", ctx, executor, {"servidores": "a.ntp.br"}, persistente=True)
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path), MODO_APLICAR)
        guardado = json.loads(Path(resultado["anteriores"]["arquivo"]).read_text())
        assert guardado["bruto"] == ler("Attributes_ntp.json")["Attributes"]
        assert guardado["restaurar"] == {"Attributes": ler("Attributes_ntp.json")["Attributes"]}
        assert validar_acao_bmc(modulo_executor.NTP_DELL_RESTAURAR, BASE + guardado["alvo"], guardado["restaurar"]) == ""

    def test_ntp_sem_copia_nada_altera(self, tmp_path, monkeypatch):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.relogio_ntp", ctx, executor, {}, persistente=True)
        monkeypatch.setattr(Path, "read_bytes", lambda self: b"x")
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path), MODO_APLICAR)
        assert resultado["situacao"] == "falhou" and "nada foi alterado" in resultado["mensagem"] and executor.escritas == []
        assert execucao.executar(pre, CONFIRMACAO, ctx, executor, None, MODO_APLICAR)["situacao"] == "falhou"
        assert executor.escritas == []


def ntp_aplicado(tmp_path, executor=None, fabricante="Dell Inc.", modo=MODO_APLICAR):
    executor = executor or BMCFalso(dell=fabricante.startswith("Dell"))
    ctx = contexto(executor, fabricante=fabricante)
    pre = execucao.prechecar("bmc.relogio_ntp", ctx, executor, {"servidores": "a.ntp.br"}, persistente=True)
    return executor, ctx, execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path), modo)


class TestDesfazer:
    def test_desfazer_dell(self, tmp_path):
        executor, ctx, resultado = ntp_aplicado(tmp_path)
        assert resultado["desfazer"]["disponivel"] and resultado["desfeita"] is None
        pre = execucao.prechecar_desfazer(resultado, ctx)
        assert pre["pode_seguir"] and "ligado → desligado" in pre["previa"][0] and "a.ntp.br → (nenhum)" in pre["previa"][1]
        assert execucao.desfazer(pre, resultado, "desfazer", ctx, MODO_APLICAR)["situacao"] == "cancelada"
        assert execucao.desfazer(pre, resultado, CONFIRMACAO, ctx, MODO_APLICAR)["situacao"] == "cancelada"
        registro = execucao.desfazer(pre, resultado, " DESFAZER ", ctx, MODO_APLICAR)
        assert registro["situacao"] == "desfeita" and resultado["desfeita"] is registro
        assert executor.escritas[-1] == ("PATCH", ATRIBUTOS, {"Attributes": ler("Attributes_ntp.json")["Attributes"]})
        assert registro["antes"]["ntp_ligado"] is True and registro["depois"]["ntp_ligado"] is False
        assert "já foi desfeita" in execucao.prechecar_desfazer(resultado, ctx)["bloqueios"][0]

    def test_desfazer_dmtf(self, tmp_path):
        executor = BMCFalso(dell=False)
        executor.get[PROTOCOLOS] = {**NTP_TELEMETRIA, "NTP": {"ProtocolEnabled": False, "NTPServers": ["old.example", ""]}}
        executor, ctx, resultado = ntp_aplicado(tmp_path, executor, fabricante="Contoso")
        pre = execucao.prechecar_desfazer(resultado, ctx)
        registro = execucao.desfazer(pre, resultado, "DESFAZER", ctx, MODO_APLICAR)
        assert registro["situacao"] == "desfeita"
        assert executor.escritas[-1] == ("PATCH", PROTOCOLOS, {"NTP": {"ProtocolEnabled": False, "NTPServers": ["old.example", ""]}})

    def test_desfazer_simulado(self, tmp_path):
        executor, ctx, resultado = ntp_aplicado(tmp_path, modo=MODO_SIMULAR)
        pre = execucao.prechecar_desfazer(resultado, ctx)
        assert not pre["pode_seguir"] and "simulação" in pre["bloqueios"][0]  # nada mudou na simulação: nada a desfazer
        assert "Desfeita: não se aplica (simulação" in formatar_acoes([resultado])

    def test_desfazer_em_executor_real_para_na_trava(self, tmp_path):
        executor = BMCFalso()
        executor, ctx, resultado = ntp_aplicado(tmp_path, executor)
        pre = execucao.prechecar_desfazer(resultado, ctx)
        real = BMCReal()
        real.get = executor.get
        ctx.bmc.cliente.executor = real
        registro = execucao.desfazer(pre, resultado, "DESFAZER", ctx, MODO_APLICAR)
        assert registro["situacao"] == "bloqueada_trava" and real.escritas == [] and resultado["desfeita"] is None
        assert resultado["tentativas_desfazer"] == [registro]

    def test_sel_nao_se_desfaz(self, tmp_path):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.sel_exportar_limpar", ctx, executor, persistente=True)
        resultado = execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path), MODO_APLICAR)
        pre_desfazer = execucao.prechecar_desfazer(resultado, ctx)
        assert not pre_desfazer["pode_seguir"] and "não voltam" in pre_desfazer["bloqueios"][0]

    def test_arquivo_apagado_bloqueia(self, tmp_path):
        _, ctx, resultado = ntp_aplicado(tmp_path)
        Path(resultado["desfazer"]["arquivo"]).unlink()
        assert "restaure pela interface do BMC" in execucao.prechecar_desfazer(resultado, ctx)["bloqueios"][0]

    @pytest.mark.parametrize(("operacao", "caminho", "corpo"), [
        (modulo_executor.NTP_DELL_RESTAURAR, ATRIBUTOS, {"Attributes": {"NTPConfigGroup.1.NTPEnable": "Talvez"}}),
        (modulo_executor.NTP_DELL_RESTAURAR, ATRIBUTOS, {"Attributes": {"IPv4.1.Address": "1.2.3.4"}}),
        (modulo_executor.NTP_DELL_RESTAURAR, ATRIBUTOS, {"Attributes": {"NTPConfigGroup.1.NTP1": "http://x"}}),
        (modulo_executor.NTP_DELL_RESTAURAR, "/redfish/v1/Systems/1/Bios/Settings", {"Attributes": {"NTPConfigGroup.1.NTP1": ""}}),
        (modulo_executor.NTP_DMTF_RESTAURAR, PROTOCOLOS, {"NTP": {"ProtocolEnabled": "sim"}}),
        (modulo_executor.NTP_DMTF_RESTAURAR, PROTOCOLOS, {"NTP": {"NTPServers": ["a", "b", "c", "d"]}}),
        (modulo_executor.NTP_DMTF_RESTAURAR, PROTOCOLOS, {"NTP": {"ProtocolEnabled": False}, "SSH": {"ProtocolEnabled": True}}),
    ])  # fmt: skip
    def test_salvaguardas_do_restaurar(self, operacao, caminho, corpo):
        assert validar_acao_bmc(operacao, BASE + caminho, corpo) != ""
        executor = BMCFalso()
        assert executor.acao_bmc(operacao, BASE + caminho, "tok", corpo).erro_tipo == "bloqueado" and executor.escritas == []

    def test_restaurar_passa_pela_trava(self, monkeypatch):
        import requests

        monkeypatch.setattr(requests.Session, "request", lambda *a, **k: pytest.fail("chegou à rede"))
        corpo = {"Attributes": {"NTPConfigGroup.1.NTPEnable": "Disabled", "NTPConfigGroup.1.NTP1": ""}}
        resposta = ExecutorReal().acao_bmc(modulo_executor.NTP_DELL_RESTAURAR, BASE + ATRIBUTOS, "tok", corpo)
        assert resposta.erro_tipo == "trava"


class TestRelatorioDesfazer:
    def test_linhas(self, tmp_path):
        _, ctx, resultado = ntp_aplicado(tmp_path)
        texto = formatar_acoes([resultado])
        assert "Antes -> depois:" in texto and "NTP ligado: não -> sim" in texto
        assert "Desfeita: não (pode ser desfeita" in texto and "Valores anteriores:" in texto
        execucao.desfazer(execucao.prechecar_desfazer(resultado, ctx), resultado, "DESFAZER", ctx, MODO_APLICAR)
        texto = formatar_acoes([resultado])
        assert "Desfeita: sim, em" in texto and "NTP ligado: sim -> não (desfazer)" in texto
        html = html_acoes([resultado])
        assert "Desfeita:</strong> sim" in html and "Depois do desfazer" in html

    def test_sel_nao_se_aplica(self, tmp_path):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.sel_exportar_limpar", ctx, executor, persistente=True)
        texto = formatar_acoes([execucao.executar(pre, CONFIRMACAO, ctx, executor, str(tmp_path))])
        assert "Desfeita: não se aplica (não se aplica: eventos apagados" not in texto
        assert "Desfeita: não se aplica" in texto and "Registro de eventos exportado:" in texto

    def test_bloqueada_nao_se_aplica(self):
        executor = BMCFalso()
        ctx = contexto(executor)
        pre = execucao.prechecar("bmc.relogio_ntp", ctx, executor, {}, persistente=False)
        texto = formatar_acoes([execucao.executar(pre, CONFIRMACAO, ctx, executor, None, MODO_APLICAR)])
        assert "Desfeita: não se aplica (nada foi alterado)" in texto


class TestDesfazerModoTextoEInterface:
    def test_modo_texto(self, tmp_path, capsys):
        entradas = ["s", "1", "2", "a.ntp.br", CONFIRMACAO, "s", "DESFAZER"]
        executor, relatorio, texto = TestModoTexto().rodar(tmp_path, entradas[:1] + entradas[1:2] + entradas[3:], aplicar=True)
        saida = capsys.readouterr().out
        assert "O que volta" in saida and relatorio["acoes"][0]["desfeita"]["situacao"] == "desfeita"
        assert "Desfeita: sim" in texto and len(executor.escritas) == 2

    def test_modo_texto_sugestao_por_numero(self, tmp_path):
        _, relatorio, _ = TestModoTexto().rodar(tmp_path, ["s", "1", "1", CONFIRMACAO, ""])
        assert relatorio["acoes"][0]["antes"] is not None
        assert "pool.ntp.org" in json.dumps(relatorio["acoes"][0]["previa"])

    def test_interface(self, tmp_path):
        executor = BMCFalso()
        sessao = sessao_pronta(executor, tmp_path, MODO_APLICAR)
        sessao.prechecar_acao("bmc.relogio_ntp", {"servidores": "a.ntp.br"})
        sessao.aguardar(30)
        assert sessao.executar_acao("bmc.relogio_ntp", CONFIRMACAO)[0]
        sessao.aguardar(30)
        estado = sessao.estado()["acoes"]
        assert estado["indice_resultado"] == 0 and estado["resultado"]["desfazer"]["disponivel"]
        assert sessao.desfazer_acao(0, "DESFAZER")[0] is False  # sem prévia
        assert sessao.prechecar_desfazer(5)[0] is False
        assert sessao.prechecar_desfazer(0) == (True, "iniciado")
        sessao.aguardar(30)
        assert sessao.estado()["acoes"]["desfazer"]["pode_seguir"]
        assert sessao.estado()["acoes"]["indice_resultado"] == 0  # o cartão do resultado continua na tela
        assert "DESFAZER" in sessao.desfazer_acao(0, "CORRIGIR")[1]
        assert sessao.desfazer_acao(0, "DESFAZER") == (True, "iniciado")
        sessao.aguardar(30)
        assert sessao.relatorio["acoes"][0]["desfeita"]["situacao"] == "desfeita" and len(executor.escritas) == 2
        assert sessao.estado()["acoes"]["ntp_sugestoes"] == []  # só lidas quando o técnico escolhe o NTP
        assert sessao.ler_sugestoes_ntp()
        for _ in range(100):
            if sessao.estado()["acoes"]["ntp_sugestoes"]:
                break
            import time

            time.sleep(0.05)
        assert sessao.estado()["acoes"]["ntp_sugestoes"][-1]["servidor"] == SERVIDOR_PADRAO

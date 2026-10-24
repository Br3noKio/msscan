"""Testes da verificação obrigatória de rede e do proxy.

Cenários de falha partem das gravações reais do R640 (sysfs, ``ip -j addr``,
respostas HTTPS), alterando só o que o cenário exige.
"""

from __future__ import annotations

import json
import os

import pytest

from msscan.core.executor import ExecutorReplay, RespostaHttp, classificar_erro_http
from msscan.parsers import ErroParser
from msscan.parsers.iproute import parse_ip_addr_json
from msscan.parsers.procfs import parse_cmdline, parse_uevent
from msscan.rede.proxy import aplicar_proxy, carregar_proxy, ler_proxy, mascarar
from msscan.rede.verificacao import (
    SEM_CABO,
    SEM_INTERNET,
    SEM_IP,
    SEM_PLACA,
    aguardar_rede,
    formatar_resultado_rede,
    listar_interfaces,
    modo_sem_rede,
    verificar_rede,
)
from msscan.relatorio.simbolos import ASCII, CONSOLE, UNICODE, escolher_simbolos
from tests.conftest import R640, ler_saida

URL_DELL = "https://downloads.dell.com/"
URL_IA = "https://api.anthropic.com/"


class ReplayAlterado(ExecutorReplay):
    """Replay do R640 com arquivos, comandos ou respostas HTTP trocados."""

    def __init__(self, arquivos=None, comandos=None, http=None, listagens=None):
        super().__init__(R640)
        self.troca_arquivos = arquivos or {}
        self.troca_comandos = comandos or {}
        self.troca_http = http or {}
        self.troca_listagens = listagens or {}

    def _ler_arquivo(self, caminho):
        if caminho in self.troca_arquivos:
            return self.troca_arquivos[caminho]
        return super()._ler_arquivo(caminho)

    def _rodar(self, comando, timeout):
        if tuple(comando) in self.troca_comandos:
            return (*self.troca_comandos[tuple(comando)], 0.0)
        return super()._rodar(comando, timeout)

    def _http_get(self, url, timeout):
        if url in self.troca_http:
            return self.troca_http[url]
        return super()._http_get(url, timeout)

    def _listar(self, caminho):
        if caminho in self.troca_listagens:
            return self.troca_listagens[caminho]
        return super()._listar(caminho)


def ip_sem_enderecos() -> str:
    dados = json.loads(ler_saida(R640, "ip__-j__addr__show"))
    for interface in dados:
        if interface["ifname"] != "lo":
            interface["addr_info"] = []
    return json.dumps(dados)


CARRIER_ENO3 = "/sys/class/net/eno3/carrier"
IP_ADDR = ("ip", "-j", "addr", "show")


# --- Parsers ------------------------------------------------------------------------


class TestParsersRede:
    def test_ip_addr_real(self):
        interfaces = {i["nome"]: i for i in parse_ip_addr_json(ler_saida(R640, "ip__-j__addr__show"))}
        assert set(interfaces) == {"lo", "eno1np0", "eno2np1", "eno3", "eno4"}
        eno3 = interfaces["eno3"]
        assert eno3["estado"] == "UP" and eno3["tipo_link"] == "ether"
        assert {"familia": "inet", "endereco": "192.0.2.2", "prefixo": 24, "escopo": "global"} in eno3["enderecos"]
        assert interfaces["eno1np0"]["enderecos"] == []

    def test_ip_addr_invalido(self):
        with pytest.raises(ErroParser):
            parse_ip_addr_json("Object \"addr\" is unknown")
        assert parse_ip_addr_json("") == []

    def test_uevent_real(self):
        uevent = parse_uevent((R640 / "arquivos/sys/class/net/eno3/device/uevent").read_text())
        assert uevent["DRIVER"] == "igb"
        assert uevent["PCI_CLASS"] == "20000"

    def test_cmdline_real(self):
        parametros = parse_cmdline((R640 / "arquivos/proc/cmdline").read_text())
        assert parametros["ro"] == "" and parametros["root"].startswith("UUID=")
        assert "msscan.semrede" not in parametros


# --- Verificação ---------------------------------------------------------------------


class TestVerificarRede:
    def test_real_ok(self):
        resultado = verificar_rede(ExecutorReplay(R640))
        assert resultado.ok and resultado.problema == ""
        assert [(h["nome"], h["status"]) for h in resultado.https] == [
            ("servidor de downloads da Dell", 200),
            ("API de IA (Anthropic)", 404),
        ]

    def test_interfaces_reais(self):
        interfaces = {i.nome: i for i in listar_interfaces(ExecutorReplay(R640))}
        assert {n: (i.driver, i.fisica, i.link) for n, i in interfaces.items()} == {
            "eno1np0": ("i40e", True, False),
            "eno2np1": ("i40e", True, False),
            "eno3": ("igb", True, True),
            "eno4": ("igb", True, False),
        }
        assert interfaces["eno3"].enderecos[0] == "192.0.2.2/24"

    def test_sem_cabo(self):
        resultado = verificar_rede(ReplayAlterado(arquivos={CARRIER_ENO3: "0\n"}))
        assert not resultado.ok and resultado.problema == SEM_CABO
        assert "eno3 (igb): sem link" in resultado.detalhes

    def test_carrier_ilegivel_conta_como_sem_link(self):
        # Interface administrativamente desligada: ler "carrier" falha (EINVAL).
        resultado = verificar_rede(ReplayAlterado(arquivos={CARRIER_ENO3: None}))
        assert resultado.problema == SEM_CABO

    def test_nic_usb_do_bmc_e_ignorada(self):
        uevent = "/sys/class/net/eno3/device/uevent"
        resultado = verificar_rede(ReplayAlterado(arquivos={uevent: "DRIVER=cdc_ether\n"}))
        assert resultado.problema == SEM_CABO
        assert any("NIC USB interna do BMC (driver cdc_ether)" in d for d in resultado.detalhes)

    def test_celular_usb_do_tecnico_nao_e_ignorado(self):
        """M4: tethering de Android usa rndis_host como a NIC do iDRAC, mas o fabricante USB é outro."""
        base = "/sys/class/net/eno3/device"
        resultado = verificar_rede(ReplayAlterado(arquivos={f"{base}/uevent": "DRIVER=rndis_host\n", f"{base}/../idVendor": "18d1\n"}))
        assert not next(i for i in resultado.interfaces if i.nome == "eno3").nic_bmc
        dell = verificar_rede(ReplayAlterado(arquivos={f"{base}/uevent": "DRIVER=cdc_ether\n", f"{base}/../idVendor": "413c\n"}))
        assert dell.problema == SEM_CABO

    def test_sem_ip(self):
        resultado = verificar_rede(ReplayAlterado(comandos={IP_ADDR: (0, ip_sem_enderecos(), "")}))
        assert resultado.problema == SEM_IP
        assert "eno3: com link, sem endereço IP" in resultado.detalhes

    def test_sem_internet_tls_com_relogio_certo_e_interceptacao(self):
        """28.1: nunca só "tls"; com o relógio certo, a causa é a rede interceptando HTTPS."""
        http = {url: RespostaHttp(url, None, "tls", "certificate verify failed") for url in (URL_DELL, URL_IA)}
        resultado = verificar_rede(ReplayAlterado(http=http))
        assert resultado.problema == SEM_INTERNET
        assert "intercepta" in resultado.titulo and "tls" not in resultado.titulo
        assert "Detalhes técnicos:" in resultado.detalhes

    def test_sem_internet_tls_com_relogio_errado_pede_a_hora(self):
        from msscan.rede.relogio import ResultadoRelogio
        from msscan.rede.verificacao import _causa_falha_https

        respostas = [RespostaHttp(URL_DELL, None, "tls", "certificate verify failed")]
        causa, orientacao = _causa_falha_https(ReplayAlterado(), respostas, ResultadoRelogio(ok=False, hora_sistema="14/04/2026 08:00"))
        assert "relógio" in causa and any("14/04/2026 08:00" in o for o in orientacao)

    @pytest.mark.parametrize(
        ("tipo", "trecho"),
        [("dns", "DNS"), ("proxy", "proxy"), ("timeout", "firewall"), ("conexao", "saída para a internet")],
    )
    def test_sem_internet_explica_causa(self, tipo, trecho):
        http = {url: RespostaHttp(url, None, tipo, "x") for url in (URL_DELL, URL_IA)}
        resultado = verificar_rede(ReplayAlterado(http=http))
        assert resultado.problema == SEM_INTERNET
        assert trecho in resultado.titulo and resultado.causa

    @pytest.mark.parametrize(("status", "trecho"), [(407, "usuário e senha"), (204, "só HTTP comum")])
    def test_sem_internet_detecta_proxy_obrigatorio(self, status, trecho, monkeypatch):
        from msscan.rede.verificacao import URL_SONDA_HTTP

        monkeypatch.delenv("https_proxy", raising=False)
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        http = {url: RespostaHttp(url, None, "timeout", "x") for url in (URL_DELL, URL_IA)}
        http[URL_SONDA_HTTP] = RespostaHttp(URL_SONDA_HTTP, status)
        resultado = verificar_rede(ReplayAlterado(http=http))
        assert trecho in resultado.titulo
        assert any("MSSCAN/config/proxy.conf" in d for d in resultado.detalhes)

    def test_dns_bloqueado_sem_proxy_conf_orienta_o_proxy(self, monkeypatch):
        """Rede que só sai por proxy bloqueia o DNS externo (conferido com proxy real num namespace, 14/10/2026)."""
        from msscan.rede.verificacao import _causa_falha_https

        monkeypatch.delenv("https_proxy", raising=False)
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        respostas = [RespostaHttp(URL_DELL, None, "dns", "Failed to resolve")]
        causa, orientacao = _causa_falha_https(ReplayAlterado(), respostas, None)
        assert "DNS" in causa and any("proxy.conf" in o for o in orientacao)
        monkeypatch.setenv("https_proxy", "http://proxy.exemplo:3128")
        assert not any("proxy.conf" in o for o in _causa_falha_https(ReplayAlterado(), respostas, None)[1])

    def test_proxy_configurado_que_pede_senha(self):
        from msscan.rede.verificacao import _causa_falha_https

        respostas = [RespostaHttp(URL_DELL, None, "proxy", "Tunnel connection failed: 407 Proxy Authentication Required")]
        causa, orientacao = _causa_falha_https(ReplayAlterado(), respostas, None)
        assert "usuário e senha" in causa and "usuario:senha@" in orientacao[0]

    def test_um_site_basta_mas_falha_da_dell_e_registrada(self):
        http = {URL_DELL: RespostaHttp(URL_DELL, None, "timeout", "x")}
        resultado = verificar_rede(ReplayAlterado(http=http))
        assert resultado.ok
        assert resultado.detalhes == ["servidor de downloads da Dell: sem resposta (timeout)"]

    def test_sem_placa_aponta_placa_sem_driver(self):
        lspci = "\n".join(
            linha
            for linha in ler_saida(R640, "lspci__-nnk").splitlines()
            if not (linha.startswith("\tKernel driver in use: i40e") or linha.startswith("\tKernel driver in use: igb"))
        )
        executor = ReplayAlterado(listagens={"/sys/class/net": []}, comandos={("lspci", "-nnk"): (0, lspci, "")})
        resultado = verificar_rede(executor)
        assert resultado.problema == SEM_PLACA
        sem_driver = [d for d in resultado.detalhes if d.startswith("placa de rede sem driver")]
        assert len(sem_driver) == 4


class TestModoSemRede:
    def test_real(self):
        assert not modo_sem_rede(ExecutorReplay(R640))

    @pytest.mark.parametrize(("sufixo", "esperado"), [(" msscan.semrede=1", True), (" msscan.semrede=0", False)])
    def test_parametro(self, sufixo, esperado):
        cmdline = (R640 / "arquivos/proc/cmdline").read_text().strip() + sufixo
        assert modo_sem_rede(ReplayAlterado(arquivos={"/proc/cmdline": cmdline})) is esperado


class ExecutorSequencia(ReplayAlterado):
    """O cabo "é conectado" depois de N leituras do carrier."""

    def __init__(self, leituras_sem_link):
        super().__init__()
        self.restantes = leituras_sem_link

    def _ler_arquivo(self, caminho):
        if caminho == CARRIER_ENO3:
            self.restantes -= 1
            return "0\n" if self.restantes >= 0 else "1\n"
        return super()._ler_arquivo(caminho)


class TestAguardarRede:
    def test_espera_automatica_resolve_sem_perguntar(self):
        mensagens, esperas = [], []
        resultado = aguardar_rede(
            ExecutorSequencia(2), saida=mensagens.append, entrada=lambda _: pytest.fail("não devia perguntar"),
            dormir=esperas.append, espera_automatica=30, intervalo=3,
        )  # fmt: skip
        assert resultado.ok
        assert esperas == [3, 3]
        assert "Aguardando a rede" in mensagens[0]

    def test_sem_rede_depois_da_espera_segue_sem_travar(self):
        """28.1: a rede não bloqueia mais (nada de "Enter para tentar de novo" em loop)."""
        mensagens = []
        resultado = aguardar_rede(
            ExecutorSequencia(99), saida=mensagens.append, entrada=lambda _: pytest.fail("não devia perguntar"),
            dormir=lambda _: None, espera_automatica=3, intervalo=3,
        )  # fmt: skip
        assert resultado.problema == SEM_CABO
        assert any("SEM CABO" in m for m in mensagens)
        assert any("Sem internet: catálogo online, downloads e IA indisponíveis" in m for m in mensagens)
        assert any("reconecta sozinho" in m for m in mensagens)

    def test_hora_impossivel_pergunta_ao_tecnico_e_verifica_de_novo(self, monkeypatch):
        from msscan.rede import relogio
        from msscan.rede.relogio import ResultadoRelogio

        estado = {"r": ResultadoRelogio(ok=False, precisa_tecnico=True, hora_sistema="14/04/2026 08:00")}
        monkeypatch.setattr(relogio, "conferir", lambda _ex, forcar=False, **_k: estado["r"])

        def ajustar(_ex, texto):
            if texto == "ontem":
                return ResultadoRelogio(mensagem="Data e hora inválidas")
            estado["r"] = ResultadoRelogio(ok=True, ajustado=True, mensagem="Relógio ajustado.")
            return estado["r"]

        monkeypatch.setattr(relogio, "ajustar_pelo_tecnico", ajustar)
        tls = {url: RespostaHttp(url, None, "tls", "x") for url in (URL_DELL, URL_IA)}
        executor = ReplayAlterado(http=tls)
        respostas = iter(["ontem", "13/10/2026 10:00"])
        perguntas, mensagens = [], []

        def entrada(pergunta):
            perguntas.append(pergunta)
            executor.troca_http = {}  # hora certa: o HTTPS passa
            return next(respostas)

        resultado = aguardar_rede(executor, saida=mensagens.append, entrada=entrada, dormir=lambda _: None)
        assert resultado.ok
        assert len(perguntas) == 2 and "14/04/2026 08:00" in perguntas[0]
        assert any("inválidas" in m for m in mensagens)

    def test_sem_internet_nao_espera_automaticamente(self):
        http = {url: RespostaHttp(url, None, "dns", "x") for url in (URL_DELL, URL_IA)}
        esperas = []
        resultado = aguardar_rede(ReplayAlterado(http=http), saida=lambda _: None, entrada=None, dormir=esperas.append)
        assert resultado.problema == SEM_INTERNET and esperas == []

    def test_replay_nao_trava(self):
        resultado = aguardar_rede(ReplayAlterado(arquivos={CARRIER_ENO3: "0"}), saida=lambda _: None, entrada=None, espera_automatica=0)
        assert resultado.problema == SEM_CABO


class TestFormatacao:
    def test_ok(self):
        texto = formatar_resultado_rede(verificar_rede(ExecutorReplay(R640)), CONSOLE)
        assert "[1/3] Placa de rede com cabo: √ eno3" in texto
        assert "[2/3] Endereço IP: √ eno3 192.0.2.2/24 (+5)" in texto
        assert "[3/3] Acesso à internet (HTTPS): √" in texto

    def test_falha_para_no_nivel_que_falhou(self):
        texto = formatar_resultado_rede(verificar_rede(ReplayAlterado(comandos={IP_ADDR: (0, ip_sem_enderecos(), "")})), ASCII)
        assert "[1/3] Placa de rede com cabo: OK eno3" in texto
        assert "[2/3] Endereço IP: X" in texto
        assert "[3/3]" not in texto
        assert "X SEM DHCP" in texto

    def test_sem_placa_mostra_nivel_1(self):
        texto = formatar_resultado_rede(verificar_rede(ReplayAlterado(listagens={"/sys/class/net": []})), UNICODE)
        assert "[1/3] Placa de rede com cabo: ✗" in texto


# --- Proxy ----------------------------------------------------------------------------


class TestProxy:
    def test_url_unica(self):
        assert ler_proxy("# empresa\nhttp://u:segredo@proxy:3128\n") == {
            "http": "http://u:segredo@proxy:3128",
            "https": "http://u:segredo@proxy:3128",
        }

    def test_chaves(self):
        texto = "export HTTPS_PROXY='http://p:3128'\nno_proxy=localhost,10.0.0.0/8  # interno\n"
        assert ler_proxy(texto) == {"https": "http://p:3128", "no_proxy": "localhost,10.0.0.0/8"}

    @pytest.mark.parametrize("texto", ["qualquer coisa", "https_proxy=ftp://x", "https_proxy=proxy:3128"])
    def test_invalido(self, texto):
        with pytest.raises(ErroParser):
            ler_proxy(texto)

    def test_senha_nunca_aparece_no_erro(self):
        with pytest.raises(ErroParser) as erro:
            ler_proxy("https_proxy=socks://u:segredo@p:1")
        assert "segredo" not in str(erro.value)

    def test_mascarar(self):
        assert mascarar("http://u:segredo@proxy:3128") == "http://u:***@proxy:3128"
        assert mascarar("http://proxy:3128") == "http://proxy:3128"

    def test_aplicar(self):
        ambiente = {}
        aplicar_proxy({"https": "http://p:1", "no_proxy": "x"}, ambiente)
        assert ambiente == {"https_proxy": "http://p:1", "HTTPS_PROXY": "http://p:1", "no_proxy": "x", "NO_PROXY": "x"}

    def test_carregar(self, tmp_path, monkeypatch):
        # Ambiente isolado: carregar_proxy exporta para os.environ.
        monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items() if "proxy" not in k.lower()})
        assert carregar_proxy(tmp_path) is None
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "proxy.conf").write_text("http://p:3128\n")
        assert carregar_proxy(tmp_path)["https"] == "http://p:3128"
        assert os.environ["HTTPS_PROXY"] == "http://p:3128"


# --- Símbolos e HTTP ----------------------------------------------------------------------


def test_escolher_simbolos():
    assert escolher_simbolos("auto", {"TERM": "linux"}) is CONSOLE
    assert escolher_simbolos("auto", {"TERM": "xterm-256color"}) is UNICODE
    assert escolher_simbolos("ascii", {"TERM": "linux"}) is ASCII
    assert "✓" not in CONSOLE.values() and "⚠" not in CONSOLE.values()


class TestHttp:
    def test_replay(self):
        ex = ExecutorReplay(R640)
        assert ex.http_get(URL_DELL).status == 200
        nao_gravada = ex.http_get("https://exemplo.invalid/")
        assert nao_gravada.status is None and nao_gravada.erro_tipo == "conexao"
        assert ex.historico[0].comando == ["GET", URL_DELL] and ex.historico[0].rc == 200

    def test_url_invalida(self):
        assert ExecutorReplay(R640).http_get("ftp://x").erro_tipo == "outro"

    def test_real_conexao_recusada(self):
        from msscan.core.executor import ExecutorReal

        resposta = ExecutorReal().http_get("https://127.0.0.1:9/", timeout=3)
        assert resposta.status is None and resposta.erro_tipo == "conexao"

    def test_classificar_erros(self):
        import requests

        assert classificar_erro_http(requests.exceptions.SSLError("certificate verify failed"))[0] == "tls"
        assert classificar_erro_http(requests.exceptions.ProxyError("recusado"))[0] == "proxy"
        assert classificar_erro_http(requests.exceptions.ConnectTimeout("t"))[0] == "timeout"
        assert classificar_erro_http(requests.exceptions.ConnectionError("Failed to resolve 'x' ([Errno -3] Temporary failure in name resolution)"))[0] == "dns"
        assert classificar_erro_http(requests.exceptions.ConnectionError("Connection refused"))[0] == "conexao"

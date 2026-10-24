"""Rede contínua (CLAUDE.md 5b): monitor em segundo plano, "Reconectando...", espera e retomada, download com Range.

Cenários partem das gravações reais do R640 (sysfs, ``ip -j addr``, HTTPS), alterando só o que o cenário exige.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import time
from pathlib import Path

import pytest

from main import iniciar_monitor_texto
from msscan.core import executor as modulo_executor
from msscan.core.executor import ExecutorReal, RespostaDownload, RespostaHttp, RespostaIA
from msscan.ia.analise import analisar_com_rede
from msscan.interface.sessao import Sessao
from msscan.rede.monitor import OK, RECONECTANDO, ExecutorSilencioso, MonitorRede, baixar_retomando, texto_status
from msscan.rede.verificacao import ResultadoRede, verificar_rede
from tests.conftest import ExecutorFalso
from tests.test_interface import ESTATICOS_TEXTO
from tests.test_rede import CARRIER_ENO3, IP_ADDR, URL_DELL, URL_IA, ReplayAlterado, ip_sem_enderecos

SEM_INTERNET_HTTP = {url: RespostaHttp(url, None, "conexao", "x") for url in (URL_DELL, URL_IA)}


class ContaHttp(ReplayAlterado):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.gets = 0

    def _http_get(self, url, timeout):
        self.gets += 1
        return super()._http_get(url, timeout)


class Relogio:
    def __init__(self):
        self.agora = 1000.0

    def __call__(self):
        return self.agora


class TestExecutorSilencioso:
    def test_mesmas_leituras_sem_historico(self):
        base = ReplayAlterado()
        silencioso = ExecutorSilencioso(base)
        assert silencioso.rodar(list(IP_ADDR)) == base.rodar(list(IP_ADDR))
        base.historico.clear()
        silencioso.rodar(list(IP_ADDR))
        silencioso.ler_arquivo(CARRIER_ENO3)
        silencioso.http_get(URL_DELL)
        assert base.historico == [] and list(silencioso.historico) == []

    def test_validacao_continua(self):
        rc, _, erro = ExecutorSilencioso(ReplayAlterado()).rodar("ip addr")  # string de shell: recusada
        assert rc == -1 and "lista" in erro

    def test_monitor_nao_enche_o_historico_do_relatorio(self):
        base = ReplayAlterado()
        monitor = MonitorRede(base)
        for _ in range(3):
            monitor.verificar_agora(forcar_internet=True)
        assert base.historico == []


class TestMonitor:
    def test_so_cai_depois_de_duas_falhas_e_volta_sozinho(self):
        executor = ReplayAlterado()
        mudancas = []
        monitor = MonitorRede(executor, ao_mudar=mudancas.append, relogio=Relogio())
        assert monitor.verificar_agora()["situacao"] == OK
        executor.troca_arquivos[CARRIER_ENO3] = "0\n"  # técnico puxou o cabo
        assert monitor.verificar_agora()["situacao"] == OK and monitor.ok  # uma falha não pisca a tela
        estado = monitor.verificar_agora()
        assert estado["situacao"] == RECONECTANDO and estado["curto"] == "sem cabo" and not monitor.ok
        assert "cabo" in estado["motivo"] and len(mudancas) == 1
        assert texto_status(estado) == "Reconectando... (cabo de rede desconectado (sem link))"
        # Cabo de volta, mas o DHCP ainda não respondeu: o motivo muda, continua reconectando.
        executor.troca_arquivos.pop(CARRIER_ENO3)
        executor.troca_comandos[IP_ADDR] = (0, ip_sem_enderecos(), "")
        assert monitor.verificar_agora()["curto"] == "sem DHCP" and len(mudancas) == 2
        executor.troca_comandos.pop(IP_ADDR)
        assert monitor.verificar_agora()["situacao"] == OK and monitor.ok and monitor.esperar(0)
        assert [m["situacao"] for m in mudancas] == [RECONECTANDO, RECONECTANDO, OK]

    def test_internet_so_a_cada_intervalo_ou_quando_estava_fora(self):
        relogio = Relogio()
        executor = ContaHttp()
        monitor = MonitorRede(executor, intervalo_internet=30, relogio=relogio)
        monitor.verificar_agora()
        assert executor.gets == 0  # a verificação inicial acabou de testar a internet
        relogio.agora += 31
        monitor.verificar_agora()
        assert executor.gets == 2  # Dell + API de IA
        monitor.verificar_agora()
        assert executor.gets == 2

    def test_internet_fora(self):
        relogio = Relogio()
        executor = ReplayAlterado(http=dict(SEM_INTERNET_HTTP))
        monitor = MonitorRede(executor, relogio=relogio)
        relogio.agora += 60
        monitor.verificar_agora()
        assert monitor.verificar_agora()["curto"] == "sem internet"  # estava fora: testa a internet toda vez

    def test_confirmar_queda_conta_a_falha_do_consumidor(self):
        executor = ReplayAlterado(http=dict(SEM_INTERNET_HTTP))
        monitor = MonitorRede(executor)
        assert monitor.confirmar_queda() and not monitor.ok
        executor.troca_http.clear()
        assert not MonitorRede(executor).confirmar_queda()  # rede de pé: problema é do servidor

    def test_thread_em_segundo_plano(self):
        executor = ReplayAlterado()
        monitor = MonitorRede(executor, intervalo=0.02)
        monitor.iniciar()
        try:
            executor.troca_arquivos[CARRIER_ENO3] = "0\n"
            assert not monitor.esperar(0) or _aguardar(lambda: not monitor.ok)
            executor.troca_arquivos.pop(CARRIER_ENO3)
            assert monitor.esperar(5)
        finally:
            monitor.parar()

    def test_verificacao_sem_internet_para_no_ip(self):
        executor = ContaHttp()
        resultado = verificar_rede(executor, testar_internet=False)
        assert resultado.ok and executor.gets == 0


def _aguardar(condicao, limite: float = 5) -> bool:
    fim = time.monotonic() + limite
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.01)
    return False


class MonitorFalso:
    """Rede cai (confirmar_queda) e volta (esperar); conta as esperas."""

    def __init__(self, caiu: bool = True):
        self.caiu = caiu
        self.ok = True
        self.esperas = 0

    def confirmar_queda(self):
        self.ok = not self.caiu
        return self.caiu

    def estado(self):
        return {"motivo": "cabo de rede desconectado (sem link)"}

    def esperar(self, timeout=None):
        self.esperas += 1
        self.ok = True
        return True


class Baixador(ExecutorFalso):
    def __init__(self, respostas):
        super().__init__()
        self.respostas = list(respostas)

    def _baixar(self, url, destino, limite, timeout):
        return self.respostas.pop(0)


URL_CAT = "https://downloads.dell.com/catalog/Catalog.xml.gz"


class TestBaixarRetomando:
    def test_rede_caiu_espera_e_retoma(self):
        executor = Baixador([RespostaDownload(URL_CAT, None, erro_tipo="conexao", erro="caiu"),
                             RespostaDownload(URL_CAT, 200, "/x", 10, "abc", retomado_de=4)])  # fmt: skip
        monitor, avisos = MonitorFalso(), []
        resposta = baixar_retomando(executor, URL_CAT, "/x", 100, monitor=monitor, avisar=avisos.append)
        assert resposta.ok and resposta.retomado_de == 4 and monitor.esperas == 1
        assert any("aguardando a reconexão" in a for a in avisos) and any("de onde parou" in a for a in avisos)

    @pytest.mark.parametrize("resposta", [RespostaDownload(URL_CAT, 404, erro_tipo="http", erro="HTTP 404"),
                                          RespostaDownload(URL_CAT, 200, erro_tipo="tamanho", erro="grande")])  # fmt: skip
    def test_erro_que_nao_e_de_rede_nao_espera(self, resposta):
        monitor = MonitorFalso()
        assert baixar_retomando(Baixador([resposta]), URL_CAT, "/x", 100, monitor=monitor) is resposta
        assert monitor.esperas == 0

    def test_rede_de_pe_nao_espera(self):
        falha = RespostaDownload(URL_CAT, None, erro_tipo="timeout", erro="lento")
        monitor = MonitorFalso(caiu=False)
        assert baixar_retomando(Baixador([falha]), URL_CAT, "/x", 100, monitor=monitor) is falha
        assert monitor.esperas == 0

    def test_sem_monitor_nao_espera(self):
        falha = RespostaDownload(URL_CAT, None, erro_tipo="conexao", erro="caiu")
        assert baixar_retomando(Baixador([falha]), URL_CAT, "/x", 100) is falha


# --- Download real com Range (requests simulado) ---------------------------------------------

CONTEUDO = bytes(range(256)) * 40  # 10240 bytes


class RespostaFalsa:
    def __init__(self, status, corpo=b"", headers=None, quebrar_depois=None):
        self.status_code = status
        self.corpo = corpo
        self.headers = headers or {}
        self.quebrar_depois = quebrar_depois

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def iter_content(self, tamanho):
        import requests

        enviado = 0
        for i in range(0, len(self.corpo), 1000):
            if self.quebrar_depois is not None and enviado >= self.quebrar_depois:
                raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")
            bloco = self.corpo[i : i + 1000]
            enviado += len(bloco)
            yield bloco


@pytest.fixture
def servidor(monkeypatch):
    """Servidor falso: ``respostas`` é uma fila de funções ``(headers) -> RespostaFalsa``; guarda os Range pedidos."""
    requests = pytest.importorskip("requests")
    estado = {"respostas": [], "ranges": []}

    def get(url, stream, timeout, allow_redirects, headers=None):
        estado["ranges"].append((headers or {}).get("Range"))
        return estado["respostas"].pop(0)(headers or {})

    monkeypatch.setattr(requests, "get", get)
    return estado


def _range_ok(headers):
    inicio = int(headers["Range"].removeprefix("bytes=").rstrip("-"))
    return RespostaFalsa(206, CONTEUDO[inicio:], {"Content-Range": f"bytes {inicio}-{len(CONTEUDO) - 1}/{len(CONTEUDO)}"})


class TestDownloadComRange:
    def test_queda_no_meio_mantem_parcial_e_retoma_com_hash_do_arquivo_inteiro(self, servidor, tmp_path):
        destino = str(tmp_path / "Catalog.xml.gz")
        servidor["respostas"] = [lambda h: RespostaFalsa(200, CONTEUDO, quebrar_depois=4000), _range_ok]
        executor = ExecutorReal()
        primeira = executor._baixar(URL_CAT, destino, 10**6, 30)
        assert primeira.erro_tipo == "conexao" and Path(destino + ".parcial").stat().st_size == 4000
        segunda = executor._baixar(URL_CAT, destino, 10**6, 30)
        assert servidor["ranges"] == [None, "bytes=4000-"]
        assert segunda.ok and segunda.retomado_de == 4000 and segunda.tamanho == len(CONTEUDO)
        assert Path(destino).read_bytes() == CONTEUDO and not Path(destino + ".parcial").exists()
        assert segunda.sha256 == hashlib.sha256(CONTEUDO).hexdigest()

    @pytest.mark.parametrize("resposta", [lambda h: RespostaFalsa(200, CONTEUDO),  # servidor ignorou o Range
                                          "416"])  # fmt: skip
    def test_parcial_que_nao_serve_recomeca_do_zero(self, servidor, tmp_path, resposta):
        destino = str(tmp_path / "c.gz")
        Path(destino + ".parcial").write_bytes(b"lixo de outro arquivo")
        if resposta == "416":
            servidor["respostas"] = [lambda h: RespostaFalsa(416), lambda h: RespostaFalsa(200, CONTEUDO)]
        else:
            servidor["respostas"] = [resposta]
        final = ExecutorReal()._baixar(URL_CAT, destino, 10**6, 30)
        assert final.ok and final.retomado_de == 0 and Path(destino).read_bytes() == CONTEUDO
        assert final.sha256 == hashlib.sha256(CONTEUDO).hexdigest()

    def test_parcial_velho_e_descartado(self, servidor, tmp_path):
        destino = str(tmp_path / "c.gz")
        parcial = Path(destino + ".parcial")
        parcial.write_bytes(CONTEUDO[:100])
        os.utime(parcial, (time.time() - modulo_executor.PARCIAL_VALIDO_S - 60,) * 2)
        servidor["respostas"] = [lambda h: RespostaFalsa(200, CONTEUDO)]
        assert ExecutorReal()._baixar(URL_CAT, destino, 10**6, 30).ok
        assert servidor["ranges"] == [None]

    def test_limite_de_tamanho_vale_para_o_total(self, servidor, tmp_path):
        destino = str(tmp_path / "c.gz")
        Path(destino + ".parcial").write_bytes(CONTEUDO[:9000])
        servidor["respostas"] = [_range_ok]
        resposta = ExecutorReal()._baixar(URL_CAT, destino, 9500, 30)
        assert resposta.erro_tipo == "tamanho" and not Path(destino + ".parcial").exists()


# --- IA espera a rede ---------------------------------------------------------------------------


class TestIAComRede:
    def provedores(self, tmp_path):
        from tests.test_ia_config import dois_provedores

        return dois_provedores(tmp_path)[:1]

    def test_sem_rede_espera_e_reenvia(self, tmp_path):
        from tests.test_ia import resposta

        executor = ExecutorFalso()
        executor.respostas_ia = [RespostaIA(None, erro_tipo="conexao", erro="caiu"), resposta("gemini_ok")]
        monitor, avisos = MonitorFalso(), []
        resultado = analisar_com_rede(executor, self.provedores(tmp_path), {"componentes": []}, monitor, avisos.append)
        assert resultado.ok and monitor.esperas == 1 and any("Rede de volta" in a for a in avisos)

    def test_limite_nao_e_rede(self, tmp_path):
        from tests.test_ia import resposta

        executor = ExecutorFalso()
        executor.respostas_ia = [resposta("gemini_429")] * 2  # padrão e modelo alternativo
        monitor = MonitorFalso()
        resultado = analisar_com_rede(executor, self.provedores(tmp_path), {"componentes": []}, monitor)
        assert not resultado.ok and monitor.esperas == 0 and len(executor.ia_enviados) == 2

    def test_espera_antes_de_enviar(self, tmp_path):
        from tests.test_ia import resposta

        executor = ExecutorFalso()
        executor.respostas_ia = [resposta("gemini_ok")]
        monitor = MonitorFalso()
        monitor.ok = False  # rede já estava fora quando o técnico pediu
        avisos = []
        assert analisar_com_rede(executor, self.provedores(tmp_path), {"componentes": []}, monitor, avisos.append).ok
        assert monitor.esperas == 1 and "aguardando a reconexão" in avisos[0]


# --- Sessão, interface e modo texto -----------------------------------------------------------


class TestSessaoEInterface:
    def test_monitor_comeca_depois_da_rede_inicial(self):
        executor = ReplayAlterado()
        sessao = Sessao(executor, None, replay=True, monitorar_rede=True)
        sessao.iniciar()
        sessao.aguardar(30)
        try:
            assert sessao.monitor is not None
            assert sessao.estado()["rede"]["continuo"]["situacao"] == OK
            executor.troca_arquivos[CARRIER_ENO3] = "0\n"
            sessao.monitor.verificar_agora()
            sessao.monitor.verificar_agora()
            continuo = sessao.estado()["rede"]["continuo"]
            assert continuo["situacao"] == RECONECTANDO and continuo["curto"] == "sem cabo"
        finally:
            sessao.encerrar()

    def test_replay_sem_monitor_por_padrao(self):
        sessao = Sessao(ReplayAlterado(), None, replay=True)
        sessao.iniciar()
        sessao.aguardar(30)
        assert sessao.monitor is None and sessao.estado()["rede"]["continuo"] is None

    def test_frontend_reconectando_animado_acessivel(self):
        js, css = ESTATICOS_TEXTO["app.js"], ESTATICOS_TEXTO["app.css"]
        assert "Reconectando…" in js and 'reconectando ? "sync" : "globe"' in js and 'reconectando ? "girando" : ""' in js
        assert "anunciar(reconectando" in js  # estado anunciado na região aria-live
        assert "prefers-reduced-motion: reduce" in css and ".girando { animation: none; }" in css


class TestModoTexto:
    def test_linha_de_status(self, capsys):
        executor = ReplayAlterado()
        args = argparse.Namespace(replay=None)
        monitor = iniciar_monitor_texto(args, executor, ResultadoRede(True))
        assert monitor is not None
        monitor.parar()
        executor.troca_arquivos[CARRIER_ENO3] = "0\n"
        monitor.verificar_agora()
        monitor.verificar_agora()
        executor.troca_arquivos.pop(CARRIER_ENO3)
        monitor.verificar_agora()
        saida = capsys.readouterr().out
        assert "[Rede] Reconectando... (cabo de rede desconectado (sem link))" in saida
        assert "IA e downloads esperam a rede voltar" in saida and "[Rede] Rede OK" in saida

    def test_sem_monitor_em_replay_ou_com_semrede(self):
        assert iniciar_monitor_texto(argparse.Namespace(replay="x"), ReplayAlterado(), ResultadoRede(True)) is None
        semrede = ResultadoRede(False, sem_rede_por_parametro=True)
        assert iniciar_monitor_texto(argparse.Namespace(replay=None), ReplayAlterado(), semrede) is None

    def test_comeca_sem_internet_e_avisa_quando_chega(self, capsys):
        """28.1: sem internet no início, o MSscan segue; o monitor reconecta, avisa e roda o que esperava a rede."""
        from main import rede_atual
        from msscan.rede.verificacao import SEM_INTERNET

        inicial = ResultadoRede(False, SEM_INTERNET, "SEM INTERNET: firewall.", causa="o firewall da rede do cliente bloqueia")
        monitor = iniciar_monitor_texto(argparse.Namespace(replay=None), ReplayAlterado(), inicial)
        monitor.parar()
        assert not monitor.ok and "firewall" in monitor.estado()["motivo"]
        assert rede_atual(inicial, monitor) is inicial
        chamados = []
        monitor.quando_voltar(lambda: chamados.append(1))
        assert chamados == []
        monitor.verificar_agora()
        assert monitor.ok and chamados == [1]
        assert rede_atual(inicial, monitor).ok
        assert "[Rede] Rede OK" in capsys.readouterr().out

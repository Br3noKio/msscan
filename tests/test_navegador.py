"""Navegador integrado (fase 11): proxy só com domínios oficiais e Firefox em perfil temporário."""

from __future__ import annotations

import getpass
import socket
import threading
from pathlib import Path

import pytest

from msscan.navegador import navegador as mod_navegador
from msscan.navegador import proxy as mod_proxy
from msscan.navegador.navegador import CLASSE_JANELA, Navegador, preferencias
from msscan.navegador.proxy import ProxyOficial, permitido
from tests.conftest import ExecutorFalso
from tests.test_pareamento import porta_livre


class TestLista:
    @pytest.mark.parametrize("host", ["www.dell.com", "dl.dell.com", "downloads.dell.com", "dell.com", "afcs.dellcdn.com",
                                      "support.hpe.com", "datacentersupport.lenovo.com", "WWW.DELL.COM."])  # fmt: skip
    def test_oficiais(self, host):
        assert permitido(host, 443)

    @pytest.mark.parametrize("host, porta", [
        ("www.dell.com", 80), ("www.dell.com", 8443), ("evildell.com", 443), ("dell.com.evil.com", 443), ("dell.co", 443),
        ("c.go-mpulse.net", 443), ("143.166.1.1", 443), ("127.0.0.1", 443), ("localhost", 443), ("", 443), ("dell..com", 443),
    ])  # fmt: skip
    def test_recusados(self, host, porta):
        assert not permitido(host, porta)


def pedir(porta: int, pedido: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", porta), timeout=5) as conexao:
        conexao.sendall(pedido)
        return conexao.recv(4096)


@pytest.fixture
def proxy():
    servidor = ProxyOficial(porta_livre())
    servidor.iniciar()
    yield servidor
    servidor.encerrar()


class TestProxy:
    @pytest.mark.parametrize("pedido", [
        b"CONNECT evil.example.com:443 HTTP/1.1\r\nHost: evil.example.com\r\n\r\n",
        b"CONNECT www.dell.com:80 HTTP/1.1\r\n\r\n",
        b"GET http://www.dell.com/ HTTP/1.1\r\nHost: www.dell.com\r\n\r\n",
        b"CONNECT 127.0.0.1:8765 HTTP/1.1\r\n\r\n",
    ])  # fmt: skip
    def test_recusa_com_403(self, proxy, pedido):
        assert pedir(proxy.porta, pedido).startswith(b"HTTP/1.1 403")
        assert proxy.resumo()["recusados"] and not proxy.resumo()["permitidos"]

    def test_tunel_para_dominio_oficial(self, proxy, monkeypatch):
        eco = socket.create_server(("127.0.0.1", 0))
        conectados = []

        def atender():
            conexao, _ = eco.accept()
            with conexao:
                conexao.sendall(conexao.recv(100).upper())

        threading.Thread(target=atender, daemon=True).start()
        original = socket.create_connection

        def conectar(host, porta):
            conectados.append((host, porta))
            return original(eco.getsockname(), timeout=5)

        monkeypatch.setattr(proxy, "conectar", conectar)
        with original(("127.0.0.1", proxy.porta), timeout=5) as conexao:
            conexao.sendall(b"CONNECT dl.dell.com:443 HTTP/1.1\r\nHost: dl.dell.com:443\r\n\r\n")
            assert conexao.recv(100).startswith(b"HTTP/1.1 200")
            conexao.sendall(b"tls")
            assert conexao.recv(100) == b"TLS"
        assert conectados == [("dl.dell.com", 443)] and proxy.resumo()["permitidos"] == {"dl.dell.com": 1}
        eco.close()


class Processo:
    def __init__(self):
        self.vivo, self.sinais = True, []

    def poll(self):
        return None if self.vivo else 0

    def terminate(self):
        self.vivo = False

    def wait(self, _=None):
        return 0

    def kill(self):
        self.vivo = False


class TestNavegador:
    def novo(self, tmp_path):
        chamadas = []

        def popen(comando, **_):
            chamadas.append(comando)
            return Processo()

        executor = ExecutorFalso(comandos={("pkill", "-u", getpass.getuser()): (0, "", "")})
        executor.ferramentas.add("xdotool")
        navegador = Navegador(executor, ":0", "", base=str(tmp_path), usuario=getpass.getuser(), porta_proxy=47999, popen=popen)
        return navegador, chamadas, executor

    def test_abre_como_usuario_sem_privilegio_com_perfil_temporario(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod_navegador, "INTERVALO_S", 3600)
        navegador, chamadas, _ = self.novo(tmp_path)
        ok, _ = navegador.abrir("https://www.dell.com/support/product-details/pt-br/product/poweredge-r620/drivers", lambda p: None)
        assert ok and navegador.aberto and navegador.perfil.parent == tmp_path
        comando = chamadas[0]
        assert comando[:4] == ["runuser", "-u", getpass.getuser(), "--"] and "env" in comando and "-i" in comando
        assert comando[comando.index("--class") + 1] == CLASSE_JANELA and comando[-1].startswith("https://www.dell.com/")
        assert (navegador.perfil.stat().st_mode & 0o777) == 0o700
        prefs = (navegador.perfil / "user.js").read_text()
        assert '"network.proxy.ssl_port", 47999' in prefs and '"signon.rememberSignons", false' in prefs
        assert str(navegador.perfil / "downloads") in prefs
        perfil = navegador.perfil
        navegador.fechar()
        assert not perfil.exists() and not navegador.aberto  # cookies, login e cache apagados

    def test_usuario_inexistente_nao_abre(self, tmp_path):
        navegador = Navegador(ExecutorFalso(), base=str(tmp_path), usuario="msscan-usuario-que-nao-existe", popen=pytest.fail)
        ok, motivo = navegador.abrir("https://www.dell.com/", lambda p: None)
        assert not ok and "não foi possível" in motivo and list(tmp_path.iterdir()) == []

    def test_downloads_so_depois_de_terminar_e_uma_vez(self, tmp_path):
        navegador, _, _ = self.novo(tmp_path)
        pasta = tmp_path / "d"
        pasta.mkdir()
        (pasta / "BIOS_W1WJW_LN_2.9.0.BIN").write_bytes(b"")
        (pasta / "BIOS_W1WJW_LN_2.9.0.BIN.part").write_bytes(b"parcial")
        assert navegador.concluidos(pasta) == []
        (pasta / "BIOS_W1WJW_LN_2.9.0.BIN.part").unlink()
        (pasta / "BIOS_W1WJW_LN_2.9.0.BIN").write_bytes(b"pacote")
        assert navegador.concluidos(pasta) == []  # tamanho ainda não conferido duas vezes
        assert navegador.concluidos(pasta) == [pasta / "BIOS_W1WJW_LN_2.9.0.BIN"]
        assert navegador.concluidos(pasta) == []

    def test_voltar_e_mostrar_pela_classe_da_janela(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod_navegador, "INTERVALO_S", 3600)
        navegador, _, executor = self.novo(tmp_path)
        executor.comandos[("xdotool", "search", "--class", CLASSE_JANELA, "windowunmap", "%@")] = (0, "", "")
        executor.comandos[("xdotool", "search", "--class", CLASSE_JANELA, "windowmap", "%@", "windowraise", "%@")] = (0, "", "")
        assert not navegador.voltar()  # fechado: nada a esconder
        navegador.abrir("https://www.dell.com/", lambda p: None)
        assert navegador.voltar() and not navegador.estado()["visivel"]
        assert navegador.mostrar() and navegador.estado()["visivel"]
        navegador.fechar()

    def test_preferencias_sem_proxy_de_fora(self):
        texto = preferencias(Path("/tmp/x/downloads"), 47480)
        assert '"network.proxy.no_proxies_on", ""' in texto and '"network.trr.mode", 5' in texto
        assert '"dom.security.https_only_mode", true' in texto


class TestConexaoDoProxy:
    """Velocidade do navegador (CLAUDE.md 15.1): cache de DNS, IPv4 direto sem IPv6 global, tentativa curta por endereço."""

    IF_INET6_SO_LOCAL = "fe800000000000000000000000000001 02 40 20 80     eno1\n00000000000000000000000000000001 01 80 10 80       lo\n"
    IF_INET6_GLOBAL = IF_INET6_SO_LOCAL + "20010db878b09d5e00000000000011e2 02 80 00 00     eno1\n"
    ROTA_PADRAO = "00000000000000000000000000000000 00 00000000000000000000000000000000 00 fe800000000000000000000000000001 00000064 00000001 00000000 00000003     eno1\n"

    def test_ipv6_global_exige_endereco_e_rota(self):
        arquivos = lambda ender, rota: (lambda c: ender if c.endswith("if_inet6") else rota)  # noqa: E731
        assert mod_proxy.tem_ipv6_global(arquivos(self.IF_INET6_GLOBAL, self.ROTA_PADRAO))
        assert not mod_proxy.tem_ipv6_global(arquivos(self.IF_INET6_SO_LOCAL, self.ROTA_PADRAO))
        assert not mod_proxy.tem_ipv6_global(arquivos(self.IF_INET6_GLOBAL, ""))

    def test_ordem(self):
        v4a, v4b, v6a, v6b = (socket.AF_INET, "1.1.1.1"), (socket.AF_INET, "2.2.2.2"), (socket.AF_INET6, "::a"), (socket.AF_INET6, "::b")
        assert mod_proxy.ordenar([v6a, v6b, v4a, v4b], ipv6=False) == [v4a, v4b]
        assert mod_proxy.ordenar([v6a, v6b, v4a, v4b], ipv6=True) == [v6a, v4a, v6b, v4b]

    def test_cache_de_dns(self):
        chamadas, agora = [], [0.0]

        def resolver(host, porta, type=None):
            chamadas.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", porta))]

        proxy = ProxyOficial(0, resolver=resolver, relogio=lambda: agora[0])
        proxy.resolver("www.dell.com", 443)
        agora[0] = 100
        proxy.resolver("www.dell.com", 443)
        assert chamadas == ["www.dell.com"]
        agora[0] = 200
        proxy.resolver("www.dell.com", 443)
        assert chamadas == ["www.dell.com"] * 2

    def test_endereco_que_falha_nao_prende_e_vai_para_o_proximo(self):
        eco = socket.create_server(("127.0.0.1", 0))
        porta = eco.getsockname()[1]
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", 9)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", porta))]
        proxy = ProxyOficial(0, resolver=lambda *a, **k: infos, ipv6=lambda: False)
        conexao = proxy.conectar("dl.dell.com", porta)
        assert conexao is not None
        conexao.close()
        eco.close()
        sem_nome = ProxyOficial(0, resolver=lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("x")))
        assert sem_nome.conectar("dl.dell.com", 443) is None and "DNS" in sem_nome.resumo()["falhas"]["dl.dell.com"]


class TestBloqueio:
    """Página "Site bloqueado pelo MSscan: <domínio>" em vez de timeout (CLAUDE.md 15.1)."""

    def test_extensao_usa_a_mesma_lista_do_proxy(self):
        import io
        import json
        import zipfile

        from msscan.navegador.navegador import EXTENSAO_ID, extensao_bloqueio
        from msscan.navegador.proxy import DOMINIOS_OFICIAIS

        with zipfile.ZipFile(io.BytesIO(extensao_bloqueio())) as xpi:
            manifesto = json.loads(xpi.read("manifest.json"))
            fundo = xpi.read("fundo.js").decode()
        assert manifesto["browser_specific_settings"]["gecko"]["id"] == EXTENSAO_ID
        assert f"const PERMITIDOS = {json.dumps(list(DOMINIOS_OFICIAIS))};" in fundo
        assert 'const BLOQUEIO = "http://127.0.0.1:8765/bloqueado?dominio=";' in fundo
        assert '"main_frame", "sub_frame"' in fundo and '["blocking"]' in fundo

    def test_pagina_escapa_o_dominio(self):
        from msscan.interface.servidor import pagina_bloqueio

        assert "Site bloqueado pelo MSscan: <code>example.com</code>" in pagina_bloqueio("Example.com")
        pagina = pagina_bloqueio('"><script>alert(1)</script>')
        assert "<script" not in pagina and "(endereço não permitido)" in pagina

    def test_preferencias_de_velocidade(self, tmp_path):
        from msscan.navegador.navegador import preferencias

        prefs = preferencias(tmp_path, 47480)
        for linha in ('"network.http.http3.enable", false', '"browser.cache.disk.enable", false', '"network.http.http2.enabled", true',
                      '"browser.cache.memory.enable", true', '"xpinstall.signatures.required", false'):
            assert linha in prefs, linha


def test_google_liberado_so_para_o_recaptcha():
    """15/10/2026: "Site bloqueado: www.google.com" ao abrir a página de drivers (R530 e R620); o usuário autorizou liberar."""
    from msscan.navegador.proxy import permitido

    assert all(permitido(h, 443) for h in ("www.google.com", "www.gstatic.com", "www.recaptcha.net", "www.dell.com"))
    assert not any(permitido(h, 443) for h in ("google.com", "accounts.google.com", "mail.google.com", "gstatic.com",
                                                "www.google.com.br", "googletagmanager.com", "www.youtube.com"))
    assert not permitido("www.google.com", 80)

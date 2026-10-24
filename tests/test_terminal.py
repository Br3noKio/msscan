"""Terminal embutido (CLAUDE.md 15.5): pseudoterminal, rotas só com token, isolado da IA e do pareamento."""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from msscan.core.executor import ExecutorReplay
from msscan.interface import servidor as modulo_servidor
from msscan.interface.servidor import ServidorUI
from msscan.interface.sessao import Sessao
from msscan.interface.terminal import ARQUIVOS_XTERM, Terminal, boas_vindas
from tests.conftest import R640

RAIZ = Path(__file__).parent.parent


def esperar_saida(terminal: Terminal, trecho: str, limite: float = 10) -> str:
    fim = time.monotonic() + limite
    texto = ""
    while time.monotonic() < fim:
        dados, _, _ = terminal.ler(0, espera=0.2)
        texto = dados.decode("utf-8", "replace")
        if trecho in texto:
            return texto
    raise AssertionError(f"{trecho!r} não apareceu em: {texto!r}")


def test_boas_vindas_com_pretty_name(tmp_path):
    arquivo = tmp_path / "os-release"
    arquivo.write_text('NAME="Debian"\nPRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n', encoding="utf-8")
    assert boas_vindas("0.12.0", arquivo) == "MSscan 0.12.0 - baseado em Debian GNU/Linux 13 (trixie)"
    assert boas_vindas("0.12.0", tmp_path / "nao-existe") == "MSscan 0.12.0 - baseado em Linux"


def test_shell_no_pseudoterminal():
    terminal = Terminal("teste", ("setsid", "-c", "/bin/sh"), liberado=True)
    try:
        assert terminal.abrir()[0] and terminal.ativo
        assert "MSscan teste - baseado em" in esperar_saida(terminal, "baseado em")
        assert terminal.redimensionar(30, 100)
        assert terminal.escrever("echo resultado-$((6*7)); stty size\n")
        texto = esperar_saida(terminal, "30 100")
        assert "resultado-42" in texto
        assert not terminal.redimensionar(0, 100)  # tamanho absurdo recusado
        terminal.escrever("exit\n")
        esperar_saida(terminal, "terminal encerrado")
        assert not terminal.ativo and not terminal.escrever("echo x\n")
    finally:
        terminal.encerrar()


def test_saida_limitada_mantem_posicao_absoluta(monkeypatch):
    from msscan.interface import terminal as modulo

    monkeypatch.setattr(modulo, "LIMITE_SAIDA", 10)
    terminal = Terminal("teste")
    with terminal._cond:
        terminal._acrescentar(b"0123456789abcdef")
    dados, fim, ativo = terminal.ler(0, espera=0)
    assert (dados, fim, ativo) == (b"6789abcdef", 16, False)
    assert terminal.ler(14, espera=0)[0] == b"ef"


@pytest.fixture
def servidor_terminal():
    sessao = Sessao(ExecutorReplay(R640), None, replay=True)
    terminal = Terminal("teste", ("setsid", "-c", "/bin/sh"), liberado=True)
    srv = ServidorUI(sessao, porta=0, terminal=terminal)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield srv
    terminal.encerrar()
    srv.shutdown()
    srv.server_close()


def chamar(srv: ServidorUI, caminho: str, corpo: dict | None = None, token: str | None = ""):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-MSscan-Token"] = token or srv.token
    dados = None if corpo is None else json.dumps(corpo).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{srv.porta}{caminho}", data=dados, headers=headers,
                                 method="GET" if corpo is None else "POST")  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=10) as resposta:
            return resposta.status, resposta.read()
    except urllib.error.HTTPError as erro:
        return erro.code, erro.read()


def test_rotas_exigem_token(servidor_terminal):
    assert chamar(servidor_terminal, "/api/terminal/abrir", {}, token=None)[0] == 403
    assert chamar(servidor_terminal, "/api/terminal/abrir", {}, token="errado")[0] == 403
    assert chamar(servidor_terminal, "/api/terminal/ler?desde=0", token=None)[0] == 403
    assert not servidor_terminal.terminal.ativo


def test_terminal_pela_api(servidor_terminal):
    assert chamar(servidor_terminal, "/api/terminal/abrir", {})[0] == 200
    assert chamar(servidor_terminal, "/api/terminal/tamanho", {"linhas": 24, "colunas": 90})[0] == 200
    assert chamar(servidor_terminal, "/api/terminal/tamanho", {"linhas": "24", "colunas": 90})[0] == 400
    assert chamar(servidor_terminal, "/api/terminal/escrever", {"dados": "echo api-$((2+3))\n"})[0] == 200
    fim = time.monotonic() + 10
    texto = ""
    while time.monotonic() < fim and "api-5" not in texto:
        status, corpo = chamar(servidor_terminal, "/api/terminal/ler?desde=0")
        resposta = json.loads(corpo)
        texto = base64.b64decode(resposta["dados"]).decode("utf-8", "replace")
    assert status == 200 and "api-5" in texto and resposta["ativo"] is True


def test_sem_terminal_no_replay_e_rotas_recusam():
    sessao = Sessao(ExecutorReplay(R640), None, replay=True)
    srv = ServidorUI(sessao, porta=0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        assert chamar(srv, "/api/terminal/abrir", {})[0] == 409
        assert chamar(srv, "/api/terminal/ler?desde=0")[0] == 409
    finally:
        srv.shutdown()
        srv.server_close()
    assert "terminal=None if args.replay else Terminal(\n" in (RAIZ / "main.py").read_text(encoding="utf-8")


def test_arquivos_do_xterm_so_da_lista(servidor_terminal, tmp_path, monkeypatch):
    destino = tmp_path / ARQUIVOS_XTERM["xterm.js"][0]
    destino.parent.mkdir(parents=True)
    destino.write_text("/* xterm */", encoding="utf-8")
    monkeypatch.setattr(modulo_servidor, "PASTA_XTERM", tmp_path)
    assert chamar(servidor_terminal, "/estaticos/xterm/xterm.js", token=None) == (200, b"/* xterm */")
    assert chamar(servidor_terminal, "/estaticos/xterm/xterm.css", token=None)[0] == 404  # não instalado
    assert chamar(servidor_terminal, "/estaticos/xterm/../../../etc/passwd", token=None)[0] == 404


def test_ia_e_pareamento_sem_acesso_ao_terminal():
    """A IA (análise e chat) e o servidor do celular não têm nenhuma referência ao terminal."""
    arquivos = list((RAIZ / "msscan/ia").glob("*.py")) + [RAIZ / "msscan/interface/sessao.py",
                                                          RAIZ / "msscan/interface/pareamento.py"]  # fmt: skip
    for arquivo in arquivos:
        assert not re.search(r"interface\.terminal|interface import terminal|Terminal\(|(self|server|servidor)\.terminal\b", arquivo.read_text(encoding="utf-8")), arquivo


def test_terminal_desligado_por_padrao_e_ativado_pelo_tecnico(servidor_terminal):
    """1.0.0: terminal e consoles de texto desligados por padrão; ligados pelo botão ou por ``msscan.terminal=1``."""
    servidor_terminal.terminal.liberado = False
    assert Terminal("teste").liberado is False
    assert chamar(servidor_terminal, "/api/terminal/abrir", {})[0] == 423 and not servidor_terminal.terminal.ativo
    assert chamar(servidor_terminal, "/api/terminal/ativar", {}, token=None)[0] == 403
    assert chamar(servidor_terminal, "/api/terminal/ativar", {})[0] == 200
    assert chamar(servidor_terminal, "/api/terminal/abrir", {})[0] == 200 and servidor_terminal.terminal.ativo
    assert 'parametro_boot(parse_cmdline(executor.ler_arquivo("/proc/cmdline") or ""), "terminal") == "1"' in (RAIZ / "main.py").read_text()
    iso = RAIZ / "iso/config"
    assert "NAutoVTs=0" in (iso / "includes.chroot/etc/systemd/logind.conf.d/10-msscan.conf").read_text()
    assert "ConditionKernelCommandLine=msscan.terminal=1" in (iso / "includes.chroot/etc/systemd/system/msscan-consoles.service").read_text()
    assert "systemctl enable msscan-consoles.service" in (iso / "hooks/normal/9000-msscan.hook.chroot").read_text()

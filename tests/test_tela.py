"""Resolução de vídeo e fonte do console (CLAUDE.md 12c). Saídas reais do xrandr capturadas no QEMU."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from msscan.core.executor import Executor
from msscan.parsers import ErroParser
from msscan.parsers.xrandr import parse_xrandr
from msscan.tela.console import aplicar_fonte, escolher_fonte, parse_virtual_size
from msscan.tela.resolucao import (
    ControleTela,
    ajustar_automaticamente,
    cadeia_de_modos,
    nome_modo,
    parse_resolucao,
    resolucao_forcada,
    saida_principal,
)
from tests.conftest import PASTA_FIXTURES, ExecutorFalso

XRANDR = PASTA_FIXTURES / "avulsas" / "xrandr"
ISO = Path(__file__).parent.parent / "iso"


def props(nome: str) -> str:
    return (XRANDR / f"props_{nome}.txt").read_text()


def trocar_atual(texto: str, saida: str, modo: str) -> str:
    """A mesma saída real do xrandr depois de trocar o modo (move o ``*`` e o "current")."""
    linhas = []
    largura, altura = modo.split("x")
    for linha in texto.splitlines():
        if linha.startswith("Screen 0:"):
            antes, _, resto = linha.partition("current ")
            linha = antes + f"current {largura} x {altura}," + resto.split(",", 1)[1]
        elif linha.startswith(f"{saida} connected"):
            partes = linha.split(" ")
            partes[3 if partes[2] == "primary" else 2] = f"{modo}+0+0"
            linha = " ".join(partes)
        elif linha.startswith("   ") and "x" in linha.split()[0]:
            linha = linha.replace("*", " ")
            if linha.split()[0] == modo:
                taxa = linha.split()[1].rstrip("+")
                linha = linha.replace(taxa, taxa + "*", 1).replace("* +", "*+")
        linhas.append(linha)
    return "\n".join(linhas) + "\n"


class ExecutorXrandr(Executor):
    """xrandr simulado sobre uma saída real: aceita os modos em ``aceitos`` e recusa os demais."""

    origem = "falso"

    def __init__(self, texto: str, aceitos: set[str] | None = None) -> None:
        super().__init__()
        self.texto = texto
        self.aceitos = aceitos
        self.arquivos: dict[str, str] = {}

    def _rodar(self, comando, timeout):
        if comando == ["xrandr", "--props"]:
            return 0, self.texto, "", 0.0
        if comando[:2] == ["xrandr", "--output"] and comando[3] == "--mode":
            if self.aceitos is not None and comando[4] not in self.aceitos:
                return 1, "", "xrandr: Configure crtc 0 failed", 0.0
            self.texto = trocar_atual(self.texto, comando[2], comando[4])
            return 0, "", "", 0.0
        return -1, "", "sem gravação", 0.0

    def _ler_arquivo(self, caminho):
        return self.arquivos.get(caminho)

    def _existe(self, caminho):
        return False

    def _listar(self, caminho):
        return []

    def _tem_comando(self, nome):
        return nome == "xrandr"


# --- Parser --------------------------------------------------------------------------------------


class TestParser:
    def test_edid_1920x1080(self):
        dados = parse_xrandr(props("edid1080"))
        saida = saida_principal(dados)
        assert (saida["nome"], saida["edid"], saida["atual"]) == ("Virtual-1", True, (1920, 1080))
        primeiro = saida["modos"][0]
        assert (primeiro["largura"], primeiro["altura"], primeiro["atual"], primeiro["preferido"]) == (1920, 1080, True, True)
        assert primeiro["taxas"] == [75.0, 60.0, 50.0] and len(saida["modos"]) == 12

    def test_sem_edid_driver_ainda_marca_preferido(self):
        saida = saida_principal(parse_xrandr(props("semedid")))
        assert not saida["edid"] and saida["modos"][0]["preferido"] and saida["atual"] == (1024, 768)

    def test_cirrus_limitada(self):
        dados = parse_xrandr(props("cirrus"))
        assert dados["maximo"] == (2044, 1024) and saida_principal(dados)["nome"] == "VGA-1"

    def test_vazio(self):
        with pytest.raises(ErroParser):
            parse_xrandr("")


# --- Cadeia de modos -------------------------------------------------------------------------------


@pytest.mark.parametrize(("nome", "esperado"), [
    ("edid1080", ["1920x1080", "1280x1024", "1024x768", "800x600"]),
    ("edid1280", ["1280x1024", "1920x1080", "1024x768", "800x600"]),  # preferido, depois o maior até 1920x1080
    ("semedid", ["1024x768", "800x600"]),                             # sem EDID: modo seguro
    ("cirrus", ["1024x768", "800x600"]),
])  # fmt: skip
def test_cadeia(nome, esperado):
    assert [nome_modo(m) for m in cadeia_de_modos(saida_principal(parse_xrandr(props(nome))))] == esperado


def test_resolucao_forcada_vem_primeiro():
    saida = saida_principal(parse_xrandr(props("semedid")))
    assert nome_modo(cadeia_de_modos(saida, (1600, 900))[0]) == "1600x900"


@pytest.mark.parametrize(("cmdline", "esperado"), [
    ("boot=live msscan.resolucao=1280x1024 quiet", (1280, 1024)),
    ("msscan.resolucao=1920X1080", (1920, 1080)),
    ("msscan.resolucao=abc", None), ("msscan.resolucao=10x10", None), ("boot=live", None), (None, None),
])  # fmt: skip
def test_parametro_de_boot(cmdline, esperado):
    assert resolucao_forcada(cmdline) == esperado


def test_parse_resolucao():
    assert parse_resolucao(" 800x600 ") == (800, 600) and parse_resolucao("800") is None


# --- Aplicação automática (boot) --------------------------------------------------------------------


class TestAutomatico:
    def test_modo_preferido(self):
        executor = ExecutorXrandr(props("semedid").replace("\tlink-status", "\tEDID: \n\t\t00ff\n\tlink-status", 1))
        # Mesmo monitor, agora "com EDID" e 1024x768 como preferido: aplica o preferido.
        resultado = ajustar_automaticamente(executor, "boot=live")
        assert resultado["ok"] and resultado["modo"] == "1024x768" and resultado["edid"]

    def test_modo_recusado_desce_para_o_proximo(self):
        # Vídeo de servidor limitado: recusa 1920x1080; o próximo da cadeia é aceito.
        executor = ExecutorXrandr(trocar_atual(props("edid1080"), "Virtual-1", "800x600"), aceitos={"1280x1024", "1024x768", "800x600"})
        resultado = ajustar_automaticamente(executor, "")
        assert resultado["ok"] and resultado["modo"] == "1280x1024"
        assert resultado["tentativas"] == ["1920x1080: xrandr: Configure crtc 0 failed", "1280x1024: ok"]

    def test_sem_edid_modo_seguro(self):
        executor = ExecutorXrandr(trocar_atual(props("cirrus"), "VGA-1", "800x600"))
        resultado = ajustar_automaticamente(executor, "")
        assert resultado["modo"] == "1024x768" and "sem EDID: modo seguro" in resultado["mensagem"]

    def test_forcada_nao_anunciada(self):
        executor = ExecutorXrandr(props("cirrus"))
        resultado = ajustar_automaticamente(executor, "msscan.resolucao=1920x1080")
        assert resultado["modo"] == "1024x768" and "não anunciado" in resultado["tentativas"][0]

    def test_forcada_aceita(self):
        executor = ExecutorXrandr(props("edid1080"))
        resultado = ajustar_automaticamente(executor, "msscan.resolucao=1280x1024")
        assert resultado["modo"] == "1280x1024" and resultado["forcada"] == "1280x1024"

    def test_tudo_recusado_mantem(self):
        executor = ExecutorXrandr(props("cirrus"), aceitos=set())
        resultado = ajustar_automaticamente(executor, "")
        assert not resultado["ok"] and resultado["modo"] == "1024x768" and "mantido" in resultado["mensagem"]

    def test_sem_xrandr(self):
        assert not ajustar_automaticamente(ExecutorFalso(), "")["ok"]


# --- Troca pela interface ------------------------------------------------------------------------------


class TestControleTela:
    def controle(self, **kwargs):
        chamadas: list = []
        executor = ExecutorXrandr(props("edid1080"), **kwargs)
        executor.arquivos["/run/msscan/tela.json"] = json.dumps({"ok": True, "modo": "1920x1080"})
        controle = ControleTela(executor, "/run/msscan/tela.json", temporizador=lambda s, f: chamadas.append((s, f)))
        return controle, chamadas

    def test_estado(self):
        controle, _ = self.controle()
        estado = controle.estado()
        assert estado["disponivel"] and estado["atual"] == "1920x1080" and estado["edid"]
        assert estado["modos"][0] == {"nome": "1920x1080", "atual": True, "preferido": True, "entrelacado": False}
        assert estado["automatico"]["modo"] == "1920x1080" and estado["pendente"] is None

    def test_trocar_e_manter(self):
        controle, chamadas = self.controle()
        ok, _ = controle.trocar("1280x1024")
        assert ok and controle.estado()["atual"] == "1280x1024" and chamadas[0][0] == 15
        assert controle.estado()["pendente"]["anterior"] == "1920x1080"
        assert controle.confirmar() and controle.estado()["pendente"] is None and controle.estado()["atual"] == "1280x1024"

    def test_sem_confirmacao_volta(self):
        controle, chamadas = self.controle()
        controle.trocar("800x600")
        chamadas[0][1]()  # passaram 15 s
        assert controle.estado()["atual"] == "1920x1080" and "voltou para 1920x1080" in controle.mensagem

    def test_voltar_agora(self):
        controle, _ = self.controle()
        controle.trocar("1024x768")
        assert controle.reverter() and controle.estado()["atual"] == "1920x1080"

    def test_recusado_ou_invalido(self):
        controle, _ = self.controle(aceitos={"1920x1080"})
        ok, mensagem = controle.trocar("1280x1024")
        assert not ok and "recusado" in mensagem and controle.estado()["atual"] == "1920x1080"
        assert controle.trocar("1234x567") == (False, "1234x567 não é anunciado por Virtual-1")
        assert not controle.trocar("xyz")[0] and not controle.confirmar()

    def test_indisponivel_sem_x(self):
        estado = ControleTela(ExecutorFalso()).estado()
        assert not estado["disponivel"] and "tela cheia" in estado["motivo"]


def test_api_de_tela():
    import threading
    import urllib.request

    from msscan.core.executor import ExecutorReplay
    from msscan.interface.servidor import ServidorUI
    from msscan.interface.sessao import Sessao
    from tests.conftest import R640

    sessao = Sessao(ExecutorReplay(R640), None, replay=True)
    srv = ServidorUI(sessao, porta=0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def pedir(caminho, metodo="GET", dados=None):
        req = urllib.request.Request(f"http://127.0.0.1:{srv.porta}/api/{caminho}", method=metodo,
                                     data=json.dumps(dados).encode() if dados is not None else None,
                                     headers={"X-MSscan-Token": srv.token, "Content-Type": "application/json"})  # fmt: skip
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as erro:
            return erro.code, json.loads(erro.read() or b"{}")

    try:
        assert pedir("tela") == (200, {"disponivel": False, "motivo": "Alteração de resolução indisponível neste modo."})
        assert pedir("tela/modo", "POST", {"modo": "800x600"})[0] == 409
        sessao.tela = ControleTela(ExecutorXrandr(props("edid1080")), temporizador=lambda s, f: None)
        assert pedir("tela")[1]["atual"] == "1920x1080"
        assert pedir("tela/modo", "POST", {"modo": "1280x1024"})[0] == 200
        pendente = pedir("tela")[1]["pendente"]
        assert pendente["modo"] == "1280x1024" and 13 <= pendente["restante_s"] <= 15
        assert pedir("tela/manter", "POST", {})[0] == 200 and pedir("tela")[1]["pendente"] is None
    finally:
        srv.shutdown()
        srv.server_close()


# --- Fonte do console -------------------------------------------------------------------------------


@pytest.mark.parametrize(("resolucao", "fonte"), [
    ((640, 480), "Uni2-Terminus16.psf.gz"), ((800, 600), "Uni2-Terminus16.psf.gz"),
    ((1024, 768), "Uni2-Terminus20x10.psf.gz"), ((1280, 1024), "Uni2-Terminus24x12.psf.gz"),
    ((1366, 768), "Uni2-Terminus24x12.psf.gz"), ((1920, 1080), "Uni2-Terminus32x16.psf.gz"),
])  # fmt: skip
def test_fonte_do_console(resolucao, fonte):
    assert escolher_fonte(*resolucao) == fonte


def test_aplicar_fonte():
    executor = ExecutorFalso(existentes={"/dev/tty1", "/dev/tty2"})
    executor.arquivos["/sys/class/graphics/fb0/virtual_size"] = "1920,1080\n"  # formato real (lido no QEMU)
    for tty in ("/dev/tty1", "/dev/tty2"):
        executor.comandos[("setfont", "-C", tty, "/usr/share/consolefonts/Uni2-Terminus32x16.psf.gz")] = (0, "", "")
    assert aplicar_fonte(executor) == "resolução 1920x1080: fonte /usr/share/consolefonts/Uni2-Terminus32x16.psf.gz"
    assert parse_virtual_size("abc") is None and "mantida" in aplicar_fonte(ExecutorFalso())


# --- ISO -----------------------------------------------------------------------------------------------


def test_iso_video():
    pacotes = (ISO / "config/package-lists/msscan.list.chroot").read_text()
    assert "\nx11-xserver-utils\n" in pacotes
    assert "systemctl enable msscan-fonte.service" in (ISO / "config/hooks/normal/9000-msscan.hook.chroot").read_text()
    unidade = (ISO / "config/includes.chroot/etc/systemd/system/msscan-fonte.service").read_text()
    assert "Before=msscan.service" in unidade and "python3 -m msscan.tela.console" in unidade


def test_hook_grub_resolucao_nativa(tmp_path):
    hook = ISO / "config/hooks/normal/9030-video-boot.hook.binary"
    (tmp_path / "boot" / "grub").mkdir(parents=True)
    cfg = tmp_path / "boot" / "grub" / "config.cfg"
    # Trecho igual ao gerado pelo live-build na ISO v0.8.0.
    cfg.write_text("if loadfont $font ; then\n    set gfxmode=800x600\n    set gfxpayload=keep\n    insmod efi_gop\nelse\n    set gfxmode=auto\n    insmod all_video\nfi\n")
    for _ in range(2):
        assert subprocess.run(["sh", str(hook)], cwd=tmp_path, timeout=10).returncode == 0
    texto = cfg.read_text()
    assert "800x600" not in texto and texto.count("set gfxmode=auto") == 2 and texto.count("set gfxpayload=keep") == 1

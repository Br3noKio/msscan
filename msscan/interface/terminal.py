"""Terminal embutido da interface (CLAUDE.md 15.5): shell de administrador num pseudoterminal.

Segurança:

- existe só no servidor local da interface (127.0.0.1 + token); o servidor do pareamento do celular não o conhece;
- é do servidor, não da ``Sessao``: a IA (análise e chat) não tem nenhuma referência a ele, nem lê a saída;
- desligado no ``--replay`` (a demonstração não roda nada no computador).

O navegador busca a saída por "long-poll" (``ler``), em bytes (base64 na API: UTF-8 cortado no meio de um caractere
é remontado pelo xterm.js), e envia o que foi digitado por ``escrever``.
"""

from __future__ import annotations

import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
import threading
from collections.abc import Callable
from pathlib import Path

from msscan import textos

PASTA_XTERM = Path("/usr/share/nodejs")
"""Biblioteca local do pacote Debian ``node-xterm`` (sem CDN)."""
ARQUIVOS_XTERM = {
    "xterm.js": ("xterm/lib/xterm.js", "text/javascript; charset=utf-8"),
    "xterm.css": ("xterm/css/xterm.css", "text/css; charset=utf-8"),
    "xterm-addon-fit.js": ("xterm-addon-fit/lib/xterm-addon-fit.js", "text/javascript; charset=utf-8"),
    "xterm-addon-canvas.js": ("xterm-addon-canvas/lib/xterm-addon-canvas.js", "text/javascript; charset=utf-8"),
}
COMANDO_PADRAO = ("setsid", "-c", "/bin/bash", "-l")
"""``setsid -c``: sessão nova com o pseudoterminal como terminal de controle (Ctrl+C e controle de tarefas)."""
LIMITE_SAIDA = 512 * 1024
"""Saída guardada para quem abrir a aba depois (o que passar disso some do início)."""


def boas_vindas(versao: str, os_release: Path = Path("/etc/os-release")) -> str:
    """Primeira linha do terminal: ``MSscan <versão> - baseado em <PRETTY_NAME>``."""
    nome = "Linux"
    try:
        for linha in os_release.read_text(encoding="utf-8", errors="replace").splitlines():
            if linha.startswith("PRETTY_NAME="):
                nome = linha.split("=", 1)[1].strip().strip('"').strip("'") or nome
    except OSError:
        pass
    return f"MSscan {versao} - baseado em {nome}"


class Terminal:
    """Um shell por vez, num pseudoterminal; a saída fica num buffer com posição absoluta."""

    def __init__(self, versao: str, comando: tuple[str, ...] = COMANDO_PADRAO, apresentacao: Callable[[], str] | None = None,
                 liberado: bool = False) -> None:  # fmt: skip
        self.versao = versao
        self.liberado = liberado
        """1.0.0: desligado por padrão; o técnico liga pelo botão "Ativar terminal" ou com o parâmetro de boot ``msscan.terminal=1``."""
        self.comando = comando
        self.apresentacao = apresentacao
        """``msscan-fetch`` ao abrir (09/10/2026): texto pronto, montado com os dados que a sessão já tem."""
        self._cond = threading.Condition()
        self._saida = bytearray()
        self._inicio = 0  # posição absoluta do primeiro byte de _saida
        self._processo: subprocess.Popen[bytes] | None = None
        self._fd: int | None = None

    def liberar(self) -> None:
        self.liberado = True

    @property
    def ativo(self) -> bool:
        """Há um shell rodando."""
        return self._fd is not None

    @property
    def fim(self) -> int:
        """Posição absoluta do fim da saída."""
        return self._inicio + len(self._saida)

    def abrir(self) -> tuple[bool, str]:
        """Inicia o shell (se ainda não houver um). Devolve (ok, mensagem)."""
        with self._cond:
            if not self.liberado:
                return False, textos.t("py_terminal.desligado")
            if self.ativo:
                return True, "terminal já aberto"
            mestre, escravo = pty.openpty()
            ambiente = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", MSSCAN_TERMINAL="1")
            try:
                self._processo = subprocess.Popen(
                    self.comando, stdin=escravo, stdout=escravo, stderr=escravo, env=ambiente,
                    cwd=os.path.expanduser("~"), close_fds=True,
                )  # fmt: skip
            except OSError as erro:
                os.close(mestre)
                os.close(escravo)
                return False, f"não foi possível abrir o shell: {erro}"
            os.close(escravo)
            self._fd = mestre
            self._acrescentar(("\x1b[1m" + boas_vindas(self.versao) + "\x1b[0m\r\n").encode("utf-8"))
            if self.apresentacao is not None:
                try:
                    self._acrescentar(("\r\n" + self.apresentacao() + "\r\n").encode("utf-8"))
                except Exception:  # noqa: BLE001 - o resumo nunca impede o terminal de abrir
                    pass
        threading.Thread(target=self._ler_continuo, args=(mestre,), name="msscan-terminal", daemon=True).start()
        return True, "terminal aberto"

    def _acrescentar(self, dados: bytes) -> None:
        """Acrescenta à saída (com o lock) e acorda quem espera."""
        self._saida += dados
        excesso = len(self._saida) - LIMITE_SAIDA
        if excesso > 0:
            del self._saida[:excesso]
            self._inicio += excesso
        self._cond.notify_all()

    def _ler_continuo(self, fd: int) -> None:
        while True:
            try:
                dados = os.read(fd, 65536)
            except OSError:
                dados = b""
            if not dados:
                break
            with self._cond:
                self._acrescentar(dados)
        with self._cond:
            if self._processo is not None:
                try:
                    self._processo.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            self._processo = None
            if self._fd == fd:
                self._fd = None
            try:
                os.close(fd)
            except OSError:
                pass
            self._acrescentar("\r\n[terminal encerrado — use \"Novo terminal\" para abrir outro]\r\n".encode("utf-8"))

    def ler(self, desde: int, espera: float = 1.0) -> tuple[bytes, int, bool]:
        """Saída a partir de ``desde`` (espera até ``espera`` s por dados novos). Devolve (dados, fim, ativo)."""
        with self._cond:
            if desde >= self.fim and self.ativo:
                self._cond.wait(espera)
            inicio = min(max(desde, self._inicio), self.fim)
            return bytes(self._saida[inicio - self._inicio:]), self.fim, self.ativo

    def escrever(self, texto: str) -> bool:
        """Envia o que foi digitado (ou colado) ao shell."""
        fd = self._fd
        if fd is None:
            return False
        dados = texto.encode("utf-8", "replace")
        try:
            while dados:
                dados = dados[os.write(fd, dados):]
        except OSError:
            return False
        return True

    def redimensionar(self, linhas: int, colunas: int) -> bool:
        """Informa ao shell o tamanho da janela (programas como ``top`` e ``vi`` usam)."""
        fd = self._fd
        if fd is None or not (2 <= linhas <= 500 and 2 <= colunas <= 1000):
            return False
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", linhas, colunas, 0, 0))
        except OSError:
            return False
        return True

    def encerrar(self) -> None:
        """Encerra o shell e tudo o que ele abriu (grupo do processo), ao fechar a interface."""
        processo = self._processo
        if processo is None:
            return
        try:
            os.killpg(processo.pid, signal.SIGHUP)
        except OSError:
            try:
                processo.kill()
            except OSError:
                pass

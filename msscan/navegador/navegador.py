"""Navegador integrado (CLAUDE.md 11b): Firefox ESR como ``msscan-web``, perfil temporário, só pelo proxy oficial.

Só na ISO (``--ui-navegador``): lá existem o usuário ``msscan-web``, a política do Firefox (lista de domínios, barra
com "Voltar ao MSscan"), o atalho Alt+M do gerenciador de janelas e o firewall que só deixa esse usuário falar com
o proxy do MSscan e com a interface.

- Perfil novo em ``/tmp`` (0700, dono ``msscan-web``): sem senhas, histórico nem cookies depois de fechar — a pasta
  inteira é apagada. Login no site do fabricante vale enquanto o navegador estiver aberto.
- Janela com classe própria (``--class MSscanNavegador``): "Voltar ao MSscan" esconde a janela (o quiosque volta a
  aparecer) e "Abrir" mostra de novo, pelo ``xdotool``.
- Downloads caem em ``<perfil>/downloads``; concluído (sem ``.part``, tamanho estável) → ``ao_baixar(caminho)``.
  Nada é executado: quem recebe só copia e analisa.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

from msscan.navegador.proxy import DOMINIOS_OFICIAIS, PORTA as PORTA_PROXY

USUARIO = "msscan-web"
CLASSE_JANELA = "MSscanNavegador"
INTERVALO_S = 1.0
PORTA_INTERFACE = 8765
EXTENSAO_ID = "bloqueio@msscan"
"""Extensão local (sem loja, sem rede) que troca o bloqueio por "Site bloqueado pelo MSscan: <domínio>" (CLAUDE.md 15.1).
Na ISO ela é gerada no build (``/usr/local/share/msscan/bloqueio.xpi``) e instalada à força pela política do Firefox
(``ExtensionSettings``, ``force_installed``: o usuário não desativa). Substitui a ``WebsiteFilter`` da política, cuja
página de bloqueio é a genérica do Firefox; o proxy e o firewall continuam sendo a garantia de rede."""
ARQUIVO_EXTENSAO = "/usr/local/share/msscan/bloqueio.xpi"


def extensao_bloqueio(dominios: tuple[str, ...] = DOMINIOS_OFICIAIS, porta_interface: int = PORTA_INTERFACE) -> bytes:
    """``.xpi`` (zip) da extensão: navegação (página ou quadro) fora dos domínios oficiais, ou sem HTTPS, vai para a
    página de bloqueio da interface, na hora — em vez de esperar o proxy/firewall recusar. Mesma lista do proxy."""
    manifesto = {"manifest_version": 2, "name": "MSscan: sites bloqueados", "version": "1.0",
                 "browser_specific_settings": {"gecko": {"id": EXTENSAO_ID}},
                 "permissions": ["webRequest", "webRequestBlocking", "<all_urls>"],
                 "background": {"scripts": ["fundo.js"]}}  # fmt: skip
    fundo = f"""const PERMITIDOS = {json.dumps(list(dominios))};
const BLOQUEIO = "http://127.0.0.1:{porta_interface}/bloqueado?dominio=";
function oficial(host) {{
  host = host.toLowerCase();
  return PERMITIDOS.some((d) => host === d || host.endsWith("." + d));
}}
browser.webRequest.onBeforeRequest.addListener((pedido) => {{
  const url = new URL(pedido.url);
  if (url.protocol === "http:" && url.hostname === "127.0.0.1" && url.port === "{porta_interface}") return {{}};
  if (["about:", "moz-extension:", "blob:", "data:"].includes(url.protocol)) return {{}};
  if (url.protocol === "https:" && (!url.port || url.port === "443") && oficial(url.hostname)) return {{}};
  if (url.protocol === "http:" && !url.port && oficial(url.hostname)) {{
    url.protocol = "https:";  // domínio oficial por HTTP: vai para o HTTPS (o proxy só abre HTTPS)
    return {{ redirectUrl: url.href }};
  }}
  return {{ redirectUrl: BLOQUEIO + encodeURIComponent(url.hostname || url.protocol) }};
}}, {{ urls: ["<all_urls>"], types: ["main_frame", "sub_frame"] }}, ["blocking"]);
"""
    saida = io.BytesIO()
    with zipfile.ZipFile(saida, "w", zipfile.ZIP_DEFLATED) as zipado:
        zipado.writestr("manifest.json", json.dumps(manifesto, indent=1))
        zipado.writestr("fundo.js", fundo)
    return saida.getvalue()


def preferencias(downloads: Path, porta_proxy: int) -> str:
    """``user.js`` do perfil temporário: tudo pelo proxy oficial, downloads direto na pasta, nada guardado."""
    prefs = {
        "network.proxy.type": 1, "network.proxy.ssl": "127.0.0.1", "network.proxy.ssl_port": porta_proxy,
        "network.proxy.http": "127.0.0.1", "network.proxy.http_port": porta_proxy, "network.proxy.share_proxy_settings": True,
        "network.proxy.no_proxies_on": "", "network.proxy.allow_hijacking_localhost": False,
        "network.trr.mode": 5, "dom.security.https_only_mode": True,
        "browser.download.dir": str(downloads), "browser.download.folderList": 2, "browser.download.useDownloadDir": True,
        "browser.download.always_ask_before_handling_new_types": False, "browser.download.start_downloads_in_tmp_dir": False,
        "browser.download.open_pdf_attachments_inline": True, "browser.safebrowsing.downloads.enabled": False,
        "browser.safebrowsing.malware.enabled": False, "browser.safebrowsing.phishing.enabled": False,
        "signon.rememberSignons": False, "browser.formfill.enable": False, "places.history.enabled": False,
        "privacy.sanitize.sanitizeOnShutdown": True, "browser.shell.checkDefaultBrowser": False,
        "browser.aboutwelcome.enabled": False, "startup.homepage_welcome_url": "", "browser.startup.homepage_override.mstone": "ignore",
        "datareporting.policy.dataSubmissionEnabled": False, "toolkit.telemetry.reportingpolicy.firstRun": False,
        "intl.locale.requested": "pt-BR", "intl.accept_languages": "pt-BR, pt, en-US, en",
        # Velocidade (CLAUDE.md 15.1): HTTP/2 pelo túnel do proxy, sem tentativas de HTTP/3 (UDP, bloqueado), cache só
        # em memória (sistema live em RAM), nenhuma espera por IPv6.
        "network.http.http2.enabled": True, "network.http.http3.enable": False, "network.dns.disableIPv6": True,
        "browser.cache.disk.enable": False, "browser.cache.memory.enable": True, "browser.cache.memory.capacity": 131072,
        # Serviços da Mozilla que ficam tentando (e sendo recusados) a cada página: desligados.
        "services.settings.server": "http://127.0.0.1:9/v1", "app.normandy.enabled": False, "app.update.auto": False,
        "extensions.update.enabled": False, "extensions.getAddons.cache.enabled": False, "extensions.blocklist.enabled": False,
        "browser.region.update.enabled": False, "geo.provider.network.url": "", "toolkit.telemetry.enabled": False,
        "browser.safebrowsing.provider.google4.updateURL": "", "browser.safebrowsing.provider.google.updateURL": "",
        "security.remote_settings.crlite_filters.enabled": False, "network.connectivity-service.enabled": False,
        "network.captive-portal-service.enabled": False, "browser.newtabpage.enabled": False,
        # Extensão local de bloqueio (sem assinatura da Mozilla: aceita no Firefox ESR com esta preferência).
        "xpinstall.signatures.required": False,
    }  # fmt: skip
    return "".join(f"user_pref({json.dumps(chave)}, {json.dumps(valor)});\n" for chave, valor in prefs.items())


class Navegador:
    """Uma janela do navegador integrado por vez. Nunca levanta exceção para quem chama."""

    def __init__(self, executor: Any, display: str | None = None, xauthority: str | None = None, base: str = "/tmp",
                 usuario: str = USUARIO, porta_proxy: int = PORTA_PROXY,
                 popen: Callable[..., Any] = subprocess.Popen) -> None:  # fmt: skip
        self.executor, self.usuario, self.porta_proxy, self._popen = executor, usuario, porta_proxy, popen
        self.display = display or os.environ.get("DISPLAY", ":0")
        self.xauthority = xauthority or os.environ.get("XAUTHORITY", "")
        self.base = base
        self.perfil: Path | None = None
        self.processo: Any = None
        self.visivel = False
        self.url = ""
        self.mensagem = ""
        self._vistos: dict[str, int] = {}
        self._entregues: set[str] = set()
        """Downloads já entregues a ``ao_baixar`` (cada um uma vez só)."""
        self._ao_baixar: Callable[[Path], None] | None = None
        self._trava = threading.Lock()

    @property
    def aberto(self) -> bool:
        return self.processo is not None and self.processo.poll() is None

    def abrir(self, url: str, ao_baixar: Callable[[Path], None]) -> tuple[bool, str]:
        """Abre (ou mostra de novo, na mesma página) o navegador em ``url``."""
        with self._trava:
            if self.aberto:
                return self.mostrar(), "navegador já aberto"
            try:
                perfil = Path(tempfile.mkdtemp(prefix="msscan-web-", dir=self.base))
                downloads = perfil / "downloads"
                downloads.mkdir()
                (perfil / "user.js").write_text(preferencias(downloads, self.porta_proxy), encoding="utf-8")
                if self.xauthority:
                    shutil.copyfile(self.xauthority, perfil / ".Xauthority")
                for caminho in (perfil, downloads, *perfil.iterdir()):
                    shutil.chown(caminho, self.usuario, self.usuario)
                os.chmod(perfil, 0o700)
                comando = ["runuser", "-u", self.usuario, "--", "env", "-i", f"DISPLAY={self.display}",
                           f"XAUTHORITY={perfil / '.Xauthority'}", f"HOME={perfil}", "LANG=pt_BR.UTF-8", "PATH=/usr/bin:/bin",
                           "firefox-esr", "--no-remote", "--class", CLASSE_JANELA, "--profile", str(perfil), url]  # fmt: skip
                self.processo = self._popen(comando, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, LookupError) as erro:
                self._apagar_perfil_de(locals().get("perfil"))
                self.mensagem = f"não foi possível abrir o navegador: {erro}"
                return False, self.mensagem
            self.perfil, self.url, self.visivel, self._ao_baixar = perfil, url, True, ao_baixar
            self._vistos, self._entregues = {}, set()
            self.mensagem = ""
        threading.Thread(target=self._vigiar, daemon=True, name="msscan-navegador").start()
        return True, "navegador aberto"

    def _janelas(self, *acoes: str) -> bool:
        """``xdotool search --class MSscanNavegador <ações> %@``: mostra/esconde as janelas do navegador."""
        comando = ["xdotool", "search", "--class", CLASSE_JANELA]
        for acao in acoes:
            comando += [acao, "%@"]
        rc, _, _ = self.executor.rodar(comando, timeout=10)
        return rc == 0

    def voltar(self) -> bool:
        """"Voltar ao MSscan": esconde a janela do navegador (continua aberto; downloads seguem)."""
        if not self.aberto:
            return False
        self.visivel = not self._janelas("windowunmap")
        return not self.visivel

    def mostrar(self) -> bool:
        if not self.aberto:
            return False
        self.visivel = self._janelas("windowmap", "windowraise")
        return self.visivel

    def fechar(self) -> None:
        """Fecha o navegador e apaga o perfil inteiro (cookies, login, cache e downloads que sobraram)."""
        processo = self.processo
        if processo is not None and processo.poll() is None:
            processo.terminate()
            try:
                processo.wait(10)
            except subprocess.TimeoutExpired:
                processo.kill()
        self.executor.rodar(["pkill", "-u", self.usuario], timeout=10)  # processos filhos do Firefox
        with self._trava:
            self._apagar_perfil_de(self.perfil)
            self.perfil, self.processo, self.visivel = None, None, False

    @staticmethod
    def _apagar_perfil_de(perfil: Path | None) -> None:
        if perfil is not None:
            shutil.rmtree(perfil, ignore_errors=True)

    def _vigiar(self) -> None:
        while True:
            time.sleep(INTERVALO_S)
            with self._trava:
                perfil, ao_baixar = self.perfil, self._ao_baixar
            if perfil is None:
                return
            for pronto in self.concluidos(perfil / "downloads"):
                if ao_baixar is not None:
                    ao_baixar(pronto)
            if not self.aberto:
                self.fechar()
                return

    def concluidos(self, pasta: Path) -> list[Path]:
        """Downloads terminados: sem ``<nome>.part`` ao lado, tamanho > 0 e igual ao da volta anterior."""
        prontos = []
        try:
            nomes = {p.name for p in pasta.iterdir()}
        except OSError:
            return []
        for nome in sorted(nomes):
            caminho = pasta / nome
            if nome in self._entregues or nome.endswith(".part") or f"{nome}.part" in nomes or not caminho.is_file():
                continue
            tamanho = caminho.stat().st_size
            if tamanho > 0 and self._vistos.get(nome) == tamanho:
                prontos.append(caminho)
                self._entregues.add(nome)
            else:
                self._vistos[nome] = tamanho
        return prontos

    def estado(self) -> dict[str, Any]:
        return {"aberto": self.aberto, "visivel": self.visivel and self.aberto, "url": self.url, "mensagem": self.mensagem}


if __name__ == "__main__":  # build da ISO: python3 -m msscan.navegador.navegador <destino.xpi>
    import sys

    Path(sys.argv[1] if len(sys.argv) > 1 else ARQUIVO_EXTENSAO).write_bytes(extensao_bloqueio())

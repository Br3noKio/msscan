"""Verificações estáticas da configuração da ISO (o build real é feito pelo usuário)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ISO = Path(__file__).parent.parent / "iso"
CONFIG = ISO / "config"
PACOTES = CONFIG / "package-lists" / "msscan.list.chroot"
SERVICO = CONFIG / "includes.chroot/etc/systemd/system/msscan.service"
INICIAR = CONFIG / "includes.chroot/usr/local/sbin/msscan-iniciar"
HOOK = CONFIG / "hooks/normal/9000-msscan.hook.chroot"
MODULOS = CONFIG / "includes.chroot/etc/modules-load.d/msscan-ipmi.conf"
BUILD = ISO / "build.sh"


def pacotes() -> set[str]:
    return {l.strip() for l in PACOTES.read_text().splitlines() if l.strip() and not l.startswith("#")}


def test_pacotes_da_especificacao():
    exigidos = {
        "python3", "python3-requests", "python3-yaml", "python3-packaging",
        "dmidecode", "lshw", "pciutils", "ipmitool", "freeipmi-tools", "lm-sensors", "smartmontools",
        "nvme-cli", "sg3-utils", "lsscsi", "mdadm", "ethtool", "iproute2", "network-manager",
        "ca-certificates", "exfatprogs",
        "firmware-linux-nonfree", "firmware-misc-nonfree", "firmware-bnx2", "firmware-bnx2x", "firmware-qlogic",
    }  # fmt: skip
    assert exigidos <= pacotes()


def test_nada_proprietario_nem_pip():
    proibidos = {"python3-pip", "perccli", "storcli", "megacli", "ssacli", "arcconf"}
    assert not proibidos & {p.lower() for p in pacotes()}


def test_build_config():
    texto = BUILD.read_text()
    for opcao in (
        "--distribution trixie",
        "--architecture amd64",
        "--binary-image iso-hybrid",
        '--bootloaders "syslinux,grub-efi"',
        '--archive-areas "main contrib non-free non-free-firmware"',
        "--debian-installer none",
    ):
        assert opcao in texto
    volume = re.search(r'--iso-volume "([^"]+)"', texto).group(1)
    assert volume != "MSSCAN", "rótulo reservado para a partição de dados"
    assert "lb build" in texto and 'id -u' in texto


def test_servico_tty1_depois_da_rede():
    texto = SERVICO.read_text()
    assert "TTYPath=/dev/tty1" in texto
    assert re.search(r"^After=network-online\.target\b", texto, re.MULTILINE)
    assert "Wants=network-online.target" in texto
    assert "ExecStart=/usr/local/sbin/msscan-iniciar" in texto
    assert "WantedBy=multi-user.target" in texto


def test_hook_habilita_servico():
    texto = HOOK.read_text()
    assert "systemctl enable msscan.service" in texto
    assert "systemctl mask getty@tty1.service" in texto
    assert 'CODESET="Uni2"' in texto
    assert "AUTO -all" in texto, "a ISO não pode montar arrays RAID automaticamente"


def test_modulos_ipmi():
    assert MODULOS.read_text().split("\n", 1)[1].split() == ["ipmi_si", "ipmi_devintf"]


SESSAO_X = CONFIG / "includes.chroot/usr/local/lib/msscan/sessao-x"
REGRA_NFT = CONFIG / "includes.chroot/etc/msscan/quiosque.nft"
PREFS_FIREFOX = CONFIG / "includes.chroot/usr/local/share/msscan/firefox-user.js"
XORG_CONF = CONFIG / "includes.chroot/etc/X11/xorg.conf.d/10-msscan.conf"


@pytest.mark.parametrize("script", [BUILD, INICIAR, HOOK, SESSAO_X], ids=lambda p: p.name)
def test_scripts_executaveis_e_sintaxe_valida(script):
    assert os.access(script, os.X_OK)
    interpretador = "bash" if script.read_text().startswith("#!/bin/bash") else "sh"
    assert subprocess.run([interpretador, "-n", str(script)], capture_output=True).returncode == 0


def test_iniciar_roda_main():
    texto = INICIAR.read_text()
    assert 'DIR="${MSSCAN_DIR:-/opt/msscan}"' in texto
    assert '$PYTHON "$DIR/main.py"' in texto and 'PYTHON="${MSSCAN_PYTHON:-python3}"' in texto


def test_versao_igual_ao_topo_do_changelog():
    """A versão do pacote (e da ISO) é a primeira do CHANGELOG.md (também no repositório público, sem o PLANO.md)."""
    from msscan import __version__

    changelog = (ISO.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(r"^## (\S+)", changelog, flags=re.MULTILINE).group(1) == __version__


def test_data_da_versao_igual_a_do_changelog():
    """28.1: ``__data_versao__`` é a data mínima plausível do relógio; acompanha a data da versão no CHANGELOG."""
    from msscan import __data_versao__, __version__

    changelog = (ISO.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## {re.escape(__version__)} — (\S+)", changelog, flags=re.MULTILINE).group(1) == __data_versao__


def test_versao_acompanha_a_fase_entregue():
    """A versão do pacote (e da ISO) é a da última fase concluída ou em revisão no PLANO.md (arquivo interno)."""
    from msscan import __version__

    if not (ISO.parent / "PLANO.md").exists():
        pytest.skip("PLANO.md é interno (fora do repositório público)")
    plano = (ISO.parent / "PLANO.md").read_text(encoding="utf-8")
    # "Pedidos de 30/09/2026 — v0.12" (entrega fora da numeração de fases) também conta.
    entregues = re.findall(r"^## (?:Fase \d+|Pedidos de [\d/]+) — v(\d+\.\d+)\b.*`\[[xr]\]`\s*$", plano, flags=re.MULTILINE)
    assert entregues, "nenhuma fase concluída no PLANO.md"
    assert __version__.startswith(entregues[-1] + "."), (__version__, entregues[-1])


# --- Quiosque gráfico (fase 5, opção C: Xorg + matchbox + Firefox ESR) ---------------------


def test_pacotes_do_quiosque():
    exigidos = {
        "xserver-xorg-core", "xserver-xorg-video-fbdev", "xserver-xorg-video-vesa", "xserver-xorg-input-libinput",
        "xinit", "xauth", "matchbox-window-manager", "matchbox-themes-extra", "firefox-esr", "fonts-dejavu-core",
        "kbd", "nftables",
    }  # fmt: skip
    assert exigidos <= pacotes()
    assert not {"chromium", "cage", "xserver-xorg"} & pacotes()  # opção C; sem o metapacote com todos os drivers


def test_regra_do_firewall_do_navegador():
    texto = REGRA_NFT.read_text()
    assert 'meta skuid != "msscan-ui" accept' in texto
    assert 'oifname "lo" ip daddr 127.0.0.0/8 accept' in texto and 'oifname "lo" ip6 daddr ::1 accept' in texto
    regras = [l.strip() for l in texto.split("chain saida {")[1].split("}")[0].splitlines() if l.strip()]
    assert regras[-1] == "counter reject"  # o resto do usuário do navegador é recusado
    assert "delete table inet msscan_quiosque" in texto  # reaplicável


POLITICA_FIREFOX = CONFIG / "includes.chroot/usr/share/firefox-esr/distribution/policies.json"
ATALHOS = CONFIG / "includes.chroot/etc/msscan/matchbox-kbdconfig"
VOLTAR = CONFIG / "includes.chroot/usr/local/lib/msscan/voltar"


def test_firewall_do_navegador_integrado_so_alcanca_proxy_e_interface():
    from msscan.interface.servidor import PORTA_PADRAO
    from msscan.navegador.proxy import PORTA

    texto = REGRA_NFT.read_text()
    saida = [l.strip() for l in texto.split("chain saida {")[1].split("}")[0].splitlines() if l.strip()]
    assert saida[1] == 'meta skuid "msscan-web" jump navegador'  # antes do "accept" dos outros usuários
    assert f'oifname "lo" tcp dport {PORTA} counter reject with tcp reset' in saida  # o quiosque não usa o proxy
    navegador = [l.strip() for l in texto.split("chain navegador {")[1].split("\n\t}")[0].splitlines() if l.strip()]
    assert navegador == [f'oifname "lo" ip daddr 127.0.0.1 tcp dport {{ {PORTA}, {PORTA_PADRAO} }} accept',
                         "meta l4proto tcp counter reject with tcp reset", "counter reject"]  # fmt: skip


def test_firewall_nunca_descarta_em_silencio():
    """Pedido de 30/09/2026 (CLAUDE.md 15.1): bloqueio com REJECT (recusa na hora), nunca DROP (timeout)."""
    regras = [l.split("#")[0] for l in REGRA_NFT.read_text().splitlines()]
    assert not any(re.search(r"\bdrop\b", l) for l in regras)
    assert "tcp dport 47443 counter reject with tcp reset" in REGRA_NFT.read_text()


def test_politica_do_firefox_so_dominios_oficiais():
    import json

    from msscan.interface.servidor import PORTA_PADRAO
    from msscan.navegador.proxy import DOMINIOS_OFICIAIS

    politica = json.loads(POLITICA_FIREFOX.read_text(encoding="utf-8"))["policies"]
    # A WebsiteFilter foi trocada pela extensão local (mesma lista do proxy) para mostrar "Site bloqueado pelo MSscan".
    assert "WebsiteFilter" not in politica
    assert politica["ExtensionSettings"]["bloqueio@msscan"] == {
        "installation_mode": "force_installed", "install_url": "file:///usr/local/share/msscan/bloqueio.xpi"}
    assert politica["BlockAboutAddons"] is True and politica["Preferences"]["xpinstall.signatures.required"]["Status"] == "locked"
    assert "python3 -m msscan.navegador.navegador /usr/local/share/msscan/bloqueio.xpi" in HOOK.read_text()
    assert DOMINIOS_OFICIAIS == ("dell.com", "dellcdn.com", "hpe.com", "lenovo.com", "www.google.com", "www.gstatic.com", "recaptcha.net")
    assert politica["Bookmarks"] == [{"Title": "⬅ Voltar ao MSscan (Alt+M)", "URL": f"http://127.0.0.1:{PORTA_PADRAO}/voltar", "Placement": "toolbar"}]
    assert politica["PasswordManagerEnabled"] is False and politica["SanitizeOnShutdown"] is True
    assert politica["ExtensionSettings"]["*"] == {"installation_mode": "blocked"} and len(politica["ExtensionSettings"]) == 2


def test_atalho_alt_m_e_sem_fechar_janela():
    from msscan.interface.servidor import PORTA_PADRAO

    linhas = [l for l in ATALHOS.read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert linhas == ["<Alt>m=!/usr/local/lib/msscan/voltar"]
    assert "-kbdconfig /etc/msscan/matchbox-kbdconfig" in SESSAO_X.read_text()
    assert os.access(VOLTAR, os.X_OK) and f"http://127.0.0.1:{PORTA_PADRAO}/voltar" in VOLTAR.read_text()
    assert subprocess.run(["sh", "-n", str(VOLTAR)]).returncode == 0


def test_iso_liga_o_navegador_integrado():
    assert "--ui-navegador" in INICIAR.read_text() and "xdotool" in pacotes()
    texto = HOOK.read_text()
    assert "useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin msscan-web" in texto


def test_hook_cria_usuario_do_navegador_sem_login():
    texto = HOOK.read_text()
    assert "useradd --system" in texto and "msscan-ui" in texto and "/usr/sbin/nologin" in texto


def test_xorg_sem_apagar_tela_e_cursor_por_software_no_bmc():
    texto = XORG_CONF.read_text()
    for opcao in ("BlankTime", "StandbyTime", "SuspendTime", "OffTime"):
        assert f'Option "{opcao}"   "0"'.replace("   ", " ") in re.sub(r" +", " ", texto)
    assert 'MatchDriver "mgag200|ast"' in texto and 'Option      "SWcursor" "on"' in texto


def test_sessao_x_matchbox_antes_e_navegador_sem_root():
    texto = SESSAO_X.read_text()
    assert texto.index("matchbox-window-manager") < texto.index("firefox-esr")
    assert 'runuser -u "$USUARIO"' in texto and "--kiosk" in texto and "env -i" in texto
    # Resolução (EDID + fallback) antes do gerenciador e do navegador; falha nunca derruba a sessão.
    assert texto.index("msscan.tela.resolucao --auto") < texto.index("matchbox-window-manager -use_titlebar")
    assert '--estado "$RUN/tela.json") >"$RUN/tela.log" 2>&1 || true' in texto


def test_preferencias_do_firefox():
    texto = PREFS_FIREFOX.read_text()
    for pref in ('"app.update.enabled", false', '"toolkit.telemetry.enabled", false', '"dom.push.enabled", false',
                 '"accessibility.typeaheadfind.manual", false', '"gfx.webrender.software", true',
                 '"network.proxy.failover_direct", false', '"browser.sessionstore.resume_from_crash", false'):
        assert f"user_pref({pref});" in texto, pref
    assert '"browser.startup.homepage",' not in texto  # a URL com token é acrescentada no boot, nunca versionada


# Lançador com stubs: nada de X, firewall ou hardware de verdade.

MAIN_FALSO = """\
import os, sys, time
open(os.environ["LOG"], "a").write("main " + " ".join(sys.argv[1:]) + "\\n")
if "--ui" in sys.argv:
    open(os.environ["PIDS"], "a").write(str(os.getpid()) + "\\n")
    if os.environ.get("FAKE_SEM_URL"):
        time.sleep(60)
    url = sys.argv[sys.argv.index("--ui-arquivo-url") + 1]
    pronto = sys.argv[sys.argv.index("--ui-arquivo-pronto") + 1]
    open(url, "w").write("http://127.0.0.1:8765/#t=TOKENSECRETO\\n")
    if os.environ.get("FAKE_PRONTO"):
        time.sleep(float(os.environ["FAKE_PRONTO"]))
        open(pronto, "w").close()
    if os.environ.get("FAKE_GRAVANDO"):
        marcador = os.path.join(os.path.dirname(url), "gravando")
        open(marcador, "w").write("firmware\\n")
        time.sleep(float(os.environ["FAKE_GRAVANDO"]))
        open(os.environ["LOG"], "a").write("fim-gravacao\\n")
        os.remove(marcador)
    time.sleep(float(os.environ.get("FAKE_VIDA_BACKEND", "60")))
"""

XINIT_FALSO = """\
#!/bin/sh
echo "xinit $*" >> "$LOG"
echo "XAUTHORITY=$XAUTHORITY" >> "$LOG.env"
# "servidor X": processo separado com a trava, como o Xorg de verdade
sleep 120 &
echo $! > "$MSSCAN_XLOCK_DIR/.X0-lock"
echo $! >> "$PIDS"
echo $$ >> "$PIDS"
exec sleep "${FAKE_VIDA_X:-60}"
"""


# python3 do lançador: "-m msscan.interface.falhas <pasta>" só registra (nada de pendrive); o resto vai ao Python real.
PYTHON_FALSO = """\
#!/bin/sh
if [ "$1" = -m ] && [ "$2" = msscan.interface.falhas ]; then
    echo "falhas $3" >> "$LOG"
    ls "$3" | grep -v '[.]log$' > "$(dirname "$LOG")/falhas-gravadas"
    exit 0
fi
exec python3 "$@"
"""


@pytest.fixture
def lancador(tmp_path):
    """Roda o msscan-iniciar com stubs; devolve (saída, log de chamadas, pasta /run falsa)."""
    app = tmp_path / "opt"
    app.mkdir()
    (app / "main.py").write_text(MAIN_FALSO)
    xinit = tmp_path / "xinit"
    xinit.write_text(XINIT_FALSO)
    xinit.chmod(0o755)
    python = tmp_path / "python-falso"
    python.write_text(PYTHON_FALSO)
    python.chmod(0o755)
    run = tmp_path / "run"
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("boot=live components quiet\n")
    log, pids = tmp_path / "chamadas.log", tmp_path / "pids"

    def rodar(timeout: float = 30, **ambiente: str):
        env = {
            "PATH": os.environ["PATH"], "LOG": str(log), "PIDS": str(pids),
            "MSSCAN_DIR": str(app), "MSSCAN_RUN": str(run), "MSSCAN_CMDLINE": str(cmdline), "MSSCAN_PYTHON": str(python),
            "MSSCAN_ESPERA_UI": "5", "MSSCAN_XINIT": str(xinit), "MSSCAN_XAUTH": "true",
            "MSSCAN_NFT": "true",
            "MSSCAN_XLOCK_DIR": str(tmp_path), "MSSCAN_FALHAS": str(tmp_path / "falhas"),
            "MSSCAN_XORG_LOG": str(tmp_path / "Xorg.0.log"),
            "MSSCAN_REGRA_NFT": str(REGRA_NFT), "MSSCAN_PREFS_FIREFOX": str(PREFS_FIREFOX),
            **ambiente,
        }  # fmt: skip
        proc = subprocess.run(["sh", str(INICIAR)], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)  # fmt: skip
        chamadas = log.read_text().splitlines() if log.exists() else []
        vivos = []
        for pid in (pids.read_text().split() if pids.exists() else []):
            try:
                os.kill(int(pid), 0)
                vivos.append(pid)
            except OSError:
                pass
        return proc.stdout, chamadas, run, vivos

    rodar.cmdline = cmdline
    rodar.falhas = tmp_path / "falhas"
    return rodar


def test_lancador_sem_modo_texto():
    """16/10/2026 ("sempre vá para a interface"): o lançador nunca roda o main.py sem --ui nem lê msscan.texto."""
    texto = INICIAR.read_text()
    assert "msscan.texto" not in texto and "modo_texto" not in texto
    assert [l for l in texto.splitlines() if '$PYTHON "$DIR/main.py"' in l and "--ui" not in l] == []


@pytest.mark.parametrize("parametro", ["msscan.texto=1", "serverdiag.texto=1"])
def test_lancador_ignora_o_parametro_de_modo_texto(lancador, parametro):
    lancador.cmdline.write_text(f"boot=live {parametro} quiet\n")
    _, chamadas, _, _ = lancador(MSSCAN_MAX_TENTATIVAS="1", MSSCAN_ESPERA_UI="1")
    assert chamadas[0].startswith("main --ui")


def test_lancador_interface_no_ar_e_reabre_quando_o_x_cai(lancador):
    saida, chamadas, run, vivos = lancador(FAKE_PRONTO="0.5", FAKE_VIDA_X="2", MSSCAN_MAX_TENTATIVAS="2")
    assert chamadas[0] == f"main --ui --ui-arquivo-url {run}/url --ui-arquivo-pronto {run}/pronto --ui-firewall-pareamento --ui-navegador --ui-arquivo-tela {run}/tela.json"
    xinit = chamadas[1]
    assert xinit.startswith("xinit ") and xinit.endswith(f"-- :0 vt7 -nolisten tcp -auth {run}/Xauthority")
    assert [c.split()[0] for c in chamadas] == ["main", "xinit", "main", "xinit"]  # caiu → abriu de novo (mesmo vídeo)
    assert "-configdir" not in chamadas[3]
    assert "A interface gráfica foi encerrada (vídeo: padrao). Abrindo de novo..." in saida
    # o xinit precisa do cookie para ver o X subir (bug achado no ensaio real com Xvfb)
    assert (run.parent / "chamadas.log.env").read_text().splitlines()[0] == f"XAUTHORITY={run}/Xauthority"
    assert not vivos  # backend encerrado junto
    user_js = (run / "firefox" / "user.js").read_text()
    assert user_js.startswith(PREFS_FIREFOX.read_text())
    assert user_js.endswith('user_pref("browser.startup.homepage", "http://127.0.0.1:8765/#t=TOKENSECRETO");\n')
    assert "TOKENSECRETO" not in xinit  # o token nunca vai para a linha de comando
    assert (run / "firefox").stat().st_mode & 0o777 == 0o700
    assert (run / "Xauthority").stat().st_mode & 0o777 == 0o600
    assert any(p.name.endswith("_queda_padrao") for p in lancador.falhas.iterdir())


def test_lancador_espera_a_interface_mesmo_com_o_relogio_acertado(lancador, tmp_path):
    """28.1 (QEMU, 14/10/2026): o MSscan adianta o relógio meses durante a espera; o limite não pode ser pela hora."""
    falsos = tmp_path / "bin"
    falsos.mkdir()
    date = falsos / "date"
    date.write_text(f'#!/bin/sh\nif [ -e {tmp_path}/saltou ]; then echo 9999999999; else touch {tmp_path}/saltou; '
                    'exec /bin/date "$@"; fi\n')  # fmt: skip
    date.chmod(0o755)
    saida, chamadas, _, _ = lancador(FAKE_PRONTO="1.5", FAKE_VIDA_X="1", MSSCAN_MAX_TENTATIVAS="1",
                                     PATH=f"{falsos}:{os.environ['PATH']}")  # fmt: skip
    assert chamadas[1].startswith("xinit ")  # a interface subiu
    assert "não abriu" not in saida


def test_lancador_nao_mata_o_backend_durante_gravacao(lancador):
    """Revisão independente (ALTO 1): o X cai no meio de uma gravação → o lançador espera o marcador sumir."""
    saida, chamadas, _, vivos = lancador(FAKE_PRONTO="0.5", FAKE_VIDA_X="2", FAKE_GRAVANDO="5", MSSCAN_MAX_TENTATIVAS="1")
    assert chamadas[-1] == "fim-gravacao"  # só depois da gravação a interface é reaberta
    assert not vivos


def test_lancador_tenta_os_tres_videos_e_recomeca(lancador, tmp_path):
    """16/10/2026 (R620 caiu no modo texto): não abriu → autodetecção, fbdev, vesa, e recomeça; nunca o modo texto."""
    saida, chamadas, _, vivos = lancador(MSSCAN_ESPERA_UI="1", MSSCAN_MAX_TENTATIVAS="4")
    xinits = [c for c in chamadas if c.startswith("xinit")]
    assert len(xinits) == 4 and "-configdir" not in xinits[0] and "-configdir" not in xinits[3]
    assert xinits[1].endswith("-configdir /etc/X11/msscan/fbdev") and xinits[2].endswith("-configdir /etc/X11/msscan/vesa")
    assert all(c.startswith(("main --ui", "xinit", "falhas")) for c in chamadas)  # nunca o main.py sem --ui
    assert "A tela gráfica não abriu em 1 s (vídeo: vesa" in saida and not vivos
    # depois dos três vídeos, os registros vão para o pendrive (msscan.interface.falhas) e a pasta é esvaziada
    assert f"falhas {lancador.falhas}" in chamadas
    gravadas = (tmp_path / "falhas-gravadas").read_text().split()
    assert sorted(n.split("_", 1)[1] for n in gravadas) == ["inicio_fbdev", "inicio_padrao", "inicio_vesa"]


def test_lancador_backend_que_nao_sobe_tenta_de_novo(lancador):
    saida, chamadas, _, vivos = lancador(MSSCAN_ESPERA_UI="3", FAKE_SEM_URL="1", MSSCAN_MAX_TENTATIVAS="2")
    assert "O MSscan não respondeu à interface" in saida
    assert not any(c.startswith("xinit") for c in chamadas) and len(chamadas) == 2 and not vivos


def test_lancador_sem_firewall_nao_abre_o_navegador(lancador):
    saida, chamadas, _, _ = lancador(MSSCAN_NFT="false", MSSCAN_MAX_TENTATIVAS="1")
    assert "Não foi possível aplicar o firewall do navegador".lower() in saida.lower()
    assert chamadas == []  # nem backend --ui, nem X


def test_lancador_remove_trava_de_x_morto(lancador, tmp_path):
    (tmp_path / ".X0-lock").write_text("     999999\n")  # PID que não existe
    lancador(FAKE_PRONTO="0.5", FAKE_VIDA_X="1", MSSCAN_MAX_TENTATIVAS="1")
    # a trava velha foi removida e a nova é a do "servidor" desta execução
    assert (tmp_path / ".X0-lock").read_text().strip() != "999999"


def test_configs_do_plano_b_de_video():
    for driver in ("fbdev", "vesa"):
        texto = (CONFIG / f"includes.chroot/etc/X11/msscan/{driver}/10-msscan.conf").read_text()
        assert f'Driver     "{driver}"' in texto and 'Option "BlankTime"   "0"' in texto
        assert f"xserver-xorg-video-{driver}" in pacotes()


def test_falhas_da_interface_vao_para_o_pendrive(tmp_path, monkeypatch):
    from msscan.interface import falhas
    from msscan.pendrive.local import LocalDados, local_manual

    origem = tmp_path / "run"
    (origem / "101010_inicio_padrao").mkdir(parents=True)
    (origem / "101010_inicio_padrao" / "Xorg.0.log").write_text("(EE) no screens found\n")
    (origem / "nft.log").write_text("")  # arquivo solto: não é tentativa
    dados = tmp_path / "MSSCAN"
    monkeypatch.setattr(falhas, "localizar_dados", lambda _e: local_manual(str(dados)))
    from datetime import datetime
    destino = falhas.gravar(origem, object(), agora=datetime(2026, 10, 16, 9, 1, 7))
    assert destino == dados / "logs" / "interface_2026-10-16_090107"
    assert (destino / "101010_inicio_padrao" / "Xorg.0.log").read_text() == "(EE) no screens found\n"
    assert not (destino / "nft.log").exists()
    # sem pendrive gravável (/tmp) ou sem tentativas: não grava, nunca levanta exceção
    monkeypatch.setattr(falhas, "localizar_dados", lambda _e: LocalDados("/tmp/x", "temporario", False, ""))
    assert falhas.gravar(origem, object()) is None
    assert falhas.gravar(tmp_path / "nao-existe", object()) is None


def test_relatorio_leva_as_falhas_da_interface(tmp_path, monkeypatch):
    from msscan.relatorio import completo

    falhas_dir = tmp_path / "run"
    (falhas_dir / "101010_inicio_padrao").mkdir(parents=True)
    (falhas_dir / "101010_inicio_padrao" / "motivo.txt").write_text("não abriu\n")
    monkeypatch.setattr(completo, "PASTA_FALHAS_INTERFACE", falhas_dir)
    pasta, _ = completo.salvar_relatorios({"equipamento": {"modelo": "PowerEdge R620"}}, "texto", str(tmp_path / "dados"))
    assert (pasta / "interface_falhou" / "101010_inicio_padrao" / "motivo.txt").exists()
    monkeypatch.setattr(completo, "PASTA_FALHAS_INTERFACE", tmp_path / "nada")
    pasta, _ = completo.salvar_relatorios({"equipamento": {"modelo": "PowerEdge R620"}}, "texto", str(tmp_path / "dados2"))
    assert not (pasta / "interface_falhou").exists()


# --- ISO universal: Rufus (ISO/DD), dd, Etcher, Ventoy; BIOS e UEFI com Secure Boot ------------

SERVICO_PENDRIVE = CONFIG / "includes.chroot/etc/systemd/system/msscan-pendrive.service"
DESLIGAR = CONFIG / "includes.chroot/usr/local/sbin/msscan-pendrive-desligar"
HOOK_INTEGRIDADE = CONFIG / "hooks/normal/9010-verificar-integridade.hook.binary"


def test_secure_boot_obrigatorio_no_build():
    # "enable": o build falha se o shim/GRUB assinados da Debian não estiverem disponíveis.
    assert "--uefi-secure-boot enable" in BUILD.read_text()


def test_build_recusa_arquivo_de_4_gib():
    texto = BUILD.read_text()
    assert "find binary -type f -size +4194303k" in texto
    assert texto.index("-size +4194303k") < texto.index('mv "$ISO_GERADA"')


def test_rotulo_cabe_no_fat32_do_rufus():
    # O Rufus usa o rótulo da ISO no FAT32 (máx. 11 caracteres); nunca "MSSCAN" (partição de dados).
    rotulo = re.search(r'--iso-volume "([^"]+)"', BUILD.read_text()).group(1)
    assert rotulo == "MSSCAN_BOOT" and len(rotulo) <= 11


def test_pacotes_para_os_dados_no_pendrive():
    assert {"exfatprogs", "dmsetup"} <= pacotes()


def test_desligamento_limpo_habilitado():
    assert "systemctl enable msscan-pendrive.service" in HOOK.read_text()
    servico = SERVICO_PENDRIVE.read_text()
    assert "ExecStop=/usr/local/sbin/msscan-pendrive-desligar" in servico
    assert "RemainAfterExit=yes" in servico and "After=local-fs.target" in servico


@pytest.mark.parametrize("script", [DESLIGAR, HOOK_INTEGRIDADE], ids=lambda p: p.name)
def test_scripts_novos_executaveis(script):
    assert os.access(script, os.X_OK)
    assert subprocess.run(["sh", "-n", str(script)], capture_output=True).returncode == 0


def test_desligar_remonta_somente_leitura_e_desmonta(tmp_path):
    log = tmp_path / "log"
    stub = tmp_path / "stub"
    stub.write_text('#!/bin/sh\necho "$(basename "$0") $*" >> "$LOG"\n[ "$(basename "$0")" = umount ] && exit "${FALHA_UMOUNT:-0}"\nexit 0\n')
    stub.chmod(0o755)
    for nome in ("mount", "umount", "mountpoint", "dmsetup"):
        (tmp_path / nome).symlink_to(stub)
    env = {"PATH": os.environ["PATH"], "LOG": str(log), "MSSCAN_PONTOS": "/mnt/msscan /mnt/msscan-ventoy",
           **{f"MSSCAN_{n.upper()}": str(tmp_path / n) for n in ("mount", "umount", "mountpoint", "dmsetup")}}  # fmt: skip
    assert subprocess.run(["sh", str(DESLIGAR)], env=env, timeout=10).returncode == 0
    chamadas = log.read_text().splitlines()
    assert "umount /mnt/msscan" in chamadas and "umount /mnt/msscan-ventoy" in chamadas
    assert "dmsetup remove msscan-ventoy" in chamadas
    assert chamadas[-1] == "mount -o remount,ro /run/live/medium"
    assert not any("-l" in c.split() or "rw" in c for c in chamadas)
    # umount recusado (em uso): fica somente leitura, sem desmontagem forçada
    log.unlink()
    subprocess.run(["sh", str(DESLIGAR)], env={**env, "FALHA_UMOUNT": "32"}, timeout=10)
    assert "mount -o remount,ro /mnt/msscan" in log.read_text().splitlines()


def test_hook_integridade_no_menu_do_bios(tmp_path):
    (tmp_path / "isolinux").mkdir()
    cfg = tmp_path / "isolinux" / "utilities.cfg"
    cfg.write_text("label hdt\n\tmenu label ^Hardware Detection Tool (HDT)\n\tcom32 hdt.c32\n")
    assert subprocess.run(["sh", str(HOOK_INTEGRIDADE)], cwd=tmp_path, timeout=10).returncode == 0
    texto = cfg.read_text()
    assert "label hdt" in texto and "Verificar integridade do pendrive" in texto
    assert "append boot=live components verify-checksums" in texto


def test_banco_de_compatibilidade_vai_na_iso():
    texto = BUILD.read_text()
    assert "-cf - main.py msscan compat_db)" in texto
    assert (ISO.parent / "compat_db" / "dell.yaml").is_file()
    assert "python3-yaml" in pacotes()


HOOK_TEMPO_MENU = CONFIG / "hooks/normal/9020-tempo-menu.hook.binary"


def test_hook_tempo_dos_menus(tmp_path):
    """Menus de boot entram sozinhos em 10 s (syslinux em décimos; GRUB em segundos)."""
    assert os.access(HOOK_TEMPO_MENU, os.X_OK)
    (tmp_path / "isolinux").mkdir()
    (tmp_path / "boot" / "grub").mkdir(parents=True)
    # Conteúdo igual ao gerado pelo live-build na ISO v0.8.0.
    (tmp_path / "isolinux" / "isolinux.cfg").write_text("include menu.cfg\ndefault vesamenu.c32\nprompt 0\ntimeout 0\n")
    (tmp_path / "boot" / "grub" / "config.cfg").write_text("set default=0\n\nif [ x$feature_default_font_path = xy ] ; then\n    font=unicode\nfi\n")
    for _ in range(2):  # idempotente
        assert subprocess.run(["sh", str(HOOK_TEMPO_MENU)], cwd=tmp_path, timeout=10).returncode == 0
    isolinux = (tmp_path / "isolinux" / "isolinux.cfg").read_text()
    assert "timeout 100\n" in isolinux and "timeout 0" not in isolinux
    grub = (tmp_path / "boot" / "grub" / "config.cfg").read_text()
    assert grub.startswith("set default=0\nset timeout=10\nset timeout_style=menu\n")
    assert grub.count("set timeout=10") == 1


def test_build_preserva_o_cache_do_live_build():
    """Decisão do usuário (28/09): builds com cache; só LIMPO=1 descarta tudo."""
    texto = BUILD.read_text()
    guarda = texto.index('mv "$TRABALHO/cache" "$CACHE_GUARDADO/cache"')
    assert guarda < texto.index("lb clean --purge") < texto.index('mv "$CACHE_GUARDADO/cache" "$TRABALHO/cache"')
    assert '"${LIMPO:-0}" != "1"' in texto
    assert "-mtime +7" in texto  # base do debootstrap renovada toda semana (atualizações de segurança)


def test_iso_tem_o_xterm_do_terminal():
    """Aba Terminal (15.5): biblioteca local do pacote Debian, nos caminhos que o node-xterm 5.3 instala."""
    from msscan.interface.terminal import ARQUIVOS_XTERM, PASTA_XTERM

    assert "node-xterm" in pacotes()
    assert PASTA_XTERM == Path("/usr/share/nodejs")
    assert {c for c, _ in ARQUIVOS_XTERM.values()} == {
        "xterm/lib/xterm.js", "xterm/css/xterm.css", "xterm-addon-fit/lib/xterm-addon-fit.js",
        "xterm-addon-canvas/lib/xterm-addon-canvas.js",
    }  # fmt: skip


def test_lista_de_licencas_da_iso():
    """1.0.0: licenças dos componentes da ISO, lidas dos arquivos copyright (DEP-5) do chroot do build."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("licencas", ISO / "licencas.py")
    licencas = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(licencas)
    status = ("Package: b\nStatus: install ok installed\nVersion: 2\n\nPackage: a\nStatus: install ok installed\nVersion: 1\n\n"
              "Package: c\nStatus: deinstall ok config-files\nVersion: 3\n")  # fmt: skip
    assert licencas.pacotes(status) == [("a", "1"), ("b", "2")]
    assert licencas.licencas("Files: *\nLicense: GPL-2+\n\nFiles: x\nLicense: MIT\n text\n\nLicense: GPL-2+\n") == ["GPL-2+", "MIT"]
    assert "| `" in (ISO.parent / "docs/licencas-iso.md").read_text(encoding="utf-8")


def test_lancador_sem_shell_de_root(lancador):
    """Revisão de 13/10/2026 (M1/M2): o lançador nunca abre shell de root no tty1 (sem modo texto desde 16/10/2026, nem
    com msscan.terminal=1, que só liga os consoles tty2 a tty6); Ctrl+C não derruba o script."""
    texto = INICIAR.read_text()
    assert "bash" not in texto and "exec $SHELL" not in texto
    assert "trap ':' INT QUIT TSTP" in INICIAR.read_text()
    assert "TimeoutStopSec=45min" in (ISO / "config/includes.chroot/etc/systemd/system/msscan.service").read_text()

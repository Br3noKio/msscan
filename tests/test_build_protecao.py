"""Incidente de 06/10/2026 (CLAUDE.md 24): o build da ISO nunca pode tocar nas variáveis UEFI do servidor.

Testa as funções de ``iso/protecao_build.sh`` com um /proc/self/mounts falso (nenhuma montagem de verdade) e confere no
``iso/build.sh`` que toda limpeza vem depois da conferência e que o build roda isolado e vigiado.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
PROTECAO = RAIZ / "iso" / "protecao_build.sh"
BUILD = (RAIZ / "iso" / "build.sh").read_text(encoding="utf-8")
TRABALHO = "/root/serverdiag/iso/trabalho"


def _bash(script: str, montagens: str, tmp_path: Path) -> subprocess.CompletedProcess:
    arquivo = tmp_path / "mounts"
    arquivo.write_text(montagens, encoding="utf-8")
    return subprocess.run(["bash", "-c", f'. "{PROTECAO}"; {script}'], capture_output=True, text=True, timeout=30,
                          env={"MONTAGENS": str(arquivo), "PATH": "/usr/bin:/bin"})  # fmt: skip


INCIDENTE = (f"sysfs-live {TRABALHO}/chroot/sys sysfs rw,relatime 0 0\n"
             f"efivarfs {TRABALHO}/chroot/sys/firmware/efi/efivars efivarfs rw,nosuid,nodev,noexec,relatime 0 0\n")  # fmt: skip
NORMAL = ("/dev/sda2 / ext4 rw,relatime 0 0\nefivarfs /sys/firmware/efi/efivars efivarfs rw 0 0\n"
          f"tmpfs {TRABALHO}-outra tmpfs rw 0 0\n")  # fmt: skip


def test_aborta_com_a_montagem_do_incidente(tmp_path):
    saida = _bash(f'exigir_sem_montagens "{TRABALHO}"', NORMAL + INCIDENTE, tmp_path)
    assert saida.returncode == 3
    assert "ABORTADA" in saida.stderr and "efivars" in saida.stderr and "chroot/sys\n" in saida.stderr


def test_segue_sem_montagem_dentro_da_pasta(tmp_path):
    # O efivarfs do próprio servidor e uma pasta com prefixo parecido (trabalho-outra) não contam.
    saida = _bash(f'exigir_sem_montagens "{TRABALHO}/"', NORMAL, tmp_path)
    assert saida.returncode == 0, saida.stderr


def test_caminho_com_espaco_codificado_em_octal(tmp_path):
    saida = _bash('montagens_em "/tmp/com espaco"', "proc /tmp/com\\040espaco/chroot/proc proc rw 0 0\n", tmp_path)
    assert saida.stdout.strip() == "/tmp/com espaco/chroot/proc"


def test_build_confere_antes_de_toda_limpeza_e_roda_isolado_e_vigiado():
    linhas = BUILD.splitlines()
    limpezas = [n for n, linha in enumerate(linhas) if not linha.lstrip().startswith("#")
                and re.search(r"lb clean|rm -rf[^\n]*TRABALHO", linha)]
    assert limpezas, "o build.sh não limpa mais a pasta de trabalho?"
    for n in limpezas:
        anteriores = "\n".join(linhas[max(0, n - 6):n])
        assert "exigir_sem_montagens" in anteriores, linhas[n]
    assert all("--one-file-system" in linhas[n] for n in limpezas if "rm -rf" in linhas[n])
    assert re.search(r"exec env MSSCAN_BUILD_ISOLADO=1 unshare --mount --propagation private", BUILD)
    assert BUILD.index("unshare --mount") < BUILD.index("&& lb clean") < BUILD.index("lb build 2>&1")
    assert BUILD.index("vigiar_efivarfs") < BUILD.index("lb build 2>&1")


def test_nenhum_arquivo_do_build_monta_efivarfs():
    for arquivo in [RAIZ / "iso" / "build.sh", *(RAIZ / "iso" / "config").rglob("*")]:
        if arquivo.is_file():
            texto = arquivo.read_text(encoding="utf-8", errors="ignore")
            assert not re.search(r"mount[^\n]*efivarfs|efivarfs[^\n]*mount", texto), arquivo


WRAPPERS = RAIZ / "iso" / "sem-efivarfs"


@pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("unshare") or not Path("/sys/firmware/efi").exists(),
                    reason="precisa de root, unshare e de uma máquina UEFI")  # fmt: skip
def test_wrapper_esconde_o_firmware_do_sysfs_do_chroot(tmp_path):
    """Num namespace de montagem privado (nada aparece no servidor): o wrapper monta o sysfs como o live-build e esconde
    /sys/firmware (como numa máquina sem UEFI): sem firmware/efi, o hook do mdadm e o pós-instalação do shim nem tentam
    nada de EFI. Nunca monta efivarfs."""
    sys_dir = tmp_path / "chroot" / "sys"
    sys_dir.mkdir(parents=True)
    script = (f'PATH="{WRAPPERS}:$PATH"; set -e; mount -t sysfs -o x-gvfs-hide sysfs-live "{sys_dir}"; '
              f'test -d "{sys_dir}/firmware"; test ! -e "{sys_dir}/firmware/efi"; test -d "{sys_dir}/class"; '
              f'if ls "{sys_dir}"/firmware/efi/efivars/* >/dev/null 2>&1; then exit 9; fi; '
              f'findmnt -n -o FSTYPE,OPTIONS "{sys_dir}/firmware"; umount "{sys_dir}"; '
              f'if findmnt -R "{tmp_path}" >/dev/null; then exit 8; fi; echo desmontado')  # fmt: skip
    saida = subprocess.run(["unshare", "--mount", "--propagation", "private", "sh", "-c", script],
                           capture_output=True, text=True, timeout=30)  # fmt: skip
    assert saida.returncode == 0, saida.stderr + saida.stdout
    assert saida.stdout.startswith("tmpfs ro,") and "desmontado" in saida.stdout


def test_build_usa_os_wrappers_e_falha_se_o_vigia_pegar_efivarfs():
    assert BUILD.index('export PATH="$RAIZ/iso/sem-efivarfs:$PATH"') < BUILD.index("lb build 2>&1")
    assert re.search(r'if \[ -s "\$EFIVARFS_LOG" \]; then\n(?:.*\n){2}\s+exit 4', BUILD)
    assert all(os.access(WRAPPERS / nome, os.X_OK) for nome in ("mount", "umount"))

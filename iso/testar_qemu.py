"""Teste da ISO no QEMU sem tela (CLAUDE.md 14, processo de testes item 3): boot, captura de tela e teclado pelo monitor.

Só a ISO num CD-ROM virtual somente leitura e uma cópia das variáveis UEFI na pasta de saída: nada toca discos, pendrive nem
as variáveis UEFI deste servidor. O mouse do QEMU não chega à sessão gráfica nesta configuração (visto com a 1.0.2 e a 1.0.3):
navegue pelo teclado (atalhos ``g i``, ``g d``, ``g a``, ``g r``, Tab e Enter).

Uso:
    python3 iso/testar_qemu.py iniciar iso/msscan-X-amd64.iso --modo uefi-sb --modelo "PowerEdge R530" [--atraso-dias 183]
    python3 iso/testar_qemu.py tela inicio         # captura → <saída>/inicio.png
    python3 iso/testar_qemu.py teclas g a tab ret  # teclas do monitor do QEMU (sendkey)
    python3 iso/testar_qemu.py parar
Saída padrão: ``iso/trabalho/qemu/`` (no .gitignore, como o resto de ``iso/trabalho``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

SAIDA = Path(__file__).resolve().parent / "trabalho" / "qemu"
OVMF = Path("/usr/share/OVMF")


def monitor(*comandos: str) -> None:
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(SAIDA / "monitor"))
        s.settimeout(3)
        def ler() -> None:
            dados = b""
            try:
                while not dados.endswith(b"(qemu) "):
                    parte = s.recv(65536)
                    if not parte:
                        return
                    dados += parte
            except socket.timeout:
                pass
        ler()
        for comando in comandos:
            s.sendall(comando.encode() + b"\n")
            ler()
            time.sleep(0.15)


def iniciar(iso: str, modo: str, modelo: str, atraso_dias: int, resolucao: str) -> None:
    SAIDA.mkdir(parents=True, exist_ok=True)
    largura, altura = resolucao.split("x")
    cmd = ["qemu-system-x86_64", "-enable-kvm", "-m", "4096", "-smp", "4",
           "-smbios", f"type=1,manufacturer=Dell Inc.,product={modelo}",
           "-device", "ipmi-bmc-sim,id=bmc0", "-device", "isa-ipmi-kcs,bmc=bmc0",
           "-netdev", "user,id=net0", "-device", "e1000,netdev=net0",
           "-device", f"VGA,edid=on,xres={largura},yres={altura}", "-usb", "-device", "usb-tablet",
           "-drive", f"file={Path(iso).resolve()},media=cdrom,readonly=on", "-boot", "d",
           "-display", "none", "-monitor", f"unix:{SAIDA / 'monitor'},server,nowait",
           "-serial", f"file:{SAIDA / 'serial.log'}", "-daemonize", "-pidfile", str(SAIDA / "qemu.pid")]  # fmt: skip
    if modo.startswith("uefi"):
        seguro = modo == "uefi-sb"
        vars_ = SAIDA / "vars.fd"  # cópia: as variáveis UEFI deste servidor nunca são tocadas (CLAUDE.md 24)
        shutil.copy(OVMF / ("OVMF_VARS_4M.ms.fd" if seguro else "OVMF_VARS_4M.fd"), vars_)
        cmd += ["-machine", "q35,smm=on" if seguro else "q35",
                "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF / ('OVMF_CODE_4M.secboot.fd' if seguro else 'OVMF_CODE_4M.fd')}",
                "-drive", f"if=pflash,format=raw,file={vars_}"]  # fmt: skip
        if seguro:
            cmd += ["-global", "driver=cfi.pflash01,property=secure,value=on"]
    if atraso_dias:
        cmd += ["-rtc", "base=" + (dt.datetime.now() - dt.timedelta(days=atraso_dias)).strftime("%Y-%m-%dT%H:%M:%S")]
    subprocess.run(cmd, check=True)
    print(f"QEMU iniciado ({modo}, {modelo}). A interface leva ~90 s; depois: tela <nome>.")


def tela(nome: str) -> None:
    ppm = SAIDA / f"{nome}.ppm"
    monitor(f"screendump {ppm}")
    time.sleep(0.5)
    try:
        from PIL import Image

        Image.open(ppm).save(SAIDA / f"{nome}.png")
        ppm.unlink()
        print(SAIDA / f"{nome}.png")
    except ImportError:
        print(ppm)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="acao", required=True)
    i = sub.add_parser("iniciar")
    i.add_argument("iso")
    i.add_argument("--modo", choices=("bios", "uefi", "uefi-sb"), default="uefi-sb")
    i.add_argument("--modelo", default="PowerEdge R640")
    i.add_argument("--atraso-dias", type=int, default=0, help="relógio da VM atrasado (CLAUDE.md 28.3)")
    i.add_argument("--resolucao", default="1280x800")
    sub.add_parser("tela").add_argument("nome")
    sub.add_parser("teclas").add_argument("teclas", nargs="+")
    sub.add_parser("parar")
    a = p.parse_args(argv)
    if a.acao == "iniciar":
        iniciar(a.iso, a.modo, a.modelo, a.atraso_dias, a.resolucao)
    elif a.acao == "tela":
        tela(a.nome)
    elif a.acao == "teclas":
        monitor(*(f"sendkey {t}" for t in a.teclas))
    else:
        monitor("quit")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

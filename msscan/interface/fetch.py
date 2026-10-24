"""``msscan-fetch``: resumo da máquina no estilo fastfetch, mostrado ao abrir o terminal (nova interface, 09/10/2026).

Desenho em texto de um rack (original) e os dados da máquina com rótulos no azul do tema. ``montar`` é puro (dados da
``visao`` da sessão); ``python3 -m msscan.interface.fetch`` coleta sozinho, só com leituras, para o comando da ISO.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from msscan import __version__, textos
from msscan.interface.visao import rotulo_tempo

AZUL, AZUL_FORTE, VERDE, CINZA, NEGRITO, FIM = ("\x1b[38;2;88;166;255m", "\x1b[1;38;2;121;192;255m", "\x1b[38;2;63;185;80m",
                                                "\x1b[38;2;139;148;158m", "\x1b[1m", "\x1b[0m")  # fmt: skip
RACK = [
    "╔══════════════════════╗",
    "║ ▣ ▣ ▣ ▣  ━━━━━━━━  ● ║",
    "╟──────────────────────╢",
    "║ ▣ ▣ ▣ ▣  ━━━━━━━━  ● ║",
    "╟──────────────────────╢",
    "║ ▢ ▢ ▢ ▢  ┄┄┄┄┄┄┄┄  ○ ║",
    "╟──────────────────────╢",
    "║ ▣ ▣ ▣ ▣  ━━━━━━━━  ● ║",
    "╟──────────────────────╢",
    "║ ░░░░░░░░░░░░░░░░░░░░ ║",
    "╚══╤════════════════╤══╝",
    "   ┷                ┷   ",
]
"""Rack de servidores visto de frente (desenho original): gavetas com baias, painel e LED; uma gaveta vazia."""
LARGURA_RACK = len(RACK[0])


def _rack(cor: bool) -> list[str]:
    if not cor:
        return list(RACK)
    return [AZUL + linha.replace("●", VERDE + "●" + AZUL) + FIM for linha in RACK]


def _gb(bytes_: Any) -> str:
    try:
        valor = float(bytes_) / 1e9
    except (TypeError, ValueError):
        return "?"
    return f"{valor / 1000:.1f} TB".replace(".", ",") if valor >= 1000 else f"{valor:.0f} GB"


def linhas_dados(servidor: dict[str, Any] | None, sistema: str, versao: str) -> list[tuple[str, str]]:
    """``(rótulo, valor)`` na ordem mostrada; dado ausente vira ``—`` (nunca inventado)."""
    s = servidor or {}
    cpus = s.get("processadores") or []
    modelos = sorted({re.sub(r"\s+", " ", re.sub(r"\((R|TM)\)| CPU", "", str(p.get("modelo") or "?"))).strip() for p in cpus})
    nucleos = sum(int(p.get("nucleos") or 0) for p in cpus)
    memoria = s.get("memoria") or {}
    ocupados = [m for m in memoria.get("slots") or [] if m.get("ocupado")]
    discos = s.get("discos") or []
    fontes = s.get("fontes") or {}
    idrac = s.get("idrac") or {}
    return [
        (textos.t("fetch.modelo"), " ".join(str(x) for x in (s.get("fabricante"), s.get("modelo")) if x) or "—"),
        (textos.t("fetch.geracao"), s.get("geracao") or "—"),
        (textos.t("fetch.bios"), s.get("bios") or "—"),
        (textos.t("fetch.controlador"), (f"{s.get('bmc') or ''} {idrac.get('versao')}".strip()
                                        + (textos.t("fetch.abreviada") if idrac.get("abreviada") else "")) if idrac else "—"),
        (textos.t("fetch.processadores"), textos.t("fetch.processadores_valor", len(cpus), ", ".join(modelos), nucleos) if cpus else "—"),
        (textos.t("fetch.memoria"), textos.t("fetch.memoria_valor", memoria.get("total_gb") or sum(m.get("gb") or 0 for m in ocupados),
                                             len(ocupados), len(memoria.get("slots") or [])) if ocupados else "—"),
        (textos.t("fetch.discos"), ", ".join(f"{d.get('modelo') or d.get('dispositivo')} ({_gb(d.get('capacidade_bytes'))})" for d in discos) or "—"),
        (textos.t("fetch.fontes"), textos.t("fetch.fontes_valor", fontes.get("instaladas"), fontes.get("com_energia"))
         if fontes.get("instaladas") is not None else "—"),
        (textos.t("fetch.tempo_ligado"), rotulo_tempo(s.get("tempo_ligado_s"))),
        (textos.t("fetch.sistema_base"), sistema or "—"),
        (textos.t("fetch.msscan"), versao),
    ]  # fmt: skip


def montar(servidor: dict[str, Any] | None, sistema: str, versao: str = __version__, cor: bool = True) -> str:
    """Texto do ``msscan-fetch`` (linhas com ``\\r\\n`` quando ``cor``: vai direto para o pseudoterminal)."""
    dados = linhas_dados(servidor, sistema, versao)
    largura = max(len(r) for r, _ in dados)
    titulo = "msscan" + "@" + str((servidor or {}).get("modelo") or "servidor").lower().replace(" ", "-")
    direita = [(AZUL_FORTE + titulo + FIM) if cor else titulo, (CINZA + "─" * len(titulo) + FIM) if cor else "─" * len(titulo)]
    for rotulo, valor in dados:
        direita.append(f"{AZUL + NEGRITO if cor else ''}{rotulo.ljust(largura)}{FIM if cor else ''}  {valor}")
    if cor:
        direita.append("")
        direita.append("".join(f"\x1b[48;2;{c}m   " for c in ("1;4;9", "13;17;23", "10;26;51", "13;45;98", "23;96;210", "88;166;255",
                                                                 "63;185;80", "210;153;34", "248;81;73")) + FIM)  # fmt: skip
    esquerda = _rack(cor)
    saida = []
    for i in range(max(len(esquerda), len(direita))):
        parte = esquerda[i] if i < len(esquerda) else " " * LARGURA_RACK
        saida.append(f" {parte}   {direita[i] if i < len(direita) else ''}".rstrip())
    return ("\r\n" if cor else "\n").join(saida) + ("\r\n" if cor else "\n")


def sistema_base(os_release: Path = Path("/etc/os-release")) -> str:
    try:
        achado = re.search(r'^PRETTY_NAME="?([^"\n]*)"?', os_release.read_text(encoding="utf-8", errors="replace"), re.M)
    except OSError:
        return ""
    return achado.group(1) if achado else ""


def coletar() -> dict[str, Any]:
    """Coleta avulsa para o comando ``msscan-fetch`` (só leituras: dmidecode, /proc, lsblk, ipmitool mc info)."""
    from msscan.core.executor import ExecutorReal

    executor = ExecutorReal()

    def ler(comando: list[str]) -> str:
        rc, saida, _ = executor.rodar(comando, timeout=15, registrar=False)
        return saida.strip() if rc == 0 else ""

    cpuinfo = executor.ler_arquivo("/proc/cpuinfo") or ""
    modelos = re.findall(r"^model name\s*:\s*(.+)$", cpuinfo, re.M)
    soquetes = len(set(re.findall(r"^physical id\s*:\s*(\d+)", cpuinfo, re.M))) or (1 if modelos else 0)
    nucleos = int((re.search(r"^cpu cores\s*:\s*(\d+)", cpuinfo, re.M) or [0, 0])[1])
    memoria = re.search(r"^MemTotal:\s*(\d+)", executor.ler_arquivo("/proc/meminfo") or "", re.M)
    discos = [linha.split(None, 2) for linha in ler(["lsblk", "-dbn", "-e", "7,11", "-o", "NAME,SIZE,MODEL"]).splitlines() if linha.strip()]
    mc = re.search(r"^Firmware Revision\s*:\s*(\S+)", ler(["ipmitool", "mc", "info"]), re.M)
    uptime = (executor.ler_arquivo("/proc/uptime") or "0").split()[0]
    return {
        "fabricante": ler(["dmidecode", "-s", "system-manufacturer"]) or None,
        "modelo": ler(["dmidecode", "-s", "system-product-name"]) or None,
        "bios": ler(["dmidecode", "-s", "bios-version"]) or None,
        "idrac": {"versao": mc.group(1), "abreviada": True} if mc else None,
        "processadores": [{"modelo": modelos[0], "nucleos": nucleos}] * soquetes if modelos else [],
        "memoria": {"total_gb": round(int(memoria.group(1)) / 1024 / 1024) if memoria else None,
                    "slots": [{"ocupado": True, "gb": None}] if memoria else []},
        "discos": [{"dispositivo": d[0], "capacidade_bytes": d[1], "modelo": d[2] if len(d) > 2 else None} for d in discos],
        "tempo_ligado_s": int(float(uptime)) if uptime.replace(".", "", 1).isdigit() else None,
    }  # fmt: skip


if __name__ == "__main__":
    sys.stdout.write(montar(coletar(), sistema_base(), cor=sys.stdout.isatty()).replace("\r\n", "\n"))

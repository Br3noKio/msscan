"""Fonte do console (tty) com tamanho adequado à resolução (CLAUDE.md 12c, item 8).

Uso no boot (``msscan-fonte.service``): ``python3 -m msscan.tela.console``.
Lê a resolução do framebuffer e aplica, com ``setfont``, a maior fonte Terminus (Uni2, que tem
√ ▲ ✗) que ainda deixa ao menos 100 colunas e 30 linhas.
"""

from __future__ import annotations

import sys

from msscan.core.executor import Executor, ExecutorReal

# (largura, altura do caractere, arquivo em /usr/share/consolefonts), do menor para o maior.
FONTES = (
    (8, 16, "Uni2-Terminus16.psf.gz"),
    (10, 20, "Uni2-Terminus20x10.psf.gz"),
    (11, 22, "Uni2-Terminus22x11.psf.gz"),
    (12, 24, "Uni2-Terminus24x12.psf.gz"),
    (14, 28, "Uni2-Terminus28x14.psf.gz"),
    (16, 32, "Uni2-Terminus32x16.psf.gz"),
)
MIN_COLUNAS = 100
MIN_LINHAS = 30
PASTA_FONTES = "/usr/share/consolefonts"
TTYS = tuple(f"/dev/tty{n}" for n in range(1, 7))
FRAMEBUFFER = "/sys/class/graphics/fb0/virtual_size"


def escolher_fonte(largura: int, altura: int) -> str:
    """Maior fonte com ≥ 100 colunas e ≥ 30 linhas; em telas menores, a de 8x16 (a menor legível)."""
    escolhida = FONTES[0][2]
    for largura_car, altura_car, arquivo in FONTES:
        if largura // largura_car >= MIN_COLUNAS and altura // altura_car >= MIN_LINHAS:
            escolhida = arquivo
    return escolhida


def parse_virtual_size(texto: str | None) -> tuple[int, int] | None:
    """``/sys/class/graphics/fb0/virtual_size`` ("1920,1080") → ``(1920, 1080)``."""
    if not texto:
        return None
    partes = texto.strip().split(",")
    if len(partes) != 2 or not all(p.isdigit() for p in partes):
        return None
    return int(partes[0]), int(partes[1])


def aplicar_fonte(executor: Executor) -> str:
    """Aplica a fonte em tty1–tty6. Devolve o que foi feito (para o log do systemd)."""
    tamanho = parse_virtual_size(executor.ler_arquivo(FRAMEBUFFER))
    if tamanho is None:
        return "framebuffer sem resolução legível: fonte do console mantida"
    fonte = f"{PASTA_FONTES}/{escolher_fonte(*tamanho)}"
    falhas = [tty for tty in TTYS if executor.existe(tty) and executor.rodar(["setfont", "-C", tty, fonte], timeout=10)[0] != 0]
    resumo = f"resolução {tamanho[0]}x{tamanho[1]}: fonte {fonte}"
    return resumo + (f" (falhou em {', '.join(falhas)})" if falhas else "")


def main() -> int:
    print(aplicar_fonte(ExecutorReal()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

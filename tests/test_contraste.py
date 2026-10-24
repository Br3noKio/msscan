"""Contraste WCAG AA (4,5:1) de todo texto do tema (CLAUDE.md 15.1, pedido de 30/09/2026).

Calculado a partir do ``tema.css`` (único arquivo com cores): cada cor de texto sobre cada fundo, com as
transparências compostas sobre o fundo real. O texto secundário tem de passar com folga (>= 7:1).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMA = (Path(__file__).parent.parent / "msscan/interface/estaticos/tema.css").read_text(encoding="utf-8")
CSS = (Path(__file__).parent.parent / "msscan/interface/estaticos/app.css").read_text(encoding="utf-8")


def variavel(nome: str) -> str:
    return re.search(rf"--ms-{nome}:\s*([^;]+);", TEMA).group(1).strip()


def rgb(valor: str, fundo: tuple[float, ...] | None = None) -> tuple[float, ...]:
    if valor.startswith("#"):
        return tuple(int(valor[i:i + 2], 16) for i in (1, 3, 5))
    r, g, b, a = (float(x) for x in re.findall(r"[\d.]+", valor))
    return tuple(a * c + (1 - a) * f for c, f in zip((r, g, b), fundo))


def luminancia(cor: tuple[float, ...]) -> float:
    canais = [c / 255 for c in cor]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in canais]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def razao(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    la, lb = sorted((luminancia(a), luminancia(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


FUNDOS_BASE = ["preto-0", "preto-1", "preto-2"]
TEXTOS = ["texto", "texto-sec", "azul-link", "ok", "alerta", "falha"]


@pytest.mark.parametrize("fundo", FUNDOS_BASE)
@pytest.mark.parametrize("texto", TEXTOS)
def test_texto_sobre_fundos_solidos(texto, fundo):
    assert razao(rgb(variavel(texto)), rgb(variavel(fundo))) >= 4.5


@pytest.mark.parametrize("fundo", FUNDOS_BASE)
def test_texto_secundario_com_folga(fundo):
    assert razao(rgb(variavel("texto-sec")), rgb(variavel(fundo))) >= 7


@pytest.mark.parametrize(("texto", "camada"), [("ok", "ok-fundo"), ("alerta", "alerta-fundo"), ("falha", "falha-fundo"),
                                               ("texto-sec", "neutro-fundo"), ("azul-link", "azul-fundo"),
                                               ("texto", "azul-fundo"), ("texto-sec", "azul-fundo"), ("texto", "hover")])  # fmt: skip
@pytest.mark.parametrize("base", FUNDOS_BASE)
def test_texto_sobre_fundos_translucidos(texto, camada, base):
    fundo = rgb(variavel(camada), rgb(variavel(base)))
    assert razao(rgb(variavel(texto)), fundo) >= 4.5


def test_botao_primario_e_desabilitado():
    assert razao(rgb(variavel("texto")), rgb(variavel("azul-2"))) >= 4.5
    opacidade = float(variavel("opacidade-desabilitado"))
    for base in FUNDOS_BASE:
        fundo = rgb(variavel(base))
        apagado = tuple(opacidade * t + (1 - opacidade) * f for t, f in zip(rgb(variavel("texto")), fundo))
        assert razao(apagado, fundo) >= 4.5
    assert "opacity: .5" not in CSS and not re.search(r"opacity:\s*0?\.[0-5]\d*\s*;", CSS)


@pytest.mark.parametrize("texto", TEXTOS)
def test_texto_sobre_painel_translucido(texto):
    """Painel translúcido (15.4) sobre o ponto mais claro do fundo (brilho azul composto sobre preto-1)."""
    brilho = re.search(r"rgba\([^)]*0\.16\)", variavel("grad-fundo")).group(0)
    fundo_claro = rgb(brilho, rgb(variavel("preto-1")))
    painel = rgb(variavel("painel"), fundo_claro)
    minimo = 7 if texto == "texto-sec" else 4.5
    assert razao(rgb(variavel(texto)), painel) >= minimo


def test_sem_blur_nem_animacao_pesada():
    """Fluidez (15.4): sem backdrop-filter/blur; animações só de transform/opacity."""
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert "backdrop-filter" not in css and "blur(" not in css
    for bloco in re.findall(r"@keyframes\s+\w+\s*\{(.*?)\}\s*\}", css, re.S):
        for prop in re.findall(r"([a-z-]+)\s*:", bloco):
            assert prop in ("opacity", "transform"), prop

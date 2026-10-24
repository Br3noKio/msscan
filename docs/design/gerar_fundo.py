"""Papel de parede original do MSscan (nova interface, 09/10/2026): corredor de datacenter em perspectiva.

Duas fileiras de racks com LEDs, piso em grade, trilhas de circuito no teto e brilho azul no ponto de fuga, no gradiente
azul e preto do tema. Determinístico (semente fixa). Gera o SVG; o ``build_fundo.sh`` rasteriza (Chromium sem tela) e
cria a versão nítida e a JÁ DESFOCADA (imagem pronta: o desfoque não custa nada na hora de desenhar a tela).

Uso: ``python3 docs/design/gerar_fundo.py > fundo.svg``
"""

from __future__ import annotations

import random

L, A = 1920, 1080
VX, HY = L * 0.5, A * 0.46  # ponto de fuga
ALEATORIO = random.Random(20261009)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _prof(u: float) -> float:
    """Perspectiva: passos iguais no corredor ficam cada vez menores perto do ponto de fuga."""
    k = 6.0
    return (1 - 1 / (1 + k * u)) / (1 - 1 / (1 + k))


def _parede(lado: int) -> list[str]:
    """Uma fileira de racks (lado -1 = esquerda, 1 = direita)."""
    partes = []
    x0 = 0 if lado < 0 else L
    xf = VX + lado * 70
    topo0, base0, topof, basef = -A * 0.08, A * 1.02, HY - 46, HY + 40
    n = 9
    for i in range(n):
        u0, u1 = i / n, (i + 0.86) / n
        t0, t1 = _prof(u0), _prof(u1)
        xa, xb = _lerp(x0, xf, t0), _lerp(x0, xf, t1)
        ya0, ya1 = _lerp(topo0, topof, t0), _lerp(base0, basef, t0)
        yb0, yb1 = _lerp(topo0, topof, t1), _lerp(base0, basef, t1)
        partes.append(f'<polygon points="{xa:.1f},{ya0:.1f} {xb:.1f},{yb0:.1f} {xb:.1f},{yb1:.1f} {xa:.1f},{ya1:.1f}" '
                      f'fill="url(#rack)" stroke="#1f3a66" stroke-width="{max(0.6, 2.2 * (1 - t0)):.2f}"/>')  # fmt: skip
        unidades = 22
        for j in range(1, unidades):
            f = j / unidades
            y_a, y_b = _lerp(ya0, ya1, f), _lerp(yb0, yb1, f)
            partes.append(f'<line x1="{xa:.1f}" y1="{y_a:.1f}" x2="{xb:.1f}" y2="{y_b:.1f}" stroke="#16304f" '
                          f'stroke-width="{max(0.3, 1.1 * (1 - t0)):.2f}" opacity="0.8"/>')  # fmt: skip
            if ALEATORIO.random() < 0.55:
                g = ALEATORIO.uniform(0.12, 0.88)
                lx = _lerp(xa, xb, g)
                ly = _lerp(y_a, y_b, g) - (ya1 - ya0) / unidades * 0.45
                cor = ALEATORIO.choices(["#58a6ff", "#79c0ff", "#3fb950", "#1f6feb"], [5, 2, 1.2, 3])[0]
                raio = max(0.5, 2.6 * (1 - t0) ** 1.4)
                partes.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="{raio:.2f}" fill="{cor}" opacity="{ALEATORIO.uniform(.55, 1):.2f}"/>')
    return partes


def _piso() -> list[str]:
    partes = []
    for i in range(-14, 15):
        x = VX + i * 150
        partes.append(f'<line x1="{VX:.1f}" y1="{HY:.1f}" x2="{x:.1f}" y2="{A}" stroke="#123056" stroke-width="1" opacity="0.55"/>')
    for i in range(1, 14):
        t = _prof(i / 14)
        y = _lerp(HY + 40, A, 1 - t)
        partes.append(f'<line x1="0" y1="{y:.1f}" x2="{L}" y2="{y:.1f}" stroke="#123056" stroke-width="1" opacity="{0.25 + 0.35 * (1 - t):.2f}"/>')
    return partes


def _circuitos() -> list[str]:
    """Trilhas em ângulo reto no teto, com nós (grade de circuito discreta)."""
    partes = []
    for _ in range(26):
        x = ALEATORIO.uniform(L * 0.18, L * 0.82)
        y = ALEATORIO.uniform(A * 0.02, HY - 120)
        caminho = [f"M{x:.0f},{y:.0f}"]
        for _ in range(ALEATORIO.randint(2, 5)):
            if ALEATORIO.random() < 0.5:
                x += ALEATORIO.choice([-1, 1]) * ALEATORIO.uniform(30, 140)
            else:
                y = min(HY - 90, max(8, y + ALEATORIO.choice([-1, 1]) * ALEATORIO.uniform(20, 70)))
            caminho.append(f"L{x:.0f},{y:.0f}")
        partes.append(f'<path d="{" ".join(caminho)}" fill="none" stroke="#1f6feb" stroke-width="1.2" opacity="0.22"/>')
        partes.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="2.6" fill="#58a6ff" opacity="0.45"/>')
    return partes


def svg() -> str:
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{L}" height="{A}" viewBox="0 0 {L} {A}">',
        "<defs>",
        '<linearGradient id="ceu" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#010409"/>'
        '<stop offset="0.46" stop-color="#0a1a33"/><stop offset="1" stop-color="#010409"/></linearGradient>',
        '<radialGradient id="brilho" cx="0.5" cy="0.46" r="0.42"><stop offset="0" stop-color="#1f6feb" stop-opacity="0.55"/>'
        '<stop offset="0.35" stop-color="#0d2d62" stop-opacity="0.35"/><stop offset="1" stop-color="#010409" stop-opacity="0"/></radialGradient>',
        '<linearGradient id="rack" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0b1424"/>'
        '<stop offset="1" stop-color="#060b14"/></linearGradient>',
        '<radialGradient id="vinheta" cx="0.5" cy="0.5" r="0.75"><stop offset="0.55" stop-color="#010409" stop-opacity="0"/>'
        '<stop offset="1" stop-color="#010409" stop-opacity="0.85"/></radialGradient>',
        "</defs>",
        f'<rect width="{L}" height="{A}" fill="url(#ceu)"/>',
        *_circuitos(),
        f'<rect width="{L}" height="{A}" fill="url(#brilho)"/>',
        *_piso(),
        *_parede(-1),
        *_parede(1),
        f'<rect width="{L}" height="{A}" fill="url(#vinheta)"/>',
        "</svg>",
    ])  # fmt: skip


if __name__ == "__main__":
    print(svg())

"""Modelo 3D (e mapa 2D) GENÉRICO para os Dell sem posições conferidas no manual (CLAUDE.md 19.4).

Gerado pela família e pelo formato do nome do modelo (PowerEdge R4xx/R6xx = rack 1U, R5xx/R7xx = rack 2U, T4xx a T6xx =
torre; o 2º algarismo é a geração: 2 = 12G … 6 = 16G), no mesmo formato do bloco ``modelo3d``/``mapa`` do banco. As
áreas seguem o arranjo comum dos PowerEdge de 2 processadores (discos e backplane na frente, ventoinhas, processadores com
a memória dos dois lados, controladora, fontes e risers atrás) e são **aproximadas**: tudo leva a marca
``aproximado`` e o texto "posições aproximadas" (nunca apresentado como exato). Quantidade de baias, ventoinhas e slots
PCIe não é afirmada; os soquetes de memória vêm do SMBIOS lido agora.

Não vira entrada do banco de compatibilidade (que continua só restringindo a atualização): fica em ``compat["generico"]``.
"""

from __future__ import annotations

import re
from typing import Any

_RE_MODELO = re.compile(r"PowerEdge ([RT])([4-7])([2-6])0(xd|xa|xs|xl|xd2)?", re.IGNORECASE)
GERACOES = {"2": "12ª geração (12G)", "3": "13ª geração (13G)", "4": "14ª geração (14G)", "5": "15ª geração (15G)",
            "6": "16ª geração (16G)"}  # fmt: skip
# Dimensões típicas por formato (mm). Torre: mostrada deitada, com a placa-mãe para baixo.
FORMATOS = {"1U": {"altura": 42.8, "largura": 434.0, "profundidade": 734.0},
            "2U": {"altura": 86.8, "largura": 434.0, "profundidade": 715.0},
            "torre": {"altura": 220.0, "largura": 440.0, "profundidade": 700.0}}  # fmt: skip
APROX = "posição aproximada"
# Mapa 2D (1000 x 610, vista de cima, frente à esquerda), mesmo arranjo do R640 conferido.
_AREAS = [
    ("discos", "Discos (frente; quantidade de baias não conferida)", "Discos", 12, 37, 129, 533),
    ("backplane", f"Backplane de discos (atrás dos discos; {APROX})", "BP", 141, 37, 49, 533),
    ("ventoinhas", f"Ventoinhas (fileira central; quantidade não conferida)", "Ventoinhas", 282, 50, 111, 505),
    ("cpu2", f"CPU2 ({APROX})", "CPU2", 470, 120, 110, 110),
    ("dimm_cpu2", f"DIMMs B (CPU2; {APROX})", "DIMMs B", 436, 37, 177, 270),
    ("cpu1", f"CPU1 ({APROX})", "CPU1", 470, 380, 110, 110),
    ("dimm_cpu1", f"DIMMs A (CPU1; {APROX})", "DIMMs A", 436, 307, 177, 263),
    ("controladora", f"Controladora de armazenamento ({APROX})", "PERC", 626, 393, 92, 135),
    ("cabos_sas", f"Cabos SAS (controladora → backplane; {APROX})", "Cabos SAS", 184, 528, 442, 42),
    ("psu2", f"Fonte 2 (PSU2, traseira; {APROX})", "PSU2", 693, 37, 252, 116),
    ("psu1", f"Fonte 1 (PSU1, traseira; {APROX})", "PSU1", 693, 153, 252, 117),
    ("riser1", "Risers PCIe (traseira; slots não conferidos)", "Risers", 730, 276, 215, 294),
    ("frente", "Painel frontal", "Frente", 0, 30, 12, 550),
    ("traseira", "Traseira", "Traseira", 945, 30, 12, 550),
    ("chassi", "Chassi / tampa", "Chassi / tampa", 0, 30, 957, 550),
]


def familia(modelo: str | None) -> dict[str, str] | None:
    """``{"formato", "geracao", "familia"}`` de um PowerEdge coberto (2 processadores, gerações 12 a 16); senão ``None``."""
    casado = _RE_MODELO.fullmatch((modelo or "").strip())
    if not casado:
        return None
    letra, classe, geracao = casado.group(1).upper(), casado.group(2), casado.group(3)
    formato = "torre" if letra == "T" else "1U" if classe in ("4", "6") else "2U"
    return {"formato": formato, "geracao": GERACOES[geracao], "familia": f"{letra}{classe}x{geracao}"}


def gerar(fabricante: str | None, modelo: str | None) -> dict[str, Any] | None:
    """``{"nome", "mapa", "modelo3d"}`` genéricos para o modelo, ou ``None`` se não for um Dell coberto."""
    if "dell" not in (fabricante or "").lower():
        return None
    info = familia(modelo)
    if info is None:
        return None
    rotulo = {"1U": "rack 1U", "2U": "rack 2U", "torre": "torre (mostrada deitada)"}[info["formato"]]
    aviso = (f"Posições aproximadas: modelo genérico de servidor {rotulo}, {info['geracao']} — o {modelo} não tem as "
             "posições conferidas no manual da Dell no banco do MSscan. Use os rótulos (ex.: DIMM A2, PSU1) e a etiqueta "
             "da tampa do servidor para achar a peça.")  # fmt: skip
    return {
        "nome": f"Dell {modelo} (genérico {rotulo})",
        "aproximado": True,
        "aviso": aviso,
        "mapa": {"vista": "vista de cima, frente à esquerda (posições aproximadas)", "largura": 1000, "altura": 610,
                 "rotulos": {"frente": "FRENTE", "traseira": "TRASEIRA"}, "aproximado": True,
                 "areas": [{"id": i, "nome": n, "curto": c, "x": x, "y": y, "w": w, "h": h} for i, n, c, x, y, w, h in _AREAS]},
        "modelo3d": {"fonte": f"modelo genérico ({rotulo}, {info['geracao']}); posições aproximadas", "aproximado": True,
                     "aviso": aviso, "dimensoes_mm": dict(FORMATOS[info["formato"]]),
                     "chassi_mapa": {"x": 0, "y": 30, "w": 957, "h": 550}, "dimm_ordem": "detectado", "ventoinhas": 0,
                     "baias": 0, "risers": {"riser1": []}},
    }  # fmt: skip


__all__ = ["FORMATOS", "familia", "gerar"]

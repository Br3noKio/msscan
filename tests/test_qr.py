"""QR Code local (msscan/qr.py) comparado com a libqrencode (tests/fixtures/qr/LEIAME.md)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from msscan.ia.catalogo import carregar_catalogo
from msscan.qr import capacidade_bytes, gerar, para_linhas

FIXTURES = Path(__file__).parent / "fixtures" / "qr"
CASOS = sorted(p.stem for p in FIXTURES.glob("*.json"))


def carregar(nome: str) -> tuple[str, list[str]]:
    dados = json.loads((FIXTURES / f"{nome}.json").read_text())
    return dados["texto"], dados["matriz"]


def mascara_de(linhas: list[str]) -> int:
    """Máscara lida dos bits de formato (cópia ao redor do padrão do canto superior esquerdo)."""
    posicoes = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(14 - i, 8) for i in range(9, 15)]
    bits = sum(int(linhas[y][x]) << i for i, (x, y) in enumerate(posicoes))
    return ((bits ^ 0x5412) >> 10) & 7


@pytest.mark.parametrize("nome", CASOS)
def test_igual_a_libqrencode_com_a_mesma_mascara(nome):
    texto, referencia = carregar(nome)
    assert para_linhas(gerar(texto, mascara=mascara_de(referencia))) == referencia


@pytest.mark.parametrize("nome", CASOS)
def test_mascara_automatica_gera_formato_consistente(nome):
    texto, referencia = carregar(nome)
    matriz = para_linhas(gerar(texto))
    assert len(matriz) == len(referencia)  # mesma versão
    assert para_linhas(gerar(texto, mascara=mascara_de(matriz))) == matriz


def test_cobre_as_versoes_1_7_e_10():
    versoes = {(len(carregar(n)[1]) - 17) // 4 for n in CASOS}
    assert {1, 2, 3, 7, 10} <= versoes


def test_capacidades_da_norma_nivel_m():
    # ISO/IEC 18004, tabela 7 (modo byte, nível M)
    assert [capacidade_bytes(v) for v in range(1, 11)] == [14, 26, 42, 62, 84, 106, 122, 152, 180, 213]


def test_longo_demais_nao_gera():
    assert gerar("a" * 214) is None
    assert gerar("a" * 213) is not None


def test_todos_os_links_do_catalogo_cabem():
    for entrada in carregar_catalogo().entradas:
        assert gerar(entrada.link_chave) is not None, entrada.id

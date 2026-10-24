"""Catálogo Linux da Dell (``.BIN``) no caminho f, sem iDRAC acessível (pedido de 21/10/2026).

Fixture: recorte SEM EDIÇÃO do "Update Catalog for Enterprise Servers" 26.10.15 (``catalog/Catalog.gz``) com a BIOS 2.29.2
(JJ09G) e o iDRAC 7.00.00.185 (K7H6Y) do R640, cada um em ``.EXE`` (LW64) e ``.BIN`` (LLXP)."""

from __future__ import annotations

from msscan.atualizacao.aplicacao import IDRAC_INACESSIVEL
from msscan.atualizacao.automatica import outro_formato
from msscan.atualizacao.catalogo import ARQUIVO_LINUX, caminho_cache, catalogo_em_cache, pacotes_linux
from msscan.atualizacao.verificacao import sem_idrac
from msscan.parsers.catalogo_dell import parse_catalogo_dell
from tests.conftest import PASTA_FIXTURES
from tests.test_atualizacao import RECORTE

LINUX = PASTA_FIXTURES / "dell" / "catalogo" / "Catalog_linux_recorte_r640_261015.gz"
BIOS_BIN = "https://downloads.dell.com/FOLDER15061636M/1/BIOS_JJ09G_LN64_2.29.2.BIN"


def _bios(**extra):
    """Item da verificação como o catálogo do iDRAC o monta: BIOS lida pelo SMBIOS (sem componentID), pacote .EXE."""
    return {"componente": "BIOS", "situacao": "atualizacao", "component_id": None, "disponivel": "2.29.2",
            "pacote": {"url": "https://downloads.dell.com/FOLDER15061639M/1/BIOS_JJ09G_WN64_2.29.2.EXE", "release": "JJ09G"},
            **extra}  # fmt: skip


def test_so_os_bin_linux_do_catalogo() -> None:
    dados = parse_catalogo_dell(LINUX.read_bytes())
    assert len(dados["componentes"]) == 4
    assert [c["caminho"].rsplit("/", 1)[-1] for c in pacotes_linux(dados)] == [
        "iDRAC-with-Lifecycle-Controller_Firmware_K7H6Y_LN64_7.00.00.185_A00.BIN", "BIOS_JJ09G_LN64_2.29.2.BIN"]
    assert all(c["base"] == "downloads.dell.com" and c["algoritmo_hash"] == "SHA256" for c in pacotes_linux(dados))
    assert pacotes_linux(None) == []


def test_bin_pelo_release_id_sem_component_id() -> None:
    catalogo = {"base": "downloads.dell.com", "componentes": [], "bin_linux": pacotes_linux(parse_catalogo_dell(LINUX.read_bytes()))}
    alternativo = outro_formato(_bios(), catalogo, "0716")
    assert alternativo["url"] == BIOS_BIN and len(alternativo["sha256"]) == 64
    # Registro da instalação (aplicacao.py) guarda o release fora de "pacote".
    assert outro_formato({"component_id": None, "disponivel": "2.29.2", "release": "JJ09G"}, catalogo, "0716")["url"] == BIOS_BIN
    # Nunca: outro modelo, outra versão, outro release, nem sem o systemID deste servidor.
    assert outro_formato(_bios(), catalogo, "04CE") is None
    assert outro_formato(_bios(disponivel="2.28.1"), catalogo, "0716") is None
    assert outro_formato(_bios(pacote={"release": "XXXXX"}), catalogo, "0716") is None
    assert outro_formato(_bios(), catalogo, None) is None


def test_sem_idrac_nao_avisa_quando_ha_bin(tmp_path) -> None:
    catalogo = {"base": "downloads.dell.com", "componentes": [], "bin_linux": pacotes_linux(parse_catalogo_dell(LINUX.read_bytes()))}
    secao = {"system_id": "0716", "acesso": {"codigo": IDRAC_INACESSIVEL}, "itens": [_bios()]}
    assert sem_idrac(secao, catalogo, "Dell Inc.", "PowerEdge R640") is None
    assert sem_idrac(secao, {**catalogo, "bin_linux": []}, "Dell Inc.", "PowerEdge R640")["componentes"] == ["BIOS"]


def test_cache_junta_os_bin_ao_catalogo_em_uso(tmp_path) -> None:
    cache = caminho_cache(str(tmp_path))
    cache.parent.mkdir(parents=True)
    cache.write_bytes(RECORTE.read_bytes())
    assert catalogo_em_cache(str(tmp_path))["bin_linux"] == []  # sem catálogo Linux baixado: como antes
    caminho_cache(str(tmp_path), ARQUIVO_LINUX).write_bytes(LINUX.read_bytes())
    dados = catalogo_em_cache(str(tmp_path))
    assert dados["versao"] == parse_catalogo_dell(RECORTE.read_bytes())["versao"] and len(dados["bin_linux"]) == 2

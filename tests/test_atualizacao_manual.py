"""Atualização manual (fase 11): página de drivers, identificação do pacote, SHA-256, modelo, versão e códigos do DUP."""

from __future__ import annotations

from pathlib import Path

import pytest

from msscan.atualizacao import manual
from msscan.atualizacao.versoes import ATUALIZACAO, INCERTA, INSTALADA_MAIS_NOVA

PACOTES = Path(__file__).parent / "fixtures/dell/pacotes"
XML_BIOS = (PACOTES / "BIOS_W1WJW_LN_2.9.0.package.xml").read_bytes()
XML_SSD = (PACOTES / "Express-Flash-PCIe-SSD_Firmware_T79D5_LN64_1.2.0_A04_01.package.xml").read_bytes()
SKU_R620 = "SKU=04CE;ModelName=PowerEdge R620"
SKU_R640 = "SKU=0716;ModelName=PowerEdge R640"


def bin_falso(xml: bytes) -> bytes:
    """Mesmo leiaute do .BIN real (LEIAME): script que cita os marcadores, pacote em UTF-16, arquivo compactado."""
    script = (b'#!/bin/sh\n_MANIFEST_XML_MARKER="#####Startofpackage#####"\n'
              b'_DUP_ARCHIVE_MARKER="#####Startofarchive#####"\nexit 0\n')  # fmt: skip
    return script + b"#####Startofpackage#####\n" + xml + b"\n#####Startofarchive#####\n\x1f\x8b\x08conteudo"


def bios(versao: str, **extra) -> dict:
    return {"nome": "BIOS", "versao": versao, "tipo": "BIOS", "component_id": None, "truncada": False, **extra}


class TestPaginaDeDrivers:
    def test_dell_por_service_tag_e_por_modelo(self):
        assert manual.url_drivers("Dell Inc.", "PowerEdge R620", "abc1234")["url"] == \
            "https://www.dell.com/support/product-details/pt-br/servicetag/ABC1234/drivers"  # fmt: skip
        pagina = manual.url_drivers("Dell Inc.", "PowerEdge R620", "Not Specified")
        assert pagina["tipo"] == "modelo" and pagina["url"].endswith("/product/poweredge-r620/drivers")
        assert manual.url_drivers("Dell Inc.", "Precision 7920", None)["tipo"] == "busca"

    def test_service_tag_invalido_nao_vai_na_url(self):
        for tag in ("ABC123", "ABC12345", "ABC-123", "../../x"):
            assert tag.upper() not in manual.url_drivers("Dell Inc.", "PowerEdge R640", tag)["url"]

    def test_hpe_lenovo_e_pastas(self):
        assert manual.url_drivers("HPE", "ProLiant DL380 Gen10", "X")["url"].startswith("https://support.hpe.com/")
        assert manual.url_drivers("Lenovo", "ThinkSystem SR650", "X")["url"].startswith("https://datacentersupport.lenovo.com/")
        assert manual.url_drivers("Supermicro", "X11", None)["url"] == ""
        assert manual.pasta_destino("HPE") == "firmware/hpe" and manual.pasta_destino("Dell Inc.") == "firmware/manual"


class TestIdentificacao:
    @pytest.mark.parametrize("nome, esperado", [
        ("BIOS_W1WJW_LN_2.9.0.BIN", ("BIOS", "W1WJW", "LN", "2.9.0", "bin", True)),
        ("Network_Firmware_F2K1J_WN64_38.11.70.00.EXE", ("Network", "F2K1J", "WN64", "38.11.70.00", "exe", False)),
        ("SAS-RAID_Firmware_06FNV_WN64_52.38.0-6897_A07_01.EXE", ("SAS-RAID", "06FNV", "WN64", "52.38.0-6897", "exe", False)),
        ("iDRAC-with-Lifecycle-Controller_Firmware_0GHF4_WN64_2.65.65.65_A00.EXE",
         ("iDRAC-with-Lifecycle-Controller", "0GHF4", "WN64", "2.65.65.65", "exe", False)),
        ("Express-Flash-PCIe-SSD_Firmware_T79D5_LN64_1.2.0_A04_01(1).BIN",
         ("Express-Flash-PCIe-SSD", "T79D5", "LN64", "1.2.0", "bin", True)),
    ])  # fmt: skip
    def test_nomes_reais(self, nome, esperado):
        campos = manual.identificar_nome(nome)
        assert (campos["categoria"], campos["release"], campos["plataforma"], campos["versao"], campos["ext"], campos["linux"]) == esperado

    @pytest.mark.parametrize("nome", ["relatorio.pdf", "R620-020900C.exe", "spp.iso", "BIOS.BIN"])
    def test_outros_arquivos(self, nome):
        assert manual.identificar_nome(nome) is None

    def test_package_xml_sem_executar(self, tmp_path):
        arquivo = tmp_path / "BIOS_W1WJW_LN_2.9.0.BIN"
        arquivo.write_bytes(bin_falso(XML_BIOS))
        arquivo.chmod(0o600)  # nem precisa ser executável: só é lido
        pacote = manual.ler_pacote(arquivo)
        assert pacote["versao"] == "2.9.0" and pacote["tipo"] == "BIOS" and pacote["reinicio"] is True
        assert {s["system_id"] for s in pacote["sistemas"]} == {"048C", "04CE", "0531"}

    def test_sem_pacote_ou_quebrado(self, tmp_path):
        (tmp_path / "a.BIN").write_bytes(b"#!/bin/sh\nexit 0\n")
        (tmp_path / "b.BIN").write_bytes(bin_falso(XML_BIOS[:300]))
        assert manual.ler_pacote(tmp_path / "a.BIN") is None and manual.ler_pacote(tmp_path / "b.BIN") is None
        assert manual.ler_pacote(tmp_path / "nao_existe.BIN") is None

    @pytest.mark.parametrize("texto", [
        "8fa8317ff0ce0e8f7095b1d4243dbe81cebf83dc63d39766d0ffef764d9174b3",
        "SHA-256: 8FA8317FF0CE0E8F7095B1D4243DBE81CEBF83DC63D39766D0FFEF764D9174B3",
        "  sha256 8fa8317f f0ce0e8f 7095b1d4 243dbe81 cebf83dc 63d39766 d0ffef76 4d9174b3\n",
    ])  # fmt: skip
    def test_sha256_colado(self, texto):
        assert manual.normalizar_sha256(texto) == "8fa8317ff0ce0e8f7095b1d4243dbe81cebf83dc63d39766d0ffef764d9174b3"

    @pytest.mark.parametrize("texto", ["", None, "a6a681e3ed401ed4f3f1868d4845bd49", "z" * 64, "8fa8317f" * 9])
    def test_sha256_invalido(self, texto):
        assert manual.normalizar_sha256(texto) is None


class TestConferencias:
    def test_modelo_pelo_system_id(self):
        pacote = manual.parse_pacote_dell(XML_BIOS)
        assert manual.conferir_modelo(pacote, SKU_R620, [])[0] is True
        ok, motivo = manual.conferir_modelo(pacote, SKU_R640, [])
        assert ok is False and "R620 (04CE)" in motivo and "0716" in motivo
        assert manual.conferir_modelo(pacote, None, [])[0] is False

    def test_modelo_pelo_dispositivo(self):
        pacote = manual.parse_pacote_dell(XML_SSD)
        assert manual.conferir_modelo(pacote, SKU_R640, [{"component_id": "107611"}])[0] is True
        assert manual.conferir_modelo(pacote, SKU_R640, [])[0] is None
        assert manual.conferir_modelo(None, SKU_R640, [])[0] is None

    def test_versao_nunca_downgrade(self):
        pacote = manual.parse_pacote_dell(XML_BIOS)
        assert manual.conferir_versao(pacote, [bios("2.7.0")])[0] == ATUALIZACAO
        assert manual.conferir_versao(pacote, [bios("2.9.1")])[0] == INSTALADA_MAIS_NOVA
        assert manual.conferir_versao(pacote, [])[0] == INCERTA
        ssd = manual.parse_pacote_dell(XML_SSD)
        situacao, _, instalada = manual.conferir_versao(ssd, [{"component_id": "107610", "versao": "1.1.0"}])
        assert situacao == ATUALIZACAO and instalada == "1.1.0"


class TestCodigosDoDup:
    def test_tabela_oficial(self):
        assert set(manual.CODIGOS_DUP) == {0, 1, 2, 3, 4, 5, 6, *range(13, 21)}
        assert manual.interpretar_dup(0)["aplicavel"] and manual.interpretar_dup(2)["categoria"] == "reinicio"
        assert manual.interpretar_dup(3)["categoria"] == "mesma_versao" and not manual.interpretar_dup(3)["aplicavel"]
        assert manual.interpretar_dup(5)["categoria"] == "nao_aplicavel"
        assert manual.interpretar_dup(99)["categoria"] == "falha" and manual.interpretar_dup(-1)["nome"] == "DESCONHECIDO"


class TestMetodo:
    def test_bin_pelo_dup_e_exe_pelo_idrac_so_se_anunciado(self):
        bin_ = manual.identificar_nome("BIOS_W1WJW_LN_2.9.0.BIN")
        exe = manual.identificar_nome("BIOS_W1WJW_WN64_2.9.0.EXE")
        assert manual.metodo_aplicacao(bin_, None)[0] == manual.DUP_LINUX
        # iDRAC9: .EXE só com a versão confirmada pelo catálogo oficial (mesmo release ID); daí, caminho automático.
        assert manual.metodo_aplicacao(exe, {"redfish": True, "multipart": "/x", "simple_update": "/y"})[0] is None
        assert manual.metodo_aplicacao(exe, {"redfish": True, "multipart": "/x"}, versao_confirmada=True)[0] == manual.AUTOMATICO
        assert manual.metodo_aplicacao(exe, {"redfish": True, "simple_update": "/y"})[0] == manual.REDFISH_IDRAC_LEGADO
        metodo, motivo = manual.metodo_aplicacao(exe, {"redfish": True})
        assert metodo is None and "não confirmada" in motivo
        # Sem credenciais agora não recusa por isso: o acesso é resolvido na instalação (18.3 a–c).
        assert manual.metodo_aplicacao(exe, {"redfish": False, "motivo": "sem credenciais"}, versao_confirmada=True)[0] == manual.AUTOMATICO
        assert manual.metodo_aplicacao(None, None)[0] is None

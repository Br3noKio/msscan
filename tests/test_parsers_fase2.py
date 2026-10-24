"""Testes dos parsers da fase 2 com saídas reais (Dell PowerEdge R640)."""

from __future__ import annotations

import pytest

from msscan.parsers import ErroParser
from msscan.parsers.dmidecode import parse_memorias, parse_processadores, tamanho_em_mb
from msscan.parsers.ipmitool import parse_sdr_tipo, parse_sel_elist, parse_sensor, tipo_do_sensor
from msscan.parsers.lsblk import parse_lsblk_json
from msscan.parsers.procfs import parse_cpuinfo
from msscan.parsers.smartctl import bits_saida, parse_scan_open, parse_smart
from tests.conftest import R640, ler_saida


def ler_arquivo_r640(caminho: str) -> str:
    return (R640 / "arquivos" / caminho.lstrip("/")).read_text(encoding="utf-8")


class TestIpmitoolSensor:
    @pytest.fixture
    def sensores(self):
        return parse_sensor(ler_saida(R640, "ipmitool__sensor"))

    def test_estrutura(self, sensores):
        assert len(sensores) == len(ler_saida(R640, "ipmitool__sensor").strip().splitlines())
        primeiro = sensores[0]
        assert primeiro["nome"] == "Temp"
        assert primeiro["unidade"] == "degrees C"
        assert not primeiro["discreto"]
        assert primeiro["status"] == "ok"
        assert primeiro["limiares"]["ucr"] == 102.0
        assert primeiro["limiares"]["unr"] is None

    def test_nomes_repetidos_sao_mantidos(self, sensores):
        assert sum(s["nome"] == "Temp" for s in sensores) >= 2

    def test_discretos_e_na(self, sensores):
        power_cable = next(s for s in sensores if s["nome"] == "Power Cable")
        assert power_cable["discreto"] and power_cable["valor"] is None and power_cable["status"] == "na"
        inlet = next(s for s in sensores if s["nome"] == "Inlet Temp")
        assert inlet["limiares"]["lcr"] == -7.0
        assert inlet["limiares"]["unc"] == 43.0

    def test_tolera_virgula_decimal(self):
        # Linha real com separador decimal pt_BR (como sai sem LC_ALL=C).
        linha = ler_saida(R640, "ipmitool__sensor").splitlines()[2].replace(".", ",")
        [sensor] = parse_sensor(linha)
        assert sensor["nome"] == "Inlet Temp"
        assert isinstance(sensor["valor"], float)

    def test_invalido(self):
        with pytest.raises(ErroParser):
            parse_sensor("Could not open device at /dev/ipmi0\n")
        assert parse_sensor("") == []


class TestIpmitoolSdr:
    def test_fontes_real(self):
        linhas = parse_sdr_tipo(ler_saida(R640, "ipmitool__sdr__type__Power_Supply"))
        assert [(l["nome"], l["entidade"], l["status"]) for l in linhas] == [
            ("PS Redundancy", "7.1", "ns"),
            ("Status", "10.1", "ok"),
            ("Status", "10.2", "ok"),
        ]
        assert linhas[0]["estados"] == ["Disabled"]
        assert linhas[1]["estados"] == ["Presence detected", "Power Supply AC lost"]
        assert linhas[1]["id_sensor"] == "85h"

    def test_ventoinhas_real(self):
        linhas = parse_sdr_tipo(ler_saida(R640, "ipmitool__sdr__type__Fan"))
        fan1a = linhas[0]
        assert fan1a["nome"] == "Fan1A" and fan1a["unidade"] == "RPM" and fan1a["valor"] > 0
        redundancia = next(l for l in linhas if l["nome"] == "Fan Redundancy")
        assert redundancia["valor"] is None and redundancia["estados"] == ["Fully Redundant"]
        assert len(linhas) == 17


class TestSel:
    @pytest.fixture
    def eventos(self):
        return parse_sel_elist(ler_saida(R640, "ipmitool__sel__elist"))

    def test_quantidade_bate_com_sel_info(self, eventos):
        assert "Entries          : 79" in ler_saida(R640, "ipmitool__sel__info")
        assert len(eventos) == 79

    def test_descricao_com_barra_vertical(self, eventos):
        ecc = next(e for e in eventos if e["id"] == "25")
        assert ecc["tipo"] == "Memory"
        assert ecc["sensor"] == "Memory ECC Uncorr Err"
        assert ecc["evento"] == "Uncorrectable ECC (UnCorrectable ECC | DIMMA2)"
        assert ecc["direcao"] == "Asserted"

    def test_tipos(self, eventos):
        por_id = {e["id"]: e for e in eventos}
        assert por_id["16"]["tipo"] == "Processor" and por_id["16"]["direcao"] == "Deasserted"
        assert por_id["41"]["tipo"] == "Drive Slot / Bay"
        assert por_id["26"]["tipo"] == "Critical Interrupt"
        assert por_id["24"]["tipo"] == ""  # "Unknown Additional Info"
        assert por_id["1"]["tipo"] == "Event Logging Disabled"

    def test_evento_vazio(self, eventos):
        post = next(e for e in eventos if e["id"] == "20")
        assert post["sensor"] == "System Firmwares POST Err" and post["evento"] == ""

    def test_tipo_do_sensor(self):
        assert tipo_do_sensor("Power Supply Status") == "Power Supply"
        assert tipo_do_sensor("Power Unit Redundancy") == "Power Unit"
        assert tipo_do_sensor("Fan") == "Fan"
        assert tipo_do_sensor("Fanatic") == ""

    def test_sel_vazio(self):
        # TODO(fixture): confirmar com saída real de "ipmitool sel elist" com SEL vazio.
        assert parse_sel_elist("SEL has no entries\n") == []

    def test_invalido(self):
        with pytest.raises(ErroParser):
            parse_sel_elist("Error: unable to open\n")


class TestDmidecodeFase2:
    def test_processadores_real(self):
        cpus = parse_processadores(ler_saida(R640, "dmidecode__-t__4"))
        assert [c["soquete"] for c in cpus] == ["CPU1", "CPU2"]
        for cpu in cpus:
            assert cpu["populado"] and cpu["habilitado"]
            assert cpu["modelo"] == "Intel(R) Xeon(R) Gold 5220 CPU @ 2.20GHz"
            assert (cpu["nucleos"], cpu["nucleos_habilitados"], cpu["threads"]) == (18, 18, 36)
            assert "serial" not in cpu

    def test_memorias_real(self):
        slots = parse_memorias(ler_saida(R640, "dmidecode__-t__17"))
        assert len(slots) == 24
        instalados = [m for m in slots if m["instalado"]]
        assert [m["slot"] for m in instalados] == ["A1", "A2"]
        a1 = instalados[0]
        assert a1["tamanho_mb"] == 8192 and a1["tipo"] == "DDR4" and a1["rank"] == 2
        assert a1["serial"] == "REMOVIDO"  # fixture anonimizada
        vazio = next(m for m in slots if m["slot"] == "A3")
        assert not vazio["instalado"] and vazio["tamanho_mb"] is None and vazio["tipo"] is None

    @pytest.mark.parametrize(
        ("texto", "esperado"),
        [("8 GB", 8192), ("16384 MB", 16384), ("1 TB", 1048576), ("No Module Installed", None), ("", None)],
    )
    def test_tamanho(self, texto, esperado):
        assert tamanho_em_mb(texto) == esperado


class TestCpuinfo:
    def test_real(self):
        info = parse_cpuinfo(ler_arquivo_r640("/proc/cpuinfo"))
        assert info == {
            "processadores_logicos": 72,
            "soquetes": 2,
            "modelos": ["Intel(R) Xeon(R) Gold 5220 CPU @ 2.20GHz"],
        }

    def test_invalido(self):
        with pytest.raises(ErroParser):
            parse_cpuinfo("lixo sem processador\n")


class TestSmartctl:
    def test_scan_open_real(self):
        assert parse_scan_open(ler_saida(R640, "smartctl__--scan-open__-j")) == [
            {"nome": "/dev/sda", "tipo": "sat", "protocolo": "ATA", "erro": ""}
        ]

    def test_smart_real(self):
        disco = parse_smart(ler_saida(R640, "smartctl__-a__-j__-d__sat__dev_sda"))
        assert disco["modelo"] == "VK001920GWSXK"
        assert disco["serial"] == "REMOVIDO"
        assert disco["smart_passou"] is True
        assert disco["codigo_saida"] == 0
        assert disco["smart_disponivel"] and disco["smart_habilitado"]
        assert isinstance(disco["temperatura"], int)
        crc = next(a for a in disco["atributos"] if a["id"] == 199)
        assert crc["nome"] == "UDMA_CRC_Error_Count" and crc["bruto"] == 4
        assert disco["nvme"] is None

    def test_invalido(self):
        with pytest.raises(ErroParser):
            parse_smart("smartctl: comando inválido")

    def test_bits(self):
        assert bits_saida(0) == set()
        assert bits_saida(0b1001_0010) == {1, 4, 7}


class TestLsblk:
    def test_real(self):
        [sda] = parse_lsblk_json(ler_saida(R640, "lsblk__-J__-d__-o__NAME_TRAN_TYPE_SIZE_MODEL"))
        assert sda["name"] == "sda" and sda["tran"] == "sata"

    def test_invalido(self):
        with pytest.raises(ErroParser):
            parse_lsblk_json("{}")

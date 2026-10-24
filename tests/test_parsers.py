"""Testes dos parsers com saídas reais (fixture Dell PowerEdge R640)."""

from __future__ import annotations

import re

import pytest

from msscan.parsers import ErroParser
from msscan.parsers.dmidecode import (
    parse_dmidecode_s,
    parse_host_interface_tipo42,
    parse_ipmi_tipo38,
    separar_estruturas,
    valor_util,
)
from msscan.parsers.ipmitool import parse_mc_info
from msscan.parsers.lspci import parse_lspci_nnk
from msscan.parsers.sensors import parse_sensors_json
from tests.conftest import R640, ler_saida


class TestDmidecode:
    def test_s_real(self):
        assert parse_dmidecode_s(ler_saida(R640, "dmidecode__-s__system-manufacturer")) == "Dell Inc."
        assert parse_dmidecode_s(ler_saida(R640, "dmidecode__-s__system-product-name")) == "PowerEdge R640"
        assert parse_dmidecode_s(ler_saida(R640, "dmidecode__-s__bios-version")) == "2.21.2"

    @pytest.mark.parametrize("generico", ["To Be Filled By O.E.M.", "Not Specified", "Default string", "  "])
    def test_valores_genericos_viram_none(self, generico):
        assert valor_util(generico) is None
        assert parse_dmidecode_s(generico + "\n") is None

    def test_s_vazio_e_comentarios(self):
        assert parse_dmidecode_s("") is None
        assert parse_dmidecode_s("# comentario\n") is None

    def test_tipo38_real(self):
        info = parse_ipmi_tipo38(ler_saida(R640, "dmidecode__-t__38"))
        assert info == {
            "interface": "KCS (Keyboard Control Style)",
            "versao_especificacao": "2.0",
            "endereco_base": "0x0000000000000CA8 (I/O)",
            "endereco_i2c": "0x10",
        }

    def test_tipo38_ausente(self):
        # Saída real do tipo 42 (sem estruturas) serve como "tipo não anunciado".
        assert parse_ipmi_tipo38(ler_saida(R640, "dmidecode__-t__42")) is None

    def test_tipo42_ausente_real(self):
        assert parse_host_interface_tipo42(ler_saida(R640, "dmidecode__-t__42")) == []

    def test_separar_estruturas_real(self):
        estruturas = separar_estruturas(ler_saida(R640, "dmidecode__-t__38"))
        assert len(estruturas) == 1
        assert estruturas[0]["tipo"] == 38
        assert estruturas[0]["handle"] == "0x2600"
        assert estruturas[0]["titulo"] == "IPMI Device Information"

    # TODO(fixture): teste positivo do tipo 42 precisa de saída real de
    # "dmidecode -t 42" em servidor com Redfish Host Interface habilitada.


class TestLspci:
    def test_real_r640(self):
        dispositivos = parse_lspci_nnk(ler_saida(R640, "lspci__-nnk"))
        assert len(dispositivos) > 20
        raid = [d for d in dispositivos if d["classe"] == "0104"]
        assert [d["slot"] for d in raid] == ["00:11.5", "00:17.0"]
        sata = raid[1]
        assert sata["descricao"] == "Intel Corporation C620 Series Chipset Family SATA Controller [RAID mode]"
        assert (sata["fabricante_id"], sata["dispositivo_id"]) == ("8086", "a186")
        assert sata["revisao"] == "09"
        assert sata["subsistema"] == "Dell Device"
        assert sata["subsistema_ids"] == "1028:0716"
        assert sata["driver"] == "ahci"
        assert sata["modulos"] == ["ahci"]

    def test_todo_dispositivo_tem_slot_e_classe(self):
        for d in parse_lspci_nnk(ler_saida(R640, "lspci__-nnk")):
            assert d["slot"] and len(d["classe"]) == 4

    def test_vazio(self):
        assert parse_lspci_nnk("") == []

    def test_lixo_levanta_erro(self):
        with pytest.raises(ErroParser):
            parse_lspci_nnk("pcilib: Cannot open /proc/bus/pci\n")


class TestIpmitool:
    def test_mc_info_real(self):
        info = parse_mc_info(ler_saida(R640, "ipmitool__mc__info"))
        assert info["versao_ipmi"] == "2.0"
        assert info["firmware"] == "7.00"
        assert info["fabricante"] == "Dell Inc."
        assert info["fabricante_id"] == "674"
        assert "SEL Device" in info["campos"]["Additional Device Support"]
        assert info["campos"]["Aux Firmware Rev Info"] == ["0x00", "0x02", "0x00", "0xad"]

    def test_mc_info_invalido(self):
        with pytest.raises(ErroParser):
            parse_mc_info("Could not open device at /dev/ipmi0: No such file or directory\n")


class TestSensors:
    def test_real_r640(self):
        chips = parse_sensors_json(ler_saida(R640, "sensors__-j"))
        assert [c["chip"] for c in chips] == [
            "coretemp-isa-0000",
            "pch_lewisburg-virtual-0",
            "coretemp-isa-0001",
            "power_meter-acpi-0",
        ]
        pacote = chips[0]["leituras"]["Package id 0"]
        assert pacote["temp1_crit"] == 102.0
        assert "Adapter" not in chips[0]["leituras"]

    def test_valores_nao_numericos_sao_descartados(self):
        # Estrutura real do R640 com um valor adulterado para não numérico.
        texto = re.sub(
            r'("pch_lewisburg-virtual-0":\{"Adapter":"Virtual device","temp1":\{"temp1_input":)[0-9.]+',
            r'\1"na"',
            ler_saida(R640, "sensors__-j"),
        )
        chips = {c["chip"]: c for c in parse_sensors_json(texto)}
        assert chips["pch_lewisburg-virtual-0"]["leituras"] == {}

    def test_vazio(self):
        assert parse_sensors_json("") == []

    def test_invalido(self):
        with pytest.raises(ErroParser):
            parse_sensors_json("No sensors found!\n")

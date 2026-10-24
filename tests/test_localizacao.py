"""Localização física do problema (CLAUDE.md 15.3): nomes reais do R640 → área física; sem regra → não localiza."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from msscan.localizacao import NAO_LOCALIZADO, local_de, localizar_relatorio, svg_mapa
from msscan.parsers.redfish import local_redfish, parse_adaptador_rede
from msscan.provedores.ipmi import avaliar_internos
from tests.test_ia import relatorio_r640


@pytest.fixture(scope="module")
def relatorio():
    return relatorio_r640()


@pytest.mark.parametrize(("texto", "entidade", "esperado"), [
    ("Status", "10.1", ("psu1", "Fonte 1 (PSU1)")),
    ("PS2 Status", None, ("psu2", "Fonte 2 (PSU2)")),
    ("Memory ECC Uncorr Err Uncorrectable ECC (UnCorrectable ECC |  DIMMA2)", None, ("dimm_cpu1", "DIMM A2 (CPU1)")),
    ("DIMM.Socket.B7", None, ("dimm_cpu2", "DIMM B7 (CPU2)")),
    ("Cable / Interconnect Cable SAS A0 Config Error", None, ("cabos_sas", "Cabo SAS A0 (controladora → backplane)")),
    ("Critical Interrupt PCIE Fatal Err Bus Fatal Error ( Slot 1)", None, ("risers", "Slot PCIe 1 (o riser exato depende da configuração)")),
    ("Fan3B", None, ("ventoinhas", "Ventoinha 3B (Fan3B)")),
    ("Physical Security Intrusion", None, ("chassi", "Tampa do chassi (sensor de intrusão)")),
    ("NIC Daughter Card", None, (None, "Placa de rede")),
])  # fmt: skip
def test_regras_com_nomes_reais(texto, entidade, esperado):
    assert local_de(texto, entidade, dell=True) == esperado


def test_nunca_adivinha():
    assert local_de("Processor Status Presence detected") is None
    assert local_de("System Firmwares POST Err") is None
    assert local_de("DIMM A2", dell=False) == (None, "DIMM A2")  # outro fabricante: a CPU do banco não é afirmada


def test_r640_real(relatorio):
    loc = relatorio["localizacao"]
    assert loc["mapa"] and loc["modelo"] == "Dell PowerEdge R640"
    por_local = {(i["componente"], i["local"]): i for i in loc["itens"]}
    assert por_local[("fontes", "Fonte 1 (PSU1)")]["area"] == "psu1"
    assert por_local[("memoria", "DIMM A2 (CPU1)")]["area"] == "dimm_cpu1"
    assert por_local[("internos", "Cabo SAS A0 (controladora → backplane)")]["ocorrencias"] == 5
    nao = [i for i in loc["itens"] if not i["localizado"]]
    assert nao and all(i["local"] == NAO_LOCALIZADO and i["area"] is None for i in nao)
    assert all(i["componente"] != "sensores" for i in loc["itens"])  # só componentes com ALERTA/FALHA


def test_modelo_sem_mapa_so_texto(relatorio):
    sem_mapa = copy.deepcopy(relatorio)
    sem_mapa["equipamento"]["compatibilidade"] = {"encontrado": False, "entrada": None}
    loc = localizar_relatorio(sem_mapa)
    assert loc["mapa"] is None and all(i["area"] is None for i in loc["itens"])
    assert any(i["local"] == "Fonte 1 (PSU1)" for i in loc["itens"]) and svg_mapa(None, set()) == ""


def test_svg_destaca_com_texto_e_escapa(relatorio):
    mapa = copy.deepcopy(relatorio["localizacao"]["mapa"])
    mapa["areas"][0]["nome"] = '<script>x</script> (a)'
    svg = svg_mapa(mapa, {"psu1"}, "<b>")
    assert "PSU1 — PROBLEMA" in svg and "<script>" not in svg and "&lt;b&gt;" in svg


def test_componentes_internos(relatorio):
    internos = {m["componente"]: m for m in relatorio["medido"]}["internos"]
    assert internos["status"] == "ALERTA" and "histórico" in internos["mensagem"]
    nomes = [p["nome"] for p in internos["dados"]["pecas"]]
    assert "Cabo SAS A0 (controladora → backplane)" in nomes and "Slot PCIe 1 (o riser exato depende da configuração)" in nomes
    assert avaliar_internos([]).status.value == "OK"


def test_local_do_redfish_mockup_dmtf():
    dados = json.loads((Path(__file__).parent / "fixtures/redfish/dmtf/public_tower/redfish.json").read_text())
    nic = dados["GET /redfish/v1/Chassis/Tower/NetworkAdapters/NIC1"]
    assert parse_adaptador_rede(nic.get("corpo", nic))["local"] == "NIC Daughter Card"
    assert local_redfish({"Location": {"PartLocation": {"LocationType": "Slot", "LocationOrdinalValue": 3}}}) == "Slot 3"
    assert local_redfish({}) is None


def test_relatorio_html_e_txt_mostram_o_local(relatorio):
    from msscan.relatorio.html import gerar_html
    from msscan.relatorio.terminal import formatar_localizacao

    html = gerar_html(relatorio)
    assert "<h2>Onde está o problema</h2>" in html and "<svg" in html and "DIMM A2 (CPU1)" in html
    texto = formatar_localizacao(relatorio["localizacao"])
    assert "Onde está o problema (local físico)" in texto and "Fonte 1 (PSU1)" in texto

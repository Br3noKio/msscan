"""Banco de compatibilidade (compat_db/): leitura, match exato, divergências; banco é só dica."""

from __future__ import annotations

import json

import pytest

from msscan.coleta.anonimizar import REMOVIDO, anonimizar_pasta
from msscan.compat.banco import ErroBanco, carregar_banco, consultar, detectar_compatibilidade, divergencias
from msscan.core.executor import ExecutorReplay
from msscan.deteccao.contexto import Contexto, Controladora
from msscan.deteccao.detectores import detectar
from msscan.deteccao.resumo import capacidades, formatar_resumo
from msscan.modulos.registro import registro_padrao
from msscan.relatorio.html import gerar_html
from tests.conftest import R640, ExecutorFalso


@pytest.fixture(scope="module")
def banco():
    return carregar_banco()


class TestBanco:
    def test_entradas_iniciais(self, banco):
        r640, arquivo = consultar(banco, "Dell Inc.", "PowerEdge R640")
        assert arquivo == "dell.yaml" and r640["bmc"] == "iDRAC9" and r640["redfish"] is True
        r410, _ = consultar(banco, "Dell Inc.", "PowerEdge R410")
        assert r410["bmc"] == "iDRAC6" and r410["redfish"] is False
        assert "perccli64" in r640["ferramentas"] and "MegaCli64" in r410["ferramentas"]

    @pytest.mark.parametrize(("fabricante", "modelo"), [
        ("Dell Inc.", "PowerEdge R6400"), ("Dell Inc.", "PowerEdge R640 XL"), ("HPE", "PowerEdge R640"),
        (None, "PowerEdge R640"), ("Dell Inc.", None),
    ])  # fmt: skip
    def test_match_exato(self, banco, fabricante, modelo):
        assert consultar(banco, fabricante, modelo) == (None, "")

    def test_banco_invalido(self, tmp_path):
        (tmp_path / "x.yaml").write_text("fabricante: X\nmatch_fabricante: X\nmodelos:\n  - match: '['\n")
        with pytest.raises(ErroBanco):
            carregar_banco(tmp_path)
        ctx = Contexto(executor=ExecutorFalso(), fabricante="X", modelo="Y")
        detectar_compatibilidade(ctx, tmp_path)
        assert not ctx.compat["encontrado"] and "x.yaml" in ctx.compat["erro"]


class TestDivergencias:
    def test_r640_sem_redfish_confirmado(self, banco):
        ctx = detectar(ExecutorReplay(R640))
        assert ctx.compat["encontrado"] and ctx.compat["entrada"]["nome"] == "Dell PowerEdge R640"
        assert any("indica Redfish (iDRAC9)" in d for d in ctx.compat["divergencias"])
        assert "Banco de compatibilidade: Dell PowerEdge R640 — iDRAC9" in formatar_resumo(ctx)
        item = {c["chave"]: c for c in capacidades(ctx)}["compatibilidade"]
        assert item["disponivel"] and "1 divergência" in item["detalhe"]

    def test_r410_com_redfish_e_controladora_desconhecida(self, banco):
        entrada, _ = consultar(banco, "Dell Inc.", "PowerEdge R410")
        ctx = Contexto(executor=ExecutorFalso())
        ctx.redfish.confirmado = True
        ctx.ipmi.disponivel = True
        ctx.controladoras.append(Controladora("03:00.0", "0104", "Controladora XYZ", "", "megaraid_sas", "raid_hardware", "1000", "0079"))
        lista = divergencias(entrada, ctx)
        assert any("banco diz que o iDRAC6 não tem Redfish" in d for d in lista)
        assert any("Controladora XYZ" in d for d in lista)

    def test_banco_nao_muda_status(self):
        ctx = detectar(ExecutorReplay(R640))
        com = [r.status for r in registro_padrao().diagnosticar(ctx)]
        ctx.compat = None
        sem = [r.status for r in registro_padrao().diagnosticar(detectar(ExecutorReplay(R640)))]
        assert com == sem

    def test_relatorio_mostra_divergencias(self):
        ctx = detectar(ExecutorReplay(R640))
        html = gerar_html({"equipamento": ctx.para_dict(), "medido": []})
        assert "Banco de compatibilidade (dica; a detecção prevalece)" in html and "Divergência com a detecção" in html


def test_anonimiza_redfish_json(tmp_path):
    gravado = {
        "GET /redfish/v1/Systems/System.Embedded.1": {"status": 200, "corpo": {
            "SerialNumber": "CN7475189J0123", "SKU": "ABC1234", "HostName": "srv-cliente",
            "Oem": {"Dell": {"DellSystem": {"NodeID": "ABC1234", "ChassisServiceTag": "ABC1234"}}},
            "UUID": "4c4c4544-0042-4310-8032-b3c04f313233", "Model": "PowerEdge R640"}},
        "GET /redfish/v1/Managers/iDRAC.Embedded.1/EthernetInterfaces/NIC.1": {"status": 200, "corpo": {
            "MACAddress": "02:00:5e:10:00:05", "IPv4Addresses": [{"Address": "10.1.2.3", "Gateway": "10.1.2.1"}]}},
    }  # fmt: skip
    (tmp_path / "redfish.json").write_text(json.dumps(gravado))
    anonimizar_pasta(tmp_path)
    texto = (tmp_path / "redfish.json").read_text()
    for segredo in ("CN7475189J0123", "ABC1234", "srv-cliente", "4c4c4544", "02:00:5e:10:00:05", "10.1.2.3"):
        assert segredo not in texto
    dados = json.loads(texto)
    sistema = dados["GET /redfish/v1/Systems/System.Embedded.1"]["corpo"]
    assert sistema["SerialNumber"] == REMOVIDO and sistema["Model"] == "PowerEdge R640"
    rede = dados["GET /redfish/v1/Managers/iDRAC.Embedded.1/EthernetInterfaces/NIC.1"]["corpo"]
    assert rede["IPv4Addresses"][0]["Address"].startswith("192.0.2.")

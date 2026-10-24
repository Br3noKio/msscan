"""Fase 8: atualizações de firmware SOMENTE INFORMATIVAS (inventário, catálogo Dell, versões, pré-checagens).

Catálogo: recorte real de ``https://downloads.dell.com/catalog/Catalog.xml.gz`` (v26.09.17).
Inventário: fixture real do R640 (SMBIOS, IPMI, lspci, ethtool) e FirmwareInventory do mockup DMTF.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import pytest

from msscan.atualizacao.catalogo import URL_CATALOGO_DELL, VALIDADE_CACHE_S, caminho_cache, obter_catalogo
from msscan.atualizacao.inventario import coletar_inventario, inventario_redfish
from msscan.atualizacao.verificacao import (
    MENSAGEM_FONTE_INCERTA,
    NAO_SUPORTADO,
    cruzar,
    disponibilidade_atualizacoes,
    executar_verificacao,
    modelo_no_catalogo,
    prechecagens,
    verificar_atualizacoes,
)
from msscan.atualizacao.versoes import ATUALIZACAO, IGUAL, INCERTA, INSTALADA_MAIS_NOVA, comparar_versoes
from msscan.core.executor import ExecutorReplay, RespostaDownload, validar_download
from msscan.deteccao.detectores import detectar
from msscan.modulos.registro import registro_padrao
from msscan.parsers import ErroParser
from msscan.parsers.catalogo_dell import parse_catalogo_dell
from msscan.parsers.dmidecode import parse_sku_dell
from msscan.relatorio.atualizacoes import formatar_atualizacoes, html_atualizacoes
from msscan.relatorio.completo import montar_relatorio
from tests.conftest import PASTA_FIXTURES, R640, ExecutorFalso, com_textos

RECORTE = PASTA_FIXTURES / "dell" / "catalogo" / "Catalog_recorte_r640.xml.gz"


@pytest.fixture(scope="module")
def catalogo():
    return parse_catalogo_dell(RECORTE.read_bytes())


@pytest.fixture(scope="module")
def r640():
    executor = ExecutorReplay(R640)
    ctx = detectar(executor)
    registro = registro_padrao()
    resultados = registro.diagnosticar(ctx)
    relatorio = montar_relatorio(ctx, resultados, None, executor, datetime(2026, 9, 25), datetime(2026, 9, 25), registro.titulo)
    return ctx, relatorio


def contexto(ctx, **mudancas):
    return {"fabricante": ctx.fabricante, "modelo": ctx.modelo, "sku": ctx.sku, "compat": ctx.compat, **mudancas}


# --- Versões ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("instalada", "disponivel", "truncada", "esperado"), [
    ("2.21.2", "2.28.1", False, ATUALIZACAO),
    ("2.28.1", "2.28.1", False, IGUAL),
    ("2.30.0", "2.28.1", False, INSTALADA_MAIS_NOVA),
    ("2.9.0", "2.10.0", False, ATUALIZACAO),        # numérico, não alfabético
    ("25.5.8.0001", "25.5.9.0001", False, ATUALIZACAO),
    ("52.30.0-6753", "52.38.0-6897", False, ATUALIZACAO),
    ("2.21", "2.21.0", False, IGUAL),
    ("7.00", "7.00.00.185", True, INCERTA),         # iDRAC pelo IPMI: só o começo da versão
    ("4.40", "7.00.00.185", True, ATUALIZACAO),
    ("7.10", "7.00.00.185", True, INSTALADA_MAIS_NOVA),
    ("1.67, 0x80000fa8, 19.0.12", "25.0.4", False, INCERTA),
    ("A07", "A09", False, INCERTA),
    (None, "2.28.1", False, INCERTA),
])  # fmt: skip
def test_comparar_versoes(instalada, disponivel, truncada, esperado):
    assert comparar_versoes(instalada, disponivel, truncada)[0] == esperado


# --- Catálogo --------------------------------------------------------------------------------


class TestCatalogo:
    def test_recorte_real(self, catalogo):
        assert catalogo["versao"] == "26.09.17" and catalogo["base"] == "downloads.dell.com" and catalogo["protocolos"] == "HTTPS"
        assert len(catalogo["componentes"]) == 14
        bios = next(c for c in catalogo["componentes"] if c["tipo"] == "BIOS" and any(s["system_id"] == "0716" for s in c["sistemas"]))
        assert (bios["versao"], bios["algoritmo_hash"], bios["reinicio"]) == ("2.28.1", "SHA256", True)
        assert bios["caminho"] == "FOLDER14889959M/1/BIOS_188CW_WN64_2.28.1.EXE" and len(bios["hash"]) == 64
        assert bios["dispositivos"][0]["component_id"] == "159"
        assert {"system_id": "0716", "modelo": "R640", "marca": "PowerEdge"} in bios["sistemas"]

    def test_catalogo_invalido(self):
        with pytest.raises(ErroParser):
            parse_catalogo_dell(b"\x1f\x8bnao e gzip")
        with pytest.raises(ErroParser):
            parse_catalogo_dell(b"<Outro/>")

    @pytest.mark.parametrize(("sku", "esperado"), [
        ("SKU=0716;ModelName=PowerEdge R640", {"system_id": "0716", "modelo": "PowerEdge R640"}),
        ("SKU=07c9;ModelName=PowerEdge R440", {"system_id": "07C9", "modelo": "PowerEdge R440"}),
        ("SKU=NotProvided;ModelName=PowerEdge R640", None), ("Default string", None), (None, None),
    ])  # fmt: skip
    def test_sku_dell(self, sku, esperado):
        assert parse_sku_dell(sku) == esperado

    def test_modelo_exato(self, catalogo):
        assert modelo_no_catalogo(catalogo, "SKU=0716;ModelName=PowerEdge R640", "PowerEdge R640") == ("0716", "")
        _, motivo = modelo_no_catalogo(catalogo, "SKU=0716;ModelName=PowerEdge R640", "PowerEdge R640 XL")
        assert "não bate exatamente" in motivo
        assert "systemID não informado" in modelo_no_catalogo(catalogo, None, "PowerEdge R640")[1]
        assert "não aparece no catálogo" in modelo_no_catalogo(catalogo, "SKU=0999;ModelName=X", "X")[1]


class TestDownloadECache:
    @pytest.mark.parametrize("url", ["http://downloads.dell.com/catalog/Catalog.xml.gz", "https://evil.example/Catalog.xml.gz",
                                     "https://downloads.dell.com.evil.example/x", "ftp://downloads.dell.com/x"])  # fmt: skip
    def test_so_https_e_dominio_oficial(self, url, tmp_path):
        executor = ExecutorFalso()
        resposta = executor.baixar(url, str(tmp_path / "x"), 1000)
        assert resposta.erro_tipo == "bloqueado" and validar_download(url) and not (tmp_path / "x").exists()

    def test_url_oficial_valida(self):
        assert validar_download(URL_CATALOGO_DELL) == ""

    def test_cache_novo_nao_baixa(self, tmp_path):
        cache = caminho_cache(str(tmp_path))
        cache.parent.mkdir(parents=True)
        cache.write_bytes(RECORTE.read_bytes())
        executor = ExecutorFalso()
        resultado = obter_catalogo(executor, str(tmp_path))
        assert resultado.dados["versao"] == "26.09.17" and "cache" in resultado.origem and executor.historico == []

    def test_cache_vencido_baixa(self, tmp_path):
        class Baixador(ExecutorFalso):
            def _baixar(self, url, destino, limite, timeout):
                Path(destino).parent.mkdir(parents=True, exist_ok=True)
                Path(destino).write_bytes(RECORTE.read_bytes())
                return RespostaDownload(url, 200, destino, RECORTE.stat().st_size, "x")

        cache = caminho_cache(str(tmp_path))
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"velho")
        os.utime(cache, (time.time() - VALIDADE_CACHE_S - 10,) * 2)
        executor = Baixador()
        resultado = obter_catalogo(executor, str(tmp_path))
        assert resultado.dados is not None and resultado.origem.startswith("baixado agora")
        assert executor.historico[-1].comando[:2] == ["GET", URL_CATALOGO_DELL]

    def test_sem_internet_usa_cache_vencido_com_aviso(self, tmp_path):
        cache = caminho_cache(str(tmp_path))
        cache.parent.mkdir(parents=True)
        cache.write_bytes(RECORTE.read_bytes())
        os.utime(cache, (time.time() - VALIDADE_CACHE_S - 86400 * 3,) * 2)
        resultado = obter_catalogo(ExecutorFalso(), str(tmp_path))
        assert resultado.dados is not None and "cache vencido" in resultado.aviso

    def test_sem_internet_e_sem_cache(self, tmp_path):
        resultado = obter_catalogo(ExecutorFalso(), str(tmp_path))
        assert resultado.dados is None and "não foi possível baixar" in resultado.aviso

    def test_replay_de_download(self, tmp_path):
        pasta = tmp_path / "fixture"
        (pasta / "downloads").mkdir(parents=True)
        (pasta / "downloads" / "Catalog.xml.gz").write_bytes(RECORTE.read_bytes())
        (pasta / "downloads.json").write_text(json.dumps({URL_CATALOGO_DELL: "downloads/Catalog.xml.gz"}))
        resposta = ExecutorReplay(pasta).baixar(URL_CATALOGO_DELL, str(tmp_path / "c.gz"), 10**8)
        assert resposta.ok and (tmp_path / "c.gz").read_bytes() == RECORTE.read_bytes() and len(resposta.sha256) == 64


# --- Inventário e cruzamento ---------------------------------------------------------------------


class TestInventario:
    def test_r640_pelo_sistema(self, r640):
        ctx, _ = r640
        itens, fonte = coletar_inventario(ctx)
        assert fonte.startswith("sistema")
        nomes = {i["nome"]: i for i in itens}
        assert nomes["BIOS"]["versao"] == "2.21.2" and nomes["iDRAC (BMC)"]["truncada"]
        placas = [i for i in itens if i["pci"]]
        # Funções do mesmo chip viram uma placa (19.3): a 2ª função da X710 (subsistema 1028:0000) junta-se à 1ª.
        assert {p["pci"]["subDeviceID"] for p in placas} == {"1f99", "1f9a"}
        assert any("X710" in p["nome"] and p["interfaces"] == ["eno1np0", "eno2np1"] for p in placas)
        assert any(p["pci"]["deviceID"] == "1521" and p["interfaces"] == ["eno3", "eno4"] for p in placas)

    def test_redfish_dmtf(self):
        from msscan.redfish.acesso import AcessoBMC
        from msscan.redfish.cliente import ClienteRedfish

        executor = ExecutorReplay(PASTA_FIXTURES / "redfish" / "dmtf" / "public_rackmount1")
        cliente = ClienteRedfish(executor, "https://bmc")
        raiz = cliente.raiz()
        from msscan.deteccao.contexto import Contexto

        ctx = Contexto(executor=executor)
        ctx.bmc = AcessoBMC(cliente, raiz["contas"])
        ctx.bmc.tentar("x", "y")
        itens = inventario_redfish(ctx)
        assert {i["nome"]: i["versao"] for i in itens}["Contoso BIOS Firmware"] == "P79 v1.45"
        assert all(i["fonte"] == "Redfish (FirmwareInventory)" for i in itens)
        assert inventario_redfish(Contexto(executor=ExecutorFalso())) == []

    def test_cruzamento_r640(self, r640, catalogo):
        ctx, _ = r640
        itens, _ = coletar_inventario(ctx)
        por_nome = {i["componente"]: i for i in cruzar(itens, catalogo, "0716")}
        bios = por_nome["BIOS"]
        assert (bios["situacao"], bios["disponivel"]) == (ATUALIZACAO, "2.28.1")
        assert bios["pacote"]["url"] == "https://downloads.dell.com/FOLDER14889959M/1/BIOS_188CW_WN64_2.28.1.EXE"
        assert len(bios["pacote"]["sha256"]) == 64
        assert por_nome["iDRAC (BMC)"]["situacao"] == INCERTA and por_nome["iDRAC (BMC)"]["disponivel"] == "7.00.00.185"
        # Catálogo com todos os <PCIInfo> (log do T1, 02/10/2026): o componente 102303 (rNDC X710/I350) lista 1521:1F9A e
        # 1572:1F99 — as 4 portas recebem o mesmo pacote, instalado uma vez.
        rndc = [v for k, v in por_nome.items() if "rNDC" in k]
        assert len(rndc) == 1 and len(por_nome) == 3
        rndc = rndc[0]
        assert rndc["componente"] == "Intel(R) Ethernet 10G 4P X710/I350 rNDC (eno1np0, eno2np1, eno3, eno4)"
        assert (rndc["situacao"], rndc["component_id"], rndc["instalada"], rndc["disponivel"]) == ("atualizacao", "102303", "19.0.12", "25.0.4")
        assert rndc["pacote"]["url"].endswith("Network_Firmware_PJDHV_WN64_25.0.4_A00.EXE")
        assert rndc["texto_situacao"].startswith("Atualização disponível (25.0.4, ")
        # Guia de escrita: data do catálogo ("January 24, 2026") exibida no padrão brasileiro.
        assert re.fullmatch(r"Atualização disponível \(25\.0\.4, \d{2}/\d{2}/\d{4}\)", rndc["texto_situacao"])

    def test_varios_pciinfo_no_catalogo(self, catalogo):
        dispositivo = next(d for c in catalogo["componentes"] for d in c["dispositivos"] if d["component_id"] == "102303")
        assert {(p["deviceID"], p["subDeviceID"]) for p in dispositivo["pcis"]} == {("1521", "1f9a"), ("1572", "1f99")}

    def test_nunca_downgrade_e_component_id(self, catalogo):
        itens = [
            {"nome": "BIOS", "versao": "2.30.0", "fonte": "x", "truncada": False, "component_id": None, "tipo": "BIOS", "categoria": None, "pci": None},
            {"nome": "BIOS (Redfish)", "versao": "2.19.1", "fonte": "Redfish", "truncada": False, "component_id": "159", "tipo": None, "categoria": None, "pci": None},
        ]  # fmt: skip
        mais_nova, redfish = cruzar(itens, catalogo, "0716")
        assert mais_nova["situacao"] == INSTALADA_MAIS_NOVA
        assert (redfish["situacao"], redfish["disponivel"]) == (ATUALIZACAO, "2.28.1")

    def test_filtra_pelo_modelo(self, catalogo):
        item = {"nome": "BIOS", "versao": "1.0.0", "fonte": "x", "truncada": False, "component_id": None, "tipo": "BIOS", "categoria": None, "pci": None}
        assert cruzar([item], catalogo, "0890")[0]["disponivel"] == "2.24.1"  # T340: o BIOS de outros modelos


# --- Pré-checagens e seção ---------------------------------------------------------------------


def relatorio_com(relatorio, **componentes):
    novo = copy.deepcopy(relatorio)
    for item in novo["medido"]:
        item.update(componentes.get(item["componente"], {}))
    return novo


class TestPrechecagens:
    def test_r640_so_avisos(self, r640):
        _, relatorio = r640
        checagens = prechecagens(relatorio, None)
        assert checagens["bloqueios"] == []
        assert any("Redundância de energia perdida" in a and "confirmação extra" in a for a in checagens["avisos"])

    def test_fonte_incerta_bloqueia_com_instrucoes(self, r640):
        _, relatorio = r640
        dados = {"classificacao_incerta": True, "defeito": False, "redundancia_perdida": False}
        checagens = prechecagens(relatorio_com(relatorio, fontes={"dados": dados}), None)
        # 09/10/2026: bloqueio de hardware — o Plano de atualização relê na hora e o técnico pode assumir o risco.
        assert checagens["bloqueios_hardware"] == [MENSAGEM_FONTE_INCERTA] and checagens["bloqueios"] == []
        assert "LEDs" in MENSAGEM_FONTE_INCERTA and "cabos" in MENSAGEM_FONTE_INCERTA and "IPMI" in MENSAGEM_FONTE_INCERTA
        assert "refaça o diagnóstico" in MENSAGEM_FONTE_INCERTA

    def test_defeito_raid_sensor_e_banco(self, r640):
        _, relatorio = r640
        mudado = relatorio_com(relatorio, fontes={"dados": {"defeito": True}}, raid={"status": "FALHA", "mensagem": "VD0 degradado"},
                               sensores={"status": "FALHA", "mensagem": "Temp 95 °C"})  # fmt: skip
        checagens = prechecagens(mudado, {"entrada": {"atualizacao": {"bloqueios": ["BIOS 1.x exige iDRAC antes"]}}})
        hardware = " | ".join(checagens["bloqueios_hardware"])
        assert "DEFEITO" in hardware and "VD0 degradado" in hardware and "Temp 95" in hardware
        assert checagens["bloqueios"] == ["Banco de compatibilidade: BIOS 1.x exige iDRAC antes"]  # fixo: nada libera

    def test_raid_desconhecido_bloqueia_so_raid(self, r640, catalogo):
        ctx, relatorio = r640
        mudado = relatorio_com(relatorio, raid={"status": "NAO_VERIFICADO"})
        inventario = [{"nome": "PERC H730P Mini", "versao": "25.5.8.0001", "fonte": "Redfish", "truncada": False,
                       "component_id": "101552", "tipo": None, "categoria": None, "pci": None}]  # fmt: skip
        secao = verificar_atualizacoes(mudado, contexto(ctx), inventario, "Redfish", catalogo, "recorte")
        assert secao["itens"][0]["situacao"] == ATUALIZACAO and "RAID desconhecido" in secao["itens"][0]["bloqueios"][0]

    def test_secao_r640(self, r640, catalogo):
        ctx, relatorio = r640
        itens, fonte = coletar_inventario(ctx)
        secao = verificar_atualizacoes(relatorio, contexto(ctx), itens, fonte, catalogo, "recorte")
        assert secao["situacao"] == "verificada" and secao["system_id"] == "0716" and secao["somente_informativo"]
        assert "2 com atualização disponível" in secao["mensagem"] and "nada foi aplicado" in secao["mensagem"]
        assert "1 em que não foi possível determinar" in secao["mensagem"]
        texto = formatar_atualizacoes(secao)
        assert "BIOS" in texto and "2.28.1" in texto and "Somente informativo" in texto
        assert "Atualizações" in html_atualizacoes(secao) and "2.28.1" in html_atualizacoes(secao)

    def test_modelo_nao_bate(self, r640, catalogo):
        ctx, relatorio = r640
        secao = verificar_atualizacoes(relatorio, contexto(ctx, modelo="PowerEdge R640 XL"), [], "x", catalogo, "recorte")
        assert secao["situacao"] == "modelo_nao_confirmado" and secao["itens"] == []
        assert secao["bloqueios"][0].startswith("Modelo não confirmado")

    def test_modelo_fora_do_catalogo_fora_de_suporte(self, r640, catalogo):
        ctx, relatorio = r640
        dados = {**contexto(ctx, modelo="PowerEdge R410"), "sku": "SKU=0FFF;ModelName=PowerEdge R410"}  # systemID fora do catálogo
        secao = verificar_atualizacoes(relatorio, dados, [], "x", catalogo, "recorte")
        assert secao["situacao"] == "fora_de_suporte" and secao["itens"] == []
        assert secao["mensagem"].startswith("Fora de suporte do fabricante: o PowerEdge R410 (systemID 0FFF)")
        from msscan.atualizacao.aplicacao import montar_plano

        assert not montar_plano(secao, ["item1"])["pode_seguir"]

    def test_outro_fabricante(self, r640):
        ctx, relatorio = r640
        secao = verificar_atualizacoes(relatorio, contexto(ctx, fabricante="HPE"), [], "x", None, "")
        assert secao["situacao"] == "nao_suportada" and NAO_SUPORTADO in secao["mensagem"]

    def test_disponibilidade(self):
        assert disponibilidade_atualizacoes(True, False) == ""
        assert "semrede=1" in disponibilidade_atualizacoes(False, True)
        assert "sem acesso à internet" in disponibilidade_atualizacoes(False, False)
        assert "modo de demonstração" in disponibilidade_atualizacoes(True, False, replay=True)


# --- Fluxo (modo texto e interface) -----------------------------------------------------------


class ExecutorR640ComCatalogo(ExecutorReplay):
    """Replay do R640 com o download do catálogo servido pelo recorte real."""

    def _baixar(self, url, destino, limite, timeout):
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        Path(destino).write_bytes(RECORTE.read_bytes())
        return RespostaDownload(url, 200, destino, RECORTE.stat().st_size, "x")


class TestFluxo:
    def preparar(self, tmp_path):
        from msscan.pendrive.local import local_manual
        from msscan.rede.verificacao import ResultadoRede
        from msscan.relatorio.completo import gravar_relatorios

        executor = ExecutorR640ComCatalogo(R640)
        ctx = detectar(executor)
        registro = registro_padrao()
        relatorio = montar_relatorio(ctx, registro.diagnosticar(ctx), None, executor, datetime.now(), datetime.now(), registro.titulo)
        gravacao = gravar_relatorios(relatorio, "texto", str(tmp_path), replay=False)
        return executor, ctx, relatorio, gravacao, local_manual(str(tmp_path)), ResultadoRede(True)

    def test_padrao_nao(self, tmp_path):
        from main import etapa_atualizacoes

        executor, ctx, relatorio, gravacao, local, rede = self.preparar(tmp_path)
        etapa_atualizacoes(argparse.Namespace(replay=None), executor, ctx, relatorio, "t", local, rede, gravacao, entrada=lambda _: "")
        assert relatorio["atualizacoes"] == {"situacao": "nao_solicitadas"}
        assert not any(c.comando[0] == "GET" for c in executor.historico)

    def test_sim_verifica_e_regrava(self, tmp_path, capsys):
        from main import etapa_atualizacoes

        executor, ctx, relatorio, gravacao, local, rede = self.preparar(tmp_path)
        etapa_atualizacoes(argparse.Namespace(replay=None), executor, ctx, relatorio, "t", local, rede, gravacao, entrada=lambda _: "s")
        assert relatorio["atualizacoes"]["situacao"] == "verificada"
        assert (tmp_path / "firmware" / "cache" / "Catalog.xml.gz").is_file()
        gravado = json.loads((gravacao.pasta / "relatorio.json").read_text())
        assert gravado["atualizacoes"]["itens"][0]["disponivel"] == "2.28.1"
        assert "Atualizações de firmware (somente informativo)" in (gravacao.pasta / "relatorio.txt").read_text()
        assert "SOMENTE INFORMATIVO" in capsys.readouterr().out

    def test_executar_verificacao(self, tmp_path):
        executor, ctx, relatorio, *_ = self.preparar(tmp_path)
        secao = executar_verificacao(ctx, relatorio, executor, str(tmp_path))
        assert secao["situacao"] == "verificada" and secao["catalogo"]["versao"] == "26.09.17"

    def test_interface(self, tmp_path):
        from msscan.interface.sessao import Sessao
        from msscan.rede.verificacao import ResultadoRede

        sessao = Sessao(ExecutorR640ComCatalogo(R640), str(tmp_path), replay=False)
        sessao.iniciar()
        sessao.aguardar(30)
        sessao.rede = ResultadoRede(True)
        assert not sessao.verificar_atualizacoes()  # antes do diagnóstico
        assert sessao.iniciar_diagnostico()
        sessao.aguardar(30)
        assert sessao.estado()["atualizacoes"]["disponivel"]
        assert sessao.verificar_atualizacoes()
        sessao.aguardar(30)
        estado = sessao.estado()["atualizacoes"]
        assert estado["situacao"] == "verificada" and estado["secao"]["itens"][0]["situacao"] == ATUALIZACAO
        gravado = json.loads((Path(sessao.gravacao["pasta"]) / "relatorio.json").read_text())
        assert gravado["atualizacoes"]["situacao"] == "verificada"


def test_catalogo_do_recorte_e_utf16_gzip():
    with gzip.open(RECORTE) as arquivo:
        assert arquivo.read(2) in (b"\xff\xfe", b"\xfe\xff")


def test_tela_sem_aplicacao_e_sem_selecao():
    html = com_textos((Path(__file__).parent.parent / "msscan/interface/estaticos/index.html").read_text())
    tela = html.split('id="tela-atualizacoes"')[1].split("</section>")[0]
    assert 'id="atu-nao" value="nao" checked' in tela and 'id="atu-aplicar" disabled' in tela
    # 18.2: ATUALIZAR e "Instalar" ficam no plano de atualização; nascem desabilitados, sem "Li os avisos" nem simulação,
    # e "Cancelar" vem antes (foco inicial dado pelo JavaScript).
    aviso = html.split('id="aviso-fundo"')[1].split('id="energia-fundo"')[0]
    assert 'id="atu-confirmacao" autocomplete="off" spellcheck="false" disabled' in aviso
    assert 'id="aviso-instalar" disabled' in aviso and 'id="aviso-li"' not in aviso and "imula" not in aviso
    assert 'id="aviso-temporarias"' in aviso and ">Plano de atualização<" in aviso
    assert "imular" not in tela and "acoes-simular" not in html and "acoes-idrac-rede" not in html
    assert aviso.index('id="aviso-cancelar"') < aviso.index('id="aviso-instalar"')
    js = com_textos((Path(__file__).parent.parent / "msscan/interface/estaticos/app.js").read_text())
    assert '$("aviso-fundo").hidden = false;\n  $("aviso-cancelar").focus();' in js


class TestPacoteMaisRecenteEPreRequisitos:
    """Pedido de 30/09/2026 (CLAUDE.md 15.2): pacote aplicável mais recente e pré-requisitos declarados pela Dell."""

    def catalogo_dependencia(self):
        from msscan.parsers.catalogo_dell import parse_catalogo_dell

        return parse_catalogo_dell((Path(__file__).parent / "fixtures/dell/catalogo/Catalog_recorte_dependencia.xml.gz").read_bytes())

    def test_parser_le_dependency_real(self):
        pacote = self.catalogo_dependencia()["componentes"][0]
        deps = [d for disp in pacote["dispositivos"] for d in disp["dependencias"]]
        assert deps and deps[0]["component_id"] == "25227" and deps[0]["versao"] == "2.40.40.40" and deps[0]["pacote"] == "2091K"
        assert "0627" in deps[0]["sistemas"] and deps[0]["nome"].startswith("iDRAC with Lifecycle Controller")

    @pytest.mark.parametrize(("instalada", "atendido"), [("2.30.30.30", False), ("2.40.40.40", True), ("2.61.60.60", True), (None, None)])
    def test_pre_requisito_conferido_contra_o_inventario(self, instalada, atendido):
        from msscan.atualizacao.verificacao import pre_requisitos

        pacote = self.catalogo_dependencia()["componentes"][0]
        inventario = [{"component_id": "25227", "versao": instalada, "truncada": False}] if instalada else []
        requisitos = pre_requisitos(pacote, "0627", inventario)
        assert requisitos and requisitos[0]["atendido"] is atendido
        assert pre_requisitos(pacote, "0716", inventario) == []  # só vale para os modelos do DepModel

    def test_empate_de_versao_escolhe_o_lancamento_mais_novo(self):
        from msscan.atualizacao.verificacao import _mais_nova

        a = {"versao": "25.0.4", "data": "January 24, 2026", "id": "a"}
        b = {"versao": "25.0.4", "data": "March 02, 2026", "id": "b"}
        c = {"versao": "22.5.7", "data": "September 22, 2027", "id": "c"}
        assert _mais_nova([a, b, c])["id"] == "b" and _mais_nova([c, a])["id"] == "a"
        assert _mais_nova([a, {"versao": "A07", "data": "", "id": "x"}]) is None  # não comparáveis → não foi possível determinar

    def test_plano_recusa_pre_requisito_nao_atendido(self):
        from msscan.atualizacao.aplicacao import montar_plano

        base = {"situacao": "atualizacao", "componente": "X", "texto_situacao": "Atualização disponível",
                "pacote": {"url": "https://dl.dell.com/a.EXE", "sha256": "0" * 64, "categoria": "Network"}}
        verificacao = {"situacao": "verificada", "itens": [
            {**base, "id": "item1", "pre_requisitos": [{"atendido": False, "texto": "exige iDRAC 2.40.40.40"}]},
            {**base, "id": "item2", "pre_requisitos": [{"atendido": None, "texto": "não foi possível confirmar"}]}]}
        plano = montar_plano(verificacao, ["item1"])
        assert not plano["pode_seguir"] and "exige iDRAC 2.40.40.40" in plano["recusados"][0]
        plano = montar_plano(verificacao, ["item2"])
        assert plano["pode_seguir"] and plano["avisos"] == ["X: não foi possível confirmar"]

    @pytest.mark.parametrize(("fab", "sub", "fw", "esperado"), [
        ("8086", "1028", "7.10 0x8000519f 19.0.12", "19.0.12"), ("8086", "1028", "1.67, 0x80000fa8, 19.0.12", "19.0.12"),
        # 16/10/2026 (R430/R620): Intel de subsistema Intel e Broadcom (FFV) passam a dar a versão da família.
        ("8086", "8086", "1.67, 0x80000dd1, 17.0.12", "17.0.12"), ("14e4", "1028", "FFV22.31.6 bc 5720-v1.39", "22.31.6"),
        ("14e4", "1028", "FFV7.10.64 bc 5720-v1.36", "7.10.64"), ("14e4", "1028", "5720-v1.39", "5720-v1.39"),
        ("15b3", "1028", "14.32.1010", "14.32.1010"),
    ])  # fmt: skip
    def test_versao_da_familia_intel(self, fab, sub, fw, esperado):
        from msscan.atualizacao.inventario import versao_placa

        assert versao_placa(fab, sub, fw)[0] == esperado

    def test_nunca_nao_necessaria_sem_confirmacao(self):
        from msscan.atualizacao.verificacao import ESTADO, NAO_NECESSARIA

        assert {s for s, e in ESTADO.items() if e == NAO_NECESSARIA} == {"igual", "instalada_mais_nova"}


def test_pacote_em_prerequisites_conta_para_o_modelo():
    """Teste de 15/10/2026 (R530 e R620): a Dell pôs o iDRAC 2.40.40.40 em ``<Prerequisites>`` e o lista como
    "Enterprise Servers R530". O R530 (063A) tem pacote; o R620 (04CE) segue fora de suporte."""
    recorte = parse_catalogo_dell((PASTA_FIXTURES / "dell/catalogo/Catalog_recorte_prerequisito.xml.gz").read_bytes())
    assert [c["pacote"] for c in recorte["componentes"]] == ["2091K"]
    idrac = [{"componente": "iDRAC", "nome": "iDRAC", "component_id": "25227", "versao": "2.30.30.30", "truncada": False,
              "fonte": "racadm"}]  # fmt: skip
    r530 = {"fabricante": "Dell Inc.", "modelo": "PowerEdge R530", "sku": "SKU=063A;ModelName=PowerEdge R530"}
    secao = verificar_atualizacoes({"medido": []}, r530, idrac, "x", recorte, "recorte")
    assert secao["situacao"] == "verificada" and secao["system_id"] == "063A"
    assert [i["estado"] for i in secao["itens"]] == ["disponivel"]
    assert secao["itens"][0]["pacote"]["url"].endswith("_2.40.40.40_A00.EXE")
    r620 = {"fabricante": "Dell Inc.", "modelo": "PowerEdge R620", "sku": "SKU=04CE;ModelName=PowerEdge R620"}
    assert verificar_atualizacoes({"medido": []}, r620, idrac, "x", recorte, "recorte")["situacao"] == "fora_de_suporte"
    outro = {**r530, "modelo": "PowerEdge R530XD"}  # o nome continua exato
    assert verificar_atualizacoes({"medido": []}, outro, idrac, "x", recorte, "recorte")["situacao"] == "modelo_nao_confirmado"


class TestCatalogoAntigo:
    """Pedido de 15/10/2026: modelo fora do catálogo atual (R530 sem BIOS no 26.10.08) usa o último catálogo da Dell com ele."""

    ATUAL = PASTA_FIXTURES / "dell/catalogo/Catalog_recorte_prerequisito.xml.gz"
    ANTIGO = PASTA_FIXTURES / "dell/catalogo/Catalog_recorte_13g_260601.xml.gz"
    URL = "https://downloads.dell.com/FOLDER14611037M/1/Catalog.xml.gz"

    def preparar(self, tmp_path, monkeypatch, sha=None):
        import hashlib

        from msscan.atualizacao import catalogo

        entrada = {"versao": "26.06.01", "data": "2026-06-01", "url": self.URL,
                   "sha256": sha or hashlib.sha256(self.ANTIGO.read_bytes()).hexdigest()}  # fmt: skip
        monkeypatch.setattr(catalogo, "carregar_legados", lambda: [entrada])
        cache = caminho_cache(str(tmp_path))
        cache.parent.mkdir(parents=True)
        cache.write_bytes(self.ATUAL.read_bytes())
        antigo = self.ANTIGO

        class Baixador(ExecutorFalso):
            def _baixar(self, url, destino, limite, timeout):
                import hashlib

                Path(destino).parent.mkdir(parents=True, exist_ok=True)
                Path(destino).write_bytes(antigo.read_bytes())
                return RespostaDownload(url, 200, destino, antigo.stat().st_size, hashlib.sha256(antigo.read_bytes()).hexdigest())

        return Baixador()

    def test_r530_usa_o_catalogo_antigo(self, tmp_path, monkeypatch):
        from msscan.atualizacao.catalogo import caminho_em_uso, catalogo_em_cache

        executor = self.preparar(tmp_path, monkeypatch)
        resultado = obter_catalogo(executor, str(tmp_path), system_id="063A")
        assert resultado.legado and resultado.dados["versao"] == "26.06.01" and "26.06.01" in resultado.origem
        assert executor.historico[-1].comando[:2] == ["GET", self.URL]
        assert caminho_em_uso(str(tmp_path)).name == "Catalog_26.06.01.xml.gz"
        assert catalogo_em_cache(str(tmp_path))["versao"] == "26.06.01"  # pacotes manuais e repositório usam o mesmo
        idrac = [{"componente": "iDRAC", "nome": "iDRAC", "component_id": "25227", "versao": "2.30.30.30", "truncada": False,
                  "fonte": "racadm"}]  # fmt: skip
        r530 = {"fabricante": "Dell Inc.", "modelo": "PowerEdge R530", "sku": "SKU=063A;ModelName=PowerEdge R530"}
        secao = verificar_atualizacoes({"medido": []}, r530, idrac, "x", resultado.dados, resultado.origem, legado=resultado.legado)
        assert secao["situacao"] == "verificada" and secao["catalogo"]["legado"]
        assert "último catálogo oficial da Dell que ainda o inclui (versão 26.06.01, de 01/06/2026)" in secao["catalogo"]["aviso"]
        assert [(i["estado"], i["disponivel"]) for i in secao["itens"]] == [("disponivel", "2.86.86.86")]
        # Segunda vez: o arquivo do cache é conferido pelo SHA-256 e não baixa de novo.
        antes = len(executor.historico)
        assert obter_catalogo(executor, str(tmp_path), system_id="063A").legado and len(executor.historico) == antes

    def test_hash_diferente_apaga_e_nao_usa(self, tmp_path, monkeypatch):
        executor = self.preparar(tmp_path, monkeypatch, sha="0" * 64)
        resultado = obter_catalogo(executor, str(tmp_path), system_id="063A")
        assert resultado.legado is None and resultado.dados["versao"] == "26.10.08" and "SHA-256 diferente" in resultado.aviso
        assert not list((caminho_cache(str(tmp_path)).parent / "legados").glob("*.gz"))

    def test_modelo_atendido_nao_baixa_nada(self, tmp_path, monkeypatch):
        executor = self.preparar(tmp_path, monkeypatch)
        resultado = obter_catalogo(executor, str(tmp_path), system_id="FFFF")  # nem no atual nem no antigo
        assert resultado.legado is None
        cache = caminho_cache(str(tmp_path))
        cache.write_bytes(RECORTE.read_bytes())  # R640 com BIOS no catálogo atual
        executor.historico.clear()
        assert obter_catalogo(executor, str(tmp_path), system_id="0716").legado is None and executor.historico == []

    def test_lista_embutida_so_dell_e_com_sha256(self):
        from msscan.atualizacao.catalogo import carregar_legados

        legados = carregar_legados()
        assert legados and all(e["url"].startswith("https://downloads.dell.com/") and len(e["sha256"]) == 64 for e in legados)
        assert [e["versao"] for e in legados] == sorted((e["versao"] for e in legados), reverse=True)

"""Frota simulada (pedido de 14/10/2026): o MSscan inteiro em Dell da 11ª à 17ª geração, blade, sled, borda e sem IPMI.

Cada servidor é a coleta real do R640 com outros valores (``tests/frota.py``); o catálogo é um recorte real do catálogo
oficial da Dell (``Catalog_recorte_frota.xml.gz``). Nada é executado: replay. O que se confere são as regras do projeto —
nunca quebrar, nunca OK sem evidência, "não sei" nunca vira "não precisa", 13ª geração e anteriores fora do catálogo.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from main import main
from msscan.core.executor import ExecutorReplay, RespostaDownload
from msscan.ia.sanitizar import preparar_envio
from msscan.interface.sessao import Sessao
from msscan.rede.verificacao import ResultadoRede
from tests import frota

STATUS_VALIDOS = {"OK", "ALERTA", "FALHA", "NAO_VERIFICADO", "NAO_APLICAVEL"}
RAID_DE_HARDWARE = {"megaraid_sas", "mpi3mr"}
DOMINIOS_DELL = ("https://downloads.dell.com/", "https://dl.dell.com/")


class ExecutorFrota(ExecutorReplay):
    """Replay do servidor da frota; o download do catálogo entrega o recorte real da frota."""

    def _baixar(self, url, destino, limite, timeout):  # noqa: ANN001
        import hashlib

        origem = frota.RECORTES_ANTIGOS.get(url, frota.RECORTE_FROTA)
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        Path(destino).write_bytes(origem.read_bytes())
        return RespostaDownload(url, 200, destino, origem.stat().st_size, hashlib.sha256(origem.read_bytes()).hexdigest())


@pytest.fixture(scope="module")
def servidores(tmp_path_factory):
    """Cada perfil: fixture montada, saída do modo texto e o estado da sessão depois do diagnóstico e da verificação."""
    import hashlib

    from msscan.atualizacao import catalogo

    # Os catálogos antigos de verdade (catalogos_dell.yaml), com o SHA-256 do recorte que o executor da frota entrega.
    legados = [{**e, "sha256": hashlib.sha256(frota.RECORTES_ANTIGOS[e["url"]].read_bytes()).hexdigest()} for e in catalogo.carregar_legados()]
    remendo = pytest.MonkeyPatch()
    remendo.setattr(catalogo, "carregar_legados", lambda: legados)
    base = tmp_path_factory.mktemp("frota")
    resultado = {}
    for nome in frota.PERFIS:
        pasta = frota.criar(nome, base)
        texto = io.StringIO()
        with contextlib.redirect_stdout(texto):
            rc = main(["--replay", str(pasta), "--sem-ia"])
        dados = base / f"{nome}-dados"
        dados.mkdir()
        # replay=False como a demonstração: o executor só devolve dados gravados, e assim a verificação de atualizações roda.
        sessao = Sessao(ExecutorFrota(str(pasta)), str(dados), replay=False, sem_ia=True)
        sessao.iniciar()
        sessao.aguardar(60)
        sessao.rede = ResultadoRede(True)
        assert sessao.iniciar_diagnostico(), nome
        sessao.aguardar(120)
        diagnostico = sessao.estado()["diagnostico"]
        assert sessao.verificar_atualizacoes(), nome
        sessao.aguardar(180)
        resultado[nome] = {"rc": rc, "texto": texto.getvalue(), "diagnostico": diagnostico,
                           "atualizacoes": sessao.estado()["atualizacoes"], "relatorio": json.loads(json.dumps(sessao.relatorio)),
                           "html": sessao.html_relatorio() or ""}  # fmt: skip
        sessao.encerrar()
    remendo.undo()
    return resultado


@pytest.mark.parametrize("nome", list(frota.PERFIS))
def test_modo_texto_identifica_geracao_e_idrac(servidores, nome) -> None:
    perfil, saida = frota.PERFIS[nome], servidores[nome]
    assert saida["rc"] == 0 and "Traceback" not in saida["texto"]
    assert f"Servidor detectado: Dell Inc. {perfil.modelo}" in saida["texto"]
    assert f"{perfil.geracao}ª geração" in saida["texto"] and perfil.idrac in saida["texto"]
    assert ("Redfish 1.4.0: disponível" in saida["texto"]) is (perfil.tem_redfish and perfil.base == frota.R620)


@pytest.mark.parametrize("nome", list(frota.PERFIS))
def test_diagnostico_nunca_ok_sem_evidencia(servidores, nome) -> None:
    perfil, diagnostico = frota.PERFIS[nome], servidores[nome]["diagnostico"]
    status = {c["componente"]: c["status"] for c in diagnostico["componentes"]}
    assert set(status.values()) <= STATUS_VALIDOS and len(status) == 10
    for componente in diagnostico["componentes"]:
        if componente["status"] == "OK":
            assert componente["fonte"], componente  # todo "Normal" diz de onde veio
    # RAID de hardware sem credenciais nem perccli: nunca OK; sem controladora: não se aplica.
    hardware = any(c.driver in RAID_DE_HARDWARE for c in perfil.controladoras)
    assert status["raid"] == ("NAO_VERIFICADO" if hardware else "NAO_APLICAVEL")
    # Volume da PERC/BOSS não é disco "NÃO verificado" (achado do R530 real, seção 29).
    # (Nas frotas sobre o R620, o disco ID 1 que não responde é real e continua NÃO verificado; o volume, não.)
    discos = next(m for m in servidores[nome]["relatorio"]["medido"] if m["componente"] == "discos")["dados"]["discos"]
    assert not [d for d in discos if d["status"] == "NAO_VERIFICADO" and (d["tipo"] == "scsi" or
                                                                            str(d.get("modelo") or "").startswith(("PERC", "DELLBOSS")))]
    if perfil.sensores_do_chassi or perfil.sem_ipmi:
        # Blade/sled (sensores do chassi) e IPMI desligado: sem leitura, "não verificado" — nunca "Normal".
        assert status["fontes"] == status["ventoinhas"] == "NAO_VERIFICADO"
    if perfil.sem_ipmi:
        assert status["log_bmc"] == status["internos"] == "NAO_VERIFICADO"
    parcial = "NAO_VERIFICADO" in status.values()
    assert diagnostico["veredito"].startswith("Servidor parcialmente diagnosticado" if parcial else "Servidor diagnosticado com sucesso")


@pytest.mark.parametrize("nome", list(frota.PERFIS))
def test_atualizacoes_por_geracao(servidores, nome) -> None:
    perfil, atualizacoes = frota.PERFIS[nome], servidores[nome]["atualizacoes"]
    secao = atualizacoes["secao"]
    if perfil.geracao <= 11:
        # 11ª geração: em nenhum catálogo oficial conferido → "fora de suporte" e Atualização manual; nada oferecido.
        assert secao["situacao"] == "fora_de_suporte" and perfil.system_id in secao["mensagem"]
        assert not [i for i in secao.get("itens", []) if i["situacao"] == "atualizacao"]
        return
    assert secao["situacao"] == "verificada"
    # 12ª e 13ª (pedido de 15/10/2026): fora do catálogo atual → último catálogo da Dell que ainda as inclui.
    assert bool(secao["catalogo"].get("legado")) is (not perfil.no_catalogo)
    if not perfil.no_catalogo:
        assert secao["catalogo"]["versao"] == {12: "22.12.30", 13: "26.06.01"}[perfil.geracao]
        assert "último catálogo oficial da Dell que ainda o inclui" in secao["catalogo"]["aviso"]
        idrac = next(i for i in secao["itens"] if i["componente"] == "iDRAC (BMC)")
        if perfil.geracao == 13:
            # iDRAC8 2.30: primeiro o 2.40.40.40 (<Prerequisites> do catálogo da Dell), depois o 2.86.86.86.
            assert (idrac["disponivel"], idrac["versao_final"]) == ("2.40.40.40", "2.86.86.86") and "2.86.86.86" in idrac["texto_situacao"]
    itens = {i["componente"]: i for i in secao["itens"]}
    assert itens["BIOS"]["situacao"] in ("atualizacao", "igual", "instalada_mais_nova", "incerta")
    assert "iDRAC (BMC)" in itens  # o iDRAC nunca some da lista, mesmo sem versão (IPMI desligado)
    for item in secao["itens"]:
        if item["situacao"] == "atualizacao":
            # Só com versão instalada conhecida, pacote oficial e hash; "não sei" nunca vira oferta. Hash: SHA-256 ou, na falta
            # dele, MD5 (11.5; o catálogo 22.12.30 da 12ª só traz MD5 — R620 real, 16/10/2026).
            pacote = item["pacote"]
            assert item["instalada"] and pacote["url"].startswith(DOMINIOS_DELL)
            assert len(pacote["sha256"]) == 64 or len(pacote.get("md5") or "") == 32, item
        if not item["instalada"]:
            assert item["situacao"] in ("incerta", "sem_pacote"), item
    if perfil.sem_ipmi:
        idrac = itens["iDRAC (BMC)"]
        assert idrac["situacao"] == "incerta" and "IPMI indisponível" in idrac["texto_situacao"] + idrac.get("motivo", "")


@pytest.mark.parametrize("nome", list(frota.PERFIS))
def test_relatorio_e_envio_para_ia_sem_identificadores(servidores, nome) -> None:
    saida = servidores[nome]
    assert frota.PERFIS[nome].modelo in saida["html"] and "<html" in saida["html"].lower()
    dados, _ = preparar_envio(saida["relatorio"])
    assert "FROTA0" not in json.dumps(dados)  # serial dos discos atrás da PERC nunca vai para a IA


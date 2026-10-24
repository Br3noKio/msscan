"""Todas as mecânicas sobre todas as coletas REAIS (CLAUDE.md 30): a mesma sessão da interface, de ponta a ponta.

Toda pasta ``tests/fixtures/dell/<modelo>/`` com ``origem.json`` "real" entra sozinha: detecção, diagnóstico, relatório (JSON,
HTML, modo texto), configuração, localização, modelo 3D, verificação de atualizações (catálogo) e o envio sanitizado para a
IA. Além das regras de sempre (nunca quebrar, nunca OK sem evidência, "não sei" nunca vira oferta), ``ESPERADO`` fixa o
resultado de cada servidor, conferido com a evidência da bancada — qualquer mudança de interpretação aparece aqui.
"""

from __future__ import annotations

import contextlib
import hashlib
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
from tests.conftest import PASTA_FIXTURES

CATALOGOS = PASTA_FIXTURES / "dell" / "catalogo"
RECORTES = {
    # URL oficial → recorte sem edição (LEIAME.md da pasta do catálogo). O catálogo atual é o recorte da frota.
    "https://downloads.dell.com/FOLDER09390002M/1/Catalog.xml.gz": CATALOGOS / "Catalog_recorte_r620_221230.xml.gz",
    **{url: caminho for url, caminho in frota.RECORTES_ANTIGOS.items() if "FOLDER09390002M" not in url},
}
COLETAS = sorted(p.parent for p in (PASTA_FIXTURES / "dell").glob("*/origem.json")
                 if json.loads(p.read_text(encoding="utf-8")).get("origem") == "real")  # fmt: skip

ESPERADO: dict[str, dict] = {
    "poweredge_r620": {
        # Coleta de 19/10/2026 (12ª, iDRAC7 2.65.65.65, PERC H710 Mini, sem credenciais). Bancada, mesmo dia, com
        # credenciais: RAID FALHA ("Virtual Disk 0: Critical"), BIOS 1.6.0 → 2.9.0 pelo catálogo 22.12.30.
        "status": {"sensores": "OK", "fontes": "OK", "ventoinhas": "OK", "cpu": "OK", "memoria": "OK", "log_bmc": "ALERTA",
                   "internos": "OK", "discos": "ALERTA", "raid": "FALHA", "rede": "OK"},
        "geracao": "12ª geração", "catalogo": "22.12.30",
        "itens": {"BIOS": ("1.6.0", "2.9.0", "atualizacao"), "iDRAC (BMC)": ("2.65", "2.65.65.65", "incerta")},
    },
    "poweredge_r640": {
        # Coleta do R640 onde o MSscan é desenvolvido (14ª, iDRAC9; uma fonte de propósito, CLAUDE.md 14).
        "status": {"sensores": "OK", "fontes": "ALERTA", "ventoinhas": "OK", "cpu": "OK", "memoria": "ALERTA",
                   "log_bmc": "ALERTA", "internos": "ALERTA", "discos": "OK", "raid": "NAO_APLICAVEL", "rede": "OK"},
        "geracao": "14ª geração", "catalogo": None,
        "itens": {},
    },
}


class ExecutorColeta(ExecutorReplay):
    """Replay da coleta real; os downloads de catálogo entregam os recortes reais."""

    def _baixar(self, url, destino, limite, timeout):  # noqa: ANN001
        origem = RECORTES.get(url, frota.RECORTE_FROTA)
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        Path(destino).write_bytes(origem.read_bytes())
        return RespostaDownload(url, 200, destino, origem.stat().st_size, hashlib.sha256(origem.read_bytes()).hexdigest())


@pytest.fixture(scope="module")
def saidas(tmp_path_factory):
    from msscan.atualizacao import catalogo

    legados = [{**e, "sha256": hashlib.sha256(RECORTES[e["url"]].read_bytes()).hexdigest()} if e["url"] in RECORTES else e
               for e in catalogo.carregar_legados()]  # fmt: skip
    remendo = pytest.MonkeyPatch()
    remendo.setattr(catalogo, "carregar_legados", lambda: legados)
    resultado = {}
    for pasta in COLETAS:
        texto = io.StringIO()
        with contextlib.redirect_stdout(texto):
            rc = main(["--replay", str(pasta), "--sem-ia"])
        dados = tmp_path_factory.mktemp(pasta.name)
        sessao = Sessao(ExecutorColeta(str(pasta)), str(dados), replay=False, sem_ia=True)
        sessao.iniciar()
        sessao.aguardar(60)
        sessao.rede = ResultadoRede(True)
        assert sessao.iniciar_diagnostico(), pasta.name
        sessao.aguardar(120)
        diagnostico = sessao.estado()["diagnostico"]
        assert sessao.verificar_atualizacoes(), pasta.name
        sessao.aguardar(180)
        resultado[pasta.name] = {"rc": rc, "texto": texto.getvalue(), "diagnostico": diagnostico,
                                 "atualizacoes": sessao.estado()["atualizacoes"],
                                 "relatorio": json.loads(json.dumps(sessao.relatorio)), "html": sessao.html_relatorio() or ""}  # fmt: skip
        sessao.encerrar()
    remendo.undo()
    return resultado


def test_toda_coleta_real_tem_resultado_esperado() -> None:
    """Coleta nova sem ``ESPERADO`` falha de propósito: o resultado precisa ser conferido com a evidência da bancada."""
    assert {p.name for p in COLETAS} == set(ESPERADO)


@pytest.mark.parametrize("nome", sorted(ESPERADO))
def test_diagnostico_igual_ao_conferido(saidas, nome) -> None:
    saida, esperado = saidas[nome], ESPERADO[nome]
    assert saida["rc"] == 0 and "Traceback" not in saida["texto"] and esperado["geracao"] in saida["texto"]
    status = {c["componente"]: c["status"] for c in saida["diagnostico"]["componentes"]}
    assert status == esperado["status"]
    for componente in saida["diagnostico"]["componentes"]:
        if componente["status"] != "NAO_VERIFICADO":
            assert componente["fonte"], componente  # todo resultado diz de onde veio
        else:
            assert componente["mensagem"] == "Não foi possível verificar este componente neste equipamento"


@pytest.mark.parametrize("nome", sorted(ESPERADO))
def test_secoes_do_relatorio_em_todas_as_coletas(saidas, nome) -> None:
    relatorio = saidas[nome]["relatorio"]
    for secao in ("configuracao", "localizacao", "modelo3d", "estado_geral", "equipamento"):
        assert relatorio.get(secao), secao
    assert relatorio["equipamento"]["modelo"] in saidas[nome]["html"]
    dados, _ = preparar_envio(relatorio)
    enviado = json.dumps(dados)
    for modulo in relatorio["medido"][4]["dados"].get("modulos") or []:  # serial dos pentes nunca vai para a IA
        assert not modulo.get("serial") or modulo["serial"] == "REMOVIDO" or modulo["serial"] not in enviado


@pytest.mark.parametrize("nome", sorted(ESPERADO))
def test_atualizacoes_iguais_as_conferidas(saidas, nome) -> None:
    secao, esperado = saidas[nome]["atualizacoes"]["secao"], ESPERADO[nome]
    if esperado["catalogo"]:
        assert secao["situacao"] == "verificada" and secao["catalogo"]["versao"] == esperado["catalogo"]
    itens = {i["componente"]: i for i in secao.get("itens", [])}
    for componente, (instalada, disponivel, situacao) in esperado["itens"].items():
        assert (itens[componente]["instalada"], itens[componente]["disponivel"], itens[componente]["situacao"]) == (
            instalada, disponivel, situacao)
    for item in secao.get("itens", []):
        if item["situacao"] == "atualizacao":
            assert item["instalada"] and item["pacote"]["url"].startswith(("https://downloads.dell.com/", "https://dl.dell.com/"))
        if not item["instalada"]:
            assert item["situacao"] in ("incerta", "sem_pacote"), item

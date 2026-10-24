"""CLAUDE.md 21.1, 21.2 e 21.7 na sessão da interface: progresso ao vivo, "Parar depois do componente atual", "Continuar de
onde parou" e o log técnico gravado no pendrive a cada mudança de etapa."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime

from msscan.atualizacao import automatica
from msscan.atualizacao import progresso as prog_mod
from msscan.interface import sessao as modulo_sessao
from tests.test_interface import sessao_concluida


def _executando(sessao, componentes=("NIC", "BIOS")):
    parcial = sessao._novo_acompanhamento([{"componente": c} for c in componentes])
    sessao.aplicacao = {"situacao": "executando", "mensagem": "", "resultado": parcial}
    return parcial["_acompanhamento"]


def test_log_automatico_a_cada_etapa_no_mesmo_arquivo(tmp_path):
    sessao = sessao_concluida()
    sessao.replay, sessao.pasta_dados = False, str(tmp_path)  # pendrive gravável simulado
    prog = _executando(sessao)
    prog.etapa("NIC", prog_mod.BAIXANDO, "baixando do site da Dell")
    logs = list((tmp_path / "logs").glob("*_auto.json"))
    assert len(logs) == 1 and "Baixando" in logs[0].read_text(encoding="utf-8")
    prog.etapa("NIC", prog_mod.ENVIANDO, "enviando o pacote ao iDRAC")
    assert list((tmp_path / "logs").glob("*_auto.json")) == logs  # o mesmo arquivo, regravado
    dados = json.loads(logs[0].read_text(encoding="utf-8"))
    assert "Enviando ao iDRAC" in json.dumps(dados, ensure_ascii=False)


def test_log_automatico_sem_pendrive_nao_grava_nem_quebra(tmp_path):
    sessao = sessao_concluida()  # replay: nada vai para o pendrive
    prog = _executando(sessao)
    prog.etapa("NIC", prog_mod.BAIXANDO)
    assert sessao._log_auto is None


def test_progresso_ao_vivo_e_parar_depois_do_atual():
    sessao = sessao_concluida()
    assert sessao.parar_depois_do_atual()[0] is False  # nada em andamento
    prog = _executando(sessao)
    prog.etapa("NIC", prog_mod.INSTALANDO, "gravando", 40, gravando=True)
    acomp = sessao.estado()["atualizacoes"]["aplicacao"]["resultado"]["_acompanhamento"]
    assert acomp["seguro"] is False and "Não desligue o servidor" in acomp["faixa"] and acomp["itens"][0]["gravando"]
    assert sessao.parar_depois_do_atual()[0] and prog.parar_pedido
    prog.etapa("NIC", prog_mod.CONCLUIDO)
    acomp = sessao.estado()["atualizacoes"]["aplicacao"]["resultado"]["_acompanhamento"]
    assert acomp["seguro"] and acomp["parar_pedido"] and "pode ser desligado com segurança" in acomp["faixa"]


def test_continuar_de_onde_parou_so_com_o_botao(monkeypatch):
    sessao = sessao_concluida()
    assert sessao.continuar_parado()[0] is False
    chamadas = []
    monkeypatch.setattr(sessao, "_continuar_automatico", lambda: chamadas.append(sessao._continuacao))
    sessao._parado = {"situacao": "parado", "pendentes": [{"componente": "BIOS"}]}
    assert sessao.continuar_parado()[0]
    sessao.aguardar(10)
    assert chamadas == [{"situacao": "parado", "pendentes": [{"componente": "BIOS"}]}] and sessao._parado is None


def test_pode_continuar_parado():
    class Ctx:
        modelo, sku = "PowerEdge R640", "0716"

    estado = {"automatico": True, "situacao": "parado", "pendentes": [{"componente": "BIOS"}],
              "equipamento": {"modelo": "PowerEdge R640", "sku": "0716"}, "confirmado": datetime.now().isoformat()}
    assert "Continuar de onde parou" in automatica.pode_continuar(estado, Ctx())  # nunca continua sozinho
    assert automatica.pode_continuar_parado(estado, Ctx()) == ""
    assert automatica.pode_continuar_parado({**estado, "situacao": "agendado"}, Ctx()) == "nada parado a pedido"


def test_janela_de_escrita_reentrante_remonta_uma_vez(monkeypatch):
    aberturas = []

    @contextmanager
    def escrita(executor, local):
        aberturas.append(1)
        yield

    monkeypatch.setattr(modulo_sessao, "escrita", escrita)
    sessao = sessao_concluida()
    aberturas.clear()
    with sessao._escrita(), sessao._escrita():
        pass
    with sessao._escrita():
        pass
    assert len(aberturas) == 2


def test_idrac_tools_no_relatorio():
    """21.6: versão e origem do iDRAC Tools vão para o relatório."""
    from types import SimpleNamespace

    from msscan.interface.sessao import ferramentas_idrac

    assert ferramentas_idrac(SimpleNamespace(cache={})) is None
    ctx = SimpleNamespace(cache={"idrac_tools": {"situacao": "preparado", "versao": "11.4.1.0", "pacote": "Dell-iDRACTools-Web-LX-11.4.1.0.tar.gz",
                                                 "origem": "download automático do site da Dell", "termos": "https://www.dell.com/aeula"}})
    dados = ferramentas_idrac(ctx)
    assert dados["versao"] == "11.4.1.0" and "site da Dell" in dados["texto"] and "aeula" in dados["texto"]

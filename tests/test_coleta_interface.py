"""Botão "Coletar dados deste servidor" (pedido de 16/10/2026): o mesmo ``--coletar`` do terminal pela interface, somente
leitura, montado na RAM e gravado conferido em ``MSSCAN/coletas/``."""

from __future__ import annotations

import tarfile

from msscan.coleta import coletor
from tests.test_interface import sessao_concluida


def _gravavel(sessao, pasta):
    sessao.replay, sessao.pasta_dados = False, str(pasta)  # pendrive gravável simulado (executor continua o replay)
    return sessao


def test_coleta_grava_pacote_no_pendrive(tmp_path):
    sessao = _gravavel(sessao_concluida(), tmp_path)
    assert sessao.coletar_dados() == (True, "")
    sessao.aguardar(120)
    coleta = sessao.estado()["coleta"]
    assert coleta["situacao"] == "concluida", coleta
    pacotes = list((tmp_path / "coletas").glob("coleta_dell_poweredge_r640_*.tar.gz"))
    assert len(pacotes) == 1 and pacotes[0].name in coleta["mensagem"]
    with tarfile.open(pacotes[0]) as tar:
        assert "dell/poweredge_r640/origem.json" in tar.getnames()


def test_coleta_recusada_na_demonstracao_sem_pendrive_e_ocupado(tmp_path):
    sessao = sessao_concluida()  # replay
    assert sessao.coletar_dados()[0] is False
    sessao.replay, sessao.pasta_dados = False, None  # sem local gravável
    assert sessao.coletar_dados()[0] is False
    _gravavel(sessao, tmp_path).etapa = "diagnostico"
    ok, motivo = sessao.coletar_dados()
    assert ok is False
    assert sessao.estado()["coleta"] == {"situacao": "recusada", "mensagem": motivo}  # a tela mostra o motivo


def test_coleta_com_falha_nao_derruba_a_sessao_nem_grava(tmp_path, monkeypatch):
    def quebra(*_a, **_k):
        raise OSError("disco cheio")

    monkeypatch.setattr(coletor, "coletar", quebra)
    sessao = _gravavel(sessao_concluida(), tmp_path)
    etapa = sessao.etapa
    assert sessao.coletar_dados()[0]
    sessao.aguardar(30)
    coleta = sessao.estado()["coleta"]
    assert coleta["situacao"] == "erro" and "disco cheio" in coleta["mensagem"]
    assert sessao.etapa == etapa and not (tmp_path / "coletas").exists()

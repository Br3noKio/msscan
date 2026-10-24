"""Correções da bancada de 19/10/2026 (R620, iDRAC7 2.65.65.65, BIOS 1.6.0 → 2.9.0 com a 1.0.8)."""

from __future__ import annotations

from msscan import textos
from msscan.atualizacao import automatica


def _fila(jobs):
    return lambda ctx: {"origem": "racadm", "jobs": jobs, "motivo": ""}


def test_racadm_sem_tarefa_na_fila_lida_e_falha(monkeypatch):
    """racadm update saiu com 0 e sem saída; a fila foi lida e ficou vazia: nada agendado (antes: "possivelmente agendado")."""
    monkeypatch.setattr(automatica.fila_jobs, "ler_fila", _fila([]))
    resultado: dict = {"passos": [], "tentativas": []}
    desfecho = automatica._acompanhar_job_racadm(None, set(), {"componente": "BIOS"}, resultado, lambda s: None)
    assert desfecho == automatica.FALHOU
    assert "NADA foi agendado" in resultado["tentativas"][-1]["detalhe"]
    assert textos.t("py_automatica.racadm_sem_tarefa_na_fila") != "py_automatica.racadm_sem_tarefa_na_fila"


def test_racadm_sem_conseguir_ler_a_fila_continua_incerto(monkeypatch):
    monkeypatch.setattr(automatica.fila_jobs, "ler_fila", lambda ctx: {"origem": "", "jobs": [], "motivo": "x"})
    resultado: dict = {"passos": [], "tentativas": []}
    assert automatica._acompanhar_job_racadm(None, None, {"componente": "BIOS"}, resultado, lambda s: None) == automatica.POSSIVELMENTE


def test_envio_do_pacote_usa_o_tempo_do_envio_na_conexao(monkeypatch, tmp_path):
    """urllib3 2.x escreve o corpo com o tempo de conexão: 15 s cortava o BIOS no iDRAC7 ("write operation timed out")."""
    import requests

    from msscan.core.executor import ENVIAR_FIRMWARE_LEGADO, ExecutorReal

    tempos = []

    class Resp:
        status_code, content, headers = 201, b"", {"ETag": "e"}

    def post(self, url, **k):
        tempos.append(k["timeout"])
        return Resp()

    monkeypatch.setattr(requests.Session, "get", lambda self, url, **k: Resp())
    monkeypatch.setattr(requests.Session, "post", post)
    pacote = tmp_path / "BIOS.EXE"
    pacote.write_bytes(b"x")
    ExecutorReal()._bmc_escrita(ENVIAR_FIRMWARE_LEGADO, "POST", "https://10.0.0.1/redfish/v1/UpdateService/FirmwareInventory",
                                "t", None, str(pacote), 900)  # fmt: skip
    assert tempos == [(900, 900)]

"""CLAUDE.md 21.1–21.4: fila de jobs do iDRAC (tradução, RED115, racadm jobqueue view) e o ``Progresso`` da atualização."""

from __future__ import annotations

import pytest

from msscan.atualizacao import jobs, progresso as prog_mod
from msscan.parsers import ErroParser

# MOCKUP(rodada final): formato do guia RACADM do iDRAC9 (sem coleta real), com a mensagem real do T1 de 02/10/2026.
JOBQUEUE = """\
-------------------------JOB QUEUE------------------------
[Job ID=JID_909869283926]
Job Name=Export: System configuration XML file
Status=Completed
Start Time=[Not Applicable]
Expiration Time=[Not Applicable]
Message=[SYS043: Successfully exported system configuration XML file.]
Percent Complete=[100]
----------------------------------------------------------
[Job ID=JID_909870509721]
Job Name=Repository Update
Status=Failed
Start Time=[2026-10-02T19:24:10]
Message=[RED115: iDRAC restart operation interrupted the job while downloading.]
Percent Complete=[0]
----------------------------------------------------------
[Job ID=JID_909870600000]
Job Name=Firmware Update: BIOS
Status=Scheduled
Message=[JCP001: Task successfully scheduled.]
Percent Complete=[0]
"""


def test_parse_jobqueue_e_traducao_do_red115():
    fila = jobs.parse_jobqueue(JOBQUEUE)
    assert [(j["id"], j["estado"], j["codigo"]) for j in fila] == [
        ("JID_909869283926", "Completed", "SYS043"), ("JID_909870509721", "Failed", "RED115"),
        ("JID_909870600000", "Scheduled", "JCP001")]
    red115 = fila[1]
    assert jobs.reinicio_do_idrac(red115) and "reiniciou durante o download" in jobs.traduzir(red115)
    assert "RED115: iDRAC restart operation interrupted" in jobs.traduzir(red115)  # o texto original vai junto
    assert not jobs.reinicio_do_idrac(fila[2]) and "próximo reinício" in jobs.traduzir(fila[2])
    assert jobs.parse_jobqueue("") == [] and jobs.parse_jobqueue("No job found") == []
    with pytest.raises(ErroParser):
        jobs.parse_jobqueue("Job ID: formato que ninguém conhece")


def test_resumir_bloqueia_instalacao_nova_com_job_ativo_ou_agendado():
    fila = {"origem": "racadm", "motivo": "", "jobs": jobs.parse_jobqueue(JOBQUEUE)}
    resumo = jobs.resumir(fila)
    assert resumo["agendados"] == 1 and resumo["ativos"] == 0 and "duplicar" in resumo["bloqueio"]
    assert [j["grupo"] for j in resumo["jobs"]] == ["agendado", "falha"]
    assert any("F11" in e for e in resumo["explicacao"])
    so_falhas = jobs.resumir({"origem": "racadm", "jobs": [j for j in fila["jobs"] if j["estado"] != "Scheduled"]})
    assert so_falhas["bloqueio"] == "" and [j["grupo"] for j in so_falhas["jobs"]] == ["falha"]


def test_esperar_sem_ativos(monkeypatch):
    respostas = [(True, "JID_1 Firmware (Running)"), (True, "JID_1 Firmware (Running)"), (False, "")]
    monkeypatch.setattr(jobs, "ha_ativo", lambda ctx, ignorar=frozenset(): respostas.pop(0))
    esperas, avisos = [], []
    assert jobs.esperar_sem_ativos(None, esperas.append, ao_esperar=avisos.append) == (True, "")
    assert len(esperas) == 2 and avisos == ["JID_1 Firmware (Running)"] * 2
    monkeypatch.setattr(jobs, "ha_ativo", lambda ctx, ignorar=frozenset(): (True, "JID_2 (Running)"))
    assert jobs.esperar_sem_ativos(None, lambda _: None, limite_s=60) == (False, "JID_2 (Running)")
    monkeypatch.setattr(jobs, "ha_ativo", lambda ctx, ignorar=frozenset(): (None, "sem racadm"))
    livre, detalhe = jobs.esperar_sem_ativos(None, lambda _: None)
    assert livre and "não lida" in detalhe  # sem como ler: segue como antes (o iDRAC recusa duplicatas)


class Relogio:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_progresso_etapas_tempo_faixa_e_sem_progresso():
    relogio, mudancas = Relogio(), []
    prog = prog_mod.Progresso([{"componente": "NIC", "estimativa_s": 300}, {"componente": "BIOS", "estimativa_s": 600}],
                              ao_mudar=lambda: mudancas.append(1), relogio=relogio)  # fmt: skip
    prog.etapa("NIC", prog_mod.BAIXANDO, "baixando", 0)
    relogio.t += 30
    prog.percentual("NIC", 50)
    estado = prog.para_dict()
    assert estado["itens"][0]["percentual"] == 8 and estado["seguro"] and "pode ser desligado com segurança" in estado["faixa"]
    assert len(mudancas) == 1  # percentual não grava o log; só mudança de etapa
    prog.etapa("NIC", prog_mod.INSTALANDO, "gravando", 0, gravando=True)
    estado = prog.para_dict()
    assert not estado["seguro"] and estado["faixa"] == "Não desligue o servidor: gravação de firmware em andamento (NIC)." and estado["itens"][0]["gravando"]
    assert estado["itens"][0]["etapas"][0] == {"etapa": "baixando", "nome": "Baixando", "detalhe": "baixando", "segundos": 30}
    prog.esperando("o iDRAC terminar a tarefa de NIC", "Job Queue")
    relogio.t += prog_mod.SEM_PROGRESSO_S
    assert "Sem progresso há 3 min" in prog.para_dict()["sem_progresso"] and "Job Queue" in prog.para_dict()["sem_progresso"]
    prog.etapa("NIC", prog_mod.CONCLUIDO)
    prog.pedir_parada()
    prog.marcar_parado("BIOS")
    estado = prog.para_dict()
    assert estado["seguro"] and estado["terminou"] and estado["geral"] == 100 and estado["restante_s"] is None
    assert [i["etapa"] for i in estado["itens"]] == ["concluido", "parado"] and estado["parado_em"] == "BIOS"


def test_progresso_de_jobs_e_tarefas():
    prog = prog_mod.Progresso([{"componente": "NIC"}])
    prog_mod.do_job(prog, "NIC", "Downloading", 40)
    assert prog.itens["NIC"]["etapa"] == prog_mod.ENVIANDO and prog.gravando is None
    prog_mod.do_job(prog, "NIC", "Running", 10)
    assert prog.itens["NIC"]["etapa"] == prog_mod.INSTALANDO and prog.gravando == "NIC"
    prog_mod.do_job(prog, "NIC", "Running", 60)
    assert prog.itens["NIC"]["percentual"] == 60 and len(prog.itens["NIC"]["etapas"]) == 2
    prog_mod.da_tarefa(prog, "NIC", {"agendada": True, "mensagens": ["Task successfully scheduled."]})
    assert prog.itens["NIC"]["etapa"] == prog_mod.AGENDADO and prog.gravando is None
    assert prog_mod.de({}).itens == {} and prog_mod.de({"_acompanhamento": prog}) is prog


def test_log_tecnico_gravado_durante_espera_longa():
    """22.1: o iDRAC respondendo sem a etapa mudar (13 min de 06/10) → log gravado a cada minuto, não só na troca de etapa."""
    agora = [0.0]
    gravacoes = []
    prog = prog_mod.Progresso(ao_mudar=lambda: gravacoes.append(agora[0]), relogio=lambda: agora[0])
    for agora[0] in (10.0, 30.0, 59.0):
        prog.resposta_idrac()
    assert gravacoes == []
    for agora[0] in (61.0, 90.0, 125.0):
        prog.resposta_idrac()
    assert gravacoes == [61.0, 125.0]
    prog.avisar_agora("Repositório: o iDRAC não buscou nada")
    prog.etapa("NIC", prog_mod.ENVIANDO)
    assert prog.para_dict()["aviso_imediato"].startswith("Repositório")  # o aviso fica na tela depois da troca de caminho

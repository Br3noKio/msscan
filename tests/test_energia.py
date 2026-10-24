"""Desligar / Reiniciar (CLAUDE.md 15.5): bloqueio durante firmware/ação no BMC; sync e desmontagem antes."""

from __future__ import annotations

from msscan.core.executor import ExecutorReplay
from msscan.interface.sessao import DESMONTAR_PENDRIVE, Sessao
from tests.conftest import R640, ExecutorFalso

SYNC, DESMONTAR, POWEROFF, REBOOT = ("sync",), (DESMONTAR_PENDRIVE,), ("systemctl", "poweroff"), ("systemctl", "reboot")


def sessao_real(**comandos) -> tuple[Sessao, ExecutorFalso]:
    executor = ExecutorFalso({SYNC: (0, "", ""), DESMONTAR: (0, "", ""), POWEROFF: (0, "", ""), REBOOT: (0, "", ""), **comandos})
    return Sessao(executor, None, replay=False), executor


def rodados(executor: ExecutorFalso) -> list[tuple[str, ...]]:
    return [tuple(e.comando) for e in executor.historico]


def test_desligar_faz_sync_e_desmonta_antes():
    sessao, executor = sessao_real()
    assert sessao.energia("desligar") == (True, "Desligando...")
    assert rodados(executor) == [SYNC, DESMONTAR, POWEROFF]
    sessao, executor = sessao_real()
    assert sessao.energia("reiniciar")[0] and rodados(executor)[-1] == REBOOT


def test_sem_desmontar_nao_desliga():
    sessao, executor = sessao_real(**{})
    executor.comandos[DESMONTAR] = (1, "", "em uso")
    ok, mensagem = sessao.energia("desligar")
    assert not ok and "pendrive" in mensagem and POWEROFF not in rodados(executor)


def test_bloqueado_durante_firmware_e_acao_no_bmc():
    for campo in ("aplicacao", "acao"):
        sessao, executor = sessao_real()
        setattr(sessao, campo, {"situacao": "executando", "mensagem": ""})
        ok, mensagem = sessao.energia("reiniciar")
        assert not ok and "em andamento" in mensagem and rodados(executor) == []
        assert sessao.estado()["energia"]["bloqueio"] == mensagem


def test_replay_e_acao_desconhecida_nao_rodam_nada():
    sessao = Sessao(ExecutorReplay(R640), None, replay=True)
    assert not sessao.energia("desligar")[0] and sessao.estado()["energia"]["disponivel"] is False
    sessao, executor = sessao_real()
    assert sessao.energia("hibernar") == (False, "Ação desconhecida.") and rodados(executor) == []


def test_rota_exige_confirmacao_e_frontend_foca_cancelar():
    import threading

    from msscan.interface.servidor import PASTA_ESTATICOS, ServidorUI
    from tests.test_terminal import chamar

    sessao, executor = sessao_real()
    srv = ServidorUI(sessao, porta=0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        assert chamar(srv, "/api/energia", {"acao": "desligar"})[0] == 400
        assert chamar(srv, "/api/energia", {"acao": "desligar", "confirmar": True}, token=None)[0] == 403
        assert rodados(executor) == []
    finally:
        srv.shutdown()
        srv.server_close()
    js = (PASTA_ESTATICOS / "app.js").read_text(encoding="utf-8")
    assert '$("energia-cancelar").focus();\n}' in js  # "Cancelar" com o foco ao abrir


def test_excecao_na_aplicacao_libera_a_tela_com_orientacao():
    """Revisão independente (BAIXO): exceção na thread não deixa "executando" (e a energia bloqueada) para sempre."""
    sessao, _ = sessao_real()
    sessao.aplicacao = {"situacao": "executando", "mensagem": ""}

    def explode() -> None:
        raise OSError("pendrive removido")

    sessao._em_segundo_plano(explode)
    sessao._thread.join(5)
    assert sessao.aplicacao["situacao"] == "falhou" and "fila de tarefas" in sessao.aplicacao["mensagem"]
    assert sessao.estado()["energia"]["bloqueio"] is None

"""CLAUDE.md 21.7: testes com o iDRAC falso dos pedidos de 05/10/2026 (T1 com a ISO 0.16.1).

iDRAC sozinho e primeiro (esperando ele voltar), agendados num só reinício, envio com progresso, job parado, job com
falha, RED115 com UMA nova tentativa, iDRAC reiniciando no meio, "Parar depois do componente atual", boot pelo pendrive
por ``UefiTarget``, tarefas pendentes ao iniciar e o desfazer adiado com job em andamento.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from msscan.atualizacao import automatica, aviso, jobs, progresso as prog_mod, repositorio, temporarias as temp
from msscan.atualizacao.aplicacao import MODO_APLICAR, executar_plano, montar_plano
from msscan.atualizacao.retomada import ler_estado
from msscan.deteccao.detectores import tentar_redfish
from tests import simuladores as sim
from tests.test_ponta_a_ponta import (  # noqa: F401 - fixtures
    BIOS, CAMINHO_BIOS, CAMINHO_CPLD, CAMINHO_NIC, CPLD, HARDWARE_OK, NIC, SKU, _ctx, _item, _pacotes, _rodar_repositorio, _sem_esperas, _sem_temporario,
    _verificacao, ipmi, repositorio_local,
)

pytestmark = pytest.mark.skipif(bool(sim.disponivel()), reason=sim.disponivel() or "simuladores disponíveis")

IDRAC = b"pacote do iDRAC (simulado)" * 64
CAMINHO_IDRAC = "FOLDER3/1/iDRAC-with-Lifecycle-Controller_Firmware_X_WN64_7.10.30.00_A00.EXE"
NADA = lambda _: None  # noqa: E731


def _tarefa(estado: str, dell: str, mensagem: str, pct: int = 0, **extra) -> dict:
    return {"TaskState": estado, "PercentComplete": pct, "Messages": [{"Message": mensagem}],
            "Oem": {"Dell": {"JobState": dell, "Message": mensagem}}, **extra}  # fmt: skip


AGENDADA = _tarefa("Running", "Scheduled", "Task successfully scheduled.", 30)
CONCLUIDA = _tarefa("Completed", "Completed", "Job completed successfully.", 100)
RED115 = _tarefa("Exception", "Failed", "RED115: iDRAC restart operation interrupted the job while downloading.", 0)


def _com_idrac(raiz: Path) -> dict:
    _pacotes(raiz)
    (raiz / "firmware/pacotes" / Path(CAMINHO_IDRAC).name).write_bytes(IDRAC)
    verificacao = _verificacao()
    verificacao["itens"].insert(0, _item("item0", "iDRAC", "iDRAC with Lifecycle Controller", "6.10.30.00", "7.10.30.00", IDRAC,
                                         CAMINHO_IDRAC, "25227"))
    return verificacao


def _rodar(tmp_path, ipmi, verificacao, ids, parcial=None, **opcoes):
    raiz = tmp_path / "MSSCAN"
    if not (raiz / "firmware/pacotes").exists():
        _pacotes(raiz)
    with sim.BMCSimulado(tmp_path, ipmi, repositorio=False, **opcoes) as bmc:
        executor = sim.ExecutorSimulador(ipmi, tmp_path)
        ctx = _ctx(executor)
        assert tentar_redfish(ctx, "127.0.0.1", bmc.server_address[1], "teste")
        resultado = executar_plano(ctx, montar_plano(verificacao, ids), executor, str(raiz), MODO_APLICAR, dormir=NADA, parcial=parcial)
    return raiz, bmc, executor, resultado


def _etapas(resultado, componente):
    item = next(i for i in resultado["acompanhamento"]["itens"] if i["componente"] == componente)
    return [e["etapa"] for e in item["etapas"]]


def _com_idrac_e_bios(raiz: Path) -> dict:
    verificacao = _com_idrac(raiz)
    verificacao["itens"].append(_item("item3", "BIOS", "BIOS", "2.21.2", "2.28.1", BIOS, CAMINHO_BIOS, "159"))
    return verificacao


def test_idrac_primeiro_e_o_resto_num_reinicio_so(tmp_path, ipmi):
    """26/10/2026: o iDRAC primeiro e sozinho (esperando ele voltar JÁ com a versão nova, M3); depois rede, CPLD e BIOS
    agendados juntos para UM reinício só."""
    raiz = tmp_path / "MSSCAN"
    verificacao = _com_idrac_e_bios(raiz)
    plano = montar_plano(verificacao, ["item3", "item0", "item2", "item1"])
    assert [i["componente"] for i in plano["itens"]] == ["iDRAC", "Rede X710", "CPLD", "BIOS"]
    roteiros = [[_tarefa("Running", "Running", "Updating", 50), {**CONCLUIDA, "_reiniciar": 3}], [], [], []]
    with sim.BMCSimulado(tmp_path, ipmi, repositorio=False, roteiros=roteiros, idrac_novo="7.10.30.00") as bmc:
        executor = sim.ExecutorSimulador(ipmi, tmp_path)
        ctx = _ctx(executor)
        assert tentar_redfish(ctx, "127.0.0.1", bmc.server_address[1], "teste")
        resultado = executar_plano(ctx, plano, executor, str(raiz), MODO_APLICAR, dormir=NADA)
        assert [f["nome"] for f in bmc.firmware] == [Path(c).name for c in (CAMINHO_IDRAC, CAMINHO_NIC, CAMINHO_CPLD, CAMINHO_BIOS)]
        assert ("idrac_reiniciou", 3) in bmc.eventos
        assert any("voltou com o firmware novo (7.10.30.00)" in p["detalhe"] for p in resultado["passos"]), resultado["passos"]
        assert resultado["situacao"] == "aguardando_reinicio" and executor.reinicios == 1, resultado["mensagem"]
        estado = ler_estado(executor, str(raiz))
        assert [i["componente"] for i in estado["enviados"]] == ["Rede X710", "CPLD", "BIOS"]
        assert estado["pendentes"] == []
    assert _sem_temporario(ipmi)


def test_idrac_que_responde_sem_a_versao_nova_nao_segue_para_a_bios(tmp_path, ipmi, monkeypatch):
    """M3: logo depois do firmware do iDRAC ele ainda responde ANTES de reiniciar — isso não é "voltou"."""
    monkeypatch.setattr(automatica, "ESPERA_IDRAC_NOVO_S", 60.0)
    raiz = tmp_path / "MSSCAN"
    verificacao = _com_idrac_e_bios(raiz)
    roteiros = [[CONCLUIDA]]
    with sim.BMCSimulado(tmp_path, ipmi, repositorio=False, roteiros=roteiros) as bmc:  # nunca muda de versão
        executor = sim.ExecutorSimulador(ipmi, tmp_path)
        ctx = _ctx(executor)
        assert tentar_redfish(ctx, "127.0.0.1", bmc.server_address[1], "teste")
        resultado = executar_plano(ctx, montar_plano(verificacao, ["item0", "item3"]), executor, str(raiz), MODO_APLICAR, dormir=NADA)
    assert [f["nome"] for f in bmc.firmware] == [Path(CAMINHO_IDRAC).name]  # a BIOS não foi enviada
    assert resultado["situacao"] == "parado" and "versão 7.10.30.00" in resultado["mensagem"] and executor.reinicios == 0  # 09/10: retomável
    assert _etapas(resultado, "BIOS")[-1] == "parado"
    assert sum(1 for e in bmc.eventos if e[0] == "login") <= 3  # uma sessão por volta, não uma por consulta


def test_retomada_para_na_primeira_falha_do_ciclo_anterior():
    """09/10/2026: um componente agendado que não ficou na versão nova depois do reinício para tudo (nada mais começa)."""
    from msscan.atualizacao.retomada import conferir

    estado = {"automatico": True, "situacao": "agendado", "confirmado": "2099-01-01T00:00:00",
              "equipamento": {"modelo": "PowerEdge R640", "sku": SKU},
              "enviados": [{"componente": "Rede X710", "component_id": "25237", "instalada": "22.5.0", "disponivel": "22.5.7"}],
              "pendentes": [{"componente": "iDRAC"}, {"componente": "BIOS"}]}  # fmt: skip
    ctx = type("C", (), {"modelo": "PowerEdge R640", "sku": SKU})()
    falhou = conferir(estado, [{"component_id": "25237", "nome": "Rede X710", "versao": "22.5.0"}], "redfish")
    motivo = automatica.pode_continuar(estado, ctx, conferencia=falhou)
    assert "primeira falha" in motivo and "Rede X710" in motivo
    ok = conferir(estado, [{"component_id": "25237", "nome": "Rede X710", "versao": "22.5.7"}], "redfish")
    assert automatica.pode_continuar(estado, ctx, conferencia=ok) == ""


def test_red115_espera_o_idrac_e_refaz_uma_vez(tmp_path, ipmi):
    raiz, bmc, executor, resultado = _rodar(tmp_path, ipmi, _verificacao(), ["item1"],
                                            roteiros=[[_tarefa("Running", "Downloading", "Downloading", 20), {**RED115, "_reiniciar": 2}]])
    assert [f["nome"] for f in bmc.firmware] == [Path(CAMINHO_NIC).name] * 2  # refeito UMA vez
    detalhes = " ".join(t["detalhe"] for t in resultado["tentativas"])
    assert "RED115" in detalhes and "refazer o envio uma vez" in detalhes
    assert resultado["situacao"] == "aguardando_reinicio" and executor.reinicios == 1, resultado["mensagem"]
    assert _etapas(resultado, "Rede X710").count("enviando") == 3  # envio, job baixando, reenvio


def test_red115_duas_vezes_nao_insiste(tmp_path, ipmi):
    _, bmc, executor, resultado = _rodar(tmp_path, ipmi, _verificacao(), ["item1"], roteiros=[[RED115], [RED115]])
    assert len(bmc.firmware) == 2 and executor.reinicios == 0 and resultado["situacao"] in ("falhou", "bloqueada")
    assert _etapas(resultado, "Rede X710")[-1] == "falhou" and "RED115" in resultado["acompanhamento"]["itens"][0]["detalhe"]


def test_envio_com_progresso_em_bytes_e_job_parado(tmp_path, ipmi):
    """Job que nunca termina: o MSscan não fica parado para sempre — "aceito, fim não confirmado", sem reiniciar."""
    parcial = {"_acompanhamento": prog_mod.Progresso()}
    percentuais = []
    original = parcial["_acompanhamento"].percentual
    parcial["_acompanhamento"].percentual = lambda c, v, d=None: (percentuais.append(v), original(c, v, d))
    _, _, executor, resultado = _rodar(tmp_path, ipmi, _verificacao(), ["item1"], parcial,
                                       roteiros=[[_tarefa("Running", "Running", "Validating package", 5)]])
    assert 100 in percentuais  # bytes enviados ao iDRAC (o último bloco fecha em 100%)
    assert resultado["situacao"] == "possivelmente_agendado" and executor.reinicios == 0
    assert "Job Queue" in resultado["mensagem"]


def test_parar_depois_do_componente_atual(tmp_path, ipmi, monkeypatch):
    original = automatica.aplicar_item

    def e_parar(*args, **kwargs):
        desfecho = original(*args, **kwargs)
        prog_mod.de(args[5]).pedir_parada()  # o técnico apertou "Parar" durante o primeiro componente
        return desfecho

    monkeypatch.setattr(automatica, "aplicar_item", e_parar)
    raiz, bmc, executor, resultado = _rodar(tmp_path, ipmi, _verificacao(), ["item1", "item2"])
    assert [f["nome"] for f in bmc.firmware] == [Path(CAMINHO_NIC).name]  # o CPLD não começou
    assert resultado["situacao"] == "parado" and executor.reinicios == 0 and "Seguro desligar agora" in resultado["mensagem"]
    assert "já agendado" in resultado["mensagem"] and _etapas(resultado, "CPLD")[-1] == "parado"
    estado = ler_estado(executor, str(raiz))
    assert estado["situacao"] == "parado" and [i["componente"] for i in estado["pendentes"]] == ["CPLD"]
    ctx = _ctx(sim.ExecutorSimulador(ipmi, tmp_path))
    assert automatica.pode_continuar(estado, ctx) != "" and automatica.pode_continuar_parado(estado, ctx) == ""
    assert _sem_temporario(ipmi)


def test_boot_pelo_pendrive_por_uefitarget(tmp_path, ipmi):
    caminho = "PciRoot(0x0)/Pci(0x14,0x0)/USB(0x3,0x0)/HD(1,MBR,0x1234,0x800,0x100000)"
    _, bmc, executor, resultado = _rodar(tmp_path, ipmi, _verificacao(), ["item1"], uefi_usb=caminho)
    assert bmc.boot_uefi == caminho and not bmc.boot_usb and executor.reinicios == 1
    assert any("caminho UEFI" in p["detalhe"] for p in resultado["passos"])


def test_tarefas_pendentes_ao_iniciar_bloqueiam_o_plano_e_a_retomada_espera(tmp_path, ipmi):
    with sim.BMCSimulado(tmp_path, ipmi, repositorio=False, jobs_iniciais=[
            {"Name": "Firmware Update: BIOS", "JobState": "Scheduled", "Message": "Task successfully scheduled."},
            {"Name": "Repository Update", "JobState": "Failed",
             "Message": "RED115: iDRAC restart operation interrupted the job while downloading."}]) as bmc:  # fmt: skip
        ctx = _ctx(sim.ExecutorSimulador(ipmi, tmp_path))
        assert tentar_redfish(ctx, "127.0.0.1", bmc.server_address[1], "teste")
        assert "Credenciais validadas" in ctx.bmc.tentar("root", sim.SENHA_ROOT)
        fila = aviso.fila_do_idrac(ctx)
        assert fila["origem"] == "redfish" and fila["agendados"] == 1 and aviso._bloqueio_jobs(fila)
        assert any("reiniciou durante o download" in j["texto"] for j in fila["jobs"])
        # Job em andamento na volta do reinício: a retomada espera e, sem terminar, não cria nada por cima.
        bmc.novo_job("Firmware Update: NIC", "FirmwareUpdate", "Running", "Updating")
        estado = {"automatico": True, "situacao": "agendado", "confirmado": "2099-01-01T00:00:00", "modo": "aplicar",
                  "equipamento": {"modelo": "PowerEdge R640", "sku": SKU}, "pendentes": [{"componente": "BIOS"}], "enviados": []}
        resultado = automatica.continuar(ctx, ctx.executor, str(tmp_path), estado, HARDWARE_OK, dormir=NADA)
    assert resultado["situacao"] == "bloqueada" and "ainda em andamento" in resultado["mensagem"] and not bmc.firmware


def test_desfazer_adiado_com_job_em_andamento_e_feito_depois(tmp_path, ipmi):
    raiz = tmp_path / "MSSCAN"
    racadm = sim.criar_racadm(tmp_path / "vendor", "127.0.0.9", ipmi=ipmi)
    executor = sim.ExecutorSimulador(ipmi, tmp_path, racadm)
    ctx = _ctx(executor, racadm)
    registro = temp.Temporarias(str(raiz))
    assert automatica._ligar_passthrough(ctx, executor, str(raiz), registro, {"passos": []}, NADA) is False  # sem Redfish
    assert "AdminState=Enabled" in (tmp_path / "vendor/racadm_estado").read_text()
    (tmp_path / "vendor/racadm_jobs").write_text("[Job ID=JID_1]\nJob Name=Repository Update\nStatus=Downloading\n"
                                                 "Message=[RED001: Downloading]\nPercent Complete=[10]\n")  # fmt: skip
    mudancas = temp.desfazer_tudo(ctx, executor, str(raiz), dormir=NADA)
    assert [m["situacao"] for m in mudancas] == [temp.NAO_DESFEITA] and "adiado" in mudancas[0]["detalhe"]
    assert "AdminState=Enabled" in (tmp_path / "vendor/racadm_estado").read_text() and temp.ha_pendentes(str(raiz))
    # Ligar de novo com o job rodando: nada muda (21.3).
    (tmp_path / "vendor/racadm_estado").write_text((tmp_path / "vendor/racadm_estado").read_text().replace("Enabled", "Disabled"))
    resultado = {"passos": [], "tentativas": []}
    assert automatica._ligar_passthrough(ctx, executor, str(raiz), temp.Temporarias(str(tmp_path / "outro")), resultado, NADA) is False
    assert "AdminState=Disabled" in (tmp_path / "vendor/racadm_estado").read_text()
    assert "tarefa do iDRAC em andamento" in resultado["tentativas"][-1]["detalhe"]
    # Job terminou: o próximo boot desfaz.
    (tmp_path / "vendor/racadm_estado").write_text((tmp_path / "vendor/racadm_estado").read_text().replace("Disabled", "Enabled"))
    (tmp_path / "vendor/racadm_jobs").write_text("[Job ID=JID_1]\nJob Name=Repository Update\nStatus=Completed\nMessage=[RED001: ok]\n")
    mudancas = temp.desfazer_tudo(ctx, executor, str(raiz), dormir=NADA)
    assert [m["situacao"] for m in mudancas] == [temp.DESFEITA] and not temp.ha_pendentes(str(raiz))
    assert jobs.ha_ativo(ctx) == (False, "")


def test_idrac_nao_busca_o_repositorio_avisa_na_hora_e_segue_pelo_envio_direto(tmp_path, ipmi, repositorio_local, monkeypatch):
    """O caso do T1 de 02/10/2026: o job do repositório ficava "baixando" até 1 h sem nada na tela."""
    monkeypatch.setattr(repositorio, "SEM_PEDIDO_S", 0.0)
    _, bmc, executor, resultado = _rodar_repositorio(tmp_path, ipmi, *repositorio_local, nao_buscar=True)
    r = [t for t in resultado["tentativas"] if t["caminho"] == "r"]
    assert any("não buscou o repositório" in t["detalhe"] for t in r), resultado["tentativas"]
    assert [f["nome"] for f in bmc.firmware] == [Path(CAMINHO_NIC).name, Path(CAMINHO_CPLD).name]  # seguiu: envio direto
    assert resultado["situacao"] == "aguardando_reinicio" and executor.reinicios == 1, resultado["mensagem"]
    # 22.3/22.4: o job parado foi CANCELADO no iDRAC (sem sobra na Job Queue), então o pass-through é desfeito já.
    assert any(e[0] == "job_cancelado" for e in bmc.eventos) and not any(j["JobState"] == "Running" for j in bmc.jobs.values())
    assert _sem_temporario(ipmi)
    situacoes = {m["tipo"]: (m["situacao"], m["detalhe"]) for m in resultado["temporarias"]}
    assert situacoes[temp.USUARIO][0] == temp.DESFEITA and situacoes[temp.PASSTHROUGH][0] == temp.DESFEITA
    assert temp.TAREFA not in situacoes  # cancelado na hora: nada a desfazer depois
    assert "tarefa criada no iDRAC: job" in " ".join(t["detalhe"] for t in r)


# --- 22: caminho do repositório conferido, troca de caminho e restauração (pedidos de 06/10/2026) ---------------------


def _posts_repositorio(bmc) -> list:
    """Pedidos ao repositório LOCAL (o online, sem internet no iDRAC falso, falha na hora e não conta)."""
    return [e[2] for e in bmc.eventos if e[0] == "post" and e[1].endswith("InstallFromRepository") and e[2]["ShareType"] == "HTTP"]


def test_poucos_componentes_vao_direto_sem_mexer_no_passthrough(tmp_path, ipmi, repositorio_local, monkeypatch):
    """22.2: uma placa de rede só → envio direto (MultipartUpload); nem liga o pass-through para o repositório."""
    monkeypatch.setattr(automatica, "POUCOS_PARA_DIRETO", 2)
    _, bmc, executor, resultado = _rodar_repositorio(tmp_path, ipmi, *repositorio_local)
    assert not _posts_repositorio(bmc) and len(bmc.firmware) == 2
    assert any(t["caminho"] == "r" and "envio direto" in t["detalhe"] for t in resultado["tentativas"])


def test_interface_do_passthrough_ausente_nao_cria_a_tarefa(tmp_path, ipmi, repositorio_local):
    porta, racadm, _ = repositorio_local
    _, bmc, executor, resultado = _rodar_repositorio(tmp_path, ipmi, porta, racadm, [])  # sem a interface com IP
    assert not _posts_repositorio(bmc) and len(bmc.firmware) == 2  # seguiu pelo envio direto
    assert any("nenhuma interface deste servidor tem IP" in t["detalhe"] for t in resultado["tentativas"])
    assert all(m["situacao"] == temp.DESFEITA for m in resultado["temporarias"])


def test_idrac_sem_ping_firewall_bloqueando_nao_cria_a_tarefa(tmp_path, ipmi, repositorio_local, monkeypatch):
    monkeypatch.setattr(sim.ExecutorSimulador, "__init__", _com(sim.ExecutorSimulador.__init__, ping_ok=False))
    _, bmc, _, resultado = _rodar_repositorio(tmp_path, ipmi, *repositorio_local)
    assert not _posts_repositorio(bmc) and len(bmc.firmware) == 2
    assert any("não respondeu ao ping" in t["detalhe"] for t in resultado["tentativas"])


def test_interface_some_no_meio_cancela_e_troca_de_caminho(tmp_path, ipmi, repositorio_local, monkeypatch):
    monkeypatch.setattr(repositorio, "SEM_PEDIDO_S", 10**6)
    monkeypatch.setattr(sim.ExecutorSimulador, "__init__", _com(sim.ExecutorSimulador.__init__, sumir_interface_apos=3))
    _, bmc, executor, resultado = _rodar_repositorio(tmp_path, ipmi, *repositorio_local, nao_buscar=True)
    detalhes = " ".join(t["detalhe"] for t in resultado["tentativas"])
    assert "sumiu deste servidor" in detalhes and "cancelado no iDRAC" in detalhes, detalhes
    assert len(_posts_repositorio(bmc)) == 1 and len(bmc.firmware) == 2 and executor.reinicios == 1
    assert "sumiu" in resultado["acompanhamento"]["aviso_imediato"]  # o aviso continua na tela depois da troca


def test_job_que_mente_em_andamento_sem_progresso_em_5_min(tmp_path, ipmi, repositorio_local, monkeypatch):
    """O iDRAC diz "baixando" para sempre: 5 min sem nada mudar (nem pedido novo) → cancela e segue."""
    monkeypatch.setattr(repositorio, "SEM_PEDIDO_S", 10**6)
    monkeypatch.setattr(repositorio, "SEM_PROGRESSO_S", 0.0)
    _, bmc, executor, resultado = _rodar_repositorio(tmp_path, ipmi, *repositorio_local, nao_buscar=True)
    detalhes = " ".join(t["detalhe"] for t in resultado["tentativas"])
    assert "sem progresso real" in detalhes and "cancelado no iDRAC" in detalhes
    assert len(bmc.firmware) == 2 and not any(j["JobState"] == "Running" for j in bmc.jobs.values())


def test_idrac_recusa_cancelar_registra_e_ainda_desfaz_o_passthrough(tmp_path, ipmi, repositorio_local, monkeypatch):
    monkeypatch.setattr(repositorio, "SEM_PEDIDO_S", 0.0)
    raiz, bmc, executor, resultado = _rodar_repositorio(tmp_path, ipmi, *repositorio_local, nao_buscar=True, cancelar_falha=True)
    situacoes = {m["tipo"]: m for m in resultado["temporarias"]}
    assert situacoes[temp.TAREFA]["situacao"] == temp.NAO_DESFEITA and "Job Queue" in situacoes[temp.TAREFA]["detalhe"]
    assert situacoes[temp.PASSTHROUGH]["situacao"] == temp.DESFEITA  # o job travado é do MSscan e não grava firmware
    assert temp.ha_pendentes(str(raiz))  # o cancelamento é tentado de novo no próximo boot


def test_repositorio_pela_rede_local_preferido_ao_passthrough(tmp_path, ipmi, repositorio_local, monkeypatch):
    """22.2: iDRAC na mesma rede do servidor → repositório no IP do servidor nessa rede (no teste, loopback)."""
    monkeypatch.setattr(automatica, "_rede_local_valida", lambda ip: bool(ip))
    _, bmc, executor, resultado = _rodar_repositorio(tmp_path, ipmi, *repositorio_local)
    r = [t["detalhe"] for t in resultado["tentativas"] if t["caminho"] == "r"]
    assert any("pela rede local" in d for d in r) and resultado["situacao"] == "aguardando_reinicio", r
    assert [c["IPAddress"] for c in _posts_repositorio(bmc)] == ["127.0.0.4", "127.0.0.4"] and not bmc.firmware
    assert not any("pass-through" in d for d in r if "servido" in d)


def test_repositorio_online_pulado_sem_dns_no_idrac(tmp_path, ipmi, repositorio_local, monkeypatch):
    monkeypatch.setattr(repositorio, "SEM_PEDIDO_S", 0.0)
    _, bmc, _, resultado = _rodar_repositorio(tmp_path, ipmi, *repositorio_local, nao_buscar=True, dns=[])
    assert any(t["caminho"] == "o" and "sem DNS" in t["detalhe"] for t in resultado["tentativas"])
    assert not any(e[0] == "post" and e[2].get("ShareType") == "HTTPS" for e in bmc.eventos)


def test_proximo_boot_cancela_o_job_e_desliga_o_passthrough_mesmo_com_racadm_atrasado(tmp_path, ipmi):
    """22.4: desligado no meio — no boot seguinte o job do repositório sai da fila e o pass-through volta ao que era, mesmo
    com o racadm ainda sendo baixado (o desfazer espera o download em vez de desistir)."""
    import threading

    raiz = tmp_path / "MSSCAN"
    racadm = sim.criar_racadm(tmp_path / "vendor", "127.0.0.9", ipmi=ipmi)
    executor = sim.ExecutorSimulador(ipmi, tmp_path, racadm)
    ctx = _ctx(executor, racadm)
    registro = temp.Temporarias(str(raiz))
    automatica._ligar_passthrough(ctx, executor, str(raiz), registro, {"passos": []}, NADA)
    tarefa = registro.registrar(temp.TAREFA, "Job JID_000000000001 do repositório no iDRAC", {"job": "JID_000000000001"})
    registro.marcar(tarefa, temp.FEITA)
    (tmp_path / "vendor/racadm_jobs").write_text("[Job ID=JID_000000000001]\nJob Name=Repository Update\nStatus=Downloading\n"
                                                 "Message=[RED001: Downloading]\nPercent Complete=[10]\n")  # fmt: skip
    # "Boot seguinte": o racadm ainda não está pronto; fica pronto daqui a pouco.
    novo = _ctx(sim.ExecutorSimulador(ipmi, tmp_path, racadm))  # o executor aceitaria; o ctx ainda não tem o racadm
    pronto = threading.Event()
    novo.cache["racadm_pendente"] = pronto

    def terminar_download():
        novo.ferramentas_fabricante = {"racadm": str(racadm)}
        pronto.set()

    threading.Timer(0.3, terminar_download).start()
    mudancas = temp.desfazer_tudo(novo, novo.executor, str(raiz), dormir=NADA)
    assert {m["tipo"]: m["situacao"] for m in mudancas} == {temp.PASSTHROUGH: temp.DESFEITA, temp.TAREFA: temp.DESFEITA}, [m["detalhe"] for m in mudancas]
    assert "AdminState=Disabled" in (tmp_path / "vendor/racadm_estado").read_text()
    assert "jobqueue delete -i JID_000000000001" in (tmp_path / "vendor/racadm_chamadas").read_text()
    assert not temp.ha_pendentes(str(raiz))


def _com(init, **opcoes):
    def novo(self, *args, **kwargs):
        init(self, *args, **{**opcoes, **kwargs})

    return novo

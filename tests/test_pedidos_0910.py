"""Pedidos de 09/10/2026: pendentes da revisão independente (M2, M3, M5, B1 a B7), liberação total neste R640, instalação
com riscos de hardware assumidos e job órfão na fila do iDRAC. Nenhum teste toca no R640: fixtures, executores falsos e o
iDRAC falso de ``tests/simuladores.py``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from msscan.atualizacao import automatica, aviso, jobs, repositorio, temporarias as temp
from msscan.atualizacao.aviso import BLOQUEIA, HISTORICO, OK, REDUNDANCIA, TEXTO_ASSUMO
from tests import simuladores as sim
from tests.conftest import PASTA_FIXTURES, ExecutorFalso
from tests.test_ponta_a_ponta import HARDWARE_OK, SKU, _ctx, ipmi  # noqa: F401 - fixture

NADA = lambda _: None  # noqa: E731
USUARIOS = (PASTA_FIXTURES / "dell/poweredge_r640_idrac/ipmitool_user_list_1.txt").read_text()
SENHA = "Abcdefghij0123456789"


# --- Revisão de 06/10/2026: médios e baixos ------------------------------------------------------------------------------


class _Simulado(ExecutorFalso):
    """Executor falso que passa pelas travas (como os simuladores): só para conferir as salvaguardas do executor."""

    origem = "simulador"
    alvo_simulado = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rodados: list[list[str]] = []
        self.arquivos_exec: list[str] = []

    def _rodar(self, comando, timeout):
        self.rodados.append(list(comando))
        if comando[:2] == ["ipmitool", "exec"]:
            self.arquivos_exec.append(Path(comando[2]).read_text())
            return 0, "", "", 0.0
        return super()._rodar(comando, timeout)


def test_b4_nome_temporario_so_em_slot_vazio_relido_agora():
    executor = _Simulado({("ipmitool", "user", "list", "1"): (0, USUARIOS, "")})
    rc, _, motivo = executor.acao_comando(["ipmitool", "user", "set", "name", "3", "msscan1a2b"])
    assert rc != 0 and "não está vazio" in motivo  # slot 3: sem nome, mas ADMINISTRATOR (usuário existente)
    assert ["ipmitool", "user", "set", "name", "3", "msscan1a2b"] not in executor.rodados
    executor.comandos[("ipmitool", "user", "set", "name", "4", "msscan1a2b")] = (0, "", "")
    assert executor.acao_comando(["ipmitool", "user", "set", "name", "4", "msscan1a2b"])[0] == 0
    sem_lista = _Simulado()
    assert "não pôde ser lida" in sem_lista.acao_comando(["ipmitool", "user", "set", "name", "4", "msscan1a2b"])[2]


def test_b6_senha_temporaria_nunca_na_linha_de_comando():
    executor = _Simulado()
    executor.slots_temporarios[4] = "msscan1a2b"
    for comando in (["ipmitool", "user", "set", "password", "4", SENHA, "20"], ["racadm", "set", "iDRAC.Users.4.Password", SENHA]):
        executor.rodados.clear()
        assert executor.acao_comando(comando)[0] == 0
        rodado = executor.rodados[-1]
        assert rodado[:2] == ["ipmitool", "exec"] and SENHA not in " ".join(rodado)
        assert not Path(rodado[2]).exists()  # apagado logo depois
        assert executor.arquivos_exec[-1] == f"user set password 4 {SENHA} 20\n"
    assert SENHA not in str([e.comando for e in executor.historico])


def test_b5_copia_do_repositorio_conferida_tambem_com_md5(tmp_path):
    pacote = tmp_path / "Network_Firmware_X_WN64_1.0_A00.EXE"
    pacote.write_bytes(b"conteudo")
    item = {"componente": "Rede", "url": "https://downloads.dell.com/FOLDER1/1/" + pacote.name, "arquivo": str(pacote),
            "hash_conferido": ["md5", "0" * 32]}  # fmt: skip
    with pytest.raises(ValueError, match="MD5"):
        repositorio.montar(tmp_path / "MSSCAN", [item], b"", "downloads.dell.com", "0716")
    assert not list((tmp_path / "MSSCAN").rglob(pacote.name))  # cópia divergente apagada


def test_b1_sem_system_id_nenhum_formato_alternativo():
    catalogo = {"componentes": [{"caminho": "F/x.BIN", "versao": "2.0", "hash": "a" * 64, "algoritmo_hash": "SHA256",
                                 "sistemas": [{"system_id": "0716"}], "dispositivos": [{"component_id": "1"}]}]}  # fmt: skip
    registro = {"component_id": "1", "disponivel": "2.0"}
    assert automatica.outro_formato(registro, catalogo, None) is None
    assert automatica.outro_formato(registro, catalogo, "0716")["url"].endswith("F/x.BIN")
    assert automatica.outro_formato(registro, catalogo, "04CE") is None


def test_b3_racadm_le_o_estado_antes_de_apagar(monkeypatch):
    chamados = []
    executor = SimpleNamespace(acao_comando=lambda c: chamados.append(c) or (0, "", ""))
    ctx = SimpleNamespace(bmc=None, executor=None)
    monkeypatch.setattr(temp.idrac, "racadm", lambda _ctx: "/opt/racadm")
    gravando = {"id": "JID_123456", "nome": "Firmware Update: BIOS", "tipo": "", "estado": "Running"}
    monkeypatch.setattr(jobs, "ler_fila", lambda _ctx: {"origem": "racadm", "jobs": [gravando], "motivo": ""})
    ok, detalhe = temp._cancelar_tarefa(ctx, executor, {"antes": {"job": "JID_123456"}})
    assert not ok and "gravando firmware" in detalhe and chamados == []
    monkeypatch.setattr(jobs, "ler_fila", lambda _ctx: {"origem": "racadm", "jobs": [], "motivo": ""})
    assert temp._cancelar_tarefa(ctx, executor, {"antes": {"job": "JID_123456"}})[0] and chamados == []
    repo = {**gravando, "nome": "Repository Update"}
    monkeypatch.setattr(jobs, "ler_fila", lambda _ctx: {"origem": "racadm", "jobs": [repo], "motivo": ""})
    assert temp._cancelar_tarefa(ctx, executor, {"antes": {"job": "JID_123456"}})[0]
    assert chamados == [["/opt/racadm", "jobqueue", "delete", "-i", "JID_123456"]]


def test_b7_a_tarefa_comeca_dentro_da_trava(monkeypatch, tmp_path):
    from msscan.interface.sessao import Sessao

    sessao = Sessao(ExecutorFalso(), str(tmp_path), replay=True)
    dentro = []
    monkeypatch.setattr(sessao, "_em_segundo_plano", lambda alvo: dentro.append(sessao._trava._is_owned()))
    sessao._parado = {"situacao": "parado"}
    sessao.etapa = "concluido"
    assert sessao.continuar_parado()[0] and dentro == [True]


def test_b2_falha_declarada_pelo_idrac_encerra_o_item(tmp_path, ipmi):
    from tests.test_ponta_a_ponta_21 import _rodar, _tarefa, _verificacao

    falhou = _tarefa("Exception", "Failed", "SUP0518: Unable to update the firmware: package not applicable", 0)
    _, bmc, _, resultado = _rodar(tmp_path, ipmi, _verificacao(), ["item1"], roteiros=[[falhou]])
    assert len(bmc.firmware) == 1 and resultado["situacao"] == "falhou"
    caminhos = [t["caminho"] for t in resultado["tentativas"] if t["componente"] == "Rede X710"]
    assert caminhos[-1] in ("a", "b") and "d" not in caminhos and "e" not in caminhos  # nenhum outro caminho


def test_m2_nao_reinicia_com_job_em_andamento(tmp_path, monkeypatch):
    reinicios = []
    executor = SimpleNamespace(reiniciar_para_aplicar=lambda: reinicios.append(1) or (True, ""))
    monkeypatch.setattr(jobs, "esperar_sem_ativos", lambda *a, **k: (False, "JID_1 Firmware Update (Running)"))
    resultado = {"estado": {"versao": 1, "enviados": [], "pendentes": []}, "passos": []}
    automatica._reiniciar(SimpleNamespace(), executor, tmp_path, resultado, NADA)
    assert resultado["situacao"] == "aguardando_reinicio_manual" and reinicios == [] and "não foi reiniciado" in resultado["mensagem"]
    monkeypatch.setattr(jobs, "esperar_sem_ativos", lambda *a, **k: (True, ""))
    resultado = {"estado": {"versao": 1, "enviados": [], "pendentes": []}, "passos": []}
    automatica._reiniciar(SimpleNamespace(), executor, tmp_path, resultado, NADA)
    assert resultado["situacao"] == "aguardando_reinicio" and reinicios == [1]


@pytest.mark.parametrize(("estado_job", "desfecho"), [("Scheduled", "aguardando_reinicio"), ("Failed", "falhou")])
def test_m2_racadm_segue_o_job_e_nao_a_saida(tmp_path, ipmi, estado_job, desfecho):
    from msscan.atualizacao.aplicacao import MODO_APLICAR, executar_plano, montar_plano
    from tests.test_ponta_a_ponta import _pacotes, _verificacao

    raiz = tmp_path / "MSSCAN"
    _pacotes(raiz)
    racadm = sim.criar_racadm(tmp_path / "vendor", "127.0.0.9", capaz=False, ipmi=ipmi)
    (tmp_path / "vendor/racadm_estado_job").write_text(estado_job)
    executor = sim.ExecutorSimulador(ipmi, tmp_path, racadm)
    resultado = executar_plano(_ctx(executor, racadm), montar_plano(_verificacao(), ["item1"]), executor, str(raiz), MODO_APLICAR,
                               dormir=NADA)  # fmt: skip
    assert resultado["situacao"] == desfecho, resultado["mensagem"]
    assert any(t["caminho"] == "d" and "JID_" in t["detalhe"] for t in resultado["tentativas"])


def test_m5_retomada_para_com_condicao_nova_nao_aceita():
    redundancia = {"item": "Fontes", "situacao": REDUNDANCIA, "texto": "uma fonte só"}
    antigo = {"item": "Registro de eventos (eventos anteriores)", "situacao": HISTORICO, "texto": "1 evento: ECC"}
    agora = HARDWARE_OK[1:] + [redundancia, antigo]
    assert len(aviso.novos_na_volta(agora, [])) == 2  # nada disso foi aceito na confirmação
    aceitos = aviso.riscos_assumidos({"hardware": [redundancia, antigo]}, assumidos=False)["aceitos"]
    assert aviso.novos_na_volta(agora, aceitos) == []
    novo = {**antigo, "texto": "2 eventos: ECC; fonte"}  # evento crítico NOVO depois do reinício
    assert aviso.novos_na_volta(HARDWARE_OK[1:] + [redundancia, novo], aceitos) == ["Registro de eventos (eventos anteriores): 2 eventos: ECC; fonte"]
    assert aviso.novos_na_volta(agora, None) == []  # estado de versão anterior: só bloqueio crítico, como antes


# --- Configuração de memória calculada a cada diagnóstico ----------------------------------------------------------------


def _compat_r640():
    from msscan.compat.banco import carregar_banco, consultar

    entrada, _ = consultar(carregar_banco(), "Dell Inc.", "PowerEdge R640")
    return {"encontrado": True, "entrada": entrada}


def _medido(slots: list[str], cpus: int = 2) -> dict:
    return {"memoria": {"componente": "memoria", "status": "OK", "dados": {"modulos": [{"slot": s, "tamanho_mb": 8192} for s in slots]}},
            "cpu": {"componente": "cpu", "status": "OK", "dados": {"processadores": [{"soquete": f"CPU{n}", "populado": True,
                                                                                     "habilitado": True} for n in range(1, cpus + 1)]}}}  # fmt: skip


def test_memoria_mostra_os_slots_e_e_calculada_agora():
    compat = _compat_r640()
    so_cpu1 = aviso._montagem_memoria(_medido(["A1", "A2"]), compat)
    assert so_cpu1["situacao"] == BLOQUEIA and "CPU1: A1, A2 (16 GB)" in so_cpu1["texto"] and "CPU2: nenhum pente" in so_cpu1["texto"]
    equilibrada = aviso._montagem_memoria(_medido(["A1", "B1"]), compat)
    assert equilibrada["situacao"] == OK and "CPU2: B1 (8 GB)" in equilibrada["texto"]
    assert aviso._montagem_memoria(_medido(["A1"], cpus=1), compat)["situacao"] == OK  # single: válido
    linhas = aviso.avaliar_hardware({**{n: {"componente": n, "status": "OK", "dados": {}} for n in ("fontes", "raid", "sensores",
                                     "ventoinhas")}, **_medido(["A1", "A2"])}, compat)  # fmt: skip
    assert next(h for h in linhas if h["item"] == "Configuração de memória")["risco"].startswith("Configuração de memória fora")


def test_texto_fixo_da_memoria_saiu_da_liberacao():
    from msscan.atualizacao import liberacao

    fonte = Path(liberacao.__file__).read_text(encoding="utf-8")
    assert "CPU2 está sem memória" not in fonte and "CPU2 está sem memória" not in Path(aviso.__file__).read_text(encoding="utf-8")


# --- Riscos assumidos ----------------------------------------------------------------------------------------------------


def _linha(item, situacao=BLOQUEIA, texto="problema"):
    return {"item": item, "situacao": situacao, "texto": texto}


CHECAGENS = {
    "fonte com defeito": {"fontes": {"status": "FALHA", "dados": {"defeito": True}}},
    "fonte incerta": {"fontes": {"status": "ALERTA", "dados": {"classificacao_incerta": True}}},
    "erro de memória": {"memoria": {"status": "FALHA", "dados": {"edac": [{"nao_corrigiveis": 2}]}}},
    "memória não verificada": {"memoria": {"status": "NAO_VERIFICADO", "dados": {}}},
    "temperatura crítica": {"sensores": {"status": "FALHA", "mensagem": "CPU1 Temp 98 °C"}},
    "ventoinha crítica": {"ventoinhas": {"status": "FALHA", "mensagem": "Fan3 0 RPM"}},
    "RAID degradado": {"raid": {"status": "FALHA", "mensagem": "VD0 degradado"}},
}


@pytest.mark.parametrize("nome", list(CHECAGENS))
def test_cada_checagem_de_hardware_pode_ser_assumida(nome):
    medido = {n: {"componente": n, "status": "OK", "dados": {}} for n in ("fontes", "memoria", "raid", "sensores", "ventoinhas")}
    medido.update({k: {"componente": k, **v} for k, v in CHECAGENS[nome].items()})
    hardware = aviso.avaliar_hardware(medido)
    rede = {"id": "item1", "componente": "Rede X710", "categoria": "Network", "assinatura": "a"}
    plano = aviso.montar_aviso("catalogo", [rede], hardware, False, agora=1000.0)
    assert not plano["pode_seguir"] and plano["pode_assumir"] and plano["bloqueios_fixos"] == []
    assert all(r["risco"] and r["risco"] != r["texto"] for r in plano["riscos"])
    assert "assumindo os riscos" in aviso.conferir_aviso(plano, "catalogo", ["item1"], agora=1000.0)
    assert aviso.conferir_aviso(plano, "catalogo", ["item1"], agora=1000.0, confirmacao_riscos="assumo os riscos")
    assert aviso.conferir_aviso(plano, "catalogo", ["item1"], agora=1000.0, confirmacao_riscos=f" {TEXTO_ASSUMO} ") == ""
    registro = aviso.riscos_assumidos(plano, assumidos=True)
    assert registro["assumidos"] and registro["quando"] and [c["item"] for c in registro["checagens"]] == [r["item"] for r in plano["riscos"]]


def test_riscos_concretos_do_pedido():
    assert "travar durante a gravação do BIOS e não ligar mais" in aviso.RISCOS["Memória"]
    assert "queda de energia durante a gravação pode inutilizar o componente" in aviso.RISCOS["Fontes"]


@pytest.mark.parametrize("fixo", [
    "Rede X710: hash SHA-256 divergente do catálogo oficial",
    "Rede X710: pacote de outro modelo (systemID 04CE)",
    "Rede X710: versão do catálogo mais antiga que a instalada — nunca downgrade",
    "Há job(s) do iDRAC em andamento ou agendado(s) de uma sessão anterior",
    "Este servidor não está liberado para a gravação de firmware",
])
def test_bloqueios_que_continuam_valendo_com_riscos_assumidos(fixo):
    hardware = [_linha("Memória", texto="2 erros não corrigíveis")]
    plano = aviso.montar_aviso("catalogo", [{"id": "item1", "componente": "Rede X710", "categoria": "Network"}], hardware, False,
                               [fixo], agora=1000.0)  # fmt: skip
    assert not plano["pode_assumir"] and plano["bloqueios_fixos"] == [fixo]
    assert fixo in aviso.conferir_aviso(plano, "catalogo", ["item1"], agora=1000.0, confirmacao_riscos=TEXTO_ASSUMO)


def test_raid_degradado_bloqueia_so_firmware_de_raid_e_discos():
    hardware = [{**_linha("RAID", texto="VD0 degradado"), "risco": aviso.RISCOS["RAID"]}]
    perc = {"id": "item2", "componente": "PERC H730P", "categoria": "SAS RAID"}
    rede = {"id": "item1", "componente": "Rede X710", "categoria": "Network"}
    com_raid = aviso.montar_aviso("catalogo", [rede, perc], hardware, False, agora=1.0)
    assert not com_raid["pode_assumir"] and "perda de dados" in com_raid["bloqueios_fixos"][0] and "PERC H730P" in com_raid["bloqueios_fixos"][0]
    so_rede = aviso.montar_aviso("catalogo", [rede], hardware, False, agora=1.0)
    assert so_rede["pode_assumir"] and so_rede["bloqueios_fixos"] == []


def test_riscos_assumidos_no_estado_e_na_retomada():
    plano = aviso.montar_aviso("catalogo", [{"id": "i", "componente": "Rede", "categoria": "Network"}],
                               [_linha("Ventoinhas", texto="Fan3 0 RPM"), _linha("Fontes", REDUNDANCIA, "uma fonte")], False, agora=1.0)  # fmt: skip
    registro = aviso.riscos_assumidos(plano, assumidos=True)
    assert "Ventoinhas|bloqueia" in registro["aceitos"] and "Fontes|redundancia" in registro["aceitos"]
    # Na volta do reinício, a mesma ventoinha (já assumida) não para; uma temperatura crítica nova para.
    assert aviso.novos_na_volta([_linha("Ventoinhas", texto="Fan3 0 RPM")], registro["aceitos"]) == []
    assert aviso.novos_na_volta([_linha("Temperatura e sensores", texto="98 °C")], registro["aceitos"])
    estado = automatica._estado(SimpleNamespace(), {"riscos_assumidos": registro}, [], [], "agendado", "aplicar")
    assert estado["riscos_assumidos"]["checagens"][0]["item"] == "Ventoinhas"


# --- Job órfão na fila do iDRAC -------------------------------------------------------------------------------------------

ORFAO = {"id": "JID_913147016453", "nome": "Repository Update", "tipo": "RepositoryUpdate", "estado": "Running",
         "percentual": 0, "mensagem": "Downloading the Catalog.xml", "codigo": "RED110", "inicio": "2026-10-06T14:25:00",
         "grupo": "ativo", "texto": ""}  # fmt: skip


def test_job_de_repositorio_sem_repositorio_servido_e_orfao():
    resumo = jobs.marcar_orfaos({"jobs": [dict(ORFAO)]}, {}, 0.0, servindo_repositorio=False, primeira_leitura=True)
    job = resumo["jobs"][0]
    assert job["orfao"] and resumo["orfaos"] == 1 and "não está servindo" in job["motivo_orfao"]
    assert job["texto_orfao"] == "Tarefa travada de uma sessão anterior (desde 06/10/2026 14:25). Ela não será concluída automaticamente."
    assert "Limpar job travado" in resumo["bloqueio"]
    # Servindo um repositório (atualização desta sessão): só órfão se veio de antes do boot.
    vistos: dict = {}
    assert jobs.marcar_orfaos({"jobs": [dict(ORFAO)]}, vistos, 0.0, True, primeira_leitura=True)["jobs"][0]["orfao"]
    assert not jobs.marcar_orfaos({"jobs": [{**ORFAO, "id": "JID_1000001"}]}, {}, 0.0, True)["jobs"][0]["orfao"]


def test_job_de_firmware_so_e_orfao_sem_progresso_por_10_minutos():
    firmware = {**ORFAO, "id": "JID_200000001", "nome": "Firmware Update: BIOS", "tipo": "FirmwareUpdate", "percentual": 40}
    vistos: dict = {}
    assert not jobs.marcar_orfaos({"jobs": [dict(firmware)]}, vistos, 0.0, False, primeira_leitura=True)["jobs"][0]["orfao"]
    assert not jobs.marcar_orfaos({"jobs": [{**firmware, "percentual": 55}]}, vistos, 500.0, False)["jobs"][0]["orfao"]
    assert not jobs.marcar_orfaos({"jobs": [{**firmware, "percentual": 55}]}, vistos, 1000.0, False)["jobs"][0]["orfao"]
    parado = jobs.marcar_orfaos({"jobs": [{**firmware, "percentual": 55}]}, vistos, 1101.0, False)["jobs"][0]
    assert parado["orfao"] and "10 minutos" in parado["motivo_orfao"]
    agendado = {**firmware, "estado": "Scheduled", "grupo": "agendado"}
    assert not jobs.marcar_orfaos({"jobs": [agendado]}, {}, 99999.0, False)["jobs"][0]["orfao"]


def _bmc_com_orfao(tmp_path, ipmi, **opcoes):
    iniciais = [{"Id": ORFAO["id"], "Name": "Repository Update", "JobType": "RepositoryUpdate", "JobState": "Running",
                 "Message": "RED110: Downloading the Catalog.xml", "StartTime": "2026-10-06T14:25:00"}] + opcoes.pop("mais", [])  # fmt: skip
    return sim.BMCSimulado(tmp_path, ipmi, repositorio=False, jobs_iniciais=iniciais, **opcoes)


def _ctx_logado(tmp_path, ipmi, bmc):
    from msscan.deteccao.detectores import tentar_redfish

    executor = sim.ExecutorSimulador(ipmi, tmp_path)
    ctx = _ctx(executor)
    assert tentar_redfish(ctx, "127.0.0.1", bmc.server_address[1], "teste")
    ctx.bmc.tentar("root", sim.SENHA_ROOT)
    assert ctx.bmc.validado
    return ctx


def test_limpar_orfao_apaga_so_o_job(tmp_path, ipmi):
    with _bmc_com_orfao(tmp_path, ipmi) as bmc:
        ctx = _ctx_logado(tmp_path, ipmi, bmc)
        resumo = jobs.marcar_orfaos(jobs.resumir(jobs.ler_fila(ctx)), {}, 0.0, False, primeira_leitura=True)
        assert resumo["jobs"][0]["orfao"]
        registro = jobs.limpar_orfao(ctx, ORFAO["id"], False, NADA)
    assert registro["situacao"] == "limpo" and not registro["forcada"] and ("job_cancelado", ORFAO["id"]) in bmc.eventos
    assert ("fila_limpa_forcada",) not in bmc.eventos and registro["passos"] and registro["fim"]


def test_limpar_orfao_recusado_pede_confirmacao_e_depois_limpa_a_fila(tmp_path, ipmi):
    with _bmc_com_orfao(tmp_path, ipmi, cancelar_falha=True) as bmc:
        ctx = _ctx_logado(tmp_path, ipmi, bmc)
        primeiro = jobs.limpar_orfao(ctx, ORFAO["id"], False, NADA)
        assert primeiro["situacao"] == "precisa_forcar" and "fila inteira" in primeiro["mensagem"]
        assert ("fila_limpa_forcada",) not in bmc.eventos  # nunca sem a confirmação do técnico
        forcado = jobs.limpar_orfao(ctx, ORFAO["id"], True, NADA)
    assert forcado["situacao"] == "limpo" and forcado["forcada"] and ("fila_limpa_forcada",) in bmc.eventos
    assert ("idrac_reiniciou", 2) in bmc.eventos and "voltou a responder" in forcado["passos"][-1]["detalhe"]


def test_limpeza_forcada_nunca_com_firmware_gravando(tmp_path, ipmi):
    gravando = {"Name": "Firmware Update: BIOS", "JobType": "FirmwareUpdate", "JobState": "Running", "Message": "Updating"}
    with _bmc_com_orfao(tmp_path, ipmi, cancelar_falha=True, mais=[gravando]) as bmc:
        ctx = _ctx_logado(tmp_path, ipmi, bmc)
        registro = jobs.limpar_orfao(ctx, ORFAO["id"], True, NADA)
    assert registro["situacao"] == "recusado" and "sendo gravado" in registro["mensagem"]
    assert ("fila_limpa_forcada",) not in bmc.eventos


def test_limpeza_forcada_so_com_o_corpo_exato():
    from msscan.core.executor import CAMINHO_CANCELAR_JOB, LIMPAR_FILA_FORCADA, validar_escrita_bmc

    url = "https://127.0.0.1" + CAMINHO_CANCELAR_JOB
    assert validar_escrita_bmc(LIMPAR_FILA_FORCADA, url, {"JobID": "JID_CLEARALL_FORCE"}) == ""
    assert validar_escrita_bmc(LIMPAR_FILA_FORCADA, url, {"JobID": "JID_CLEARALL"})
    assert validar_escrita_bmc("cancelar_job", url, {"JobID": "JID_CLEARALL_FORCE"})  # o cancelar de um job continua só de um


def test_trava_continua_valendo_para_a_limpeza_forcada():
    from msscan.core.executor import CAMINHO_CANCELAR_JOB, LIMPAR_FILA_FORCADA, ExecutorReal

    resposta = ExecutorReal().bmc_escrita(LIMPAR_FILA_FORCADA, "https://127.0.0.1" + CAMINHO_CANCELAR_JOB, "t",
                                          {"JobID": "JID_CLEARALL_FORCE"})  # fmt: skip
    assert resposta.erro_tipo == "trava"



@pytest.mark.parametrize("hardware", [
    [{"item": "RAID", "situacao": aviso.AVISO, "texto": "RAID não verificado"}],  # PERC sem perccli e sem Redfish
    [{"item": "Hardware", "situacao": aviso.BLOQUEIA, "texto": "releitura falhou", "risco": "x"}],  # sem linha "RAID"
    [{"item": "RAID", "situacao": aviso.BLOQUEIA, "texto": "degradado", "risco": "x"}],
])
def test_firmware_de_armazenamento_so_com_raid_conferido(hardware):
    """Revisão de 13/10/2026 (C1): firmware de RAID/disco com o RAID não conferido agora é bloqueio fixo (nada libera)."""
    disco = {"id": "d1", "componente": "Disco SAS 1,2 TB", "categoria": "Serial ATA", "assinatura": "a"}
    rede = {"id": "r1", "componente": "Rede X710", "categoria": "Network", "assinatura": "b"}
    plano = aviso.montar_aviso("catalogo", [disco, rede], hardware, False, agora=1000.0)
    assert plano["bloqueios_fixos"] and not plano["pode_assumir"] and "Disco SAS" in plano["bloqueios_fixos"][0]
    assert aviso.conferir_aviso(plano, "catalogo", ["d1", "r1"], agora=1000.0, confirmacao_riscos=TEXTO_ASSUMO)
    sem_disco = aviso.montar_aviso("catalogo", [rede], hardware, False, agora=1000.0)
    assert not any("Rede X710" in b for b in sem_disco["bloqueios_fixos"])
    ok = aviso.montar_aviso("catalogo", [disco], [{"item": "RAID", "situacao": aviso.OK, "texto": "sem RAID de hardware"}], False, agora=1000.0)
    assert ok["bloqueios_fixos"] == []

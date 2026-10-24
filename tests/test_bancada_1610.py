"""Correções dos relatórios da bancada de 16/10/2026 (R620, T620, R530, R430 com a 1.0.5).

Casos montados a partir dos relatórios reais (só os campos relevantes, sem serial/IP do cliente).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from msscan.atualizacao.automatica import explicar_falta
from msscan.core.executor import _escrita_racadm, caminho_curto_racadm
from msscan.core.fallback import FalhaColeta
from msscan.core.modelo import Status
from msscan.deteccao.contexto import Contexto, Controladora
from msscan.localizacao import localizar_relatorio
from msscan.provedores.raid import ProvedorMdadm
from msscan.provedores.sistema import avaliar_cpu
from msscan.provedores.smart import ProvedorSmartctl, consolidar_discos
from msscan.relatorio.html import gerar_html
from tests.conftest import ExecutorFalso

H310 = Controladora(slot="02:00.0", classe="0104", nome="Dell PERC H310 Adapter", descricao="Broadcom / LSI MegaRAID SAS 2008 [Falcon]",
                    driver=None, categoria="sem_driver", fabricante_id="1000", dispositivo_id="0073", subsistema_ids="1028:1f4e")  # fmt: skip


# --- R620: racadm update com RAC923 (caminho de 64+ caracteres) ---------------------------------------------------------


def test_caminho_curto_racadm(tmp_path):
    curto = "/m/MSSCAN/firmware/x.EXE"
    assert caminho_curto_racadm(curto) == curto
    pacote = tmp_path / ("MSSCAN/firmware/pacotes/" + "iDRAC-with-Lifecycle-Controller_Firmware_X_WN64_2.65.65.65_A00.EXE")
    pacote.parent.mkdir(parents=True)
    pacote.write_bytes(b"x")
    link = caminho_curto_racadm(str(pacote))
    assert len(link) < 64 and link.endswith("fw.EXE") and "firmware" in Path(link).parts
    assert Path(link).resolve() == pacote.resolve()


def test_caminho_g_mostra_a_recusa_do_racadm():
    resultado = {"tentativas": [{"componente": "iDRAC (BMC)", "caminho": "d", "situacao": "falhou",
                                 "detalhe": "racadm terminou com 4: ERROR: RAC923 : The file path is too long."}]}  # fmt: skip
    texto = explicar_falta(Contexto(executor=ExecutorFalso()), {}, resultado, "iDRAC (BMC)")
    assert texto.split(": ", 1)[1].startswith("o racadm local recusou o pacote (racadm terminou com 4: ERROR: RAC923"), texto


@pytest.mark.parametrize(("args", "escrita"), [
    (["storage", "get", "pdisks", "-o"], False), (["raid", "get", "vdisks", "-o"], False), (["swinventory"], False),
    (["hwinventory"], False), (["storage", "createvd:RAID.Integrated.1-1"], True), (["hwinventory", "export", "-f", "x"], True),
    (["storage", "get", "pdisks"], True), (["raid", "deletevd:Disk.Virtual.0"], True),
])  # fmt: skip
def test_racadm_so_leituras_exatas(args, escrita):
    assert _escrita_racadm(["/opt/dell/srvadmin/sbin/racadm", *args]) is escrita


# --- T620: PERC H310 sem driver ---------------------------------------------------------------------------------------


def test_raid_com_controladora_sem_driver_nao_e_nao_aplicavel():
    ctx = Contexto(executor=ExecutorFalso(), controladoras=[H310])
    with pytest.raises(FalhaColeta, match="PERC H310 Adapter.*sem driver"):
        ProvedorMdadm().coletar(ctx)


def test_discos_sem_nada_no_scan_dizem_o_motivo():
    ex = ExecutorFalso({("smartctl", "--scan-open", "-j"): (0, '{"devices": []}', "")})
    with pytest.raises(FalhaColeta, match="sem driver"):
        ProvedorSmartctl().coletar(Contexto(executor=ex, controladoras=[H310]))
    with pytest.raises(FalhaColeta, match="sem discos instalados"):
        ProvedorSmartctl().coletar(Contexto(executor=ex))


# --- R620: disco com defeitos e disco que não respondeu atrás da PERC ------------------------------------------------


def test_disco_atras_da_perc_identificado():
    discos = [{"dispositivo": "/dev/bus/0", "tipo": "megaraid,0", "modelo": "SEAGATE ST9300653SS", "serial": "S1", "status": "ALERTA",
               "problemas": ["SAS: 177 defeito(s) crescido(s)"]},
              {"dispositivo": "/dev/bus/0", "tipo": "megaraid,1", "status": "NAO_VERIFICADO", "problemas": ["erro ao abrir: INQUIRY failed"]}]  # fmt: skip
    r = consolidar_discos(discos, [])
    assert r.dados["problemas"][0] == "disco ID 0 da controladora (SEAGATE ST9300653SS, S1): SAS: 177 defeito(s) crescido(s)"
    assert r.dados["problemas"][1].startswith("disco ID 1 da controladora: ")


# --- R620: CPU2 "Populated, Idle" com as duas CPUs em uso -------------------------------------------------------------


def _cpu(soquete, status):
    return {"soquete": soquete, "status": status, "populado": True, "habilitado": status == "Populated, Enabled",
            "modelo": "Intel(R) Xeon(R) CPU E5-2620 0 @ 2.00GHz", "fabricante": "Intel", "nucleos": 6, "nucleos_habilitados": 6, "threads": 12}  # fmt: skip


def test_cpu_idle_em_uso_nao_e_alerta():
    cpus = [_cpu("CPU1", "Populated, Enabled"), _cpu("CPU2", "Populated, Idle")]
    r = avaliar_cpu(cpus, {"processadores_logicos": 24, "soquetes": 2, "modelos": [cpus[0]["modelo"]]}, [])
    assert r.status == Status.OK and r.mensagem.startswith("2 processador(es)")
    assert any("CPU2" in o and "não é defeito" in o for o in r.dados["observacoes"])
    r = avaliar_cpu(cpus, {"processadores_logicos": 12, "soquetes": 1, "modelos": [cpus[0]["modelo"]]}, [])
    assert r.status == Status.ALERTA  # o sistema não usa a CPU2: continua alerta


# --- R430: o mesmo evento em dois componentes e a data da última ocorrência ------------------------------------------


def test_localizacao_sem_repetir_evento_e_com_a_ultima_ocorrencia():
    cabo = "Cable / Interconnect Intrusion Cable — Config Error"
    relatorio = {"equipamento": {"fabricante": "Dell Inc."}, "medido": [
        {"componente": "log_bmc", "status": "ALERTA", "dados": {"eventos_problema": [
            {"data": "08/04/26", "hora": "21:57:21 -03", "sensor": "Cable / Interconnect Intrusion Cable", "evento": "Config Error"},
            {"data": "10/16/26", "hora": "11:59:07 -03", "sensor": "Cable / Interconnect Intrusion Cable", "evento": "Config Error"},
            {"data": "11/13/25", "hora": "11:36:08 -03", "sensor": "Power Supply PS Redundancy", "evento": "Redundancy Lost"},
            {"data": "10/16/26", "hora": "11:45:03 -03", "sensor": "Power Supply PS Redundancy", "evento": "Redundancy Lost"}]}},
        {"componente": "internos", "status": "ALERTA", "dados": {"eventos_problema": [
            {"data": "10/16/26", "hora": "11:59:07 -03", "sensor": "Cable / Interconnect Intrusion Cable", "evento": "Config Error"}]}},
    ]}  # fmt: skip
    itens = localizar_relatorio(relatorio)["itens"]
    assert [i["componente"] for i in itens if cabo in i["problema"]] == ["internos"]
    fonte = next(i for i in itens if "Redundancy Lost" in i["problema"])
    assert fonte["ocorrencias"] == 2 and "10/16/26" in fonte["problema"]


# --- Todos: o relatório traz a caixa "Acesso ao iDRAC" ----------------------------------------------------------------


def test_relatorio_html_com_acesso_ao_idrac():
    html = gerar_html({"acesso_idrac": {"titulo": "iDRAC acessível só por dentro do servidor", "racadm_texto": "pronto",
                                        "mensagens": [{"nivel": "alerta", "texto": "O iDRAC está em outra rede: <x>"}]}})  # fmt: skip
    assert "Acesso ao iDRAC: iDRAC acessível só por dentro do servidor" in html and "&lt;x&gt;" in html


# --- R430 (iDRAC8 2.86, Redfish 1.4.0): login dava HTTP 405 --------------------------------------
# A raiz real anuncia ``Links.Sessions = /redfish/v1/Sessions`` (coleta de 16/10/2026); o POST em
# ``/redfish/v1/SessionService/Sessions`` devolve 405 nesse iDRAC.


def _bmc_idrac8(raiz_anuncia: bool):
    from msscan.core.executor import RespostaRedfish
    from tests.conftest import ExecutorFalso

    base, executor = "https://b", ExecutorFalso()
    raiz = {"@odata.id": "/redfish/v1", "RedfishVersion": "1.4.0"}
    if raiz_anuncia:
        raiz["Links"] = {"Sessions": {"@odata.id": "/redfish/v1/Sessions"}}
    executor.respostas_redfish[("GET", base + "/redfish/v1")] = RespostaRedfish(base + "/redfish/v1", 200, corpo=raiz)
    norma, idrac = base + "/redfish/v1/SessionService/Sessions", base + "/redfish/v1/Sessions"
    executor.respostas_redfish[("POST", norma)] = RespostaRedfish(norma, 405)
    executor.respostas_redfish[("POST", idrac)] = RespostaRedfish(idrac, 201, token="t", local="/redfish/v1/Sessions/7")
    executor.respostas_redfish[("DELETE", idrac + "/7")] = RespostaRedfish(idrac + "/7", 200)
    return executor, base


@pytest.mark.parametrize("raiz_anuncia", [True, False])
def test_login_idrac8_sessoes_fora_do_session_service(raiz_anuncia):
    from msscan.redfish.cliente import ClienteRedfish

    executor, base = _bmc_idrac8(raiz_anuncia)
    cliente = ClienteRedfish(executor, base)
    cliente.raiz()
    assert cliente.login("root", "x").ok and cliente.caminho_sessoes == "/redfish/v1/Sessions"
    assert cliente.logout()  # DELETE na sessão do iDRAC8 passa pela salvaguarda
    posts = [e.comando[1] for e in executor.historico if e.comando[0] == "POST"]
    assert posts == ([base + "/redfish/v1/Sessions"] if raiz_anuncia else
                     [base + "/redfish/v1/SessionService/Sessions", base + "/redfish/v1/Sessions"])  # fmt: skip


def test_erro_que_nao_e_de_senha_nao_gasta_tentativa():
    from msscan.core.executor import RespostaRedfish
    from msscan.redfish.acesso import MAX_TENTATIVAS, AcessoBMC
    from msscan.redfish.cliente import ClienteRedfish
    from tests.conftest import ExecutorFalso

    executor = ExecutorFalso()
    for caminho in ("/redfish/v1/SessionService/Sessions", "/redfish/v1/Sessions"):
        executor.respostas_redfish[("POST", "https://b" + caminho)] = RespostaRedfish("https://b" + caminho, 405)
    acesso = AcessoBMC(ClienteRedfish(executor, "https://b"))
    for _ in range(MAX_TENTATIVAS + 1):
        assert "não conta como tentativa" in acesso.tentar("root", "x")
    assert acesso.tentativas == 0 and acesso.pode_tentar


def test_dup_roda_com_term_de_terminal_de_texto(tmp_path):
    # R620 (16/10/2026): o .BIN da Dell sem tty e com TERM=linux/xterm faz "exec xterm" (ausente na ISO) → 127.
    from msscan.core.executor import ExecutorReal

    falso = tmp_path / "x.BIN"
    falso.write_text('case $TERM in vt100*) echo ok ;; *) exec xterm-inexistente ;; esac\n')
    assert ExecutorReal().rodar(["bash", str(falso), "-c"])[:2] == (0, "ok\n")


# --- R620 (1.0.7): iDRAC7 1.40.40 recusou o .EXE no racadm (RAC989) → imagem firmimg.d7 do mesmo .EXE -----------------


def test_imagem_nativa_do_exe_e_racadm_tenta_de_novo(tmp_path, monkeypatch):
    import zipfile

    from msscan.atualizacao import automatica
    from msscan.core.executor import imagem_nativa_racadm, validar_atualizacao_racadm

    exe = tmp_path / "iDRAC_WN64_2.65.65.65_A00.EXE"
    with zipfile.ZipFile(exe, "w") as z:
        z.writestr("bmcfw64.exe", b"win")
        z.writestr("payload/firmimg.d7", b"IMAGEM")
    imagem = imagem_nativa_racadm(str(exe))
    assert imagem and Path(imagem).read_bytes() == b"IMAGEM" and len(imagem) < 64
    assert validar_atualizacao_racadm(["/opt/dell/srvadmin/sbin/racadm", "update", "-f", imagem]) == ""
    sem = tmp_path / "BIOS_WN64.EXE"
    with zipfile.ZipFile(sem, "w") as z:
        z.writestr("bios.hdr", b"x")
    assert imagem_nativa_racadm(str(sem)) is None and imagem_nativa_racadm(str(tmp_path / "nao_existe.EXE")) is None

    enviados = []

    class Exec:
        def atualizar_racadm(self, racadm, arquivo):
            enviados.append(Path(arquivo).name)
            if arquivo.endswith(".EXE"):
                return 62, "", "ERROR: RAC989: Unable to apply the fw.EXE firmware update. The file used for the operation is invalid."
            return 0, "RAC987: Firmware update job for firmimg.d7 is initiated.", ""

    monkeypatch.setattr(automatica, "_esperar_racadm", lambda ctx, resultado: None)
    monkeypatch.setattr(automatica.idrac, "racadm", lambda ctx: "/opt/dell/srvadmin/sbin/racadm")
    monkeypatch.setattr(automatica, "_ids_na_fila", lambda ctx: set())
    monkeypatch.setattr(automatica, "_acompanhar_job_racadm", lambda *a, **k: automatica.APLICADO)
    resultado: dict = {"passos": [], "tentativas": []}
    registro = {"componente": "iDRAC (BMC)", "arquivo": str(exe)}
    assert automatica._racadm(None, Exec(), registro, resultado) == automatica.APLICADO
    assert enviados == [exe.name, "firmimg.d7"]

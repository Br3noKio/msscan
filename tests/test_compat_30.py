"""Mecânicas por geração (CLAUDE.md 30): achados da coleta REAL do R620 (12ª, iDRAC7 2.65.65.65, PERC H710 Mini).

A fixture ``tests/fixtures/dell/poweredge_r620`` é a coleta da bancada de 19/10/2026, anonimizada. Os relatórios da mesma
bancada com credenciais (Redfish) são a referência do que o iDRAC7 diz sobre o mesmo servidor: RAID 5 "Virtual Disk 0"
com saúde Critical, "The PERC1 battery is low" (Warning) e "Drive 0/1 is removed from disk drive bay 1" (Critical).
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tarfile
from pathlib import Path

from main import main
from msscan.coleta.coletor import coletar
from msscan.core.executor import RACADM_LEITURA_EXATA, ExecutorReplay, nome_arquivo_comando
from msscan.deteccao import idrac_tools
from tests.conftest import R620


def _copia_r620(destino: Path) -> Path:
    pasta = destino / "poweredge_r620"
    shutil.copytree(R620, pasta)
    return pasta


def _gravar_comando(pasta: Path, comando: list[str], saida: str, rc: int = 0) -> None:
    nome = nome_arquivo_comando(comando)
    (pasta / "comandos" / f"{nome}.json").write_text(json.dumps({"comando": comando, "rc": rc, "duracao": 0.1}), encoding="utf-8")
    (pasta / "comandos" / f"{nome}.stdout").write_text(saida, encoding="utf-8")


def test_coleta_usa_o_racadm_ja_preparado_na_sessao(tmp_path) -> None:
    """Bancada de 19/10: o racadm respondia na sessão, mas a coleta (que não prepara nada) não o via."""
    pasta = _copia_r620(tmp_path)
    caminhos = json.loads((pasta / "caminhos.json").read_text())
    caminhos[idrac_tools.RACADM] = True
    (pasta / "caminhos.json").write_text(json.dumps(caminhos), encoding="utf-8")
    _gravar_comando(pasta, [idrac_tools.RACADM, "getversion"], " iDRAC Version                    = 2.65.65.65\n")

    pacote = coletar(tmp_path / "saida", base=ExecutorReplay(pasta))
    with tarfile.open(pacote) as tar:
        nomes = {Path(n).name for n in tar.getnames()}
    for args in RACADM_LEITURA_EXATA:
        assert f"{nome_arquivo_comando([idrac_tools.RACADM, *args])}.json" in nomes, args


def test_coleta_sem_racadm_continua_sem_racadm(tmp_path) -> None:
    """Sem o racadm preparado, a coleta não tenta prepará-lo (modo sem alterações) e não roda racadm nenhum."""
    pacote = coletar(tmp_path / "saida", base=ExecutorReplay(R620))
    with tarfile.open(pacote) as tar:
        assert not [n for n in tar.getnames() if "racadm" in Path(n).name]


# --- RAID pelos sensores do BMC (baias e bateria da controladora) ------------------------------------------------------

import pytest  # noqa: E402

from msscan.core.modelo import Status  # noqa: E402
from msscan.deteccao.detectores import detectar  # noqa: E402
from msscan.modulos.registro import registro_padrao  # noqa: E402
from msscan.parsers.ipmitool import parse_sel_elist, parse_sensor  # noqa: E402
from msscan.provedores.comum import evento_sel_e_problema  # noqa: E402
from msscan.provedores.raid import ProvedorRaidSensoresBMC, avaliar_sensores_bmc_raid, bits_discretos  # noqa: E402
from tests.conftest import R640, ler_saida  # noqa: E402


def _diagnostico(pasta: Path) -> dict:
    ctx = detectar(ExecutorReplay(pasta))
    return {r.componente: r for r in registro_padrao().diagnosticar(ctx)}


@pytest.mark.parametrize("estado, bits", [
    ("0x4180", {0, 6}),  # R620 real: Drive 0 presente e em volume falho
    ("0x0180", {0}),     # R640 real: Drive 0 presente (o "sdr type" do mesmo BMC diz "Drive Present")
    ("0x8080", {7}),     # R640 real: CPU Status (tipo 07h, bit 7 = "Presence detected" no "sdr type")
    ("0x0080", set()),   # R620 real: ROMB Battery sem estado ativo
    ("0x0181", {0, 8}),  # 2º byte: estados 8–14 (Rebuild Aborted)
    ("na", None), ("0x1", None), ("", None),
])
def test_bits_dos_sensores_discretos(estado, bits) -> None:
    assert bits_discretos(estado) == bits


def test_r620_raid_falho_sem_credenciais_pelo_bmc() -> None:
    """Sem credenciais nem perccli, o RAID do R620 era "não verificado"; o Redfish do mesmo iDRAC (com credenciais, mesma
    bancada) dizia "Virtual Disk 0 (StripedWithParity): saúde Critical". O sensor do BMC dá o mesmo veredito."""
    raid = _diagnostico(R620)["raid"]
    assert raid.status == Status.FALHA and raid.fonte.startswith("ipmitool sensor")
    assert raid.dados["problemas"] == ["baia de disco (Drive 0): In Failed Array — leitura atual do BMC"]
    assert raid.dados["volumes_verificados"] is False and "credenciais do iDRAC" in raid.mensagem


def test_r640_sem_raid_de_hardware_nao_muda() -> None:
    """R640 da fixture: sem controladora RAID de hardware → o método nem se aplica e o resultado continua o de antes."""
    raid = _diagnostico(R640)["raid"]
    assert raid.status == Status.NAO_APLICAVEL
    tentativa = next(t for t in raid.dados["tentativas"] if t["provedor"] == ProvedorRaidSensoresBMC.nome)
    assert tentativa["resultado"] == "indisponível"


def test_so_presenca_nunca_vira_ok() -> None:
    """Linha real do R640 ("Drive Present"): não prova que os volumes estão bons → NAO_VERIFICADO com o que foi lido."""
    linha = next(l for l in ler_saida(R640, "ipmitool__sensor").splitlines() if l.startswith("Drive 0"))
    r = avaliar_sensores_bmc_raid(parse_sensor(linha))
    assert r.status == Status.NAO_VERIFICADO and "não informa o estado dos volumes" in r.mensagem
    assert r.dados["sensores_bmc"] == [{"sensor": "Drive 0", "estado": "0x0180", "estados": ["Drive Present"]}]


def _linha(nome: str, estado: str) -> str:
    return f"{nome:<17}| 0x{estado[2:4]:<9}| discrete   | {estado}| na        | na        | na        | na        | na        | na        "


@pytest.mark.parametrize("estado, status, texto", [
    ("0x0380", Status.FALHA, "Drive Fault"),
    ("0x0580", Status.ALERTA, "Predictive Failure"),
    ("0x2180", Status.FALHA, "In Critical Array"),
    ("0x8180", Status.ALERTA, "Rebuild In Progress"),
    ("0x0181", Status.ALERTA, "Rebuild Aborted"),
])
def test_cada_estado_de_baia(estado, status, texto) -> None:
    r = avaliar_sensores_bmc_raid(parse_sensor(_linha("Drive 0", estado)))
    assert r.status == status and texto in r.dados["problemas"][0]


def test_hot_spare_e_paridade_nao_sao_problema() -> None:
    r = avaliar_sensores_bmc_raid(parse_sensor(_linha("Drive 0", "0x1980")))  # presente + hot spare + parity check
    assert r.status == Status.NAO_VERIFICADO and r.dados["sensores_bmc"][0]["estados"] == [
        "Drive Present", "Hot Spare", "Parity Check In Progress"]


def test_bateria_da_controladora_ao_vivo() -> None:
    assert avaliar_sensores_bmc_raid(parse_sensor(_linha("ROMB Battery", "0x0180"))).status == Status.ALERTA  # Low
    assert avaliar_sensores_bmc_raid(parse_sensor(_linha("ROMB Battery", "0x0280"))).status == Status.ALERTA  # Failed
    assert avaliar_sensores_bmc_raid(parse_sensor(_linha("ROMB Battery", "0x0480"))).status == Status.NAO_VERIFICADO


def test_sensores_de_outros_nomes_sao_ignorados() -> None:
    """Só "Drive N" e "ROMB Battery" (nomes reais no R620 e no R640): "Status", "CMOS Battery" etc. não são da RAID."""
    r = avaliar_sensores_bmc_raid(parse_sensor(_linha("Status", "0x0380") + "\n" + _linha("CMOS Battery", "0x0180")))
    assert r.status == Status.NAO_VERIFICADO and r.dados["sensores_bmc"] == []


def test_provedor_so_com_raid_de_hardware_e_ipmi() -> None:
    ctx = detectar(ExecutorReplay(R620))
    assert ProvedorRaidSensoresBMC().disponivel(ctx).ok
    ctx.ipmi.disponivel = False
    assert not ProvedorRaidSensoresBMC().disponivel(ctx).ok
    ctx = detectar(ExecutorReplay(R620))
    ctx.controladoras = []
    assert not ProvedorRaidSensoresBMC().disponivel(ctx).ok


# --- SEL: bateria "Low" ------------------------------------------------------------------------------------------------


def test_bateria_low_do_sel_conta_como_problema() -> None:
    """R620: "ROMB Battery Low" 7x em 19/10 (o iDRAC7 diz pelo Redfish "The PERC1 battery is low", Warning)."""
    eventos = parse_sel_elist(ler_saida(R620, "ipmitool__sel__elist"))
    low = [e for e in eventos if e["tipo"] == "Battery" and e["evento"] == "Low"]
    assert sum(evento_sel_e_problema(e) for e in low) == 7
    assert not any(evento_sel_e_problema(e) for e in low if e["direcao"] == "Deasserted")
    log = _diagnostico(R620)["log_bmc"]
    assert any("ROMB Battery — Low (Asserted) — 7 ocorrências" in p for p in log.dados["problemas"])


def test_disco_removido_continua_fora_do_log_de_problemas() -> None:
    """Decisão mantida: "Drive Present" deasserted sem saber se voltou à baia é tanto troca quanto defeito."""
    eventos = parse_sel_elist(ler_saida(R620, "ipmitool__sel__elist"))
    assert not any(evento_sel_e_problema(e) for e in eventos if e["tipo"].startswith("Drive Slot"))


def test_raid_falho_pelo_bmc_bloqueia_como_hardware_assumivel() -> None:
    """Mesmo contrato do Redfish: RAID FALHA → bloqueio de hardware (o técnico pode assumir, CLAUDE.md 25), e o firmware
    da PERC deixa de ser recusado por "RAID desconhecido"."""
    from msscan.atualizacao.verificacao import prechecagens

    relatorio = {"medido": [r.para_dict() for r in _diagnostico(R620).values()]}
    checagens = prechecagens(relatorio, None)
    assert any("Drive 0" in b for b in checagens["bloqueios_hardware"])
    assert checagens["por_componente"]["raid"] == [] and checagens["bloqueios"] == []


# --- Log do BMC pelo Redfish do iDRAC7: só os 50 mais recentes ---------------------------------------------------------

R620_IDRAC = R620.parent / "poweredge_r620_idrac"


def test_log_redfish_parcial_do_idrac7_completado_pelo_sel_ipmi() -> None:
    """Entradas REAIS do Redfish do R620 (50, IDs 1024–975) + SEL REAL do IPMI do mesmo dia (1024 eventos)."""
    from msscan.provedores.redfish import avaliar_log_redfish, complementar_com_sel_ipmi

    entradas = json.loads((R620_IDRAC / "log_sel_redfish_entradas.json").read_text())["entradas"]
    sel = parse_sel_elist(ler_saida(R620, "ipmitool__sel__elist"))
    assert sel[-1]["evento"] == "Fully Redundant" and entradas[0]["mensagem"] == "The power supplies are redundant."

    r = complementar_com_sel_ipmi(avaliar_log_redfish(entradas, "Sel"), sel)
    assert r.dados["complemento_ipmi"]["eventos_redfish"] == 50 and r.dados["complemento_ipmi"]["eventos_ipmi"] == 1024
    assert "entregou só os 50 evento(s) mais recentes de 1024" in r.mensagem and r.fonte.endswith("+ ipmitool sel elist")
    antigos = r.dados["complemento_ipmi"]["problemas"]
    assert antigos and all(p.endswith("(evento mais antigo, só pelo IPMI)") for p in antigos)
    assert any("ROMB Battery — Failed" in p for p in antigos)  # 2018: só no registro completo
    assert r.status == Status.ALERTA


def test_log_redfish_completo_nao_muda() -> None:
    from msscan.provedores.redfish import avaliar_log_redfish, complementar_com_sel_ipmi

    entradas = json.loads((R620_IDRAC / "log_sel_redfish_entradas.json").read_text())["entradas"]
    sel = parse_sel_elist(ler_saida(R620, "ipmitool__sel__elist"))[-50:]
    for ipmi in (sel, sel[:10], None):
        r = complementar_com_sel_ipmi(avaliar_log_redfish(entradas, "Sel"), ipmi)
        assert "complemento_ipmi" not in r.dados and r.fonte == "Redfish (LogServices/Sel)"


# --- Log técnico e IA: versões de firmware no formato de IPv4 --------------------------------------------------------


def test_sanitizador_mantem_versoes_e_continua_tirando_ips() -> None:
    """Bancada de 19/10: o log técnico do R620 escondia as versões ("instalada": "[IP-3]", "racadm pronto (versão [IP-6])")."""
    from msscan.ia.sanitizar import Sanitizador

    s = Sanitizador().dados({
        "instalada": "2.65.65.65", "disponivel": "2.65.65.65", "ip": "192.168.0.120",
        "racadm_texto": "pronto (versão 11.4.1.0)", "nome": "Lifecycle Controller v. 2.65.65.65",
        "comando": ["GET", "https://192.168.0.120/redfish/v1/UpdateService/FirmwareInventory/Installed-25227-2.65.65.65"],
        "pacote": "Dell-iDRACTools-Web-LX-11.4.1.0-1685_A00.tar.gz", "componente": "OS Drivers Pack, 7.6.0.5, A00",
        "url": "https://dl.dell.com/x/iDRAC-with-Lifecycle-Controller_Firmware_0GHF4_WN64_2.65.65.65_A00.EXE",
        "livre": "o iDRAC 10.0.0.5 respondeu; gateway 10.0.0.1",
    })
    assert s["instalada"] == s["disponivel"] == "2.65.65.65" and s["racadm_texto"] == "pronto (versão 11.4.1.0)"
    assert s["comando"][1] == "https://[IP-1]/redfish/v1/UpdateService/FirmwareInventory/Installed-25227-2.65.65.65"
    assert "LX-11.4.1.0-1685" in s["pacote"] and "Pack, 7.6.0.5" in s["componente"] and "_WN64_2.65.65.65_" in s["url"]
    assert s["nome"].endswith("v. 2.65.65.65")
    assert s["ip"] == "[IP-1]" and s["livre"] == "o iDRAC [IP-2] respondeu; gateway [IP-3]"


# --- Slots de memória no formato da 12ª geração ("DIMM_A1") -------------------------------------------------------------


@pytest.mark.parametrize("texto, slot", [
    ("DIMM_A2", "A2"),  # dmidecode do R620 (12ª)
    ("A2", "A2"),  # dmidecode do R640 (14ª)
    ("Uncorrectable ECC (UnCorrectable ECC |  DIMMA2)", "A2"),  # SEL do R640
    ("DIMM.Socket.B11", "B11"),  # FQDD do Redfish
    ("DIMM A2", "A2"), ("Fan1A", None), ("CPU1", None), ("", None),
])
def test_slot_de_memoria_em_todas_as_geracoes(texto, slot) -> None:
    from msscan.historico import slot_dimm

    assert slot_dimm(texto) == slot


def test_evento_de_memoria_num_pente_instalado_no_r620_nao_vira_peca_ausente(tmp_path) -> None:
    """Antes: os slots do R620 ("DIMM_A1") viravam "_A1" e o pente A1 instalado parecia ausente para o evento "DIMMA1"."""
    pasta = _copia_r620(tmp_path)
    sel = pasta / "comandos" / "ipmitool__sel__elist.stdout"
    linha = " 6eb1 | 10/19/26 | 13:00:00 -03 | Memory ECC Uncorr Err | Uncorrectable ECC (UnCorrectable ECC |  DIMMA1) | Asserted\n"
    sel.write_text(sel.read_text() + linha)
    memoria = _diagnostico(pasta)["memoria"]
    evento = next(e for e in memoria.dados["eventos_sel"] if "DIMMA1" in e["texto"])
    assert evento["presente"] is True and evento["peca"] == "o slot A1"


def test_configuracao_agrupa_dimm_com_sublinhado() -> None:
    from msscan.configuracao import _por_processador

    modulos = [{"slot": "DIMM_A1", "tamanho_mb": 4096}, {"slot": "DIMM_B1", "tamanho_mb": 4096}]
    grupos = _por_processador(modulos, {"memoria": {"banco_por_cpu": {"A": 1, "B": 2}, "canal_do_slot": {1: 0}}})
    assert grupos == {"CPU1": {"slots": ["DIMM_A1"], "canais": [0], "gb": 4}, "CPU2": {"slots": ["DIMM_B1"], "canais": [0], "gb": 4}}


def test_bateria_romb_localizada_na_controladora() -> None:
    from msscan.localizacao import local_de

    assert local_de("Battery ROMB Battery Failed") == ("controladora", "Bateria da controladora RAID integrada (PERC)")


# --- Exportação da configuração antes de instalar (CLAUDE.md 11.7): formato anunciado pelo iDRAC ------------------------

ERRO_400 = {"error": {"@Message.ExtendedInfo": [{"Message": "Unable to complete the operation because the value JSON "
                                                             "entered for the property ExportFormat is not in the list of "
                                                             "acceptable values."}]}}  # formato DMTF; texto ilustrativo


class ClienteExportacao:
    """Responde por formato: ``respostas[formato] = (status, corpo)``; 202 com Location = tarefa criada."""

    def __init__(self, respostas: dict) -> None:
        self.respostas, self.corpos = respostas, []

    def escrever(self, operacao, caminho, corpo, *args, **kwargs):  # noqa: ANN001
        from msscan.core.executor import RespostaRedfish

        self.corpos.append(corpo)
        status, resposta = self.respostas[corpo["ExportFormat"]]
        return RespostaRedfish(caminho, status, resposta, local="/redfish/v1/TaskService/Tasks/JID_1" if status == 202 else "")


def _exportar(tmp_path, cliente, formatos):  # noqa: ANN001
    from msscan.atualizacao import automatica

    resultado = {"passos": []}
    tarefas = []
    remendo = pytest.MonkeyPatch()
    remendo.setattr(automatica, "_acompanhar_tarefa", lambda c, local, dormir: tarefas.append(local) or {"falhou": False})
    bmc = {"exportacao": "/redfish/v1/Managers/iDRAC.Embedded.1/Actions/Oem/EID_674_Manager.ExportSystemConfiguration",
           "exportacao_formatos": formatos}
    assert automatica._exportar(cliente, bmc, tmp_path, lambda s: None, resultado) is True
    remendo.undo()
    return resultado["passos"], tarefas


def test_formato_anunciado_pelo_idrac_vem_primeiro(tmp_path) -> None:
    cliente = ClienteExportacao({"XML": (202, None)})
    _, tarefas = _exportar(tmp_path, cliente, ["XML"])
    assert [c["ExportFormat"] for c in cliente.corpos] == ["XML"] and tarefas


def test_idrac7_recusa_json_com_400_e_tenta_xml_uma_vez(tmp_path) -> None:
    """Bancada de 19/10/2026: 400 em todas as exportações do R620. Sem anúncio: JSON, e depois do 400, XML."""
    cliente = ClienteExportacao({"JSON": (400, ERRO_400), "XML": (202, None)})
    passos, tarefas = _exportar(tmp_path, cliente, None)
    assert [c["ExportFormat"] for c in cliente.corpos] == ["JSON", "XML"] and tarefas
    assert "not in the list of acceptable values" in passos[0]["detalhe"] and "(JSON)" in passos[0]["detalhe"]


def test_erro_que_nao_e_400_nao_tenta_outro_formato(tmp_path) -> None:
    cliente = ClienteExportacao({"JSON": (500, None), "XML": (202, None)})
    passos, tarefas = _exportar(tmp_path, cliente, None)
    assert [c["ExportFormat"] for c in cliente.corpos] == ["JSON"] and not tarefas and "exporte pela interface" in passos[0]["detalhe"]


def test_dois_400_param_sem_tarefa(tmp_path) -> None:
    cliente = ClienteExportacao({"JSON": (400, None), "XML": (400, ERRO_400)})
    passos, tarefas = _exportar(tmp_path, cliente, None)
    assert len(cliente.corpos) == 2 and not tarefas and len(passos) == 2


def test_formatos_de_exportacao() -> None:
    from msscan.atualizacao.automatica import formatos_exportacao

    assert formatos_exportacao(None) == formatos_exportacao([]) == ["JSON", "XML"]
    assert formatos_exportacao(["XML", "JSON"]) == ["JSON", "XML"] and formatos_exportacao(["XML"]) == ["XML"]


def test_ler_bmc_guarda_os_formatos_anunciados() -> None:
    from msscan.atualizacao.aplicacao import ler_bmc
    from msscan.core.executor import RespostaRedfish
    from tests.test_aplicacao import BASE, BMCFalso, contexto

    executor = BMCFalso(com_exportacao=True)
    gerente = BASE + "/redfish/v1/Managers/BMC"
    corpo = executor.respostas_redfish[("GET", gerente)].corpo
    acao = next(iter(corpo["Actions"]["Oem"].values()))
    assert ler_bmc(contexto(executor))["exportacao_formatos"] is None  # sem anúncio (como no mockup)
    acao["ExportFormat@Redfish.AllowableValues"] = ["XML"]
    executor.respostas_redfish[("GET", gerente)] = RespostaRedfish(gerente, 200, corpo=corpo)
    info = ler_bmc(contexto(executor))
    assert info["exportacao_formatos"] == ["XML"] and info["exportacao"].endswith("ExportSystemConfiguration")


def test_system_id_da_estrutura_oem_208_real_do_r620() -> None:
    """2º layout real (R620, 12ª): "D0 10 00 D0 02 00 FE 00 CE 04 ..." (o R640 tem "03 0B" no lugar de "02 00"); SKU é
    "SKU=NotProvided", então o System ID vem daqui."""
    from msscan.parsers.dmidecode import parse_system_id_dell_oem

    assert ler_saida(R620, "dmidecode__-s__system-sku-number").startswith("SKU=NotProvided")
    assert parse_system_id_dell_oem(ler_saida(R620, "dmidecode__-t__208")) == "04CE"
    assert detectar(ExecutorReplay(R620)).sku.startswith("SKU=04CE")


def test_idrac6_sem_redfish_nao_manda_atualizar_o_firmware() -> None:
    """Frota sobre o R620 (20/10/2026): no iDRAC6 a mensagem do 404 mandava atualizar o firmware para ter Redfish."""
    from msscan import geracao_idrac
    from tests import frota

    assert geracao_idrac.tem_redfish("PowerEdge R710") is False and geracao_idrac.tem_redfish("PowerEdge R620") is True
    assert geracao_idrac.tem_redfish("ProLiant DL380") is None
    for nome, nunca in (("r710_11g", True), ("r530_13g", False)):
        pasta = frota.criar(nome, Path(__import__("tempfile").mkdtemp()))
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            main(["--replay", str(pasta), "--sem-ia", "--sem-credenciais"])
        texto = saida.getvalue()
        assert ("não tem Redfish em nenhuma versão" in texto) is nunca
        assert ("o que falta é atualizar o firmware do iDRAC" in texto) is (not nunca)

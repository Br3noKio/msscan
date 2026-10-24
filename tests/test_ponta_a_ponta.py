"""Fluxo REAL da atualização automática de ponta a ponta, contra simuladores (CLAUDE.md 18.6 e 18.7).

HTTP, processos e IPMI de verdade, mas só com os simuladores de ``tests/simuladores.py``: ``ipmi_sim`` (usuário
temporário), iDRAC9 falso por HTTPS (envio multipart, tarefas, boot único, repositório), ``racadm`` e DUPs falsos.
Nenhum comando chega ao R640 onde os testes rodam (o executor recusa tudo o que não for simulador) e o "reinício" só é
contado. Cobre: usuário temporário criado e apagado, retomada depois do "reinício", repositório pelo pass-through,
falha no meio, queda do MSscan com as mudanças temporárias desfeitas no próximo boot, .BIN e racadm.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from msscan.atualizacao import automatica, repositorio, temporarias as temp
from msscan.atualizacao.aplicacao import MODO_APLICAR, executar_plano, montar_plano
from msscan.atualizacao.retomada import ler_estado
from msscan.core import executor as modulo_executor
from msscan.deteccao.contexto import Contexto
from msscan.deteccao.detectores import tentar_redfish
from tests import simuladores as sim

pytestmark = pytest.mark.skipif(bool(sim.disponivel()), reason=sim.disponivel() or "simuladores disponíveis")

SKU = "SKU=0716;ModelName=PowerEdge R640"
NIC = b"pacote de rede (simulado)" * 64
BIOS = b"pacote de BIOS (simulado)" * 64
CPLD = b"pacote do CPLD (simulado)" * 64
CAMINHO_NIC = "FOLDER1/1/Network_Firmware_1R0W0_WN64_22.5.7_A00.EXE"
CAMINHO_BIOS = "FOLDER2/1/BIOS_W1WJW_WN64_2.28.1.EXE"
CAMINHO_CPLD = "FOLDER4/1/CPLD_Firmware_X1Y2Z_WN64_1.0.6_A00.EXE"
HARDWARE_OK = [{"item": n, "situacao": "ok", "texto": "ok (simulado)"} for n in ("Fontes", "Memória", "RAID", "Temperatura e sensores", "Ventoinhas")]


def _item(ident, componente, categoria, instalada, disponivel, conteudo, caminho, component_id):
    return {"id": ident, "componente": componente, "component_id": component_id, "instalada": instalada, "disponivel": disponivel,
            "situacao": "atualizacao", "texto_situacao": "Atualização disponível",
            "pacote": {"url": f"https://downloads.dell.com/{caminho}", "tamanho": len(conteudo), "sha256": hashlib.sha256(conteudo).hexdigest(),
                       "md5": "", "categoria": categoria, "reinicio": True}}  # fmt: skip


def _verificacao():
    return {"situacao": "verificada", "system_id": "0716", "bloqueios": [], "redundancia_perdida": False, "itens": [
        _item("item1", "Rede X710", "Network", "22.5.0", "22.5.7", NIC, CAMINHO_NIC, "25237"),
        # 09/10/2026: o segundo item é um componente comum (CPLD); iDRAC e BIOS vão sozinhos e têm testes próprios.
        _item("item2", "CPLD", "CPLD", "1.0.5", "1.0.6", CPLD, CAMINHO_CPLD, "27763")]}


def _catalogo(raiz: Path) -> None:
    """Catálogo no formato oficial (UTF-16, gzip) com os dois pacotes, no cache do pendrive."""
    componentes = "".join(
        f'<SoftwareComponent path="{c}" hash="{hashlib.sha256(d).hexdigest()}" hashAlgorithm="SHA256" vendorVersion="{v}" '
        f'rebootRequired="true" packageType="LW64"><Name><Display>{n}</Display></Name><SupportedSystems><Brand><Model systemID="0716">'
        f'<Display>R640</Display></Model></Brand></SupportedSystems></SoftwareComponent>'
        for c, d, v, n in ((CAMINHO_NIC, NIC, "22.5.7", "Rede X710"), (CAMINHO_CPLD, CPLD, "1.0.6", "CPLD"),
                              (CAMINHO_BIOS, BIOS, "2.28.1", "BIOS")))
    xml = f'<?xml version="1.0" encoding="utf-16"?><Manifest baseLocation="downloads.dell.com" version="26.09">{componentes}</Manifest>'
    destino = raiz / "firmware/cache/Catalog.xml.gz"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(gzip.compress(xml.encode("utf-16")))


def _pacotes(raiz: Path) -> None:
    """Pacotes já baixados e conferidos (etapa 2): a instalação reusa porque o hash confere."""
    pasta = raiz / "firmware/pacotes"
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / Path(CAMINHO_NIC).name).write_bytes(NIC)
    (pasta / Path(CAMINHO_BIOS).name).write_bytes(BIOS)
    (pasta / Path(CAMINHO_CPLD).name).write_bytes(CPLD)


def _ctx(executor, racadm=None) -> Contexto:
    ctx = Contexto(executor=executor, fabricante="Dell Inc.", modelo="PowerEdge R640", sku=SKU)
    ctx.ipmi.disponivel = True
    if racadm:
        ctx.ferramentas_fabricante = {"racadm": str(racadm)}
    return ctx


@pytest.fixture
def ipmi(tmp_path):
    with sim.SimuladorIPMI(tmp_path / "ipmi") as simulador:
        yield simulador


@pytest.fixture(autouse=True)
def _sem_esperas(monkeypatch):
    from msscan.atualizacao import aplicacao

    monkeypatch.setattr(aplicacao, "ESPERA_TAREFA_S", 0.0)
    monkeypatch.setattr(repositorio, "ESPERA_JOB_S", 0.0)
    monkeypatch.setattr(aplicacao.shutil, "disk_usage", lambda _: type("U", (), {"free": 10 * 2**30})())


def _sem_temporario(ipmi) -> bool:
    return not any(nome.startswith("msscan") for nome, _ in ipmi.usuarios().values())


def test_trava_continua_valendo_para_executor_real():
    assert modulo_executor.GRAVACAO_REAL_LIBERADA is False and not modulo_executor.ExecutorReal().escrita_permitida()
    assert sim.ExecutorSimulador.alvo_simulado and sim.ExecutorSimulador.origem != "real"


def test_usuario_temporario_redfish_reinicio_e_retomada(tmp_path, ipmi):
    """Sem credenciais: administrador temporário pelo IPMI, envio multipart, boot único, desfaz, "reinicia" e continua."""
    raiz = tmp_path / "MSSCAN"
    _pacotes(raiz)
    with sim.BMCSimulado(tmp_path, ipmi, repositorio=False) as bmc:
        executor = sim.ExecutorSimulador(ipmi, tmp_path)
        ctx = _ctx(executor)
        assert tentar_redfish(ctx, "127.0.0.1", bmc.server_address[1], "teste")
        resultado = executar_plano(ctx, montar_plano(_verificacao(), ["item1", "item2"]), executor, str(raiz), MODO_APLICAR, dormir=lambda _: None)
        caminhos = [(t["caminho"], t["situacao"]) for t in resultado["tentativas"]]
        assert ("a", "indisponivel") in caminhos and ("b", "ok") in caminhos, resultado["tentativas"]
        assert resultado["situacao"] == "aguardando_reinicio" and executor.reinicios == 1, resultado["mensagem"]
        # O firmware entrou pela sessão do usuário temporário, com o pacote certo; os dois agendados para o MESMO reinício
        # (21.3); boot único pelo USB antes do reinício.
        assert [f["nome"] for f in bmc.firmware] == [Path(CAMINHO_NIC).name, Path(CAMINHO_CPLD).name]
        assert bmc.firmware[0]["usuario"].startswith("msscan")
        assert bmc.firmware[0]["sha256"] == hashlib.sha256(NIC).hexdigest() and bmc.boot_usb
        assert bmc.firmware[0]["parametros"]["@Redfish.OperationApplyTime"] == "OnReset"
        # Mudança temporária desfeita ANTES do reinício, conferida no ipmi_sim; relatório diz isso.
        assert _sem_temporario(ipmi) and [m["situacao"] for m in resultado["temporarias"]] == [temp.DESFEITA]
        assert not temp.ha_pendentes(str(raiz))
        estado = ler_estado(executor, str(raiz))
        assert estado["automatico"] and [i["componente"] for i in estado["enviados"]] == ["Rede X710", "CPLD"]
        assert estado["pendentes"] == [] and "senha" not in json.dumps(estado)
        # Na volta, nada fica para continuar sozinho: a retomada só confere os dois.
        assert automatica.pode_continuar(estado, _ctx(sim.ExecutorSimulador(ipmi, tmp_path))) == "nenhum item pendente"
        assert not executor.recusados


def test_opcao_b_reconsultada_depois_de_ligar_o_cabo(tmp_path, ipmi):
    """Na detecção o iDRAC não respondeu (cabo fora); antes de desistir da rede, o MSscan tenta de novo o mesmo endereço."""
    raiz = tmp_path / "MSSCAN"
    _pacotes(raiz)
    with sim.BMCSimulado(tmp_path, ipmi, repositorio=False) as bmc:
        executor = sim.ExecutorSimulador(ipmi, tmp_path)
        ctx = _ctx(executor)
        ctx.redfish.anunciado, ctx.redfish.endereco, ctx.redfish.porta = True, "127.0.0.1", bmc.server_address[1]
        ctx.redfish.origem = "ipmitool lan print 1"
        assert ctx.bmc is None and not ctx.redfish.confirmado
        resultado = executar_plano(ctx, montar_plano(_verificacao(), ["item1"]), executor, str(raiz), MODO_APLICAR, dormir=lambda _: None)
        caminhos = [(t["caminho"], t["situacao"]) for t in resultado["tentativas"]]
        assert ("b", "ok") in caminhos and not any(c == "c" for c, _ in caminhos), resultado["tentativas"]
        assert ctx.redfish.origem == "ipmitool lan print 1 (nova tentativa)"
        assert [f["nome"] for f in bmc.firmware] == [Path(CAMINHO_NIC).name] and _sem_temporario(ipmi)


def test_retomada_para_com_problema_atual_de_hardware(tmp_path, ipmi):
    estado = {"automatico": True, "situacao": "agendado", "confirmado": "2099-01-01T00:00:00", "modo": "aplicar",
              "equipamento": {"modelo": "PowerEdge R640", "sku": SKU}, "pendentes": [{"componente": "BIOS"}], "enviados": []}
    executor = sim.ExecutorSimulador(ipmi, tmp_path)
    critico = HARDWARE_OK + [{"item": "RAID", "situacao": "bloqueia", "texto": "VD0 degradado"}]
    resultado = automatica.continuar(_ctx(executor), executor, str(tmp_path), estado, critico)
    assert resultado["situacao"] == "bloqueada" and "VD0 degradado" in resultado["mensagem"] and executor.reinicios == 0
    assert "outro servidor" in automatica.pode_continuar({**estado, "equipamento": {"modelo": "R740", "sku": SKU}}, _ctx(executor))


def test_falha_no_meio_depois_do_aceite_nao_reinicia_e_desfaz(tmp_path, ipmi):
    raiz = tmp_path / "MSSCAN"
    _pacotes(raiz)
    with sim.BMCSimulado(tmp_path, ipmi, repositorio=False, falhar_tarefas_depois_de=0) as bmc:
        executor = sim.ExecutorSimulador(ipmi, tmp_path)
        ctx = _ctx(executor)
        assert tentar_redfish(ctx, "127.0.0.1", bmc.server_address[1], "teste")
        resultado = executar_plano(ctx, montar_plano(_verificacao(), ["item1"]), executor, str(raiz), MODO_APLICAR, dormir=lambda _: None)
    assert resultado["situacao"] == "possivelmente_agendado" and "Job Queue" in resultado["mensagem"]
    assert executor.reinicios == 0 and _sem_temporario(ipmi)
    assert ler_estado(executor, str(raiz)) is not None  # o próximo boot pelo pendrive confere o antes × depois


def test_queda_do_msscan_e_o_proximo_boot_desfaz(tmp_path, ipmi):
    """O MSscan "cai" com o usuário temporário criado e o pass-through ligado: o próximo boot desfaz os dois."""
    raiz = tmp_path / "MSSCAN"
    racadm = sim.criar_racadm(tmp_path / "vendor", "127.0.0.9", ipmi=ipmi)
    with sim.BMCSimulado(tmp_path, ipmi, repositorio=False) as bmc:
        executor = sim.ExecutorSimulador(ipmi, tmp_path, racadm)
        ctx = _ctx(executor, racadm)
        registro = temp.Temporarias(str(raiz))
        resultado: dict = {"passos": []}
        automatica._ligar_passthrough(ctx, executor, str(raiz), registro, resultado)
        assert "AdminState=Enabled" in (tmp_path / "vendor/racadm_estado").read_text()
        assert tentar_redfish(ctx, "127.0.0.1", bmc.server_address[1], "teste")
        assert automatica._criar_usuario(ctx, executor, str(raiz), registro, resultado)
        assert not _sem_temporario(ipmi) and len(temp.Temporarias(str(raiz)).pendentes()) == 2
        # --- queda: nada é desfeito por este processo. Próximo boot: executor e contexto novos, só o pendrive.
        executor2 = sim.ExecutorSimulador(ipmi, tmp_path, racadm)
        mudancas = temp.desfazer_tudo(_ctx(executor2, racadm), executor2, str(raiz))
    assert [m["situacao"] for m in mudancas] == [temp.DESFEITA, temp.DESFEITA], mudancas
    assert _sem_temporario(ipmi) and "AdminState=Disabled" in (tmp_path / "vendor/racadm_estado").read_text()
    assert not temp.ha_pendentes(str(raiz)) and list((raiz / "estado").glob("mudancas_desfeitas_*.json"))


def test_queda_logo_depois_de_registrar_e_idempotente(tmp_path, ipmi):
    """Registro gravado e o MSscan caiu antes de mudar o iDRAC: o próximo boot só confere e fecha o registro."""
    raiz = tmp_path / "MSSCAN"
    temp.Temporarias(str(raiz)).registrar(temp.USUARIO, "Usuário administrador temporário msscan0000 (slot 4)")
    executor = sim.ExecutorSimulador(ipmi, tmp_path)
    mudancas = temp.desfazer_tudo(_ctx(executor), executor, str(raiz))
    assert [m["situacao"] for m in mudancas] == [temp.DESFEITA] and not temp.ha_pendentes(str(raiz))


@pytest.mark.parametrize("rc_checar, rc_aplicar, situacao", [(0, 2, "aguardando_reinicio"), (0, 0, "aplicada"), (3, 0, "bloqueada"),
                                                             (0, 1, "falhou")])  # fmt: skip
def test_bin_direto_com_codigos_oficiais(tmp_path, ipmi, rc_checar, rc_aplicar, situacao):
    """Sem iDRAC: o pacote Linux (.BIN) é checado (-c) e aplicado (-q); -c recusado = nunca forçar (sem -q)."""
    raiz = tmp_path / "MSSCAN"
    conteudo = b"#!/bin/bash\n"  # o DUP falso é reescrito abaixo; o hash do plano é o do arquivo final
    verificacao = _verificacao()
    pasta = raiz / "firmware/pacotes"
    pasta.mkdir(parents=True)
    dup = sim.criar_dup(pasta / "Network_Firmware_1R0W0_LN64_22.5.7_A00.BIN", rc_checar, rc_aplicar)
    conteudo = dup.read_bytes()
    nic = verificacao["itens"][0]
    nic["pacote"].update(url="https://downloads.dell.com/FOLDER1/1/Network_Firmware_1R0W0_LN64_22.5.7_A00.BIN",
                         sha256=hashlib.sha256(conteudo).hexdigest(), tamanho=len(conteudo))
    executor = sim.ExecutorSimulador(ipmi, tmp_path)
    resultado = executar_plano(_ctx(executor), montar_plano(verificacao, ["item1"]), executor, str(raiz), MODO_APLICAR, dormir=lambda _: None)
    assert resultado["situacao"] == situacao, resultado["mensagem"]
    chamadas = Path(str(dup) + ".chamadas").read_text().split()
    assert chamadas[0] == "-c" and (("-q" in chamadas) == (rc_checar == 0))
    assert _sem_temporario(ipmi)  # .BIN não precisa do iDRAC: nenhum usuário temporário



def _assert_update_curto(tmp_path, pacote) -> None:
    """``racadm update -f`` recebeu um caminho com menos de 64 caracteres (RAC923, R620 em 16/10/2026) que aponta para o pacote."""
    linha = next(l for l in (tmp_path / "vendor/racadm_chamadas").read_text().splitlines() if "update -f " in l)
    caminho = linha.split("update -f ", 1)[1].strip()
    assert len(caminho) < 64 and Path(caminho).resolve() == Path(pacote).resolve(), caminho

def test_racadm_update_quando_o_redfish_nao_responde(tmp_path, ipmi):
    raiz = tmp_path / "MSSCAN"
    _pacotes(raiz)
    racadm = sim.criar_racadm(tmp_path / "vendor", "127.0.0.9", capaz=False, ipmi=ipmi)
    executor = sim.ExecutorSimulador(ipmi, tmp_path, racadm)
    resultado = executar_plano(_ctx(executor, racadm), montar_plano(_verificacao(), ["item1"]), executor, str(raiz), MODO_APLICAR,
                               dormir=lambda _: None)  # fmt: skip
    assert resultado["situacao"] == "aguardando_reinicio", resultado["mensagem"]
    assert ("d", "ok") in [(t["caminho"], t["situacao"]) for t in resultado["tentativas"]]
    # 19.1: sem Redfish alcançável, o caminho b fica registrado como indisponível, com o motivo (log do T1: ele sumia).
    b = [t for t in resultado["tentativas"] if t["caminho"] == "b"]
    assert [t["situacao"] for t in b] == ["indisponivel"] and "não responde a partir deste servidor" in b[0]["detalhe"] and _sem_temporario(ipmi)
    _assert_update_curto(tmp_path, raiz / "firmware/pacotes" / Path(CAMINHO_NIC).name)
    assert "boot único" in json.dumps(resultado["passos"], ensure_ascii=False)  # sem iDRAC: aviso de escolher o pendrive (F11)


# --- Repositório pelo iDRAC (18.7) ---------------------------------------------------------------------------------------


@pytest.fixture
def repositorio_local(tmp_path, ipmi, monkeypatch):
    """iDRAC alcançável só pelo pass-through (127.0.0.3:443), servidor em 127.0.0.4 na "interface" interna."""
    porta = sim.porta_livre(ip="127.0.0.4")
    monkeypatch.setattr(repositorio, "PORTA", porta)
    monkeypatch.setattr(automatica, "POUCOS_PARA_DIRETO", 0)  # 22.2: com 2 itens o padrão é o envio direto
    racadm = sim.criar_racadm(tmp_path / "vendor", "127.0.0.3", ipmi=ipmi)
    interfaces = [{"ifname": "idrac", "addr_info": [{"local": "127.0.0.4", "prefixlen": 8}]}]
    return porta, racadm, interfaces


def _rodar_repositorio(tmp_path, ipmi, porta, racadm, interfaces, **opcoes):
    raiz = tmp_path / "MSSCAN"
    _pacotes(raiz)
    _catalogo(raiz)
    try:
        bmc = sim.BMCSimulado(tmp_path, ipmi, ip="127.0.0.3", porta=443, porta_repositorio=porta, **opcoes)
    except PermissionError:
        pytest.skip("porta 443 exige root")
    with bmc:
        executor = sim.ExecutorSimulador(ipmi, tmp_path, racadm, interfaces)
        ctx = _ctx(executor, racadm)
        resultado = executar_plano(ctx, montar_plano(_verificacao(), ["item1", "item2"]), executor, str(raiz), MODO_APLICAR,
                                   dormir=lambda _: None)  # fmt: skip
    return raiz, bmc, executor, resultado


def test_repositorio_pelo_passthrough(tmp_path, ipmi, repositorio_local):
    raiz, bmc, executor, resultado = _rodar_repositorio(tmp_path, ipmi, *repositorio_local)
    caminhos = [(t["caminho"], t["situacao"]) for t in resultado["tentativas"]]
    assert ("c", "ok") in caminhos and ("b", "ok") in caminhos and ("r", "ok") in caminhos, resultado["tentativas"]
    assert resultado["situacao"] == "aguardando_reinicio" and executor.reinicios == 1, resultado["mensagem"]
    # O "iDRAC" leu o catálogo filtrado e baixou os dois pacotes do pendrive, com o SHA-256 conferido; nada por multipart.
    assert not bmc.firmware and {p["PackageName"] for p in bmc.lista_repositorio} == {Path(CAMINHO_NIC).name, Path(CAMINHO_CPLD).name}
    pedidos = " ".join(resultado["repositorio_pedidos"])
    assert "Catalog.xml" in pedidos and Path(CAMINHO_CPLD).name in pedidos
    instalacoes = [e[2] for e in bmc.eventos if e[0] == "post" and e[1].endswith("InstallFromRepository")]
    assert [c["ApplyUpdate"] for c in instalacoes] == ["False", "True"]
    assert all(c["RebootNeeded"] is False and "ApplyDowngradeVersions" not in c and "ApplySameVersions" not in c for c in instalacoes)
    assert len(resultado["repositorio"]["jobs"]) == 2 and bmc.boot_usb
    # Tudo o que mudou no iDRAC foi desfeito antes do reinício.
    assert _sem_temporario(ipmi) and "AdminState=Disabled" in (tmp_path / "vendor/racadm_estado").read_text()
    assert [m["situacao"] for m in resultado["temporarias"]] == [temp.DESFEITA, temp.DESFEITA]
    estado = ler_estado(executor, str(raiz))
    assert estado["pendentes"] == [] and len(estado["enviados"]) == 2 and len(estado["jobs"]) == 2
    catalogo = (raiz / repositorio.PASTA_REPOSITORIO / "Catalog.xml").read_bytes().decode("utf-16")
    assert 'baseLocation=""' in catalogo and CAMINHO_NIC in catalogo


def test_repositorio_job_falhou_no_meio_nao_reenvia_por_outro_caminho(tmp_path, ipmi, repositorio_local):
    _, bmc, executor, resultado = _rodar_repositorio(tmp_path, ipmi, *repositorio_local, falhar_job=True)
    assert resultado["situacao"] == "possivelmente_agendado" and "fila de tarefas" in resultado["mensagem"]
    assert not bmc.firmware and executor.reinicios == 0 and _sem_temporario(ipmi)


def test_sem_servico_de_repositorio_cai_para_o_envio_avulso(tmp_path, ipmi, repositorio_local):
    _, bmc, executor, resultado = _rodar_repositorio(tmp_path, ipmi, *repositorio_local, repositorio=False)
    assert ("r", "indisponivel") in [(t["caminho"], t["situacao"]) for t in resultado["tentativas"]]
    assert [f["nome"] for f in bmc.firmware] == [Path(CAMINHO_NIC).name, Path(CAMINHO_CPLD).name] and executor.reinicios == 1


# --- 13ª geração (pedido de 15/10/2026): R530 com iDRAC8 2.30, sem Redfish, só racadm local ---------------------------------


IDRAC_240 = b"iDRAC 2.40.40.40 (simulado)" * 64
CAMINHO_IDRAC_240 = "FOLDER03884128M/2/iDRAC-with-Lifecycle-Controller_Firmware_2091K_WN64_2.40.40.40_A00.EXE"


def test_r530_idrac8_pelo_racadm_degrau_240(tmp_path, ipmi):
    raiz = tmp_path / "MSSCAN"
    pasta = raiz / "firmware/pacotes"
    pasta.mkdir(parents=True)
    (pasta / Path(CAMINHO_IDRAC_240).name).write_bytes(IDRAC_240)
    racadm = sim.criar_racadm(tmp_path / "vendor", "127.0.0.9", capaz=False, ipmi=ipmi)
    executor = sim.ExecutorSimulador(ipmi, tmp_path, racadm)
    ctx = Contexto(executor=executor, fabricante="Dell Inc.", modelo="PowerEdge R530", sku="SKU=063A;ModelName=PowerEdge R530")
    ctx.ipmi.disponivel = True
    ctx.ferramentas_fabricante = {"racadm": str(racadm)}
    item = _item("item1", "iDRAC (BMC)", "iDRAC with Lifecycle Controller", "2.30", "2.40.40.40", IDRAC_240, CAMINHO_IDRAC_240, "25227")
    verificacao = {"situacao": "verificada", "system_id": "063A", "bloqueios": [], "redundancia_perdida": False, "itens": [item]}
    resultado = executar_plano(ctx, montar_plano(verificacao, ["item1"]), executor, str(raiz), MODO_APLICAR, dormir=lambda _: None)
    texto = json.dumps(resultado, ensure_ascii=False)
    assert resultado["situacao"] in ("aplicada", "aguardando_reinicio", "parado"), resultado["mensagem"]
    _assert_update_curto(tmp_path, pasta / Path(CAMINHO_IDRAC_240).name)
    assert "Traceback" not in texto and _sem_temporario(ipmi)

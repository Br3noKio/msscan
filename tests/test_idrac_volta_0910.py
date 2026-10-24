"""09/10/2026, R640 real com a ISO 0.18.1: o firmware do iDRAC foi aplicado ("Concluído"), mas o MSscan não reconheceu a
volta dele (26 min 40 s sem resposta registrada) e não seguiu para a BIOS.

Causa: depois do job concluído o iDRAC ainda responde com a versão antiga e a sessão válida; quando reinicia, a sessão
antiga morre (401) e o inventário caía no do sistema (dmidecode/ethtool), que nunca vem vazio — a sessão nova nunca era
aberta. O simulador reproduz a sequência real (``reiniciar_apos_inventarios``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from msscan.atualizacao import automatica
from msscan.atualizacao.aplicacao import MODO_APLICAR, executar_plano, montar_plano
from msscan.atualizacao.retomada import ler_estado
from msscan.deteccao.detectores import tentar_redfish
from tests import simuladores as sim
from tests.test_ponta_a_ponta import CAMINHO_BIOS, HARDWARE_OK, _ctx, ipmi  # noqa: F401 - fixture
from tests.test_ponta_a_ponta_21 import CAMINHO_IDRAC, CONCLUIDA, NADA, _com_idrac_e_bios, _etapas

pytestmark = pytest.mark.skipif(bool(sim.disponivel()), reason=sim.disponivel() or "simuladores disponíveis")


def _rodar(tmp_path, ipmi, **opcoes):
    raiz = tmp_path / "MSSCAN"
    verificacao = _com_idrac_e_bios(raiz)
    bmc = sim.BMCSimulado(tmp_path, ipmi, repositorio=False, roteiros=[[CONCLUIDA]], **opcoes).__enter__()
    executor = sim.ExecutorSimulador(ipmi, tmp_path)
    ctx = _ctx(executor)
    assert tentar_redfish(ctx, "127.0.0.1", bmc.server_address[1], "teste")
    assert "Credenciais validadas" in ctx.bmc.tentar("root", sim.SENHA_ROOT)  # como no R640: administrador informado
    resultado = executar_plano(ctx, montar_plano(verificacao, ["item0", "item3"]), executor, str(raiz), MODO_APLICAR, dormir=NADA)
    return raiz, bmc, executor, ctx, resultado


def test_idrac_reinicia_depois_de_responder_com_a_versao_antiga_e_a_bios_segue(tmp_path, ipmi):
    raiz, bmc, executor, ctx, resultado = _rodar(tmp_path, ipmi, idrac_novo="7.10.30.00", reiniciar_apos_inventarios=(2, 4))
    try:
        assert ("idrac_reiniciou", 4) in bmc.eventos
        assert any("voltou com o firmware novo (7.10.30.00)" in p["detalhe"] and "sessão nova" in p["detalhe"]
                   for p in resultado["passos"]), resultado["passos"]  # fmt: skip
        assert [f["nome"] for f in bmc.firmware] == [Path(CAMINHO_IDRAC).name, Path(CAMINHO_BIOS).name]
        assert resultado["situacao"] == "aguardando_reinicio", resultado["mensagem"]
    finally:
        bmc.__exit__()


def test_idrac_sem_volta_para_e_continuar_retoma_da_bios_sem_reenviar(tmp_path, ipmi, monkeypatch):
    monkeypatch.setattr(automatica, "ESPERA_IDRAC_NOVO_S", 60.0)
    raiz, bmc, executor, ctx, resultado = _rodar(tmp_path, ipmi, reiniciar_apos_inventarios=(1, 2))  # volta sem a versão nova
    try:
        assert [f["nome"] for f in bmc.firmware] == [Path(CAMINHO_IDRAC).name]
        assert resultado["situacao"] == "parado" and "Continuar de onde parou" in resultado["mensagem"], resultado["mensagem"]
        assert _etapas(resultado, "BIOS")[-1] == "parado" and executor.reinicios == 0
        estado = ler_estado(executor, str(raiz))
        assert [i["componente"] for i in estado["enviados"]] == ["iDRAC"] and [i["componente"] for i in estado["pendentes"]] == ["BIOS"]
        # "Continuar de onde parou" com o iDRAC ainda na versão antiga: nada é enviado, o plano continua parado.
        ainda = automatica.continuar(ctx, executor, str(raiz), estado, HARDWARE_OK, dormir=NADA)
        assert ainda["situacao"] == "parado" and "Nada foi enviado" in ainda["mensagem"], ainda["mensagem"]
        assert len(bmc.firmware) == 1 and ler_estado(executor, str(raiz)) == estado
        # O iDRAC aparece com a versão nova (reinício tardio, sessões antigas mortas): confere e segue só com a BIOS.
        bmc.versao_idrac = "7.10.30.00"
        bmc.sessoes.clear()
        retomada = automatica.continuar(ctx, executor, str(raiz), estado, HARDWARE_OK, dormir=NADA)
        assert [f["nome"] for f in bmc.firmware] == [Path(CAMINHO_IDRAC).name, Path(CAMINHO_BIOS).name], retomada["mensagem"]
        assert retomada["situacao"] == "aguardando_reinicio", retomada["mensagem"]
        assert any("versão nova confirmada" in p["detalhe"] for p in retomada["passos"])
    finally:
        bmc.__exit__()

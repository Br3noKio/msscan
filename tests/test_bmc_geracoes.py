"""iDRAC de todas as gerações (CLAUDE.md 28.2/28.3): simuladores com a criptografia de cada uma e as mensagens de acesso.

Caso real: R530 (13ª, iDRAC8 2.30) com ``[SSL: DH_KEY_TOO_SMALL]`` no Redfish e "iDRAC não acessível" com o racadm local
funcionando. O MSscan precisa conectar em todos os perfis, guardar o modo por BMC e nunca relaxar a criptografia da internet.
"""

from __future__ import annotations

import json
import shutil

import pytest

from msscan import geracao_idrac
from msscan.acesso_idrac import IPMI, RACADM, REDE, avaliar
from msscan.core import tls_bmc
from msscan.core.executor import ExecutorReal
from msscan.deteccao.contexto import Contexto
from msscan.deteccao.detectores import tentar_redfish
from tests import simuladores as sim

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl não instalado")

ESPERADO = {
    "moderno": tls_bmc.PADRAO,  # iDRAC9/10 (R640): nada muda
    "sha1_rsa1024": tls_bmc.PADRAO,  # o certificado não é conferido (autoassinado), então a chave fraca não impede
    "dh1024_ecdhe": tls_bmc.SEM_DHE,
    "dh1024": tls_bmc.SEGURANCA_REDUZIDA,  # R530
    "tls10": tls_bmc.SEGURANCA_REDUZIDA,
    "idrac6": tls_bmc.SEGURANCA_REDUZIDA,
    "so_cifras_antigas": tls_bmc.LEGADO,
}


@pytest.mark.parametrize("perfil", sim.PERFIS_TLS)
def test_conecta_em_todas_as_geracoes_e_guarda_o_modo(tmp_path, perfil):
    with sim.BMCSimulado(tmp_path, None, tls=perfil) as bmc:
        executor = ExecutorReal()
        ctx = Contexto(executor=executor, fabricante="Dell Inc.", modelo="PowerEdge R530")
        porta = bmc.server_address[1]
        assert tentar_redfish(ctx, "127.0.0.1", porta, "ipmitool lan print 1"), ctx.redfish.tentativas
        assert ctx.redfish.modo_tls == ESPERADO[perfil] == executor.modos_tls_bmc[f"127.0.0.1:{porta}"]
        # A segunda conversa vai direto no modo aprendido (nenhum erro de TLS registrado de novo).
        resposta = executor.redfish("GET", f"{bmc.base}/redfish/v1", timeout=5)
        assert resposta.ok and not resposta.erro
        caixa = avaliar({"redfish": {"confirmado": True, "endereco": "127.0.0.1", "modo_tls": ctx.redfish.modo_tls},
                         "ipmi": True, "interfaces": []})  # fmt: skip
        assert caixa["situacao"] == REDE and caixa["modo_tls"] == ESPERADO[perfil]
        compat = ESPERADO[perfil] != tls_bmc.PADRAO
        assert ("modo de compatibilidade" in caixa["titulo"]) is compat
        relatorio = json.dumps(ctx.redfish.__dict__, default=str)
        assert ESPERADO[perfil] in relatorio  # vai para o relatório e o log técnico (equipamento.redfish)


def test_internet_nunca_usa_os_modos_de_compatibilidade(tmp_path):
    """Os modos valem só para o BMC: o mesmo servidor fraco falha no ``http_get`` (internet), com TLS normal."""
    with sim.BMCSimulado(tmp_path, None, tls="dh1024") as bmc:
        executor = ExecutorReal()
        assert executor.redfish("GET", f"{bmc.base}/redfish/v1", timeout=5).ok
        resposta = executor.http_get(f"{bmc.base}/redfish/v1", timeout=5)
        assert resposta.status is None and resposta.erro_tipo == "tls"


def test_modo_aprendido_e_por_bmc(tmp_path):
    with sim.BMCSimulado(tmp_path, None, tls="dh1024") as antigo, sim.BMCSimulado(tmp_path, None, tls="moderno") as novo:
        executor = ExecutorReal()
        assert executor.redfish("GET", f"{antigo.base}/redfish/v1", timeout=5).ok
        assert executor.redfish("GET", f"{novo.base}/redfish/v1", timeout=5).ok
        assert sorted(executor.modos_tls_bmc.values()) == [tls_bmc.PADRAO, tls_bmc.SEGURANCA_REDUZIDA]


def test_bmc_desligado_nao_multiplica_a_espera():
    """Sem resposta (porta fechada) não é problema de criptografia: uma tentativa só, sem percorrer os modos."""
    executor = ExecutorReal()
    resposta = executor.redfish("GET", "https://127.0.0.1:9/redfish/v1", timeout=2)
    assert resposta.status is None and resposta.erro_tipo == "conexao" and "nenhum modo" not in resposta.erro
    assert executor.modos_tls_bmc == {}


@pytest.mark.skipif(bool(sim.disponivel()), reason=sim.disponivel() or "")
@pytest.mark.parametrize("perfil", ["dh1024", "idrac6"])
def test_atualizacao_completa_num_idrac_antigo(tmp_path, perfil):
    """Login, usuário temporário pelo IPMI, envio do firmware (escrita no modo aprendido) e boot único num iDRAC com a
    criptografia do R530 e do iDRAC6."""
    from msscan.atualizacao.aplicacao import MODO_APLICAR, executar_plano, montar_plano
    from tests import test_ponta_a_ponta as pap

    raiz = tmp_path / "MSSCAN"
    pap._pacotes(raiz)
    with sim.SimuladorIPMI(tmp_path) as ipmi, sim.BMCSimulado(tmp_path, ipmi, repositorio=False, tls=perfil) as bmc:
        executor = sim.ExecutorSimulador(ipmi, tmp_path)
        ctx = pap._ctx(executor)
        assert tentar_redfish(ctx, "127.0.0.1", bmc.server_address[1], "teste")
        assert ctx.redfish.modo_tls == tls_bmc.SEGURANCA_REDUZIDA
        resultado = executar_plano(ctx, montar_plano(pap._verificacao(), ["item1"]), executor, str(raiz), MODO_APLICAR,
                                   dormir=lambda _: None)  # fmt: skip
        assert resultado["situacao"] == "aguardando_reinicio", resultado
        assert bmc.firmware and bmc.boot_usb


# --- Camadas e mensagens (28.2) ---------------------------------------------------------------------------------------


def _leituras(**extra):
    base = {"ipmi": True, "rede": {"lido": True, "ip": "192.0.2.10", "mascara": "255.255.255.0", "origem": "Static"},
            "ativa": "dedicada", "interfaces": [{"nome": "eno1", "link": True, "enderecos": ["192.0.2.20/24"]}],
            "racadm": {}, "racadm_caminho": None, "modelo": "PowerEdge R530",
            "redfish": {"anunciado": True, "confirmado": False, "endereco": "192.0.2.10",
                        "motivo": "erro de TLS com o BMC"}}  # fmt: skip
    base.update(extra)
    return base


def _texto(estado):
    return estado["titulo"] + " " + " ".join(m["texto"] for m in estado["mensagens"])


def test_r530_racadm_respondendo_nunca_nao_acessivel():
    estado = avaliar(_leituras(racadm_caminho="/opt/dell/srvadmin/sbin/racadm", racadm={"situacao": "pronto", "versao": "11.3"}))
    texto = _texto(estado)
    assert estado["situacao"] == RACADM and "acessível só por dentro do servidor (racadm/IPMI)" in estado["titulo"]
    assert "não acessível" not in texto and "iDRAC inacessível pela rede (cabo, IP, porta)" in texto


def test_sem_redfish_so_ipmi():
    estado = avaliar(_leituras(redfish={"anunciado": False, "confirmado": False}))
    texto = _texto(estado)
    assert estado["situacao"] == IPMI and estado["titulo"] == "iDRAC acessível só por dentro do servidor (IPMI)"
    assert "não acessível" not in texto and "sem acesso interno" not in texto


def test_sem_camada_de_rede_recomenda_firmware_da_geracao():
    texto = _texto(avaliar(_leituras()))
    assert "13ª geração (iDRAC8)" in texto and "2.86.86.86" in texto and "página web do iDRAC" in texto
    rede_ok = avaliar(_leituras(redfish={"confirmado": True, "endereco": "192.0.2.10", "modo_tls": "padrao"}))
    assert "Firmware do iDRAC recomendado" not in _texto(rede_ok)


@pytest.mark.parametrize("modelo, numero, idrac", [
    ("PowerEdge R610", 11, "iDRAC6"), ("PowerEdge R410", 11, "iDRAC6"), ("PowerEdge T320", 12, "iDRAC7"),
    ("PowerEdge R530", 13, "iDRAC8"), ("PowerEdge R730xd", 13, "iDRAC8"), ("PowerEdge R640", 14, "iDRAC9"),
    ("PowerEdge R7425", 14, "iDRAC9"), ("PowerEdge MX740c", 14, "iDRAC9"), ("PowerEdge R6515", 15, "iDRAC9"),
    ("PowerEdge R760", 16, "iDRAC9"), ("PowerEdge XE9680", 16, "iDRAC9"), ("PowerEdge R670", 17, "iDRAC10"),
    ("PowerEdge R7725", 17, "iDRAC10"), ("PowerEdge 2950", None, None), (None, None, None),
])  # fmt: skip
def test_geracao_pelo_modelo(modelo, numero, idrac):
    assert geracao_idrac.geracao(modelo) == numero and geracao_idrac.idrac(numero) == idrac

"""Compatibilidade máxima com servidores Dell (CLAUDE.md 29): achados do log real do R530 e da auditoria por geração."""

from __future__ import annotations

import pytest

from msscan.deteccao.contexto import Contexto
from msscan.deteccao.detectores import detectar_identificacao
from msscan.parsers.dmidecode import parse_sku_dell, parse_system_id_dell_oem
from tests.conftest import R640, ler_saida
from tests.test_rede import ReplayAlterado


def test_system_id_da_estrutura_oem_208_real_do_r640() -> None:
    """Saída real (R640): o ID estendido FE → 0x0716, o mesmo do SKU=0716 dele."""
    assert parse_system_id_dell_oem(ler_saida(R640, "dmidecode__-t__208")) == "0716"


def test_system_id_de_um_byte_e_ausente() -> None:
    um_byte = "Handle 0xD000, DMI type 208, 12 bytes\nOEM-specific Type\n\tHeader and Data:\n\t\tD0 0C 00 D0 02 00 B1 00 00 00 00 00\n"
    assert parse_system_id_dell_oem(um_byte) == "00B1"
    assert parse_system_id_dell_oem("# dmidecode 3.6\nGetting SMBIOS data from sysfs.\n") is None
    curto = "Handle 0xD000, DMI type 208, 8 bytes\nOEM-specific Type\n\tHeader and Data:\n\t\tD0 08 00 D0 03 0B FE 00\n"
    assert parse_system_id_dell_oem(curto) is None  # FE sem os bytes do ID estendido: nunca adivinhar


def test_sku_vazio_da_13a_geracao_usa_o_tipo_208() -> None:
    """Caso real do R530: SKU vazio → "systemID não informado" e nenhuma atualização. Agora vem do tipo 208."""
    executor = ReplayAlterado(comandos={("dmidecode", "-s", "system-sku-number"): (0, "\n", "")})
    ctx = Contexto(executor)
    detectar_identificacao(ctx)
    assert parse_sku_dell(ctx.sku) == {"system_id": "0716", "modelo": "PowerEdge R640"}


def test_sku_das_geracoes_novas_continua_valendo() -> None:
    executor = ReplayAlterado(comandos={("dmidecode", "-t", "208"): (0, "", "")})
    ctx = Contexto(executor)
    detectar_identificacao(ctx)
    assert ctx.sku == "SKU=0716;ModelName=PowerEdge R640"


def test_volume_da_perc_nao_conta_como_disco_nao_verificado() -> None:
    """R530: o disco virtual da PERC (/dev/sdb aqui) entrava como "1 NÃO verificado"; os físicos vêm por megaraid,N."""
    import json

    from msscan.provedores.smart import ProvedorSmartctl

    scan = json.loads(ler_saida(R640, "smartctl__--scan-open__-j"))
    scan["devices"].append({"name": "/dev/sdb", "info_name": "/dev/sdb", "type": "scsi", "protocol": "SCSI"})
    lsblk = json.loads(ler_saida(R640, "lsblk__-J__-d__-o__NAME_TRAN_TYPE_SIZE_MODEL"))
    lsblk["blockdevices"].append({"name": "sdb", "tran": None, "type": "disk", "size": "1.1T", "model": "PERC H730P Mini"})
    executor = ReplayAlterado(comandos={
        ("smartctl", "--scan-open", "-j"): (0, json.dumps(scan), ""),
        ("lsblk", "-J", "-d", "-o", "NAME,TRAN,TYPE,SIZE,MODEL"): (0, json.dumps(lsblk), ""),
    })
    resultado = ProvedorSmartctl().coletar(Contexto(executor))
    assert resultado.mensagem == "1 disco(s) verificado(s)"
    assert any("volume de RAID (PERC H730P Mini)" in i for i in resultado.dados["ignorados"])


def test_perc12_reconhecida_e_perccli2_nunca_altera_volume() -> None:
    from msscan.core.executor import validar_dados_cliente
    from msscan.deteccao.detectores import CATEGORIA_POR_DRIVER, FERRAMENTAS_FABRICANTE

    assert CATEGORIA_POR_DRIVER["mpi3mr"] == "raid_hardware"
    assert "perccli2" in FERRAMENTAS_FABRICANTE["storcli"]
    for ferramenta in ("perccli2", "storcli2"):
        assert validar_dados_cliente([f"/x/vendor/{ferramenta}", "/c0/v0", "delete"])
        assert not validar_dados_cliente([ferramenta, "/call", "show", "all", "J"])


GERACOES = {
    11: "R210 R310 R410 R510 R610 R710 R810 R910 R715 R815 T110 T310 T410 T610 T710 M610 M710 M910 C6100",
    12: "R220 R320 R420 R520 R620 R720 R720xd R820 R920 T320 T420 T620 M620 M820 C6220",
    13: "R230 R330 R430 R530 R630 R730 R730xd R830 R930 T130 T330 T430 T630 M630 M830 FC430 FC630 FC830 C6320 C4130",
    14: "R240 R340 R440 R540 R640 R740 R740xd R740xd2 R840 R940 R6415 R7415 R7425 T140 T340 T440 T640 M640 FC640 "
        "MX740c MX840c C6420 C4140 XE2420 XE7100",
    15: "R250 R350 R450 R550 R650 R650xs R750 R750xa R6515 R7515 R6525 R7525 C6520 C6525 T150 T350 T550 MX750c XE8545 "
        "XR11 XR12",
    16: "R260 R360 R660 R760 R860 R960 R6615 R7615 R6625 R7625 T160 T360 T560 MX760c C6620 XE8640 XE9640 XE9680 "
        "HS5610 XR5610 XR7620 XR8610t XR8620t",
    17: "R470 R570 R670 R770 R6715 R7715 R6725 R7725 XE7740 XE7745",
}


def test_geracao_de_toda_a_linha_poweredge() -> None:
    from msscan.geracao_idrac import geracao, idrac

    for numero, modelos in GERACOES.items():
        for modelo in modelos.split():
            assert geracao(f"PowerEdge {modelo}") == numero, modelo
            assert idrac(numero)
    for fora in ("PowerEdge T30", "PowerEdge 2950", "PowerEdge R200", "OptiPlex 7090", "", None):
        assert geracao(fora) is None, fora


def test_nome_do_catalogo_com_espaco_ou_maiuscula_confere_o_modelo() -> None:
    """Catálogo real (14/10/2026): "R650 XS", "R750 xa", "R760XA"; o SMBIOS diz "PowerEdge R650xs". systemID segue exato."""
    from msscan.atualizacao.verificacao import modelo_no_catalogo

    def catalogo(system_id: str, nome: str) -> dict:
        return {"componentes": [{"sistemas": [{"system_id": system_id, "marca": "PowerEdge", "modelo": nome}]}]}

    for nome_catalogo, smbios in (("R650 XS", "PowerEdge R650xs"), ("R750 xa", "PowerEdge R750xa"), ("R760XA", "PowerEdge R760xa")):
        assert modelo_no_catalogo(catalogo("0A6B", nome_catalogo), "SKU=0A6B;ModelName=x", smbios) == ("0A6B", "")
    assert modelo_no_catalogo(catalogo("0A6B", "R650 XS"), "SKU=0A6B;ModelName=x", "PowerEdge R650")[0] is None
    assert modelo_no_catalogo(catalogo("0A6B", "R650 XS"), "SKU=0A6C;ModelName=x", "PowerEdge R650xs")[0] is None


# Servidor desconhecido (gerações e firmwares sem coleta): toda saída é uma destas. Nada pode quebrar, nenhum erro de
# programação pode ser engolido e nenhum componente pode sair OK/ALERTA/FALHA sem evidência.
VARIANTES_LIXO = {
    "vazio": (0, "", ""),
    "lixo": (0, "lixo sem formato\n\x00\xff:::\n||||\n", ""),
    "json_quebrado": (0, '{"a": [1, 2', ""),
    "json_outro": (0, '{"devices": "x", "blockdevices": null, "blah": []}', ""),
    "erro": (1, "", "Error: not supported\n"),
    "numeros": (0, "1 2 3\n| | |\nNaN | inf | -1\n", ""),
    "tabela": (0, "Name | Value | Status\nX | na | ok\nY | 0x00 | ns\n", ""),
}


@pytest.mark.parametrize("variante", VARIANTES_LIXO)
def test_servidor_desconhecido_nunca_quebra_nem_inventa(variante) -> None:
    import json
    import re
    from datetime import datetime

    from msscan.deteccao.detectores import detectar
    from msscan.modulos.registro import registro_padrao
    from msscan.relatorio.completo import montar_relatorio
    from msscan.relatorio.html import gerar_html
    from msscan.relatorio.terminal import formatar_relatorio
    from tests.conftest import ExecutorFalso

    saida = VARIANTES_LIXO[variante]

    class Lixo(ExecutorFalso):
        def _rodar(self, comando, timeout):
            return (*saida, 0.0)

        def _tem_comando(self, nome):
            return True

        def _existe(self, caminho):
            return True

        def _ler_arquivo(self, caminho):
            return saida[1]

        def _listar(self, caminho):
            return ["x0", "eno1"]

    erro_de_programa = re.compile(r"\b(?!ErroParser)[A-Z]\w*(Error|Exception|Iteration)\b")
    ctx = detectar(Lixo())
    assert not [v for v in ctx.erros_deteccao.values() if erro_de_programa.search(str(v))]
    resultados = registro_padrao().diagnosticar(ctx)
    for r in resultados:
        assert not erro_de_programa.search((r.mensagem or "") + json.dumps(r.dados, default=str)), r.componente
        assert r.status.value in ("NAO_VERIFICADO", "NAO_APLICAVEL"), (r.componente, r.mensagem)
    relatorio = montar_relatorio(ctx, resultados, None, ctx.executor, datetime.now(), datetime.now())
    gerar_html(relatorio)
    formatar_relatorio(resultados)
    json.dumps(relatorio, default=str)


def _variantes_redfish():
    import json

    from tests.test_redfish import LOCALSTORAGE

    respostas = json.loads((LOCALSTORAGE / "redfish.json").read_text())
    essenciais = {"GET /redfish/v1", "POST /redfish/v1/SessionService/Sessions"}
    for chave in respostas:
        if chave in essenciais or not chave.startswith("GET "):
            continue
        yield f"sem {chave}", {k: v for k, v in respostas.items() if k != chave}
        for nome, estranho in (("{}", {}), ("Members nulo", {"Members": None, "Status": None}), ("lista", [])):
            alterado = json.loads(json.dumps(respostas))
            alterado[chave]["corpo"] = estranho
            yield f"{chave} = {nome}", alterado


def test_idrac_antigo_ou_firmware_diferente_no_redfish(tmp_path) -> None:
    """iDRAC7/8 (Redfish 1.0.2) e firmwares diferentes: recurso ausente ou com corpo inesperado nunca quebra o diagnóstico
    nem engole erro de programação; o componente cai para a próxima fonte."""
    import json
    import re
    import shutil

    from msscan.provedores.redfish import COMPONENTES_REDFISH
    from msscan.modulos.registro import registro_padrao
    from tests.test_redfish import LOCALSTORAGE, contexto_replay

    erro_de_programa = re.compile(r"\b(?!ErroParser)[A-Z]\w*(Error|Exception|Iteration)\b")
    falhas = []
    for nome, respostas in _variantes_redfish():
        pasta = tmp_path / "rf"
        shutil.rmtree(pasta, ignore_errors=True)
        shutil.copytree(LOCALSTORAGE, pasta)
        (pasta / "redfish.json").write_text(json.dumps(respostas))
        try:
            resultados = registro_padrao().diagnosticar(contexto_replay(pasta), somente=COMPONENTES_REDFISH)
        except Exception as erro:  # noqa: BLE001
            falhas.append((nome, repr(erro)))
            continue
        for r in resultados:
            texto = (r.mensagem or "") + json.dumps(r.dados, default=str)
            if erro_de_programa.search(texto):
                falhas.append((nome, r.componente, erro_de_programa.search(texto).group(0), texto[:300]))
    assert not falhas, "\n".join(map(str, falhas))

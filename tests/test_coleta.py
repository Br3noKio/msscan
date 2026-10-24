"""Testes do modo coleta e da anonimização de fixtures."""

from __future__ import annotations

import json
import tarfile
from datetime import datetime
from pathlib import Path

import pytest

from main import main
from msscan.coleta.anonimizar import (
    CHAVES_JSON,
    RE_ENX,
    RE_IPV4,
    RE_LINHA_IP,
    RE_MAC,
    RE_UUID,
    REMOVIDO,
    _RE_LINHA,
    Anonimizador,
    anonimizar_conteudo,
    anonimizar_pasta,
    anonimizar_texto,
    ip_anonimo,
    mac_anonimo,
)
from msscan.coleta.coletor import coletar, slug_fabricante, slug_modelo
from msscan.core.executor import ExecutorReplay
from msscan.core.modelo import Status
from msscan.deteccao.detectores import detectar
from msscan.modulos.registro import registro_padrao
from msscan.parsers.dmidecode import valor_util
from tests.conftest import PASTA_FIXTURES, R640, ler_saida


def status_diagnostico(pasta: Path) -> dict[str, Status]:
    ctx = detectar(ExecutorReplay(pasta))
    return {r.componente: r.status for r in registro_padrao().diagnosticar(ctx)}


class TestColeta:
    def test_ida_e_volta(self, tmp_path):
        """Coletar (a partir do replay do R640) e reproduzir dá o mesmo diagnóstico."""
        arquivo = coletar(tmp_path, base=ExecutorReplay(R640), agora=datetime(2026, 9, 24, 15, 0, 0))
        assert arquivo.name == "coleta_dell_poweredge_r640_20260924_150000.tar.gz"
        with tarfile.open(arquivo) as tar:
            nomes = tar.getnames()
            tar.extractall(tmp_path / "extraido", filter="data")
        assert "dell/poweredge_r640/origem.json" in nomes
        assert "dell/poweredge_r640/comandos/ipmitool__sel__info.stdout" in nomes
        pasta = tmp_path / "extraido" / "dell" / "poweredge_r640"
        origem = json.loads((pasta / "origem.json").read_text())
        assert origem["modelo"] == "PowerEdge R640" and origem["anonimizado"] is False
        assert status_diagnostico(pasta) == status_diagnostico(R640)

    def test_main_coletar(self, tmp_path, capsys):
        assert main(["--replay", str(R640), "--coletar", "--saida", str(tmp_path)]) == 0
        assert len(list(tmp_path.glob("coleta_dell_poweredge_r640_*.tar.gz"))) == 1
        assert "anonimizar" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("fabricante", "esperado"),
        [("Dell Inc.", "dell"), ("HPE", "hpe"), ("Hewlett Packard Enterprise", "hpe"), ("Supermicro", "supermicro"),
         (None, "desconhecido"), ("Acme Servidores Ltda", "acme_servidores_ltda")],
    )  # fmt: skip
    def test_slug_fabricante(self, fabricante, esperado):
        assert slug_fabricante(fabricante) == esperado

    def test_slug_modelo(self):
        assert slug_modelo("PowerEdge R640") == "poweredge_r640"
        assert slug_modelo("ProLiant DL380 Gen10") == "proliant_dl380_gen10"
        assert slug_modelo(None) == "modelo_desconhecido"


class TestAnonimizar:
    def test_texto_dmidecode(self):
        texto = "\tSize: 8 GB\n\tSerial Number: 12AB34CD\n\tAsset Tag: Not Specified\n\tPart Number: X\n"
        assert anonimizar_texto(texto) == "\tSize: 8 GB\n\tSerial Number: REMOVIDO\n\tAsset Tag: Not Specified\n\tPart Number: X\n"

    def test_json(self):
        original = json.dumps({"model_name": "X", "serial_number": "ABC", "wwn": {"naa": 5, "id": 9}})
        anonimo = json.loads(anonimizar_conteudo(original))
        assert anonimo == {"model_name": "X", "serial_number": REMOVIDO, "wwn": REMOVIDO}

    def test_json_sem_dados_sensiveis_nao_e_reformatado(self):
        texto = ler_saida(R640, "sensors__-j")
        assert anonimizar_conteudo(texto) == texto

    def test_mac_ip_uuid(self):
        anonimizador = Anonimizador()
        texto = "root=UUID=dfe579ec-8d69-4598-aa10-40bf47d88a0d mac 3c:fd:fe:aa:bb:cc e 3C:FD:FE:AA:BB:CC"
        assert anonimizador.texto(texto) == (
            "root=UUID=00000000-0000-0000-0000-000000000001 mac 02:00:00:00:00:01 e 02:00:00:00:00:01"
        )

    def test_ip_json(self):
        dados = [{"ifname": "eno3", "address": "3c:fd:fe:aa:bb:cc", "broadcast": "ff:ff:ff:ff:ff:ff",
                  "addr_info": [{"family": "inet", "local": "10.1.2.3", "broadcast": "10.1.2.255"},
                                {"family": "inet6", "local": "fe80::1234:5678"},
                                {"family": "inet", "local": "127.0.0.1"}]}]  # fmt: skip
        anonimo = json.loads(anonimizar_conteudo(json.dumps(dados)))[0]
        assert anonimo["address"] == "02:00:00:00:00:01"
        assert anonimo["broadcast"] == "ff:ff:ff:ff:ff:ff"
        locais = [a["local"] for a in anonimo["addr_info"]]
        assert locais[0].startswith("192.0.2.") and locais[1].startswith("fe80::") and locais[2] == "127.0.0.1"
        assert locais[1] != "fe80::1234:5678"
        assert anonimo["addr_info"][0]["broadcast"].startswith("192.0.2.")

    def test_pasta_idempotente(self, tmp_path):
        arquivo = coletar(tmp_path, base=ExecutorReplay(R640))
        with tarfile.open(arquivo) as tar:
            tar.extractall(tmp_path / "x", filter="data")
        pasta = tmp_path / "x" / "dell" / "poweredge_r640"
        # A fixture já está anonimizada: nada deve mudar, só o metadado.
        assert anonimizar_pasta(pasta) == []
        assert json.loads((pasta / "origem.json").read_text())["anonimizado"] is True


def _saidas_de_fixtures() -> list[Path]:
    return sorted(PASTA_FIXTURES.rglob("comandos/*.stdout")) + sorted(
        p for p in PASTA_FIXTURES.rglob("arquivos/**/*") if p.is_file()
    )


def _chaves_sensiveis(dados, caminho=""):
    if isinstance(dados, dict):
        for chave, valor in dados.items():
            if chave in CHAVES_JSON and valor != REMOVIDO:
                yield f"{caminho}/{chave}"
            yield from _chaves_sensiveis(valor, f"{caminho}/{chave}")
    elif isinstance(dados, list):
        for i, valor in enumerate(dados):
            yield from _chaves_sensiveis(valor, f"{caminho}[{i}]")


@pytest.mark.parametrize("arquivo", _saidas_de_fixtures(), ids=lambda p: str(p.relative_to(PASTA_FIXTURES)))
def test_fixtures_versionadas_estao_anonimizadas(arquivo):
    """Guarda: nenhuma fixture no repositório pode ter serial, asset tag, UUID ou WWN."""
    texto = arquivo.read_text(encoding="utf-8", errors="replace")
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError:
        dados = None
    if isinstance(dados, (dict, list)):
        assert list(_chaves_sensiveis(dados)) == []
        return
    for linha in texto.splitlines():
        combinacao = _RE_LINHA.match(linha)
        if combinacao:
            assert combinacao["valor"] == REMOVIDO or valor_util(combinacao["valor"]) is None, linha


@pytest.mark.parametrize("arquivo", _saidas_de_fixtures(), ids=lambda p: str(p.relative_to(PASTA_FIXTURES)))
def test_fixtures_sem_mac_uuid_ip_reais(arquivo):
    """Guarda: MAC, UUID e IP só podem aparecer já substituídos (faixas de documentação)."""
    texto = arquivo.read_text(encoding="utf-8", errors="replace")
    assert [m for m in RE_MAC.findall(texto) if not mac_anonimo(m)] == []
    assert [e for e in RE_ENX.findall(texto) if not e.startswith("02000000")] == []
    assert [u for u in RE_UUID.findall(texto) if not u.startswith("00000000-0000-0000-0000-")] == []
    for linha in texto.splitlines():  # 19/10/2026: IP do iDRAC no "ipmitool lan print" passava
        rotulo = RE_LINHA_IP.match(linha)
        if rotulo and "mask" not in rotulo["rotulo"].lower() and "subnet" not in rotulo["rotulo"].lower():
            assert [ip for ip in RE_IPV4.findall(rotulo["resto"]) if not ip_anonimo(ip)] == [], linha
    if arquivo.name.startswith("ip__"):
        enderecos = [a["local"] for i in json.loads(texto) for a in i.get("addr_info", [])]
        assert [e for e in enderecos if not ip_anonimo(e)] == []


def test_ip_em_linha_de_endereco_e_versao_preservada():
    """IPv4 só é trocado em linha de endereço; versões de firmware (mesmo formato) ficam."""
    texto = ("IP Address              : 192.168.0.120\nSubnet Mask             : 255.255.255.0\n"
             "Default Gateway IP      : 192.168.0.1\nFirmware Revision       : 2.65.65.65\n"
             " iDRAC Version = 2.65.65.65\nIPv4.Address=10.1.2.3\nRedfish 1.4.0 em 10.1.2.3\n")
    novo = anonimizar_texto(texto)
    assert "192.168." not in novo and "IPv4.Address=192.0.2." in novo and "255.255.255.0" in novo
    assert novo.count("2.65.65.65") == 2 and "Redfish 1.4.0 em 10.1.2.3" in novo  # texto livre: não é linha de endereço

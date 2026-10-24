"""Testes do executor: real (só comandos inofensivos), replay e gravador."""

from __future__ import annotations

import json

import pytest

from msscan.core.executor import ExecutorGravador, ExecutorReal, ExecutorReplay, nome_arquivo_comando
from tests.conftest import ExecutorFalso


class TestExecutorReal:
    def test_comando_ausente_nao_levanta_excecao(self):
        ex = ExecutorReal()
        rc, saida, erro = ex.rodar(["comando-que-nao-existe-msscan"])
        assert rc == -1
        assert saida == ""
        assert "não encontrado" in erro
        assert ex.historico[-1].rc == -1

    def test_timeout_devolve_rc_menos_um(self):
        ex = ExecutorReal()
        rc, _, erro = ex.rodar(["sleep", "5"], timeout=0.2)
        assert rc == -1
        assert "tempo esgotado" in erro
        assert ex.historico[-1].duracao < 3

    def test_codigo_de_saida_e_historico(self):
        ex = ExecutorReal()
        assert ex.rodar(["true"])[0] == 0
        assert ex.rodar(["false"])[0] == 1
        assert [e.comando for e in ex.historico] == [["true"], ["false"]]
        assert all(e.origem == "real" for e in ex.historico)

    def test_timeout_invalido_usa_padrao(self):
        ex = ExecutorReal()
        assert ex.rodar(["true"], timeout=0)[0] == 0

    def test_string_de_shell_e_recusada(self):
        ex = ExecutorReal()
        rc, _, erro = ex.rodar("echo perigo; rm -rf /")  # type: ignore[arg-type]
        assert rc == -1
        assert "lista" in erro

    def test_locale_c(self):
        rc, saida, _ = ExecutorReal().rodar(["sh", "-c", "echo $LC_ALL"])
        assert rc == 0 and saida.strip() == "C"

    def test_ler_arquivo_inexistente(self):
        assert ExecutorReal().ler_arquivo("/caminho/que/nao/existe") is None


class TestExecutorReplay:
    def test_devolve_saida_gravada(self, pasta_r640):
        ex = ExecutorReplay(pasta_r640)
        rc, saida, _ = ex.rodar(["dmidecode", "-s", "system-product-name"])
        assert rc == 0
        assert saida.strip() == "PowerEdge R640"
        assert ex.historico[-1].origem == "replay"

    def test_comando_nao_gravado_se_comporta_como_ausente(self, pasta_r640):
        ex = ExecutorReplay(pasta_r640)
        rc, _, erro = ex.rodar(["storcli64", "/call", "show", "all", "J"])
        assert rc == -1
        assert "sem gravação" in erro

    def test_existe_usa_caminhos_gravados(self, pasta_r640):
        ex = ExecutorReplay(pasta_r640)
        assert ex.existe("/dev/ipmi0") is True
        assert ex.existe("/dev/ipmi/0") is False

    def test_nao_escapa_da_pasta(self, pasta_r640):
        assert ExecutorReplay(pasta_r640).ler_arquivo("/../../../etc/passwd") is None

    def test_pasta_inexistente(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ExecutorReplay(tmp_path / "nao-existe")


class TestGravador:
    def test_ida_e_volta_gravador_replay(self, tmp_path):
        base = ExecutorFalso(
            comandos={("ipmitool", "sdr", "type", "Power Supply"): (0, "saida\n", "")},
            existentes={"/dev/ipmi0"},
        )
        base.arquivos["/proc/cpuinfo"] = "conteudo"
        base.diretorios["/sys/class/net"] = ["eno1", "lo"]
        grav = ExecutorGravador(tmp_path, base=base)
        grav.rodar(["ipmitool", "sdr", "type", "Power Supply"])
        grav.rodar(["ferramenta-ausente"])
        grav.existe("/dev/ipmi0")
        grav.existe("/dev/ipmi/0")
        grav.ler_arquivo("/proc/cpuinfo")
        grav.listar("/sys/class/net")

        meta = json.loads((tmp_path / "comandos" / "ipmitool__sdr__type__Power_Supply.json").read_text())
        assert meta["comando"] == ["ipmitool", "sdr", "type", "Power Supply"]

        rep = ExecutorReplay(tmp_path)
        assert rep.rodar(["ipmitool", "sdr", "type", "Power Supply"]) == (0, "saida\n", "")
        assert rep.rodar(["ferramenta-ausente"])[0] == -1
        assert rep.existe("/dev/ipmi0") and not rep.existe("/dev/ipmi/0")
        assert rep.ler_arquivo("/proc/cpuinfo") == "conteudo"
        assert rep.listar("/sys/class/net") == ["eno1", "lo"]


def test_nome_arquivo_comando():
    assert nome_arquivo_comando(["ipmitool", "sdr", "type", "Power Supply"]) == "ipmitool__sdr__type__Power_Supply"
    assert nome_arquivo_comando(["storcli64", "/call", "show", "all", "J"]) == "storcli64__call__show__all__J"

"""Compatibilidade com o nome antigo do projeto (ServerDiag → MSscan, 28/09/2026).

Pendrives já preparados (partição ``SERVERDIAG`` ou pasta ``SERVERDIAG/``, com a chave da IA e ``vendor/``)
e os parâmetros de boot ``serverdiag.*`` continuam funcionando; o nome novo sempre tem prioridade.
"""

from __future__ import annotations

import pytest

from msscan.pendrive import local as mod
from msscan.pendrive.local import MIDIA_BOOT, localizar_dados
from msscan.pendrive.particao import OPCOES_MONTAGEM, localizar_ou_montar
from msscan.parsers.procfs import parametro_boot, parse_cmdline
from msscan.rede.verificacao import modo_sem_rede
from msscan.tela.resolucao import resolucao_forcada
from tests.conftest import ExecutorFalso
from tests.test_iso import lancador  # noqa: F401 - fixture do lançador da ISO
from tests.test_pendrive import FINDMNT_ROTULO, executor_cenario, gravacao_pasta, ok

FINDMNT_ANTIGO = ("findmnt", "-J", "-S", "LABEL=SERVERDIAG")


class TestParametrosDeBoot:
    @pytest.mark.parametrize(("cmdline", "esperado"), [
        ("boot=live msscan.semrede=1", "1"), ("boot=live serverdiag.semrede=1", "1"),
        ("serverdiag.semrede=0 msscan.semrede=1", "1"),  # o nome novo tem prioridade
        ("boot=live quiet", None),
    ])  # fmt: skip
    def test_parametro_boot(self, cmdline, esperado):
        assert parametro_boot(parse_cmdline(cmdline), "semrede") == esperado

    @pytest.mark.parametrize("prefixo", ["msscan", "serverdiag"])
    def test_semrede(self, prefixo):
        executor = ExecutorFalso()
        executor.arquivos["/proc/cmdline"] = f"boot=live {prefixo}.semrede=1 quiet\n"
        assert modo_sem_rede(executor)

    @pytest.mark.parametrize("prefixo", ["msscan", "serverdiag"])
    def test_resolucao(self, prefixo):
        assert resolucao_forcada(f"boot=live {prefixo}.resolucao=1280x1024") == (1280, 1024)
        assert resolucao_forcada("boot=live") is None

    def test_texto_no_lancador_da_iso_e_ignorado(self, lancador):  # noqa: F811
        """16/10/2026: sem modo texto no boot; o parâmetro antigo não impede a interface."""
        lancador.cmdline.write_text("boot=live serverdiag.texto=1 quiet\n")
        _, chamadas, _, _ = lancador(MSSCAN_MAX_TENTATIVAS="1", MSSCAN_ESPERA_UI="1")
        assert chamadas[0].startswith("main --ui")


class TestParticaoDeDados:
    def test_rotulo_antigo_ja_montado(self):
        saida = '{"filesystems": [{"target": "/media/sd", "source": "/dev/sdb2", "fstype": "exfat", "options": "rw"}]}'
        executor = ExecutorFalso(comandos={FINDMNT_ROTULO: (1, "", ""), FINDMNT_ANTIGO: (0, saida, "")})
        particao = localizar_ou_montar(executor)
        assert particao.pasta == "/media/sd" and "SERVERDIAG" in particao.mensagem
        assert "renomeie para MSSCAN" in particao.aviso

    def test_rotulo_antigo_montado_agora(self):
        antigo = "/dev/disk/by-label/SERVERDIAG"
        montar = ("mount", "-o", OPCOES_MONTAGEM, antigo, "/mnt/msscan")
        executor = ExecutorFalso(comandos={FINDMNT_ROTULO: (1, "", ""), FINDMNT_ANTIGO: (1, "", ""),
                                           ("mkdir", "-p", "/mnt/msscan"): (0, "", ""), montar: (0, "", "")},
                                 existentes={antigo})  # fmt: skip
        particao = localizar_ou_montar(executor)
        assert particao.pasta == "/mnt/msscan" and particao.montada_agora and particao.aviso

    def test_rotulo_novo_tem_prioridade(self):
        novo, antigo = "/dev/disk/by-label/MSSCAN", "/dev/disk/by-label/SERVERDIAG"
        montar = ("mount", "-o", OPCOES_MONTAGEM, novo, "/mnt/msscan")
        executor = ExecutorFalso(comandos={FINDMNT_ROTULO: (1, "", ""), ("mkdir", "-p", "/mnt/msscan"): (0, "", ""),
                                           montar: (0, "", "")}, existentes={novo, antigo})  # fmt: skip
        particao = localizar_ou_montar(executor)
        assert particao.pasta == "/mnt/msscan" and particao.aviso == "" and "MSSCAN" in particao.mensagem

    def test_aviso_chega_ao_local_dos_dados(self):
        saida = '{"filesystems": [{"target": "/media/sd", "source": "/dev/sdb2", "fstype": "exfat", "options": "rw"}]}'
        executor = executor_cenario("rufus_mbr_uefi_sb", uefi=True, extra={FINDMNT_ANTIGO: (0, saida, "")})
        local = localizar_dados(executor)
        assert local.pasta == "/media/sd" and "renomeie para MSSCAN" in local.aviso
        assert local.para_dict()["aviso"] == local.aviso  # tela inicial e relatório


class TestPastaNoPendriveDeBoot:
    def cenario(self, existentes: set[str], pasta: str):
        executor = executor_cenario(
            "rufus_mbr_uefi_sb", uefi=True,
            extra={FINDMNT_ANTIGO: (1, "", ""), **ok("mount", "-o", "remount,rw", MIDIA_BOOT),
                   **ok("mount", "-o", "remount,ro", MIDIA_BOOT), **gravacao_pasta(pasta)},
        )  # fmt: skip
        executor.existentes |= existentes
        return localizar_dados(executor)

    def test_so_a_pasta_antiga_existe(self):
        antiga = f"{MIDIA_BOOT}/SERVERDIAG"
        local = self.cenario({antiga}, antiga)
        assert local.pasta == antiga and local.persistente and "renomeie para MSSCAN/" in local.aviso

    def test_as_duas_existem_vale_a_nova(self):
        nova = f"{MIDIA_BOOT}/MSSCAN"
        local = self.cenario({nova, f"{MIDIA_BOOT}/SERVERDIAG"}, nova)
        assert local.pasta == nova and not local.aviso

    def test_nenhuma_existe_cria_a_nova(self):
        nova = f"{MIDIA_BOOT}/{mod.PASTA_NO_PENDRIVE}"
        local = self.cenario(set(), nova)
        assert local.pasta == nova and not local.aviso

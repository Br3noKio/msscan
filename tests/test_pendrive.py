"""Forma de gravação do pendrive e escolha do local dos dados (ISO universal).

As saídas de ``findmnt``/``lsblk``/``od`` são reais: capturadas dentro da ISO v0.5.0 no
QEMU, gravada como DD (ISO crua no pendrive) e como o Rufus em modo ISO faz (FAT32, MBR e GPT).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from msscan.parsers import ErroParser
from msscan.parsers.efivar import parse_od_booleano_efi
from msscan.parsers.lsblk import parse_lsblk_arvore
from msscan.pendrive import local as mod
from msscan.pendrive.local import (
    COMANDO_FINDMNT_MIDIA,
    COMANDO_LSBLK,
    COMANDO_SECURE_BOOT,
    DM_VENTOY,
    MIDIA_BOOT,
    OPCOES_VENTOY,
    PASTA_TEMPORARIA,
    PONTO_VENTOY,
    LocalDados,
    detectar_gravacao,
    escrita,
    localizar_dados,
)
from msscan.pendrive.particao import DISPOSITIVO
from tests.conftest import PASTA_FIXTURES, ExecutorFalso

PENDRIVE = PASTA_FIXTURES / "avulsas" / "pendrive"
FINDMNT_ROTULO = ("findmnt", "-J", "-S", "LABEL=MSSCAN")
PASTA_BOOT = f"{MIDIA_BOOT}/MSSCAN"
PROIBIDOS = ("mkfs", "mkfs.fat", "mkfs.exfat", "mkfs.vfat", "fdisk", "sfdisk", "parted", "sgdisk", "dd", "wipefs")


def fixture(nome: str) -> str:
    return (PENDRIVE / nome).read_text()


def executor_cenario(cenario: str, uefi: bool, com_secure_boot: bool = False, extra: dict | None = None) -> ExecutorFalso:
    """Executor com as saídas reais de um cenário (dd_bios, rufus_mbr_uefi_sb, rufus_gpt_uefi)."""
    comandos = {
        tuple(COMANDO_FINDMNT_MIDIA): (0, fixture(f"{cenario}__findmnt_medium.json"), ""),
        tuple(COMANDO_LSBLK): (0, fixture(f"{cenario}__lsblk.json"), ""),
        FINDMNT_ROTULO: (1, "", ""),
    }
    if com_secure_boot:
        comandos[tuple(COMANDO_SECURE_BOOT)] = (0, fixture(f"{cenario}__od_secureboot.txt"), "")
    comandos.update(extra or {})
    return ExecutorFalso(comandos=comandos, existentes={"/sys/firmware/efi"} if uefi else set())


def ok(*comando: str) -> dict[tuple[str, ...], tuple[int, str, str]]:
    return {tuple(comando): (0, "", "")}


def gravacao_pasta(pasta: str) -> dict[tuple[str, ...], tuple[int, str, str]]:
    """Comandos da gravação de teste numa pasta, todos bem-sucedidos."""
    teste = f"{pasta}/{mod.ARQUIVO_TESTE}"
    return {**ok("mkdir", "-p", pasta), **ok("touch", teste), **ok("rm", "-f", teste), **ok("sync")}


def assert_nada_destrutivo(executor: ExecutorFalso) -> None:
    for comando in executor.comandos_rodados():
        assert comando[0] not in PROIBIDOS, comando


# --- Parsers ---------------------------------------------------------------------------------


class TestParsers:
    def test_lsblk_arvore_plana_com_pai(self):
        dispositivos = parse_lsblk_arvore(fixture("dd_bios__lsblk.json"))
        nomes = [d["name"] for d in dispositivos]
        assert nomes == ["/dev/fd0", "/dev/loop0", "/dev/sda", "/dev/sda1", "/dev/sda2", "/dev/sr0"]
        sda1 = dispositivos[3]
        assert sda1["pkname"] == "/dev/sda" and sda1["mountpoint"] == MIDIA_BOOT and "children" not in dispositivos[2]

    def test_lsblk_arvore_invalida(self):
        with pytest.raises(ErroParser):
            parse_lsblk_arvore("{quebrado")

    def test_secure_boot_ligado(self):
        assert parse_od_booleano_efi(fixture("rufus_mbr_uefi_sb__od_secureboot.txt")) is True

    def test_secure_boot_desligado(self):
        # Mesmo formato da saída real acima, com o byte de valor 0.
        assert parse_od_booleano_efi("   6   0   0   0   0\n") is False

    @pytest.mark.parametrize("texto", ["", "abc", "   6   0   0   0", "   6   0   0   0   7"])
    def test_secure_boot_inesperado(self, texto):
        with pytest.raises(ErroParser):
            parse_od_booleano_efi(texto)


# --- Forma de gravação ------------------------------------------------------------------------


class TestFormaGravacao:
    def test_dd_bios(self):
        forma = detectar_gravacao(executor_cenario("dd_bios", uefi=False))
        assert (forma.metodo, forma.sistema_arquivos, forma.tabela) == ("dd", "iso9660", "MBR")
        assert forma.boot == "BIOS legado" and forma.secure_boot is None
        assert forma.dispositivo == "/dev/sda1" and forma.disco == "/dev/sda"
        assert "Rufus modo DD" in forma.descricao and forma.descricao_boot == "BIOS legado"

    def test_rufus_iso_mbr_uefi_secure_boot(self):
        forma = detectar_gravacao(executor_cenario("rufus_mbr_uefi_sb", uefi=True, com_secure_boot=True))
        assert (forma.metodo, forma.sistema_arquivos, forma.tabela) == ("rufus_iso", "vfat", "MBR")
        assert forma.boot == "UEFI" and forma.secure_boot is True
        assert "Rufus modo ISO" in forma.descricao and "(VFAT, MBR)" in forma.descricao
        assert forma.descricao_boot == "UEFI, Secure Boot ligado"

    def test_rufus_iso_gpt_uefi_sem_variavel_secure_boot(self):
        forma = detectar_gravacao(executor_cenario("rufus_gpt_uefi", uefi=True))
        assert (forma.metodo, forma.tabela) == ("rufus_iso", "GPT")
        assert forma.secure_boot is False and "SecureBoot ausente" in forma.observacoes[0]

    def test_ventoy_pela_particao_vtoyefi(self):
        # TODO(fixture): lsblk real de um pendrive do Ventoy. Aqui, a saída real do DD com uma partição
        # "VTOYEFI" acrescentada (rótulo documentado pelo Ventoy).
        lsblk = fixture("dd_bios__lsblk.json").replace('"label": null,\n               "pttype": "dos",\n               "parttype": "0xef"',
                                                     '"label": "VTOYEFI",\n               "pttype": "dos",\n               "parttype": "0xef"')  # fmt: skip
        assert "VTOYEFI" in lsblk
        executor = executor_cenario("dd_bios", uefi=False, extra={tuple(COMANDO_LSBLK): (0, lsblk, "")})
        forma = detectar_gravacao(executor)
        assert forma.metodo == "ventoy" and forma.tabela is None

    def test_sem_midia_de_boot(self):
        executor = ExecutorFalso(comandos={tuple(COMANDO_FINDMNT_MIDIA): (1, "", "")})
        forma = detectar_gravacao(executor)
        assert forma.metodo == "desconhecido" and "não identificada" in forma.descricao
        assert any(MIDIA_BOOT in o for o in forma.observacoes)

    def test_para_dict(self):
        dados = detectar_gravacao(executor_cenario("rufus_gpt_uefi", uefi=True)).para_dict()
        assert dados["metodo"] == "rufus_iso" and dados["descricao_boot"].startswith("UEFI")


# --- Local dos dados --------------------------------------------------------------------------


class TestLocalDados:
    def test_particao_msscan_tem_prioridade(self):
        saida = (PASTA_FIXTURES / "avulsas" / "findmnt_raiz.json").read_text().replace('"/"', '"/media/pendrive"')
        executor = executor_cenario("rufus_mbr_uefi_sb", uefi=True, com_secure_boot=True, extra={FINDMNT_ROTULO: (0, saida, "")})
        local = localizar_dados(executor)
        assert (local.tipo, local.pasta, local.persistente) == ("particao_msscan", "/media/pendrive", True)
        assert local.gravacao.metodo == "rufus_iso"
        assert ["mount", "-o", "remount,rw", MIDIA_BOOT] not in executor.comandos_rodados()

    def test_rufus_iso_usa_pasta_no_pendrive_de_boot(self):
        executor = executor_cenario(
            "rufus_mbr_uefi_sb", uefi=True, com_secure_boot=True,
            extra={**ok("mount", "-o", "remount,rw", MIDIA_BOOT), **ok("mount", "-o", "remount,ro", MIDIA_BOOT),
                   **gravacao_pasta(PASTA_BOOT)},
        )  # fmt: skip
        local = localizar_dados(executor)
        assert (local.tipo, local.pasta, local.persistente) == ("pendrive_boot", PASTA_BOOT, True)
        assert local.remontar == MIDIA_BOOT and not local.aviso
        rodados = executor.comandos_rodados()
        rw = rodados.index(["mount", "-o", "remount,rw", MIDIA_BOOT])
        assert rw < rodados.index(["touch", f"{PASTA_BOOT}/{mod.ARQUIVO_TESTE}"]) < rodados.index(["sync"])
        # Depois do teste, o pendrive de boot volta a ser somente leitura.
        assert rodados[-1] == ["mount", "-o", "remount,ro", MIDIA_BOOT]
        assert_nada_destrutivo(executor)

    def test_rufus_iso_protegido_contra_gravacao_cai_para_tmp(self):
        executor = executor_cenario("rufus_gpt_uefi", uefi=True, extra={
            ("mount", "-o", "remount,rw", MIDIA_BOOT): (32, "", "mount: /run/live/medium: cannot remount /dev/sda1 read-write, is write-protected."),
            **ok("mkdir", "-p", PASTA_TEMPORARIA),
        })  # fmt: skip
        local = localizar_dados(executor)
        assert (local.tipo, local.pasta, local.persistente) == ("temporario", PASTA_TEMPORARIA, False)
        assert "PERDIDOS" in local.aviso and any("write-protected" in t for t in local.tentativas)
        assert_nada_destrutivo(executor)

    def test_dd_sem_particao_vai_para_tmp_com_aviso(self):
        executor = executor_cenario("dd_bios", uefi=False, extra=ok("mkdir", "-p", PASTA_TEMPORARIA))
        local = localizar_dados(executor)
        assert local.tipo == "temporario" and not local.persistente
        assert "nada será salvo no pendrive" in local.mensagem and "Rufus em modo ISO" in local.aviso
        assert any("iso9660" in t for t in local.tentativas)
        assert not any(c[:2] == ["mount", "-o"] for c in executor.comandos_rodados())
        assert_nada_destrutivo(executor)

    def test_ventoy_monta_a_particao_de_dados(self):
        # TODO(fixture): saídas reais de um pendrive do Ventoy (ver test_ventoy_pela_particao_vtoyefi).
        lsblk = fixture("rufus_mbr_uefi_sb__lsblk.json").replace('"mountpoint": "/run/live/medium"', '"mountpoint": null')
        lsblk = lsblk.replace('"label": "SDIAG_BOOT"', '"label": "Ventoy"').replace(
            '"parttype": "0xc",', '"parttype": "0xc",'
        )
        efi = '{"name": "/dev/sda2", "pkname": "/dev/sda", "type": "part", "tran": null, "rm": false, "ro": false, "fstype": "vfat", "label": "VTOYEFI", "pttype": "dos", "parttype": "0xef", "mountpoint": null}'
        lsblk = lsblk.replace('"mountpoint": null\n            }\n         ]', '"mountpoint": null\n            },' + efi + "\n         ]")
        assert "VTOYEFI" in lsblk
        pasta = f"{PONTO_VENTOY}/MSSCAN"
        executor = executor_cenario("dd_bios", uefi=False, extra={
            tuple(COMANDO_LSBLK): (0, lsblk, ""),
            **ok("mkdir", "-p", PONTO_VENTOY),
            ("mount", "-o", OPCOES_VENTOY, "/dev/sda1", PONTO_VENTOY): (32, "", "mount: /mnt/msscan-ventoy: /dev/sda1 already mounted or mount point busy."),
            ("blockdev", "--getsz", "/dev/sda1"): (0, "2048000\n", ""),
            **ok("dmsetup", "create", DM_VENTOY, "--table", "0 2048000 linear /dev/sda1 0"),
            **ok("mount", "-o", OPCOES_VENTOY, f"/dev/mapper/{DM_VENTOY}", PONTO_VENTOY),
            **gravacao_pasta(pasta),
        })  # fmt: skip
        local = localizar_dados(executor)
        assert (local.tipo, local.pasta, local.persistente) == ("ventoy", pasta, True)
        assert local.gravacao.metodo == "ventoy"
        assert_nada_destrutivo(executor)

    def test_ventoy_sem_montagem_possivel_cai_para_tmp(self):
        lsblk = fixture("dd_bios__lsblk.json").replace('"label": null,\n               "pttype": "dos",\n               "parttype": "0xef"',
                                                     '"label": "VTOYEFI",\n               "pttype": "dos",\n               "parttype": "0xef"')  # fmt: skip
        lsblk = lsblk.replace('"fstype": "iso9660",\n               "label": "SDIAG_BOOT"', '"fstype": "exfat",\n               "label": "Ventoy"')
        lsblk = lsblk.replace('"mountpoint": "/run/live/medium"', '"mountpoint": null')
        executor = executor_cenario("dd_bios", uefi=False, extra={tuple(COMANDO_LSBLK): (0, lsblk, "")})
        local = localizar_dados(executor)
        assert local.tipo == "temporario" and any(t.startswith("Ventoy: não foi possível montar") for t in local.tentativas)

    def test_nunca_levanta_excecao_sem_nada(self):
        local = localizar_dados(ExecutorFalso())
        assert local.tipo == "temporario" and local.gravacao.metodo == "desconhecido"


class TestEscrita:
    def test_pendrive_de_boot_rw_so_durante_a_gravacao(self):
        executor = ExecutorFalso(comandos={**ok("mount", "-o", "remount,rw", MIDIA_BOOT), **ok("mount", "-o", "remount,ro", MIDIA_BOOT), **ok("sync")})
        local = LocalDados(PASTA_BOOT, "pendrive_boot", True, "", remontar=MIDIA_BOOT)
        with escrita(executor, local):
            assert executor.comandos_rodados() == [["mount", "-o", "remount,rw", MIDIA_BOOT]]
        assert executor.comandos_rodados()[1:] == [["sync"], ["mount", "-o", "remount,ro", MIDIA_BOOT]]

    def test_volta_a_somente_leitura_mesmo_com_erro(self):
        executor = ExecutorFalso()
        local = LocalDados(PASTA_BOOT, "pendrive_boot", True, "", remontar=MIDIA_BOOT)
        with pytest.raises(OSError), escrita(executor, local):
            raise OSError("disco cheio")
        assert executor.comandos_rodados()[-1] == ["mount", "-o", "remount,ro", MIDIA_BOOT]

    def test_particao_so_sync(self):
        executor = ExecutorFalso()
        with escrita(executor, LocalDados("/mnt/msscan", "particao_msscan", True, "")):
            pass
        assert executor.comandos_rodados() == [["sync"]]

    def test_sem_local_nada(self):
        executor = ExecutorFalso()
        with escrita(executor, None):
            pass
        assert executor.comandos_rodados() == []


# --- Tela, relatório e main -------------------------------------------------------------------


def local_rufus() -> LocalDados:
    executor = executor_cenario(
        "rufus_mbr_uefi_sb", uefi=True, com_secure_boot=True,
        extra={**ok("mount", "-o", "remount,rw", MIDIA_BOOT), **ok("mount", "-o", "remount,ro", MIDIA_BOOT),
               **gravacao_pasta(PASTA_BOOT)},
    )  # fmt: skip
    return localizar_dados(executor)


def local_tmp() -> LocalDados:
    return localizar_dados(executor_cenario("dd_bios", uefi=False, extra=ok("mkdir", "-p", PASTA_TEMPORARIA)))


class TestExibicao:
    def test_resumo_e_capacidades(self):
        from msscan.deteccao.contexto import Contexto
        from msscan.deteccao.resumo import capacidades, formatar_resumo

        ctx = Contexto(executor=ExecutorFalso(), pasta_dados=PASTA_BOOT, pendrive=local_rufus().para_dict())
        resumo = formatar_resumo(ctx)
        assert "Pendrive: arquivos copiados para o pendrive (Rufus modo ISO) (VFAT, MBR); boot UEFI, Secure Boot ligado" in resumo
        assert f"Dados: pasta MSSCAN/ no próprio pendrive de boot ({PASTA_BOOT})" in resumo
        itens = {c["chave"]: c for c in capacidades(ctx)}
        assert itens["gravacao"]["disponivel"] and itens["pendrive"]["disponivel"]

    def test_tmp_aparece_como_aviso(self):
        from msscan.deteccao.contexto import Contexto
        from msscan.deteccao.resumo import capacidades, formatar_resumo

        ctx = Contexto(executor=ExecutorFalso(), pasta_dados=PASTA_TEMPORARIA, pendrive=local_tmp().para_dict())
        assert "PERDIDOS" in formatar_resumo(ctx)
        item = {c["chave"]: c for c in capacidades(ctx)}["pendrive"]
        assert not item["disponivel"] and "PERDIDOS" in item["detalhe"]

    def test_relatorio_json_e_html(self):
        from msscan.relatorio.html import gerar_html

        relatorio = {"pendrive": local_tmp().para_dict(), "medido": []}
        html = gerar_html(relatorio)
        assert "imagem gravada bit a bit" in html and "PERDIDOS" in html and "BIOS legado" in html

    def test_gravar_relatorios_temporario_avisa(self, tmp_path):
        from msscan.relatorio.completo import gravar_relatorios

        gravado = gravar_relatorios({"equipamento": {"modelo": "X"}}, "t", str(tmp_path), replay=False, persistente=False)
        assert not gravado.no_pendrive and "NADA foi salvo no pendrive" in gravado.mensagem
        assert Path(gravado.pasta, "relatorio.json").is_file()

    def test_main_dados_manual(self):
        from main import etapa_dados

        local = etapa_dados(argparse.Namespace(dados="/x", replay=None), ExecutorFalso())
        assert local is not None and (local.tipo, local.pasta, local.persistente) == ("manual", "/x", True)

    def test_main_replay_sem_local(self):
        from main import etapa_dados

        assert etapa_dados(argparse.Namespace(dados=None, replay="/fixture"), ExecutorFalso()) is None

    def test_particao_continua_sem_formatar(self):
        assert DISPOSITIVO == "/dev/disk/by-label/MSSCAN"


def test_midia_optica_sem_tabela():
    # Mesma saída real do DD, com a mídia de boot num CD (como a mídia virtual do iDRAC).
    lsblk = fixture("dd_bios__lsblk.json").replace('"name": "/dev/sr0"', '"name": "/dev/sr0x"')
    lsblk = lsblk.replace('"type": "rom"', '"type": "rom", "x": 1')
    findmnt = fixture("dd_bios__findmnt_medium.json").replace('"/dev/sda1"', '"/dev/sr0x"')
    executor = executor_cenario("dd_bios", uefi=False, extra={tuple(COMANDO_LSBLK): (0, lsblk, ""), tuple(COMANDO_FINDMNT_MIDIA): (0, findmnt, "")})
    forma = detectar_gravacao(executor)
    assert forma.metodo == "midia_optica" and forma.tabela is None and "(ISO9660)" in forma.descricao

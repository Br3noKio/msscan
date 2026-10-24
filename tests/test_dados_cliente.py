"""CLAUDE.md 18.5: acesso total ao hardware, menos os dados do cliente — o executor recusa antes de executar."""

from __future__ import annotations

import pytest

from msscan.core.executor import validar_dados_cliente
from tests.conftest import ExecutorFalso


@pytest.mark.parametrize("comando", [
    ["mount", "/dev/sda1", "/mnt/x"], ["mount", "-o", "ro", "/dev/sda2", "/mnt/msscan"], ["mount", "-o", "rw,noatime,fmask=0022,dmask=0022", "/dev/nvme0n1p2", "/tmp/a"],
    ["mount", "-t", "ntfs3", "/dev/sdb1", "/mnt/msscan"], ["fsck", "-n", "/dev/sda1"], ["e2fsck", "/dev/sda1"], ["debugfs", "/dev/sda1"],
    ["dd", "if=/dev/sda", "of=/tmp/x"], ["cp", "/mnt/cliente/etc/shadow", "/tmp"], ["mkfs.ext4", "/dev/sdb"], ["parted", "/dev/sda", "print"],
    ["wipefs", "-a", "/dev/sda"], ["ntfs-3g", "/dev/sda2", "/mnt"], ["cryptsetup", "open", "/dev/sda3", "x"],
    ["storcli64", "/c0", "add", "vd", "r1", "drives=32:0-1"], ["storcli64", "/c0/v0", "del"], ["perccli64", "/c0/v0", "start", "init"],
    ["MegaCli64", "-CfgLdAdd", "-r1[32:0,32:1]", "-a0"], ["ssacli", "ctrl", "slot=0", "create", "type=ld", "drives=1I:1:1"],
    ["arcconf", "DELETE", "1", "LOGICALDRIVE", "1"], ["mdadm", "--assemble", "--scan"], ["mdadm", "--create", "/dev/md0"],
    ["mdadm", "/dev/md0", "--fail", "/dev/sda1"],
])  # fmt: skip
def test_recusa_tudo_o_que_toca_nos_dados_do_cliente(comando):
    assert validar_dados_cliente(comando)
    executor = ExecutorFalso(comandos={tuple(comando): (0, "", "")})
    rc, _, erro = executor.rodar(comando)
    assert rc == -1 and erro and executor.historico[-1].rc == -1  # nunca chega a executar


@pytest.mark.parametrize("comando", [
    ["mount", "-o", "rw,noatime,fmask=0022,dmask=0022", "/dev/sdc1", "/mnt/msscan"], ["mount", "-o", "remount,rw", "/run/live/medium"],
    ["mount", "-o", "remount,ro", "/mnt/msscan-ventoy"], ["smartctl", "-a", "/dev/sda"], ["lsblk", "-J"], ["mdadm", "--examine", "--scan"],
    ["mdadm", "--detail", "/dev/md0"], ["storcli64", "/c0", "show", "all", "J"], ["perccli64", "/c0/v0", "show", "J"], ["ipmitool", "sdr"],
])  # fmt: skip
def test_diagnostico_e_pendrive_continuam(comando):
    assert validar_dados_cliente(comando) == ""

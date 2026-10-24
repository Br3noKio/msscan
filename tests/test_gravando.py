"""Revisão independente, ALTO 1 e MÉDIO 7: gravação de firmware nunca interrompida pelo MSscan."""

from __future__ import annotations

import os
import signal

import pytest

import main
from msscan.core import gravando as modulo
from msscan.core.executor import ExecutorReal


def test_marcador_aninhado(tmp_path, monkeypatch):
    monkeypatch.setattr(modulo, "MARCADOR", tmp_path / "run" / "gravando")
    assert not modulo.em_gravacao()
    with modulo.gravando("firmware"):
        assert modulo.MARCADOR.read_text() == "firmware\n" and modulo.em_gravacao()
        with modulo.gravando("interno"):
            pass
        assert modulo.MARCADOR.exists()  # só some quando a última gravação termina
    assert not modulo.MARCADOR.exists() and not modulo.em_gravacao()


def test_sigterm_ignorado_so_durante_gravacao(tmp_path, monkeypatch):
    monkeypatch.setattr(modulo, "MARCADOR", tmp_path / "gravando")
    with modulo.gravando("firmware"):
        main._sigterm(signal.SIGTERM, None)  # não encerra
    with pytest.raises(SystemExit):
        main._sigterm(signal.SIGTERM, None)


def test_dup_desacoplado_saida_em_arquivo_e_nunca_morto():
    executor = ExecutorReal()
    rc, saida, erro, _ = executor._rodar_gravacao(["sh", "-c", "echo progresso; echo erro >&2; exit 3"], 10)
    assert (rc, erro) == (3, "") and "progresso" in saida and "erro" in saida
    rc, _, erro, _ = executor._rodar_gravacao(["sh", "-c", "echo $$ > /dev/null; exec sleep 37.25"], 0.5)
    assert rc < 0 and "NÃO desligue" in erro
    # o processo continua vivo, em outra sessão (sinal para o MSscan não chega a ele): limpa aqui
    vivos = [p for p in os.listdir("/proc") if p.isdigit() and _cmd(p) == "sleep37.25"]
    assert vivos and all(os.getsid(int(p)) != os.getsid(0) for p in vivos)
    for p in vivos:
        os.kill(int(p), signal.SIGKILL)


def _cmd(pid: str) -> str:
    try:
        return open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b"").decode()
    except OSError:
        return ""


@pytest.mark.parametrize("comando", [["ipmitool", "fru", "write", "0", "x"], ["ipmitool", "hpm", "upgrade", "f"],
                                     ["ipmitool", "chassis", "power", "soft"], ["ipmitool", "chassis", "identify"]])  # fmt: skip
def test_ipmitool_de_escrita_ampliado(comando):
    from msscan.core.executor import _escrita_ipmitool

    assert _escrita_ipmitool(comando)
    assert not _escrita_ipmitool(["ipmitool", "sel", "elist"]) and not _escrita_ipmitool(["ipmitool", "sdr", "type", "Power Supply"])

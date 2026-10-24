"""Marcador "gravando firmware / alterando o BMC" (revisão independente de 30/09/2026, achado ALTO 1).

Enquanto existir ``/run/msscan/gravando``:
- o lançador da ISO (``msscan-iniciar``) não encerra o backend, mesmo que a interface gráfica caia;
- o MSscan ignora SIGTERM (``main._sigterm``): interromper uma gravação pode inutilizar o componente;
- os botões Desligar/Reiniciar ficam bloqueados (a sessão já bloqueia pelo estado da aplicação).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MARCADOR = Path("/run/msscan/gravando")
_trava = threading.Lock()
_ativos = 0


@contextmanager
def gravando(descricao: str) -> Iterator[None]:
    """Marca o início e o fim de uma gravação (aninhável; nunca levanta exceção por causa do arquivo)."""
    global _ativos
    with _trava:
        _ativos += 1
        try:
            MARCADOR.parent.mkdir(parents=True, exist_ok=True)
            MARCADOR.write_text(descricao + "\n", encoding="utf-8")
        except OSError:
            pass  # sem /run gravável: o bloqueio de SIGTERM continua valendo pelo contador
    try:
        yield
    finally:
        with _trava:
            _ativos -= 1
            if _ativos == 0:
                try:
                    MARCADOR.unlink()
                except OSError:
                    pass


def em_gravacao() -> bool:
    """Há uma gravação em andamento neste processo."""
    return _ativos > 0

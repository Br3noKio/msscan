"""Logs das tentativas da interface gráfica que não abriram (pedido de 16/10/2026: sem modo texto no boot).

O ``msscan-iniciar`` guarda os logs de cada tentativa (Xorg, navegador, vídeo) em ``/run/msscan-interface/`` e, quando
nenhum vídeo abriu, chama ``python3 -m msscan.interface.falhas <pasta>``: copia as tentativas para
``MSSCAN/logs/interface_<data>/`` no pendrive, para o técnico mandar. Nunca levanta exceção.
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

from msscan.core.executor import Executor, ExecutorReal
from msscan.pendrive.local import escrita, localizar_dados
from msscan.relatorio.log_tecnico import PASTA_LOGS


def gravar(origem: Path, executor: Executor, agora: datetime | None = None) -> Path | None:
    """Copia as subpastas de ``origem`` para o pendrive; devolve o destino, ou ``None`` se não gravou."""
    try:
        tentativas = sorted(p for p in origem.iterdir() if p.is_dir())
        if not tentativas:
            return None
        local = localizar_dados(executor)
        if not local.persistente or not local.pasta:
            return None
        destino = Path(local.pasta) / PASTA_LOGS / f"interface_{(agora or datetime.now()):%Y-%m-%d_%H%M%S}"
        with escrita(executor, local):
            for tentativa in tentativas:
                shutil.copytree(tentativa, destino / tentativa.name, dirs_exist_ok=True)
        return destino
    except Exception:  # noqa: BLE001 - os logs nunca impedem a próxima tentativa da interface
        return None


def main(argv: list[str]) -> int:
    destino = gravar(Path(argv[1] if len(argv) > 1 else "/run/msscan-interface"), ExecutorReal())
    if destino:
        print(f"Registros das tentativas gravados no pendrive: {destino}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

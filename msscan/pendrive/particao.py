"""Localiza e monta a partição de dados do pendrive (rótulo ``MSSCAN``, exFAT).

A partição guarda ``config/`` (proxy, chave da IA), ``vendor/`` (ferramentas
proprietárias), ``relatorios/`` e ``estado/``. Nunca é formatada pelo MSscan.
"""

from __future__ import annotations

from dataclasses import dataclass

from msscan.core.executor import Executor
from msscan.parsers import ErroParser
from msscan.parsers.findmnt import parse_findmnt_json

ROTULO = "MSSCAN"
ROTULO_ANTIGO = "SERVERDIAG"
DISPOSITIVO = f"/dev/disk/by-label/{ROTULO}"
"""Nome antigo do projeto: pendrives já preparados continuam funcionando (com aviso para renomear)."""
PONTO_MONTAGEM = "/mnt/msscan"
AVISO_ROTULO_ANTIGO = (
    f"A partição de dados ainda tem o rótulo antigo {ROTULO_ANTIGO}: funciona, mas renomeie para {ROTULO} "
    "(no Windows: botão direito na unidade > Renomear)."
)
# fmask/dmask valem para exFAT e FAT32 e deixam as ferramentas de vendor/ executáveis.
OPCOES_MONTAGEM = "rw,noatime,fmask=0022,dmask=0022"


@dataclass
class ParticaoDados:
    """Onde está a partição de dados (``pasta`` ``None`` = indisponível) e o que aconteceu."""

    pasta: str | None
    montada_agora: bool
    mensagem: str
    aviso: str = ""


def localizar_ou_montar(executor: Executor, ponto: str = PONTO_MONTAGEM) -> ParticaoDados:
    """Usa a partição ``MSSCAN`` (ou a antiga ``SERVERDIAG``) se já estiver montada; senão, monta pelo rótulo."""
    for rotulo in (ROTULO, ROTULO_ANTIGO):
        aviso = AVISO_ROTULO_ANTIGO if rotulo == ROTULO_ANTIGO else ""
        rc, saida, _ = executor.rodar(["findmnt", "-J", "-S", f"LABEL={rotulo}"], timeout=10)
        if rc == 0:
            try:
                montadas = parse_findmnt_json(saida)
            except ErroParser:
                montadas = []
            if montadas:
                alvo = montadas[0].get("target")
                return ParticaoDados(alvo, False, f"partição {rotulo} já montada em {alvo}", aviso)
        dispositivo = DISPOSITIVO if rotulo == ROTULO else f"/dev/disk/by-label/{rotulo}"
        if not executor.existe(dispositivo):
            continue
        executor.rodar(["mkdir", "-p", ponto], timeout=10)
        rc, _, erro = executor.rodar(["mount", "-o", OPCOES_MONTAGEM, dispositivo, ponto], timeout=30)
        if rc != 0:
            detalhe = erro.strip().splitlines()[-1] if erro.strip() else f"código {rc}"
            return ParticaoDados(None, False, f"não foi possível montar a partição {rotulo}: {detalhe}")
        return ParticaoDados(ponto, True, f"partição {rotulo} montada em {ponto}", aviso)
    return ParticaoDados(
        None, False, f"partição {ROTULO} não encontrada (o pendrive tem a partição de dados? veja iso/README.md)"
    )

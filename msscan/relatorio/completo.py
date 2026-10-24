"""Relatório completo (JSON) e gravação em ``<dados>/relatorios/<modelo>_<data>/``."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from msscan import __version__
from msscan import textos
from msscan.coleta.coletor import slug_modelo
from msscan.core.executor import Executor
from msscan.configuracao import avaliar_configuracao
from msscan.modelo3d import estado_geral
from msscan.modelo3d import montar as montar_modelo3d
from msscan.localizacao import localizar_relatorio
from msscan.core.modelo import Resultado, Status
from msscan.deteccao.contexto import Contexto
from msscan.rede.verificacao import ResultadoRede
from msscan.relatorio.bmc import resumo_acesso_bmc
from msscan.relatorio.html import gerar_html
from msscan.relatorio.terminal import veredito

PASTA_RESERVA = "/tmp/msscan-relatorios"
# Logs das tentativas da interface gráfica que falharam (msscan-iniciar, pedido de 16/10/2026): vão junto do relatório.
PASTA_FALHAS_INTERFACE = Path("/run/msscan-interface")


def montar_relatorio(
    ctx: Contexto,
    resultados: list[Resultado],
    rede: ResultadoRede | None,
    executor: Executor,
    inicio: datetime,
    fim: datetime,
    titulo_de: Callable[[str], str] = lambda nome: nome,
) -> dict[str, Any]:
    """Monta o relatório completo, serializável em JSON.

    Seções separadas: "medido" (tudo que foi coletado pelas ferramentas),
    "interpretacao_ia" e "atualizacoes" (preenchidas nas fases 7 e 8+).
    """
    contagem = {s.value: sum(r.status == s for r in resultados) for s in Status}
    relatorio = {
        "msscan": {"versao": __version__, "modo": executor.origem},
        "inicio": inicio.isoformat(timespec="seconds"),
        "fim": fim.isoformat(timespec="seconds"),
        "equipamento": ctx.para_dict(),
        "veredito": veredito(resultados),
        "contagem": contagem,
        "rede": rede.para_dict() if rede else None,
        "pendrive": ctx.pendrive,
        "bmc": resumo_acesso_bmc(ctx, resultados, titulo_de),
        "medido": [dict(r.para_dict(), titulo=titulo_de(r.componente)) for r in resultados],
        "interpretacao_ia": {"situacao": "nao_solicitada"},
        "atualizacoes": {"situacao": "nao_solicitadas"},
        "historico_comandos": executor.historico_dict(),
    }
    # Onde está o problema (CLAUDE.md 15.3): área física de cada problema e, se o modelo tiver, o mapa do chassi.
    relatorio["localizacao"] = localizar_relatorio(relatorio)
    # Configuração (pedido de 01/10/2026): processadores, pentes por processador/canal, fontes e discos × regras do modelo.
    relatorio["configuracao"] = avaliar_configuracao(relatorio)
    # Modelo 3D do chassi (CLAUDE.md 18.1): peças, presença e problema atual × histórico; só modelos com ``modelo3d``.
    relatorio["modelo3d"] = montar_modelo3d(relatorio)
    # Indicador do estado geral (pedido de 02/10/2026): Tudo OK / Atenção / Problema, nunca OK com item não verificado.
    relatorio["estado_geral"] = estado_geral(relatorio)
    return relatorio


def _para_json(valor: Any) -> Any:
    if isinstance(valor, Status):
        return valor.value
    if isinstance(valor, (set, tuple)):
        return list(valor)
    return str(valor)


def salvar_relatorios(
    relatorio: dict[str, Any],
    texto_terminal: str,
    pasta_dados: str | None,
    agora: datetime | None = None,
    pasta: Path | None = None,
) -> tuple[Path, bool]:
    """Grava ``relatorio.json``, ``relatorio.html`` e ``relatorio.txt``.

    Destino: ``<pasta_dados>/relatorios/<modelo>_<AAAA-MM-DD_HHMMSS>/``. Sem
    partição de dados, grava em ``/tmp/msscan-relatorios`` (perde-se ao
    desligar). ``pasta``: regrava numa pasta existente (relatório refeito). Devolve ``(pasta, no_pendrive)``.
    """
    agora = agora or datetime.now()
    if pasta is None:
        modelo = slug_modelo((relatorio.get("equipamento") or {}).get("modelo"))
        raiz = Path(pasta_dados) / "relatorios" if pasta_dados else Path(PASTA_RESERVA)
        pasta = raiz / f"{modelo}_{agora:%Y-%m-%d_%H%M%S}"
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / "relatorio.json").write_text(
        json.dumps(relatorio, ensure_ascii=False, indent=2, default=_para_json) + "\n", encoding="utf-8"
    )
    (pasta / "relatorio.html").write_text(gerar_html(relatorio), encoding="utf-8")
    rodape = textos.t("py_rel_html.gerado_pelo_msscan_em", (relatorio.get("msscan") or {}).get("versao", ""), textos.data_br(relatorio.get("fim")))
    (pasta / "relatorio.txt").write_text(f"{texto_terminal}\n\n{rodape}\n", encoding="utf-8")
    _copiar_falhas_interface(pasta)
    if hasattr(os, "sync"):
        os.sync()  # o técnico costuma arrancar o pendrive logo em seguida
    return pasta, pasta_dados is not None


def _copiar_falhas_interface(pasta: Path) -> None:
    """Copia para ``<pasta>/interface_falhou/`` uma subpasta por tentativa da interface que não abriu ou caiu (logs do
    Xorg, do navegador e do vídeo). Sem tentativas com falha, não cria nada. Nunca levanta exceção."""
    try:
        tentativas = sorted(p for p in PASTA_FALHAS_INTERFACE.iterdir() if p.is_dir())
    except OSError:
        return
    for tentativa in tentativas:
        try:
            shutil.copytree(tentativa, pasta / "interface_falhou" / tentativa.name, dirs_exist_ok=True)
        except (OSError, shutil.Error):
            pass


@dataclass
class Gravacao:
    """Resultado da gravação dos relatórios (mostrado no terminal e na interface)."""

    pasta: Path | None
    no_pendrive: bool
    mensagem: str


def gravar_relatorios(
    relatorio: dict[str, Any],
    texto_terminal: str,
    pasta_dados: str | None,
    replay: bool,
    persistente: bool = True,
    pasta: Path | None = None,
) -> Gravacao:
    """Decide onde gravar e grava; nunca levanta exceção.

    Replay sem pasta de dados não grava nada (a fixture não é um pendrive).
    ``persistente=False``: a pasta de dados é temporária (/tmp), o aviso de perda vale também.
    """
    if replay and not pasta_dados:
        return Gravacao(None, False, textos.t("py_rel_completo.modo_replay_sem_dados_relatorio"))
    try:
        pasta, no_pendrive = salvar_relatorios(relatorio, texto_terminal, pasta_dados, pasta=pasta)
    except OSError as erro:
        return Gravacao(None, False, textos.t("py_rel_completo.nao_foi_possivel_gravar_o", erro))
    if no_pendrive and persistente:
        return Gravacao(pasta, True, textos.t("py_rel_completo.relatorio_gravado_no_pendrive", pasta))
    return Gravacao(
        pasta, False, textos.t("py_rel_completo.atencao_nada_foi_salvo_no", pasta)
    )

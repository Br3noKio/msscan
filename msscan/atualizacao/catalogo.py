"""Catálogo de firmware da Dell: download pela fonte oficial e cache de 7 dias (CLAUDE.md 11)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from msscan.core.executor import Executor
from msscan.rede.monitor import baixar_retomando
from msscan.parsers import ErroParser
from msscan.parsers.catalogo_dell import parse_catalogo_dell

URL_CATALOGO_DELL = "https://downloads.dell.com/catalog/Catalog.xml.gz"
VALIDADE_CACHE_S = 7 * 24 * 3600
LIMITE_CATALOGO = 100 * 1024 * 1024
PASTA_CACHE = "firmware/cache"
ARQUIVO_CACHE = "Catalog.xml.gz"
URL_CATALOGO_LINUX = "https://downloads.dell.com/catalog/Catalog.gz"
"""Update Catalog for Enterprise Servers (o do Dell System Update): além dos ``.EXE``, os pacotes Linux ``.BIN`` (``LLXP``) com
o MESMO release ID e a mesma versão do ``.EXE`` (conferido em 21/10/2026, 26.10.15: 838 ``.BIN``, 834 com SHA-256; da 14ª
geração em diante). Usado só no caminho f, sem iDRAC acessível (pedido de 21/10/2026)."""
ARQUIVO_LINUX = "Catalog_linux.gz"


ARQUIVO_LEGADOS = Path(__file__).with_name("catalogos_dell.yaml")
PASTA_LEGADOS = "legados"
ARQUIVO_EM_USO = "em_uso.txt"
"""Nome (relativo à pasta do cache) do catálogo usado na última verificação: o atual ou um antigo (pedido de 15/10/2026)."""
LIMITE_LEGADO = 20 * 1024 * 1024


@dataclass
class Catalogo:
    """Catálogo lido (``dados`` de ``parse_catalogo_dell``) e de onde veio. ``legado``: entrada de ``catalogos_dell.yaml``
    quando o modelo saiu do catálogo atual e foi usado o último catálogo da Dell que o inclui."""

    dados: dict[str, Any] | None
    origem: str
    aviso: str = ""
    legado: dict[str, Any] | None = None


def caminho_cache(pasta_dados: str | None, arquivo: str = ARQUIVO_CACHE) -> Path:
    """Catálogo ATUAL da Dell (o que é baixado a cada 7 dias); ``ARQUIVO_LINUX`` = o dos pacotes ``.BIN``."""
    return Path(pasta_dados or "/tmp/msscan") / PASTA_CACHE / arquivo


def pacotes_linux(dados: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Só os ``.BIN`` Linux x86-64 (``LLXP``) do catálogo, cada um com a base de download do próprio catálogo."""
    base = (dados or {}).get("base") or "downloads.dell.com"
    return [{**c, "base": base} for c in (dados or {}).get("componentes") or []
            if c.get("tipo_pacote") == "LLXP" and c.get("caminho", "").lower().endswith(".bin")]  # fmt: skip


def obter_pacotes_linux(executor: Executor, pasta_dados: str | None, monitor=None, avisar=None) -> tuple[list[dict[str, Any]], str]:
    """``(pacotes .BIN, aviso)`` do ``URL_CATALOGO_LINUX`` (mesmo cache de 7 dias). Nunca levanta exceção."""
    linux = _obter_atual(executor, pasta_dados, None, monitor, avisar, URL_CATALOGO_LINUX, ARQUIVO_LINUX)
    return pacotes_linux(linux.dados), linux.aviso


def caminho_em_uso(pasta_dados: str | None) -> Path:
    """Catálogo da última verificação (atual ou antigo); sem registro, o atual."""
    pasta = Path(pasta_dados or "/tmp/msscan") / PASTA_CACHE
    try:
        nome = (pasta / ARQUIVO_EM_USO).read_text(encoding="utf-8").strip()
    except OSError:
        nome = ""
    if nome and (nome == ARQUIVO_CACHE or (nome.startswith(PASTA_LEGADOS + "/") and ".." not in nome)) and (pasta / nome).is_file():
        return pasta / nome
    return pasta / ARQUIVO_CACHE


def carregar_legados(arquivo: Path = ARQUIVO_LEGADOS) -> list[dict[str, Any]]:
    """Catálogos antigos válidos (só https em downloads.dell.com, SHA-256 presente), do mais novo ao mais antigo."""
    import re

    import yaml

    from msscan.core.executor import validar_download

    try:
        entradas = (yaml.safe_load(arquivo.read_text(encoding="utf-8")) or {}).get("catalogos") or []
    except (OSError, yaml.YAMLError, AttributeError):
        return []
    validas = [{**e, "sha256": str(e["sha256"]).lower()} for e in entradas if isinstance(e, dict)
               and not validar_download(str(e.get("url") or "")) and str(e.get("url") or "").startswith("https://downloads.dell.com/")
               and re.fullmatch(r"[0-9a-fA-F]{64}", str(e.get("sha256") or "")) and re.fullmatch(r"\d\d\.\d\d\.\d\d", str(e.get("versao") or ""))]  # fmt: skip
    return sorted(validas, key=lambda e: e["versao"], reverse=True)


def tem_bios(dados: dict[str, Any], system_id: str) -> bool:
    """O catálogo atende o modelo: todo servidor atendido tem pacote de BIOS (o catálogo 26.10.08 ainda lista o R530 só no
    pré-requisito do iDRAC 2.40.40.40, sem BIOS: isso é "fora do catálogo")."""
    return any(c["tipo"] == "BIOS" and any(s["system_id"] == system_id for s in c["sistemas"]) for c in dados.get("componentes") or [])


def _marcar_em_uso(pasta_dados: str | None, nome: str) -> None:
    try:
        (Path(pasta_dados or "/tmp/msscan") / PASTA_CACHE / ARQUIVO_EM_USO).write_text(nome + "\n", encoding="utf-8")
    except OSError:
        pass  # sem registro, quem lê usa o catálogo atual (o mesmo comportamento de antes)


def _legado(executor: Executor, pasta_dados: str | None, entrada: dict[str, Any], monitor=None, avisar=None) -> tuple[dict[str, Any] | None, str]:
    """Catálogo antigo do cache (conferido de novo pelo SHA-256) ou baixado de downloads.dell.com. ``(dados, erro)``."""
    import hashlib

    nome = f"{PASTA_LEGADOS}/Catalog_{entrada['versao']}.xml.gz"
    destino = Path(pasta_dados or "/tmp/msscan") / PASTA_CACHE / nome
    try:
        conferido = destino.is_file() and hashlib.sha256(destino.read_bytes()).hexdigest() == entrada["sha256"]
    except OSError:
        conferido = False
    if not conferido:
        destino.parent.mkdir(parents=True, exist_ok=True)
        resposta = baixar_retomando(executor, entrada["url"], str(destino), LIMITE_LEGADO, timeout=180, monitor=monitor, avisar=avisar)
        if not resposta.ok:
            return None, f"não foi possível baixar o catálogo {entrada['versao']} da Dell: {resposta.erro}"
        if resposta.sha256 != entrada["sha256"]:
            destino.unlink(missing_ok=True)
            return None, f"catálogo {entrada['versao']} com SHA-256 diferente do oficial: apagado"
    try:
        return parse_catalogo_dell(destino.read_bytes()), ""
    except (OSError, ErroParser) as erro:
        return None, f"catálogo {entrada['versao']} ilegível: {erro}"


_MEMORIA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _ler_em_memoria(cache: Path) -> dict[str, Any] | None:
    try:
        mtime = cache.stat().st_mtime
        guardado = _MEMORIA_CACHE.get(str(cache))
        if guardado is None or guardado[0] != mtime:
            _MEMORIA_CACHE[str(cache)] = guardado = (mtime, parse_catalogo_dell(cache.read_bytes()))
        return guardado[1]
    except (OSError, ErroParser):
        return None


def catalogo_em_cache(pasta_dados: str | None) -> dict[str, Any] | None:
    """Catálogo já baixado (sem rede), lido uma vez por versão do arquivo; ``None`` se não houver ou estiver ilegível.
    ``bin_linux``: os ``.BIN`` do catálogo Linux já baixado (caminho f), vazio sem ele.

    Usado para descobrir a versão de um pacote manual pelo release ID (3b); nunca baixa nada."""
    dados = _ler_em_memoria(caminho_em_uso(pasta_dados))
    if dados is None:
        return None
    return {**dados, "bin_linux": pacotes_linux(_ler_em_memoria(caminho_cache(pasta_dados, ARQUIVO_LINUX)))}


def obter_catalogo(executor: Executor, pasta_dados: str | None, agora: float | None = None, monitor=None,
                   avisar=None, system_id: str | None = None) -> Catalogo:  # fmt: skip
    """Catálogo atual; se o modelo (``system_id``) não tiver BIOS nele, o último catálogo antigo da Dell que o inclui
    (``catalogos_dell.yaml``). Registra qual foi usado (``caminho_em_uso``). Nunca levanta exceção."""
    atual = _obter_atual(executor, pasta_dados, agora, monitor, avisar)
    if atual.dados is None or not system_id or tem_bios(atual.dados, system_id):
        _marcar_em_uso(pasta_dados, ARQUIVO_CACHE)
        return atual
    falhas = []
    for entrada in carregar_legados():
        dados, erro = _legado(executor, pasta_dados, entrada, monitor, avisar)
        if erro:
            falhas.append(erro)
        elif tem_bios(dados, system_id):
            _marcar_em_uso(pasta_dados, f"{PASTA_LEGADOS}/Catalog_{entrada['versao']}.xml.gz")
            return Catalogo(dados, f"{entrada['url']} (catálogo {entrada['versao']})", "; ".join(falhas), legado=entrada)
    _marcar_em_uso(pasta_dados, ARQUIVO_CACHE)
    return Catalogo(atual.dados, atual.origem, "; ".join([atual.aviso] + falhas if atual.aviso else falhas))


def _obter_atual(executor: Executor, pasta_dados: str | None, agora: float | None = None, monitor=None,
                 avisar=None, url: str = URL_CATALOGO_DELL, arquivo: str = ARQUIVO_CACHE) -> Catalogo:  # fmt: skip
    """Usa o cache se tiver menos de 7 dias; senão baixa. Nunca levanta exceção.

    Sem download possível e com cache vencido: usa o cache COM aviso (modo informativo). Com ``monitor``
    (rede contínua), queda de rede no meio do download espera a rede voltar e continua de onde parou.
    """
    agora = time.time() if agora is None else agora
    cache = caminho_cache(pasta_dados, arquivo)
    idade = agora - cache.stat().st_mtime if cache.is_file() else None
    aviso = ""
    if idade is None or idade > VALIDADE_CACHE_S:
        resposta = baixar_retomando(executor, url, str(cache), LIMITE_CATALOGO, timeout=180,
                                    monitor=monitor, avisar=avisar)  # fmt: skip
        if resposta.ok:
            origem = f"baixado agora de {url}"
        elif idade is not None:
            origem = f"cache de {idade / 86400:.0f} dia(s) ({cache})"
            aviso = f"não foi possível baixar o catálogo novo ({resposta.erro}); usando o cache vencido"
        else:
            return Catalogo(None, "", f"não foi possível baixar o catálogo da Dell: {resposta.erro}")
    else:
        origem = f"cache de {idade / 3600:.0f} hora(s) ({cache})"
    try:
        dados = parse_catalogo_dell(cache.read_bytes())
    except (OSError, ErroParser) as erro:
        return Catalogo(None, origem, f"catálogo inválido: {erro}")
    return Catalogo(dados, origem, aviso)

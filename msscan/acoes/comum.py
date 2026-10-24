"""Peças comuns às ações corretivas (fase 10): modos, confirmação digitada, passos e leitura do BMC.

Regras (CLAUDE.md 10b, 12b, 14 e 18.2): padrão NÃO, nenhuma ação selecionada, nada automático; cada ação tem uma tela só
com a confirmação ``CORRIGIR``. O modo real é o padrão (sem simulação na interface nem no modo texto); ``--simular`` é só
uma opção interna de desenvolvimento (faz as leituras e descreve, pedido por pedido, o que SERIA alterado). Alterar o BMC
exige a trava ``ACOES_REAIS_LIBERADAS`` ligada (liberada em 30/09/2026, CLAUDE.md 15.6).
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from msscan.core.fallback import FalhaColeta
from msscan.parsers import ErroParser
from msscan.parsers.redfish import membros, parse_gerenciador

MODO_SIMULAR, MODO_APLICAR = "simular", "aplicar"
CONFIRMACAO = "CORRIGIR"
"""Confirmação digitada das ações corretivas (diferente do ATUALIZAR do firmware)."""
CONFIRMACAO_DESFAZER = "DESFAZER"
"""Confirmação digitada para desfazer uma ação (volta aos valores guardados no pendrive)."""
PASTA_ACOES = "acoes"


def novo_resultado(prechecagem: dict[str, Any], modo: str) -> dict[str, Any]:
    """Resultado vazio de uma execução (vai para o relatório).

    ``antes``/``depois``: o que era e o que ficou; ``desfazer``: se dá para desfazer e com qual arquivo do pendrive;
    ``desfeita``: ``None`` (não desfeita) ou o resultado do desfazer (relatório: "desfeita: sim/não/não se aplica").
    """
    return {"acao": prechecagem.get("acao"), "titulo": prechecagem.get("titulo"), "modo": modo, "metodo": prechecagem.get("metodo"),
            "situacao": "", "mensagem": "", "passos": [], "antes": prechecagem.get("antes") or {}, "depois": None,
            "desfazer": {"disponivel": False, "motivo": ""}, "desfeita": None,
            "inicio": datetime.now().isoformat(timespec="seconds")}  # fmt: skip


def passo(resultado: dict[str, Any], acao: str, detalhe: str, situacao: str = "ok") -> None:
    resultado["passos"].append({"acao": acao, "detalhe": detalhe, "situacao": situacao})


def conferir_confirmacao(confirmacao: str, esperada: str = CONFIRMACAO) -> str:
    """``""`` se a confirmação digitada bate; senão, o motivo. Espaços nas pontas são ignorados; maiúsculas não."""
    if (confirmacao or "").strip() != esperada:
        return f"Confirmação não conferida: digite {esperada} (em maiúsculas) para continuar."
    return ""


def gravar_conferido(destino: Path, dados: bytes) -> str:
    """Grava no pendrive e CONFERE: fsync, descarta o cache do arquivo e relê; devolve o SHA-256 conferido.

    Levanta ``OSError`` se a gravação falhar e ``ValueError`` se a cópia relida não bater com o que foi gravado (tamanho e
    SHA-256). Quem chama abre a janela de escrita do pendrive (``escrita()``).
    """
    esperado = hashlib.sha256(dados).hexdigest()
    destino.parent.mkdir(parents=True, exist_ok=True)
    with open(destino, "wb") as arquivo:
        arquivo.write(dados)
        arquivo.flush()
        os.fsync(arquivo.fileno())
        if hasattr(os, "posix_fadvise"):
            # A releitura vem do pendrive, não do cache da memória (melhor esforço: o kernel pode ignorar).
            os.posix_fadvise(arquivo.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    relido = destino.read_bytes()
    obtido = hashlib.sha256(relido).hexdigest()
    if len(relido) != len(dados) or obtido != esperado:
        raise ValueError(f"a cópia relida do pendrive não confere (SHA-256 {obtido[:16]}… ≠ {esperado[:16]}…)")
    return obtido


def copiar_conferido(origem: Path, destino: Path) -> str:
    """Copia em blocos (arquivos grandes), fsync, descarta o cache e relê: devolve o SHA-256 conferido.

    Levanta ``OSError`` (sem espaço, arquivo de 4 GiB ou mais em FAT32...) e ``ValueError`` (cópia relida diferente);
    nos dois casos a cópia incompleta é apagada. Quem chama abre a janela de escrita do pendrive (``escrita()``).
    """
    soma = hashlib.sha256()
    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(origem, "rb") as entrada, open(destino, "wb") as saida:
            for bloco in iter(lambda: entrada.read(1024 * 1024), b""):
                soma.update(bloco)
                saida.write(bloco)
            saida.flush()
            os.fsync(saida.fileno())
            if hasattr(os, "posix_fadvise"):
                os.posix_fadvise(saida.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        relido = hashlib.sha256()
        with open(destino, "rb") as copia:
            for bloco in iter(lambda: copia.read(1024 * 1024), b""):
                relido.update(bloco)
        if relido.hexdigest() != soma.hexdigest():
            raise ValueError(f"a cópia relida do pendrive não confere (SHA-256 {relido.hexdigest()[:16]}… ≠ {soma.hexdigest()[:16]}…)")
    except (OSError, ValueError):
        destino.unlink(missing_ok=True)
        raise
    return soma.hexdigest()


def no_pendrive(arquivo: str | Path, pasta_dados: str | None) -> str:
    """Caminho para mostrar ao técnico: ``MSSCAN/acoes/x.json`` (relativo ao pendrive), ou o caminho completo."""
    try:
        base = Path(str(pasta_dados))
        return str(Path(base.name) / Path(arquivo).relative_to(base))
    except (ValueError, TypeError):
        return str(arquivo)


def redfish_validado(ctx: Any) -> bool:
    acesso = getattr(ctx, "bmc", None)
    return acesso is not None and acesso.validado and acesso.cliente is not None


def gerenciador(ctx: Any) -> tuple[str, dict[str, Any]]:
    """``(caminho, Manager)`` do primeiro BMC do Redfish. Levanta ``FalhaColeta``/``ErroParser``."""
    cliente = ctx.bmc.cliente
    gerentes = membros(cliente.get("/redfish/v1/Managers"))
    if not gerentes:
        raise FalhaColeta("o Redfish não lista nenhum Manager (BMC)")
    return gerentes[0], parse_gerenciador(cliente.get(gerentes[0]))


def garantir_administrador(ctx: Any, pedir_administrador: Any, resultado: dict[str, Any]) -> bool:
    """Usuário do BMC administrador (pede um na hora, se preciso). Sem ele, a ação fica só informativa."""
    if ctx.bmc.administrador is True:
        return True
    if pedir_administrador is not None and pedir_administrador() and ctx.bmc.administrador is True:
        return True
    resultado["situacao"] = "so_informativo"
    resultado["mensagem"] = "O usuário do BMC não é administrador: sem um administrador, nada é alterado no BMC."
    passo(resultado, "BMC", resultado["mensagem"], "bloqueado")
    return False


def resposta_de_escrita(resultado: dict[str, Any], rotulo: str, resposta: Any) -> bool:
    """Trata a resposta de ``acao_bmc``: ``True`` se o BMC aceitou; senão registra trava/recusa no resultado."""
    if resposta.erro_tipo == "trava":
        resultado["situacao"], resultado["mensagem"] = "bloqueada_trava", resposta.erro
        passo(resultado, rotulo, resposta.erro, "bloqueado")
        return False
    if resposta.erro_tipo == "bloqueado":
        resultado["situacao"], resultado["mensagem"] = "bloqueada", f"salvaguarda: {resposta.erro}"
        passo(resultado, rotulo, resultado["mensagem"], "bloqueado")
        return False
    if resposta.status not in (200, 202, 204):
        resultado["situacao"] = "falhou"
        resultado["mensagem"] = f"o BMC recusou a alteração ({resposta.erro or resposta.status})"
        passo(resultado, rotulo, resultado["mensagem"], "falhou")
        return False
    passo(resultado, rotulo, f"aceito pelo BMC (HTTP {resposta.status})")
    return True


ERROS_LEITURA = (FalhaColeta, ErroParser, AttributeError, TypeError)

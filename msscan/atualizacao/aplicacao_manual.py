"""Assistente do pacote manual (fase 11, CLAUDE.md 11b): analisar → SHA-256 → checagem → instalar.

Etapas (``analise["etapa"]``): ``sha`` (esperando o SHA-256 colado) → ``checagem`` (SHA conferido; ``.BIN`` roda
``DUP -c``) → ``pronto`` ("Instalar" habilitado) ou ``recusado`` (com o motivo). Nada executa o pacote antes do
SHA-256 conferido. "Instalar" segue as regras da seção 11: pré-checagens, ``ATUALIZAR`` (e ``SEM REDUNDANCIA``
quando for o caso) e a trava ``GRAVACAO_REAL_LIBERADA``; depois do ``ATUALIZAR``, tudo automático (``automatica.py``, 18.2).

O resultado de ``instalar`` tem o formato da aplicação da fase 9 (``modo``, ``situacao``, ``passos``, ``itens``) e
entra no relatório pelo mesmo caminho.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from msscan.atualizacao import manual
from msscan.atualizacao.aplicacao import MODO_APLICAR, _hash_arquivo, _passo, conferir_confirmacoes
from msscan.atualizacao.versoes import ATUALIZACAO, INCERTA


def analisar(arquivo: Path, sku: str | None, inventario: list[dict[str, Any]], bmc: dict[str, Any] | None,
             catalogo: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """Primeira etapa, sem executar nada: identificação, ``package.xml``, modelo, versão e método.

    Versão do pacote (pedido de 01/10/2026, 3b), nesta ordem: ``package.xml`` embutido; entrada do catálogo oficial com
    o mesmo release ID (``catalogo``, já em cache); senão fica para a checagem do próprio pacote (``DUP -c``, em
    ``checar``). O nome do arquivo só aparece como apoio, nunca decide.
    """
    identificacao = manual.identificar_nome(arquivo.name)
    pacote = manual.ler_pacote(arquivo) if identificacao and identificacao["linux"] else None
    origem_versao = "package.xml do pacote" if pacote else ""
    if pacote is None and identificacao:
        pacote = manual.pacote_do_catalogo(identificacao, catalogo, sku)
        origem_versao = "catálogo oficial da Dell (mesmo release ID)" if pacote else ""
    modelo_ok, motivo_modelo = manual.conferir_modelo(pacote, sku, inventario) if identificacao else (None, "")
    versao, motivo_versao, instalada = manual.conferir_versao(pacote, inventario)
    if origem_versao:
        motivo_versao = f"{motivo_versao} (versão pelo {origem_versao})"
    elif identificacao and identificacao["linux"]:
        motivo_versao = (f"versão do pacote não confirmada: sem package.xml e sem entrada correspondente no catálogo; o nome do "
                         f"arquivo indica {identificacao['versao']} (só apoio). A checagem do próprio pacote (DUP -c) decide "
                         "depois do SHA-256")  # fmt: skip
    metodo, motivo_metodo = manual.metodo_aplicacao(identificacao, bmc, bool(origem_versao) and versao == ATUALIZACAO)
    analise = {
        "arquivo": str(arquivo), "nome": arquivo.name, "tamanho": _tamanho(arquivo),
        "identificacao": identificacao, "pacote": _resumo(pacote), "metodo": metodo, "motivo_metodo": motivo_metodo,
        "modelo_ok": modelo_ok, "motivo_modelo": motivo_modelo, "versao": versao, "motivo_versao": motivo_versao,
        "instalada": instalada, "disponivel": (pacote or {}).get("versao") or (identificacao or {}).get("versao"),
        "sha256": "", "checagem": None, "etapa": "sha", "motivo": "",
    }  # fmt: skip
    recusa = _recusa_inicial(analise)
    if recusa:
        analise["etapa"], analise["motivo"] = "recusado", recusa
    return analise


def _tamanho(arquivo: Path) -> int:
    try:
        return arquivo.stat().st_size
    except OSError:
        return 0


def _resumo(pacote: dict[str, Any] | None) -> dict[str, Any] | None:
    if pacote is None:
        return None
    return {"nome": pacote["nome"], "versao": pacote["versao"], "tipo": pacote["tipo"], "reinicio": pacote["reinicio"],
            "data": pacote["data"], "sistemas": sorted({f"{s['modelo']} ({s['system_id']})" for s in pacote["sistemas"]}),
            "component_ids": [d["component_id"] for d in pacote["dispositivos"] if d.get("component_id")]}  # fmt: skip


def _recusa_inicial(analise: dict[str, Any]) -> str:
    if analise["identificacao"] is None:
        return "arquivo não reconhecido como pacote de atualização da Dell (.BIN ou .EXE com o nome original)"
    if analise["metodo"] is None:
        return analise["motivo_metodo"]
    if analise["modelo_ok"] is False:
        return analise["motivo_modelo"]
    if analise["versao"] not in (ATUALIZACAO, INCERTA):
        return f"a versão do pacote não é mais nova que a instalada ({analise['motivo_versao']}): nunca downgrade"
    # .BIN com versão incerta (MÉDIO 6) NÃO é instalado enquanto incerto: segue para o SHA-256 e a checagem do próprio
    # pacote; só fica "pronto" se o DUP -c confirmar que é aplicável e mais novo (``checar``; pedido de 01/10/2026, 3b).
    # O iDRAC7/8 (.EXE) confere a versão do pacote enviado contra o inventário antes do SimpleUpdate.
    return ""


def conferir_sha(analise: dict[str, Any], texto: str | None) -> dict[str, Any]:
    """SHA-256 colado pelo técnico. Formato inválido: pede de novo. Divergente: apaga o arquivo e recusa (CLAUDE.md 11.5)."""
    if analise["etapa"] != "sha":
        return analise
    esperado = manual.normalizar_sha256(texto)
    if esperado is None:
        return {**analise, "motivo": "Cole o SHA-256 que aparece na página da Dell (64 caracteres de 0 a 9 e a a f)."}
    arquivo = Path(analise["arquivo"])
    try:
        obtido = _hash_arquivo(arquivo, "sha256")
    except OSError as erro:
        return {**analise, "etapa": "recusado", "motivo": f"não foi possível ler o arquivo: {erro}"}
    if obtido != esperado:
        arquivo.unlink(missing_ok=True)
        return {**analise, "etapa": "recusado", "sha256": obtido,
                "motivo": f"SHA-256 divergente (página {esperado[:16]}…, arquivo {obtido[:16]}…): arquivo apagado. Baixe de novo."}  # fmt: skip
    return {**analise, "etapa": "checagem", "sha256": obtido, "motivo": "SHA-256 confere com a página da Dell."}


def checar(analise: dict[str, Any], executor: Any) -> dict[str, Any]:
    """Depois do SHA-256: ``.BIN`` roda ``DUP -c`` (não grava; tabela oficial de códigos). ``.EXE`` (iDRAC7/8): a
    versão é conferida pelo iDRAC ao instalar, antes do ``SimpleUpdate``."""
    if analise["etapa"] != "checagem":
        return analise
    if analise["metodo"] != manual.DUP_LINUX:
        return {**analise, "etapa": "pronto",
                "motivo": "Pronto. O iDRAC informa a versão do pacote depois do envio; se não for mais nova, nada é aplicado."}  # fmt: skip
    rc, saida, erro = executor.checar_dup(analise["arquivo"])
    checagem = {**manual.interpretar_dup(rc), "saida": (saida or erro or "").strip()[-2000:]}
    novo = {**analise, "checagem": checagem}
    if not checagem["aplicavel"]:
        return {**novo, "etapa": "recusado", "motivo": f"Checagem do pacote (DUP -c): {checagem['texto']} (código {rc})."}
    if novo["versao"] != ATUALIZACAO:
        # A tabela oficial separa "igual ou mais antiga" (código 3, recusado acima) de "pode ser aplicado": o próprio
        # pacote confirmou que é aplicável a este servidor e mais novo que o instalado.
        novo = {**novo, "versao": ATUALIZACAO, "versao_confirmada_por": "DUP -c",
                "motivo_versao": f"confirmada pelo próprio pacote (DUP -c, código {rc}): aplicável e mais novo que o instalado"}  # fmt: skip
    return {**novo, "etapa": "pronto", "motivo": f"Checagem do pacote (DUP -c): pode ser aplicado (código {rc})."}


# --- Instalar -------------------------------------------------------------------------------------


def instalar(analise: dict[str, Any], ctx: Any, executor: Any, prechecagens: dict[str, Any], confirmacao: str,
             confirmacao_redundancia: str, pasta_dados: str | None, modo: str, inventario: list[dict[str, Any]],
             bmc: dict[str, Any], dormir: Callable[[float], None] = time.sleep,
             parcial: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """Instala um pacote já pronto, sem perguntar nada depois do ``ATUALIZAR`` (CLAUDE.md 18.2): o mesmo motor da
    atualização pelo catálogo (``automatica.py``: acesso automático, caminhos a–g, reinício, retomada, restauração).
    Nunca levanta exceção. Formato do resultado: o da fase 9.

    ``parcial``: dicionário preenchido durante a execução (progresso ao vivo na interface).
    """
    resultado: dict[str, Any] = parcial if parcial is not None else {}
    pacote = analise.get("pacote") or {}
    resultado.update({"modo": modo, "situacao": "", "mensagem": "", "passos": [], "metodo": "manual",
                      "inicio": datetime.now().isoformat(timespec="seconds"),
                      "itens": [{"id": "manual", "componente": pacote.get("nome") or analise["nome"],
                                 "instalada": analise.get("instalada"), "disponivel": analise.get("disponivel"),
                                 "component_id": (pacote.get("component_ids") or [None])[0],
                                 "categoria": pacote.get("categoria") or (analise.get("identificacao") or {}).get("categoria") or "",
                                 "arquivo": analise["arquivo"], "hash": f"SHA-256 {analise['sha256']}",
                                 "hash_conferido": ["sha256", analise["sha256"]], "reinicio": bool(pacote.get("reinicio", True)),
                                 "legado": analise.get("metodo") == manual.REDFISH_IDRAC_LEGADO,
                                 "checado": bool((analise.get("checagem") or {}).get("aplicavel")),
                                 "situacao": "pronto"}]})  # fmt: skip
    from msscan.atualizacao import progresso as prog_mod
    from msscan.atualizacao.aplicacao import encerrar_progresso

    if not isinstance(resultado.get("_acompanhamento"), prog_mod.Progresso):
        resultado["_acompanhamento"] = prog_mod.Progresso([{"componente": resultado["itens"][0]["componente"]}])
    resultado["_acompanhamento"].etapa(resultado["itens"][0]["componente"], prog_mod.CONFERIDO, "SHA-256 conferido com a página da Dell")
    try:
        _instalar(analise, ctx, executor, prechecagens, confirmacao, confirmacao_redundancia, pasta_dados, modo, inventario, bmc,
                  dormir, resultado)  # fmt: skip
    except Exception as erro:  # noqa: BLE001 - a aplicação nunca derruba o programa
        resultado["situacao"], resultado["mensagem"] = "falhou", f"erro inesperado: {type(erro).__name__}: {erro}"
    resultado["fim"] = datetime.now().isoformat(timespec="seconds")
    encerrar_progresso(resultado)
    return resultado


def _bloquear(resultado: dict[str, Any], mensagem: str, situacao: str = "bloqueada") -> None:
    resultado["situacao"], resultado["mensagem"] = situacao, mensagem
    _passo(resultado, "bloqueio", mensagem, "bloqueado")


def _instalar(analise, ctx, executor, prechecagens, confirmacao, confirmacao_redundancia, pasta_dados, modo, inventario, bmc, dormir,
              resultado) -> None:  # noqa: ANN001, PLR0913
    if analise.get("etapa") != "pronto":
        return _bloquear(resultado, "O pacote ainda não passou pelo SHA-256 e pela checagem.")
    if prechecagens.get("bloqueios"):
        return _bloquear(resultado, "; ".join(prechecagens["bloqueios"]))
    motivo = conferir_confirmacoes({"confirmacao_redundancia": prechecagens.get("redundancia_perdida")}, confirmacao, confirmacao_redundancia)
    if motivo:
        return _bloquear(resultado, motivo)
    _passo(resultado, "pacote", f"{analise['nome']}: SHA-256 conferido com a página da Dell; {analise['motivo_metodo']}")
    if modo != MODO_APLICAR:  # só o --simular interno de desenvolvimento (18.2)
        como = {manual.DUP_LINUX: f"bash {analise['arquivo']} -q", manual.REDFISH_IDRAC_LEGADO: "upload no iDRAC + SimpleUpdate (OnReset)"}
        _passo(resultado, "instalar (modo interno de teste)", f"{analise['nome']}: seria {como.get(analise['metodo'], 'Redfish do iDRAC ou racadm')}; "
               "nada é executado nem enviado ao iDRAC", "simulado")  # fmt: skip
        resultado["situacao"], resultado["mensagem"] = "simulada", "Modo interno de teste (--simular): nada foi gravado no servidor."
        return
    from msscan.atualizacao.automatica import aplicar_automatico
    from msscan.atualizacao.catalogo import catalogo_em_cache

    resultado["inventario_atual"] = list(inventario or [])
    aplicar_automatico(ctx, executor, Path(pasta_dados or "/tmp/msscan"), resultado, dormir, modo, catalogo_em_cache(pasta_dados),
                       bmc_inicial=bmc)  # fmt: skip

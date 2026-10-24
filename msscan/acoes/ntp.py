"""Ação ``bmc.relogio_ntp``: ligar o NTP do BMC com os servidores escolhidos pelo técnico.

Detectar primeiro: iDRAC (Dell) → atributos ``NTPConfigGroup.1`` em ``Managers/<id>/Attributes`` (documentação da
Dell); demais BMCs → ``NTP`` do ``ManagerNetworkProtocol`` (norma DMTF). Sem nenhum dos dois: não suportado.
Pré-checagens só leem; a prévia mostra antes → depois e o pedido exato; a alteração passa por ``Executor.acao_bmc``.

Ajustes de 28/09/2026: servidor sugerido = o NTP que o DHCP da rede do cliente informar (``nmcli``), senão
``pool.ntp.org``; o técnico escolhe ou digita. Antes de alterar, os valores EXATOS do BMC são gravados e conferidos no
pendrive (``MSSCAN/acoes/ntp_antes_<data>.json``); sem essa cópia nada é alterado. "Desfazer" volta a eles (``DESFAZER``
digitado, mesma trava).

MOCKUP(rodada final): atributos do iDRAC9 sem coleta real (nomes e valores Enabled/Disabled da documentação da Dell).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from msscan.acoes.comum import (
    ERROS_LEITURA,
    MODO_APLICAR,
    PASTA_ACOES,
    garantir_administrador,
    gerenciador,
    gravar_conferido,
    no_pendrive,
    novo_resultado,
    passo,
    redfish_validado,
    resposta_de_escrita,
)
from msscan.core.executor import NTP_DELL, NTP_DELL_RESTAURAR, NTP_DMTF, NTP_DMTF_RESTAURAR, servidor_ntp_valido
from msscan.parsers.nmcli import parse_dhcp4_opcoes, servidores_ntp_dhcp
from msscan.parsers.redfish import parse_ntp_dell, parse_ntp_protocolo

IDENTIFICADOR = "bmc.relogio_ntp"
SERVIDOR_PADRAO = "pool.ntp.org"
MAX_SERVIDORES = 3


def sugestoes(executor: Any) -> list[dict[str, str]]:
    """Servidores NTP sugeridos ao técnico: os do DHCP da rede do cliente (se houver) e, por último, ``pool.ntp.org``.

    Só leitura (``nmcli -t -f DHCP4 device show <placa>`` nas placas físicas com IP). Nunca levanta exceção.
    """
    from msscan.rede.verificacao import listar_interfaces

    saida: list[dict[str, str]] = []
    try:
        placas = [i.nome for i in listar_interfaces(executor) if i.candidata and i.enderecos]
    except Exception:  # noqa: BLE001 - sugestão nunca derruba a ação
        placas = []
    for placa in placas:
        rc, texto, _ = executor.rodar(["nmcli", "-t", "-f", "DHCP4", "device", "show", placa], timeout=10)
        if rc != 0:
            continue
        for servidor in servidores_ntp_dhcp(parse_dhcp4_opcoes(texto)):
            if servidor_ntp_valido(servidor) and servidor not in [s["servidor"] for s in saida]:
                saida.append({"servidor": servidor, "origem": f"informado pelo DHCP da rede do cliente ({placa})"})
    saida.append({"servidor": SERVIDOR_PADRAO, "origem": "público (NTP Pool Project)"})
    return saida[: MAX_SERVIDORES + 1]


def servidores_de(texto_ou_lista: Any, padrao: list[str] | None = None) -> tuple[list[str], str]:
    """Lista de servidores (texto separado por vírgula/espaço ou lista) → ``(servidores, erro)``. Vazio = ``padrao``."""
    if isinstance(texto_ou_lista, str):
        itens = texto_ou_lista.replace(",", " ").replace(";", " ").split()
    elif isinstance(texto_ou_lista, list):
        itens = [str(s).strip() for s in texto_ou_lista if str(s).strip()]
    else:
        itens = []
    itens = list(dict.fromkeys(itens)) or list(padrao or [SERVIDOR_PADRAO])[:1]
    if len(itens) > MAX_SERVIDORES:
        return itens, f"no máximo {MAX_SERVIDORES} servidores NTP"
    invalidos = [s for s in itens if not servidor_ntp_valido(s)]
    if invalidos:
        return itens, f"servidor NTP inválido: {', '.join(invalidos)} (use nome de host ou IP, sem http:// nem porta)"
    return itens, ""


def _diferenca_relogio(data_hora: str | None, agora: datetime | None = None) -> int | None:
    """Segundos entre o relógio do BMC e o deste computador (positivo: BMC adiantado)."""
    if not data_hora:
        return None
    try:
        bmc = datetime.fromisoformat(data_hora.replace("Z", "+00:00"))
    except ValueError:
        return None
    if bmc.tzinfo is None:
        return None
    return int((bmc - (agora or datetime.now(timezone.utc))).total_seconds())


def corpo_restaurar(metodo: str, bruto: dict[str, Any]) -> dict[str, Any]:
    """Pedido que volta o BMC aos valores exatos lidos antes da alteração."""
    if metodo == "dell":
        return {"Attributes": dict(bruto)}
    return {"NTP": dict(bruto)}


def _corpo(metodo: str, servidores: list[str]) -> dict[str, Any]:
    if metodo == "dell":
        atributos = {"NTPConfigGroup.1.NTPEnable": "Enabled"}
        atributos.update({f"NTPConfigGroup.1.NTP{n}": s for n, s in enumerate(servidores, 1)})
        return {"Attributes": atributos}
    return {"NTP": {"ProtocolEnabled": True, "NTPServers": servidores}}


def _ler_ntp(ctx: Any, caminho_gerente: str, gerente: dict[str, Any]) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """``(metodo, alvo, ntp)``: Dell pelos atributos (se o fabricante for Dell) ou DMTF pelo NetworkProtocol."""
    cliente = ctx.bmc.cliente
    if "dell" in str(getattr(ctx, "fabricante", "") or "").lower():
        alvo = caminho_gerente.rstrip("/") + "/Attributes"
        try:
            ntp = parse_ntp_dell(cliente.get(alvo))
        except ERROS_LEITURA:
            ntp = None
        if ntp is not None:
            return "dell", alvo, ntp
    if gerente.get("protocolos"):
        ntp = parse_ntp_protocolo(cliente.get(gerente["protocolos"]))
        if ntp is not None:
            return "dmtf", gerente["protocolos"], ntp
    return None, None, None


def prechecar(ctx: Any, parametros: dict[str, Any] | None = None, agora: datetime | None = None,
              persistente: bool = True, sugeridos: list[dict[str, str]] | None = None) -> dict[str, Any]:  # fmt: skip
    """Só leitura: método, estado atual, relógio do BMC, prévia do que muda e bloqueios. Nunca levanta exceção.

    Campo vazio = primeira sugestão (DHCP da rede do cliente, senão ``pool.ntp.org``).
    """
    parametros = parametros or {}
    if sugeridos is None:
        sugeridos = sugestoes(ctx.executor) if getattr(ctx, "executor", None) is not None else []
    servidores, erro = servidores_de(parametros.get("servidores"), [s["servidor"] for s in sugeridos])
    pre: dict[str, Any] = {"acao": IDENTIFICADOR, "titulo": "Acertar o relógio do BMC via NTP", "metodo": None, "alvo": None,
                           "corpo": None, "parametros": {"servidores": servidores}, "sugestoes": sugeridos, "antes": {},
                           "previa": [], "avisos": [], "bloqueios": [], "nada_a_fazer": False, "pode_seguir": False}  # fmt: skip
    if erro:
        pre["bloqueios"].append(erro[0].upper() + erro[1:] + ".")
    if not persistente:
        pre["bloqueios"].append("Sem local gravável no pendrive: os valores atuais do NTP precisam ficar guardados no pendrive "
                                "(para poder desfazer) antes de qualquer alteração.")  # fmt: skip
    if not redfish_validado(ctx):
        pre["bloqueios"].append("Sem credenciais do BMC validadas: o NTP do BMC só é configurado pelo Redfish "
                                "(informe as credenciais ou acerte pela interface do BMC).")  # fmt: skip
        return pre
    try:
        caminho, gerente = gerenciador(ctx)
        metodo, alvo, ntp = _ler_ntp(ctx, caminho, gerente)
    except ERROS_LEITURA as falha:
        pre["bloqueios"].append(f"Leitura do BMC incompleta: {falha}.")
        return pre
    diferenca = _diferenca_relogio(gerente.get("data_hora"), agora)
    pre["antes"] = {"ntp_ligado": (ntp or {}).get("ativo"), "servidores": (ntp or {}).get("servidores", []),
                    "data_hora_bmc": gerente.get("data_hora"), "diferenca_s": diferenca}  # fmt: skip
    pre["bruto_antes"] = (ntp or {}).get("bruto") or {}
    if metodo is None:
        pre["bloqueios"].append("O BMC não publica a configuração de NTP no Redfish: acerte pela interface do BMC.")
        return pre
    pre["metodo"], pre["alvo"] = metodo, alvo
    pre["corpo"] = _corpo(metodo, servidores)
    if diferenca is not None:
        pre["avisos"].append(f"Relógio do BMC {'adiantado' if diferenca > 0 else 'atrasado'} {abs(diferenca)} s em relação a este "
                             "computador (que também pode estar errado se o pendrive iniciou sem rede).")  # fmt: skip
    pre["avisos"].append("O BMC precisa alcançar o servidor NTP pela rede dele (porta dedicada/compartilhada do BMC).")
    if ntp.get("ativo") is True and ntp.get("servidores", [])[: len(servidores)] == servidores:
        pre["nada_a_fazer"] = True
        pre["avisos"].append("O NTP do BMC já está ligado com esses servidores: nada a alterar.")
        return pre
    atuais = ", ".join(ntp.get("servidores") or []) or "(nenhum)"
    ligado = {True: "ligado", False: "desligado", None: "não informado"}[ntp.get("ativo")]
    pre["previa"] = [f"NTP do BMC: {ligado} → ligado", f"Servidores: {atuais} → {', '.join(servidores)}",
                     f"Pedido: PATCH {alvo} {json.dumps(pre['corpo'], ensure_ascii=False)}",
                     f"Antes de alterar: valores atuais guardados e conferidos em MSSCAN/{PASTA_ACOES}/ (para Desfazer)"]  # fmt: skip
    if metodo == "dell" and len(ntp.get("servidores") or []) > len(servidores):
        pre["previa"].append(f"NTP{len(servidores) + 1} em diante continuam como estão")
    pre["pode_seguir"] = not pre["bloqueios"]
    return pre


def guardar_anteriores(pre: dict[str, Any], modo: str, pasta_dados: str) -> tuple[Path, str]:
    """Grava e confere no pendrive os valores exatos do NTP antes da alteração: ``(arquivo, sha256)``.

    Levanta ``OSError``/``ValueError`` se a gravação ou a conferência falhar. Quem chama abre a janela de escrita.
    """
    destino = Path(pasta_dados) / PASTA_ACOES / f"ntp_antes_{datetime.now():%Y-%m-%d_%H%M%S}.json"
    conteudo = {"acao": IDENTIFICADOR, "guardado": datetime.now().isoformat(timespec="seconds"), "modo": modo,
                "metodo": pre["metodo"], "alvo": pre["alvo"], "antes": pre["antes"], "bruto": pre.get("bruto_antes") or {},
                "restaurar": corpo_restaurar(pre["metodo"], pre.get("bruto_antes") or {})}  # fmt: skip
    soma = gravar_conferido(destino, (json.dumps(conteudo, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    return destino, soma


def executar(ctx: Any, pre: dict[str, Any], modo: str, pedir_administrador: Any = None,
             pasta_dados: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Executa a ação já confirmada. ``simular`` (padrão) só descreve; ``aplicar`` passa pela trava.

    Nos dois modos, antes de tudo, guarda os valores atuais no pendrive (sem essa cópia conferida, nada é alterado).
    """
    resultado = novo_resultado(pre, modo)
    try:
        if not pasta_dados:
            raise OSError("sem local gravável no pendrive")
        arquivo, soma = guardar_anteriores(pre, modo, pasta_dados)
    except (OSError, ValueError) as falha:
        resultado["situacao"] = "falhou"
        resultado["mensagem"] = f"Valores atuais do NTP não guardados no pendrive ({falha}): nada foi alterado no BMC."
        passo(resultado, "guardar valores atuais", resultado["mensagem"], "falhou")
        return resultado
    local = no_pendrive(arquivo, pasta_dados)
    resultado["anteriores"] = {"arquivo": str(arquivo), "no_pendrive": local, "sha256": soma}
    passo(resultado, "guardar valores atuais", f"{local} (relido do pendrive; SHA-256 {soma[:16]}… conferido)")
    if modo != MODO_APLICAR:
        passo(resultado, "alterar NTP (simulado)", f"PATCH {pre['alvo']} {json.dumps(pre['corpo'], ensure_ascii=False)}", "simulado")
        resultado["situacao"] = "simulada"
        resultado["mensagem"] = f"Simulação concluída: nada foi alterado no BMC. Valores atuais guardados em {local}."
        resultado["desfazer"] = {"disponivel": False, "arquivo": str(arquivo),
                                 "motivo": "simulação: nada foi alterado no BMC (no modo aplicar, o botão Desfazer volta a estes valores)"}
        return resultado
    if not garantir_administrador(ctx, pedir_administrador, resultado):
        return resultado
    operacao = NTP_DELL if pre["metodo"] == "dell" else NTP_DMTF
    if not resposta_de_escrita(resultado, "alterar NTP", ctx.bmc.cliente.acao(operacao, pre["alvo"], pre["corpo"])):
        return resultado
    resultado["desfazer"] = {"disponivel": True, "arquivo": str(arquivo), "motivo": ""}
    ntp, resultado["depois"] = _reler(ctx, resultado)
    servidores = pre["parametros"]["servidores"]
    if ntp and ntp.get("ativo") is True and ntp.get("servidores", [])[: len(servidores)] == servidores:
        resultado["situacao"], resultado["mensagem"] = "aplicada", "NTP do BMC ligado com os servidores escolhidos."
        passo(resultado, "conferir", "o BMC confirma a configuração nova")
    else:
        resultado["situacao"] = "aplicada_sem_confirmacao"
        resultado["mensagem"] = "O BMC aceitou o pedido, mas a releitura não mostra a configuração nova (confira no BMC)."
        passo(resultado, "conferir", resultado["mensagem"], "aviso")
    return resultado


def _reler(ctx: Any, resultado: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Releitura depois de uma escrita: ``(ntp, estado para o relatório)``."""
    try:
        caminho, gerente = gerenciador(ctx)
        _, _, ntp = _ler_ntp(ctx, caminho, gerente)
    except ERROS_LEITURA as falha:
        ntp, gerente = None, {}
        passo(resultado, "conferir", f"releitura do BMC falhou: {falha}", "aviso")
    return ntp, {"ntp_ligado": (ntp or {}).get("ativo"), "servidores": (ntp or {}).get("servidores", []),
                 "data_hora_bmc": gerente.get("data_hora"), "diferenca_s": _diferenca_relogio(gerente.get("data_hora"))}  # fmt: skip


# --- Desfazer ------------------------------------------------------------------------------------


def _ler_guardado(arquivo: str) -> dict[str, Any]:
    """Conteúdo do arquivo de valores anteriores. Levanta ``OSError``/``ValueError`` se ilegível ou de outra ação."""
    dados = json.loads(Path(arquivo).read_text(encoding="utf-8"))
    if not isinstance(dados, dict) or dados.get("acao") != IDENTIFICADOR or dados.get("metodo") not in ("dell", "dmtf"):
        raise ValueError("arquivo de valores anteriores inválido")
    return dados


def prechecar_desfazer(ctx: Any, resultado: dict[str, Any]) -> dict[str, Any]:
    """Só leitura: lê do pendrive os valores guardados, relê o BMC e mostra atual → guardado. Nunca levanta exceção."""
    pre: dict[str, Any] = {"acao": IDENTIFICADOR, "titulo": "Desfazer: " + str(resultado.get("titulo") or IDENTIFICADOR),
                           "desfazer": True, "modo_original": resultado.get("modo"), "metodo": None, "alvo": None, "corpo": None,
                           "arquivo": None, "antes": {}, "previa": [], "avisos": [], "bloqueios": [], "nada_a_fazer": False,
                           "pode_seguir": False}  # fmt: skip
    info = resultado.get("desfazer") or {}
    if resultado.get("desfeita"):
        pre["bloqueios"].append("Esta ação já foi desfeita.")
        return pre
    if not info.get("disponivel") or not info.get("arquivo"):
        pre["bloqueios"].append(info.get("motivo") or "Esta ação não pode ser desfeita (nada foi alterado).")
        return pre
    try:
        guardado = _ler_guardado(info["arquivo"])
    except (OSError, ValueError) as falha:
        pre["bloqueios"].append(f"Valores anteriores não lidos do pendrive ({falha}): restaure pela interface do BMC.")
        return pre
    pre["arquivo"], pre["metodo"], pre["alvo"], pre["corpo"] = info["arquivo"], guardado["metodo"], guardado["alvo"], guardado["restaurar"]
    if not redfish_validado(ctx):
        pre["bloqueios"].append("Sem credenciais do BMC validadas: não dá para ler nem restaurar o NTP.")
        return pre
    try:
        caminho, gerente = gerenciador(ctx)
        _, _, ntp = _ler_ntp(ctx, caminho, gerente)
    except ERROS_LEITURA as falha:
        pre["bloqueios"].append(f"Leitura do BMC incompleta: {falha}.")
        return pre
    pre["antes"] = {"ntp_ligado": (ntp or {}).get("ativo"), "servidores": (ntp or {}).get("servidores", []),
                    "data_hora_bmc": gerente.get("data_hora")}  # fmt: skip
    anterior = guardado.get("antes") or {}
    if ntp and ntp.get("bruto") == guardado.get("bruto"):
        pre["nada_a_fazer"] = True
        pre["avisos"].append("O NTP do BMC já está com os valores guardados: nada a desfazer.")
        return pre
    ligado = {True: "ligado", False: "desligado", None: "não informado"}
    pre["previa"] = [f"NTP do BMC: {ligado[(ntp or {}).get('ativo')]} → {ligado[anterior.get('ntp_ligado')]}",
                     f"Servidores: {', '.join((ntp or {}).get('servidores') or []) or '(nenhum)'} → "
                     f"{', '.join(anterior.get('servidores') or []) or '(nenhum)'}",
                     f"Valores guardados em {Path(info['arquivo']).name} ({guardado.get('guardado')})",
                     f"Pedido: PATCH {pre['alvo']} {json.dumps(pre['corpo'], ensure_ascii=False)}"]  # fmt: skip
    pre["pode_seguir"] = True
    return pre


def desfazer(ctx: Any, pre: dict[str, Any], resultado: dict[str, Any], modo: str, pedir_administrador: Any = None) -> dict[str, Any]:
    """Volta aos valores guardados. Registra em ``resultado["desfeita"]`` (vai para o relatório). Nunca levanta exceção."""
    registro: dict[str, Any] = {"modo": modo, "situacao": "", "mensagem": "", "passos": [], "antes": pre.get("antes") or {},
                                "depois": None, "quando": datetime.now().isoformat(timespec="seconds")}  # fmt: skip
    if modo != MODO_APLICAR:
        passo(registro, "restaurar NTP (simulado)", f"PATCH {pre['alvo']} {json.dumps(pre['corpo'], ensure_ascii=False)}", "simulado")
        registro["situacao"], registro["mensagem"] = "simulada", "Simulação do desfazer: nada foi alterado no BMC."
    elif garantir_administrador(ctx, pedir_administrador, registro):
        operacao = NTP_DELL_RESTAURAR if pre["metodo"] == "dell" else NTP_DMTF_RESTAURAR
        if resposta_de_escrita(registro, "restaurar NTP", ctx.bmc.cliente.acao(operacao, pre["alvo"], pre["corpo"])):
            ntp, registro["depois"] = _reler(ctx, registro)
            guardado = _ler_guardado(pre["arquivo"]) if Path(pre["arquivo"]).is_file() else {}
            if ntp and ntp.get("bruto") == guardado.get("bruto"):
                registro["situacao"], registro["mensagem"] = "desfeita", "NTP do BMC de volta aos valores anteriores."
            else:
                registro["situacao"] = "desfeita_sem_confirmacao"
                registro["mensagem"] = "O BMC aceitou o pedido, mas a releitura não mostra os valores anteriores (confira no BMC)."
            passo(registro, "conferir", registro["mensagem"], "ok" if registro["situacao"] == "desfeita" else "aviso")
    registro["fim"] = datetime.now().isoformat(timespec="seconds")
    if registro["situacao"] in ("simulada", "desfeita", "desfeita_sem_confirmacao"):
        resultado["desfeita"] = registro
    else:
        resultado.setdefault("tentativas_desfazer", []).append(registro)
    return registro

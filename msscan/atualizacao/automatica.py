"""Atualização automática depois do único ``ATUALIZAR`` (CLAUDE.md 18.2 a 18.4).

Depois da confirmação na tela "Plano de atualização", nada mais é perguntado: acesso ao iDRAC, credenciais, instalação,
reinícios pelo pendrive, retomada, restauração e relatório. Por componente, em ordem, até um caminho funcionar (cada
tentativa vai para ``resultado["tentativas"]`` e para o relatório):

a. Redfish com as credenciais já informadas;
b. Redfish com um administrador TEMPORÁRIO criado pelo IPMI interno (apagado no fim);
c. iDRAC inalcançável → OS to iDRAC Pass-through TEMPORÁRIO (racadm local) e repete a/b;
d. ``racadm update -f`` local (racadm em ``vendor/``);
e. pacote Linux ``.BIN`` direto (``-c`` e depois ``-q``, tabela oficial de códigos de saída);
f. pacote sem caminho disponível (ex.: ``.EXE`` sem iDRAC) → outro formato oficial do mesmo componente e da mesma versão
   no catálogo (hash conferido) e repete e;
g. só então: o que faltou e o que o técnico pode fazer.

Limites que continuam (proteção, não prévia): nada é forçado (o pacote que o próprio fabricante diz não aplicável — ``-c``
recusado — encerra o item), hash divergente aborta, nunca outro modelo, nunca downgrade, bloqueio só por evidência ATUAL
e crítica de hardware (relida na retomada).

Mudanças temporárias no iDRAC: registradas no pendrive ANTES (``temporarias.py``) e desfeitas antes de cada reinício e ao
fim; se o MSscan fechar, no próximo boot.

MOCKUP(rodada final): o ``racadm update`` local e o pass-through não foram testados num iDRAC real.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from msscan import textos
from msscan.acoes import idrac
from msscan.atualizacao import liberacao
from msscan.acoes.comum import MODO_APLICAR as MODO_ACAO_APLICAR
from msscan.atualizacao import jobs as fila_jobs, manual, progresso as prog_mod, temporarias as temp
from msscan.core.fallback import FalhaColeta
from msscan.parsers import ErroParser
from msscan.parsers.redfish import membros, parse_item_firmware
from msscan.atualizacao.aplicacao import (
    PASTA_BACKUP,
    PASTA_PACOTES,
    _acompanhar_tarefa,
    _apagar_estado,
    _baixar_e_conferir,
    _passo,
    falha_explicita,
    hash_ainda_confere,
    ler_bmc,
    motivo_metodo,
    salvar_estado,
)
from msscan.atualizacao.versoes import ATUALIZACAO, comparar_versoes
from msscan.core.executor import (
    BOOT_UNICO_USB,
    CAMINHO_PUSH_LEGADO,
    ENVIAR_FIRMWARE_LEGADO,
    SIMPLE_UPDATE,
    corpo_simple_update,
    CORPO_BOOT_USB,
    ENVIAR_FIRMWARE,
    corpo_boot_uefi,
    EXPORTAR_CONFIGURACAO,
    EXTENSOES_RACADM_UPDATE,
    imagem_nativa_racadm,
    MOTIVO_TRAVA,
    PARAMETROS_UPDATE,
)

APLICADO, AGENDADO, POSSIVELMENTE, FALHOU, RECUSADO, TRAVA, ENCERRADO = (
    "aplicado", "agendado", "possivelmente_agendado", "falhou", "recusado", "trava", "encerrado")  # fmt: skip
"""Desfecho de um item. ``recusado``: o fabricante (``-c``) ou o hash disseram não — nenhum outro caminho é tentado."""
CAMINHOS = {
    "r": textos.t("py_automatica.repositorio_pelo_idrac_como_o"),
    "o": textos.t("py_automatica.repositorio_online_da_dell_pelo"),
    "a": textos.t("py_automatica.redfish_com_as_credenciais_informadas"),
    "b": textos.t("py_automatica.redfish_com_administrador_temporario_ipmi"),
    "c": textos.t("py_automatica.os_to_idrac_pass_through"),
    "d": textos.t("py_automatica.racadm_local_update_f"),
    "e": textos.t("py_automatica.pacote_linux_bin_c_e"),
    "f": textos.t("py_automatica.outro_formato_oficial_no_catalogo"),
    "g": textos.t("py_automatica.nenhum_caminho_disponivel"),
}
VALIDADE_RETOMADA = timedelta(hours=24)
"""Plano confirmado continua sozinho depois de reinícios por até 24 h (depois disso, o técnico confirma de novo)."""


def _tentativa(resultado: dict[str, Any], componente: str | None, letra: str, situacao: str, detalhe: str) -> None:
    resultado.setdefault("tentativas", []).append({"componente": componente or textos.t("py_automatica.acesso_ao_idrac"), "caminho": letra,
                                                   "descricao": CAMINHOS[letra], "situacao": situacao, "detalhe": detalhe,
                                                   "quando": datetime.now().isoformat(timespec="seconds")})  # fmt: skip
    _passo(resultado, f"{letra}) {CAMINHOS[letra]}", f"{componente + ': ' if componente else ''}{detalhe}",
           {"ok": "ok", "indisponivel": "aviso", "falhou": "falhou", "recusado": "bloqueado"}.get(situacao, "aviso"))  # fmt: skip


def _admin(bmc: dict[str, Any]) -> bool:
    return bool(bmc.get("redfish") and bmc.get("administrador") is True)


def _redfish_responde(ctx: Any) -> bool:
    return bool(getattr(getattr(ctx, "redfish", None), "confirmado", False) and getattr(ctx, "bmc", None) is not None)


# --- Plano (só informativo) -------------------------------------------------------------------------------------------


def prever_temporarias(ctx: Any, bmc: dict[str, Any]) -> list[str]:
    """O que o MSscan PODE mudar temporariamente no iDRAC (tela "Plano de atualização"; só informativo)."""
    if _admin(bmc):
        return [textos.t("py_automatica.nenhuma_as_credenciais_de_administrador")]
    linhas = []
    ipmi = bool(getattr(getattr(ctx, "ipmi", None), "disponivel", False)) or bool(idrac.racadm(ctx))
    if not _redfish_responde(ctx):
        if idrac.racadm(ctx):
            linhas.append(textos.t("py_automatica.os_to_idrac_pass_through_3"))  # fmt: skip
        elif not ((pendente := (getattr(ctx, "cache", None) or {}).get("racadm_pendente")) is not None and not pendente.is_set()):
            linhas.append(textos.t("py_automatica.o_idrac_nao_responde_daqui"))  # fmt: skip
        else:
            linhas.append(textos.t("py_automatica.o_idrac_tools_esta_sendo"))  # fmt: skip
    if ipmi:
        linhas.append(textos.t("py_automatica.usuario_administrador_temporario_do_msscan"))  # fmt: skip
    linhas.append(textos.t("py_automatica.nunca_rede_do_idrac_ip"))
    return linhas


# --- Acesso ao iDRAC (a, b, c) -----------------------------------------------------------------------------------------


def _sem_job_ativo(ctx: Any, resultado: dict[str, Any], dormir: Callable[[float], None], letra: str, mudanca: str) -> bool:
    """21.3: nada que possa reiniciar o iDRAC (ou a rede interna dele: pass-through) com um job dele em andamento — espera
    terminar; senão, não muda nada. Criar/apagar o usuário temporário não reinicia nada e não espera."""
    prog = prog_mod.de(resultado)

    def ao_esperar(jobs: str) -> None:
        prog.esperando(textos.t("py_automatica.o_job_do_idrac_em", mudanca, jobs), textos.t("py_automatica.maintenance_job_queue_na_pagina"))

    livre, detalhe = fila_jobs.esperar_sem_ativos(ctx, dormir, ao_esperar=ao_esperar)
    if not livre:
        _tentativa(resultado, None, letra, "indisponivel", textos.t("py_automatica.job_do_idrac_em_andamento", detalhe, mudanca))  # fmt: skip
    return livre


def _criar_usuario(ctx: Any, executor: Any, pasta_dados: str, registro: temp.Temporarias, resultado: dict[str, Any]) -> bool:
    pre = idrac.prechecar_usuario(ctx, persistente=True)
    if not pre["pode_seguir"]:
        _tentativa(resultado, None, "b", "indisponivel", "; ".join(pre["bloqueios"]) or textos.t("py_automatica.nao_foi_possivel_preparar_o"))
        return False
    parametros = pre["parametros"]
    try:
        mudanca = registro.registrar(temp.USUARIO, textos.t("py_automatica.usuario_administrador_temporario_slot", parametros['nome'], parametros['slot']))
    except (OSError, ValueError) as erro:
        _tentativa(resultado, None, "b", "indisponivel", textos.t("py_automatica.registro_da_mudanca_temporaria_nao", erro))
        return False
    feito = idrac.executar(ctx, executor, pre, MODO_ACAO_APLICAR, pasta_dados)
    if feito["situacao"] == "aplicada":
        registro.marcar(mudanca, temp.FEITA)
    if feito["situacao"] == "aplicada" and ctx.bmc is not None and ctx.bmc.validado:
        _tentativa(resultado, None, "b", "ok", textos.t("py_automatica.usuario_temporario_criado_sessao_redfish", parametros['nome']))
        return True
    _tentativa(resultado, None, "b", "falhou", feito["mensagem"] or feito["situacao"])
    return False


def _ligar_passthrough(ctx: Any, executor: Any, pasta_dados: str, registro: temp.Temporarias, resultado: dict[str, Any],
                       dormir: Callable[[float], None] = time.sleep) -> bool:  # fmt: skip
    pre = idrac.prechecar_passthrough(ctx, persistente=True)
    if pre["nada_a_fazer"]:
        ip = (pre.get("antes") or {}).get("IP interno (USB NIC)")
        achou = bool(ip) and idrac._procurar_redfish(ctx, ip)
        _tentativa(resultado, None, "c", "ok" if achou else "falhou",
                   textos.t("py_automatica.pass_through_ja_estava_ligado", 'encontrado' if achou else textos.t("py_automatica.nao_respondeu"), ip or '?'))  # fmt: skip
        return achou
    if not pre["pode_seguir"]:
        _tentativa(resultado, None, "c", "indisponivel", "; ".join(pre["bloqueios"]))
        return False
    if not _sem_job_ativo(ctx, resultado, dormir, "c", "ligar o pass-through"):
        return False
    try:
        mudanca = registro.registrar(temp.PASSTHROUGH, textos.t("py_automatica.os_to_idrac_pass_through_2"), pre.get("bruto_antes"))
    except (OSError, ValueError) as erro:
        _tentativa(resultado, None, "c", "indisponivel", textos.t("py_automatica.registro_da_mudanca_temporaria_nao", erro))
        return False
    feito = idrac.executar(ctx, executor, pre, MODO_ACAO_APLICAR, pasta_dados)
    if feito["situacao"].startswith("aplicada"):
        registro.marcar(mudanca, temp.FEITA)
    achou = _redfish_responde(ctx)
    _tentativa(resultado, None, "c", "ok" if achou else "falhou", feito["mensagem"] or feito["situacao"])
    return achou


def _reconsultar_redfish(ctx: Any) -> bool:
    """Opção B (CLAUDE.md 19.1): o técnico pode ter ligado o cabo do iDRAC depois da detecção — uma nova tentativa no
    mesmo endereço antes de desistir da rede."""
    info = getattr(ctx, "redfish", None)
    if _redfish_responde(ctx) or info is None or not info.endereco:
        return _redfish_responde(ctx)
    from msscan.deteccao.detectores import tentar_redfish

    try:
        return tentar_redfish(ctx, info.endereco, info.porta, textos.t("py_automatica.nova_tentativa", info.origem or textos.t("py_automatica.ip_do_idrac")))
    except Exception:  # noqa: BLE001 - reconsultar nunca derruba a atualização
        return False


def garantir_acesso(ctx: Any, executor: Any, pasta_dados: str, registro: temp.Temporarias, resultado: dict[str, Any],
                    dormir: Callable[[float], None] = time.sleep) -> dict[str, Any]:  # fmt: skip
    """Caminhos a, b e c, sem perguntar nada, na ordem B (rede) → A (pass-through pelo racadm local) da 19.1.

    Devolve o ``ler_bmc`` final (com ou sem Redfish de administrador). O caminho b (usuário temporário) só serve com um
    Redfish alcançável; sem isso fica registrado como indisponível, com o motivo (nunca some do relatório).
    """
    bmc = ler_bmc(ctx)
    if _admin(bmc):
        _tentativa(resultado, None, "a", "ok", textos.t("py_automatica.credenciais_de_administrador_informadas_pelo"))
        return bmc
    _tentativa(resultado, None, "a", "indisponivel", motivo_metodo(bmc)["texto"] or bmc.get("motivo") or textos.t("py_automatica.sem_administrador"))
    _esperar_racadm(ctx, resultado)
    if not _reconsultar_redfish(ctx):
        _ligar_passthrough(ctx, executor, pasta_dados, registro, resultado, dormir)
    if not _redfish_responde(ctx):
        _tentativa(resultado, None, "b", "indisponivel", textos.t("py_automatica.o_redfish_do_idrac_nao"))  # fmt: skip
    elif _criar_usuario(ctx, executor, pasta_dados, registro, resultado):
        bmc = ler_bmc(ctx)
    return bmc


# --- Caminhos de instalação (d, e, f) e Redfish ----------------------------------------------------------------------


def _redfish(ctx: Any, bmc: dict[str, Any], registro: dict[str, Any], resultado: dict[str, Any], dormir: Callable[[float], None],
             nova_tentativa: bool = True) -> str:  # fmt: skip
    cliente = ctx.bmc.cliente
    letra = _letra(resultado)
    prog, componente = prog_mod.de(resultado), registro["componente"]
    prog.etapa(componente, prog_mod.ENVIANDO, textos.t("py_automatica.enviando_o_pacote_ao_idrac"), 0)
    prog.esperando(textos.t("py_automatica.o_idrac_receber_o_pacote", componente), textos.t("py_automatica.o_cabo_e_o_ip"))
    resposta = cliente.escrever(ENVIAR_FIRMWARE, bmc["multipart"], PARAMETROS_UPDATE, registro["arquivo"],
                                progresso=lambda feito, total: prog.percentual(componente, feito * 100 / total if total else None))  # fmt: skip
    if resposta.erro_tipo == "trava":
        _tentativa(resultado, registro["componente"], letra, "falhou", resposta.erro)
        return TRAVA
    if resposta.status != 202 or not resposta.local:
        if not _recusa_explicita(resposta):
            return _incerto(resultado, componente, letra, textos.t("py_automatica.envio_do_pacote", resposta.erro or resposta.status))
        motivo = textos.t("py_automatica.o_idrac_recusou_o_firmware", resposta.erro or resposta.status)
        prog.avisar_agora(textos.t("py_automatica.o_msscan_tenta_o_proximo", componente, motivo))
        _tentativa(resultado, registro["componente"], letra, "falhou", motivo)
        return FALHOU
    prog.resposta_idrac()
    prog.etapa(componente, prog_mod.VALIDANDO, textos.t("py_automatica.o_idrac_esta_conferindo_o"))
    desfecho = _acompanhar(cliente, resposta.local, registro, resultado, letra, dormir, ctx)
    if desfecho == FALHOU and nova_tentativa and fila_jobs.reinicio_do_idrac(_job_da_tarefa(registro)):
        _tentativa(resultado, componente, letra, "falhou", textos.t("py_automatica.o_idrac_reiniciou_no_meio"))  # fmt: skip
        if reconectar_idrac(ctx, resultado, dormir):
            return _redfish(ctx, bmc, registro, resultado, dormir, nova_tentativa=False)
    return desfecho


def _recusa_explicita(resposta: Any) -> bool:
    from msscan.atualizacao.repositorio import recusa_explicita

    return recusa_explicita(resposta)


def _incerto(resultado: dict[str, Any], componente: str, letra: str, pedido: str) -> str:
    """Resposta incerta (timeout, conexão cortada, 5xx, "já existe job"): o iDRAC pode ter recebido o pacote e criado o job.
    Nunca seguir pelo racadm/.BIN (gravaria duas vezes — revisão de 06/10/2026, C2): parar e conferir."""
    motivo = (textos.t("py_automatica.resposta_incerta_do_idrac_ao", pedido))  # fmt: skip
    prog_mod.de(resultado).avisar_agora(f"{componente}: {motivo}.")
    _tentativa(resultado, componente, letra, "falhou", motivo)
    return POSSIVELMENTE


def _job_da_tarefa(registro: dict[str, Any]) -> dict[str, Any]:
    tarefa = registro.get("tarefa") or {}
    return {"mensagem": " ".join([*(tarefa.get("mensagens") or []), str(tarefa.get("dell_mensagem") or "")])}


ESPERA_IDRAC_S = 900.0
"""Até 15 min para o iDRAC voltar depois de reiniciar (firmware do iDRAC, RED115)."""
INTERVALO_IDRAC_S = 15.0


def reconectar_idrac(ctx: Any, resultado: dict[str, Any], dormir: Callable[[float], None], limite_s: float = ESPERA_IDRAC_S) -> bool:
    """Espera o iDRAC voltar a responder pelo Redfish e reabre a sessão (mesmas credenciais, só na memória). CLAUDE.md
    21.3/21.4. ``False`` se não voltou no limite."""
    prog = prog_mod.de(resultado)
    prog.esperando(textos.t("py_automatica.o_idrac_voltar_a_responder"),
                   textos.t("py_automatica.as_luzes_da_porta_do"))  # fmt: skip
    acesso = getattr(ctx, "bmc", None)
    cliente = getattr(acesso, "cliente", None)
    esperado = 0.0
    while cliente is not None and esperado <= limite_s:
        if cliente.reconectar():
            prog.resposta_idrac()
            _passo(resultado, "iDRAC", textos.t("py_automatica.o_idrac_voltou_a_responder", int(esperado)))
            return True
        dormir(INTERVALO_IDRAC_S)
        esperado += INTERVALO_IDRAC_S
    _passo(resultado, "iDRAC", textos.t("py_automatica.o_idrac_nao_voltou_a", int(limite_s // 60)), "falhou")
    return False


ESPERA_IDRAC_NOVO_S = 1800.0
"""Até 30 min para o iDRAC voltar JÁ com o firmware novo (M3 de 06/10/2026; 30 min pelo ciclo real de 09/10/2026)."""


def esperar_idrac_atualizado(ctx: Any, resultado: dict[str, Any], dormir: Callable[[float], None], item: dict[str, Any],
                             limite_s: float = ESPERA_IDRAC_NOVO_S) -> bool:  # fmt: skip
    """Revisão de 06/10/2026, M3: logo depois do firmware do iDRAC, ele ainda responde ANTES de reiniciar. "Voltou" só vale
    quando o inventário lido pelo Redfish mostra a versão nova do iDRAC; até lá, nada segue.

    09/10/2026 (R640 real): a sessão antiga morre no reinício e o inventário do sistema (dmidecode, ethtool) nunca vem
    vazio, então ele não serve de sinal. A volta é conferida sem a sessão antiga: ping, ``/redfish/v1`` sem login, sessão
    NOVA (credenciais só na memória) e a versão no FirmwareInventory do Redfish. Leitura falhou → sessão nova na próxima
    volta (uma por volta, nunca uma por consulta: o iDRAC tem limite de sessões)."""
    from urllib.parse import urlsplit

    from msscan.atualizacao.inventario import inventario_redfish

    prog = prog_mod.de(resultado)
    dica = textos.t("py_automatica.nao_desligue_o_servidor_a")
    cliente = getattr(getattr(ctx, "bmc", None), "cliente", None)
    if cliente is None:
        return _esperar_idrac_ipmi(ctx, resultado, dormir, item, limite_s)
    ip = urlsplit(str(getattr(cliente, "base", ""))).hostname
    esperado, versao, sessao_nova = 0.0, None, False
    while cliente is not None and esperado <= limite_s:
        conferido = [textos.t("py_automatica.de_min", int(esperado // 60), int(limite_s // 60))]
        ping = bool(ip) and ctx.executor.rodar([*PING, ip], timeout=10)[0] == 0
        conferido.append("ping: " + (textos.t("py_automatica.responde") if ping else textos.t("py_automatica.sem_resposta")))
        try:
            cliente.raiz()
            raiz = True
        except FalhaColeta:
            raiz = False
        conferido.append(textos.t("py_automatica.redfish_sem_login") + (textos.t("py_automatica.responde") if raiz else textos.t("py_automatica.sem_resposta")))
        if raiz:
            prog.resposta_idrac()
            if not cliente.autenticado:
                sessao_nova = cliente.reconectar()
                conferido.append(textos.t("py_automatica.sessao_nova") + (textos.t("py_automatica.aberta") if sessao_nova else textos.t("py_automatica.recusada_o_idrac_ainda_esta")))
            atual = next((i for i in inventario_redfish(ctx) if item.get("component_id")
                          and str(i.get("component_id") or "") == str(item["component_id"])), None) if cliente.autenticado else None  # fmt: skip
            if atual is None and cliente.autenticado:
                cliente.descartar_sessao()  # leitura falhou: o iDRAC reiniciou agora; sessão nova na próxima volta
            versao = (atual or {}).get("versao") or versao
            conferido.append(textos.t("py_automatica.versao_do_idrac_esperada", (atual or {}).get('versao') or textos.t("py_automatica.nao_lida"), item.get('disponivel')))
            if atual and atual.get("versao") == item.get("disponivel"):
                _passo(resultado, "iDRAC", textos.t("py_automatica.o_idrac_voltou_com_o", atual['versao'], int(esperado))
                       + (textos.t("py_automatica.sessao_nova_aberta") if sessao_nova else textos.t("py_automatica.conferido_pelo_inventario_do_redfish")))  # fmt: skip
                return True
        prog.esperando(textos.t("py_automatica.o_idrac_reiniciar_e_voltar") + "; ".join(conferido), dica)
        dormir(INTERVALO_IDRAC_S)
        esperado += INTERVALO_IDRAC_S
    _passo(resultado, "iDRAC", textos.t("py_automatica.o_idrac_nao_voltou_com", item.get('disponivel'), int(limite_s // 60), versao or 'nenhuma'), "falhou")  # fmt: skip
    return False


SEM_QUEDA_IPMI_S = 600.0
"""Sem Redfish: se o IPMI local nunca saiu do ar, o iDRAC é dado como de volta depois de 10 min respondendo (o DUP pode ter
esperado o reinício dele antes de terminar)."""


def _esperar_idrac_ipmi(ctx: Any, resultado: dict[str, Any], dormir: Callable[[float], None], item: dict[str, Any],
                        limite_s: float) -> bool:  # fmt: skip
    """Pedido de 22/10/2026: firmware do iDRAC pelo ``.BIN`` (caminho f) sem Redfish acessível daqui. O próprio pacote já
    confirmou a gravação; antes, a falta do Redfish virava "o iDRAC não voltou" na hora e o técnico via falha. A volta é
    conferida pelo IPMI local (``ipmitool mc info``): sai do ar no reinício do iDRAC e volta. A versão completa fica para o
    próximo diagnóstico (o IPMI só mostra a abreviada)."""
    prog = prog_mod.de(resultado)
    esperado, caiu = 0.0, False
    # ponytail: sem IPMI local, só a espera fixa (nada a conferir daqui); com IPMI, a queda e a volta dele
    tem_ipmi = bool(getattr(getattr(ctx, "ipmi", None), "disponivel", False))
    while esperado <= limite_s:
        responde = tem_ipmi and ctx.executor.rodar(["ipmitool", "mc", "info"], timeout=20)[0] == 0
        caiu = caiu or (tem_ipmi and not responde)
        if (responde and caiu) or (esperado >= SEM_QUEDA_IPMI_S and (responde or not tem_ipmi)):
            _passo(resultado, "iDRAC", textos.t("py_automatica.idrac_voltou_ipmi" if tem_ipmi else "py_automatica.idrac_sem_ipmi",
                                                item.get("disponivel"), int(esperado // 60)))  # fmt: skip
            return True
        prog.esperando(textos.t("py_automatica.idrac_reiniciando_ipmi", int(esperado // 60)), textos.t("py_automatica.nao_desligue_o_servidor_a"))
        dormir(INTERVALO_IDRAC_S)
        esperado += INTERVALO_IDRAC_S
    _passo(resultado, "iDRAC", textos.t("py_automatica.o_idrac_nao_voltou_a", int(limite_s // 60)), "falhou")
    return False

def _letra(resultado: dict[str, Any]) -> str:
    return "b" if any(t["caminho"] == "b" and t["situacao"] == "ok" for t in resultado.get("tentativas") or []) else "a"


def _acompanhar(cliente: Any, local: str, registro: dict[str, Any], resultado: dict[str, Any], letra: str,
                dormir: Callable[[float], None], ctx: Any = None) -> str:  # fmt: skip
    prog, componente = prog_mod.de(resultado), registro["componente"]

    def ao_ler(tarefa: dict[str, Any]) -> None:
        prog.resposta_idrac()
        prog_mod.da_tarefa(prog, componente, tarefa)

    prog.esperando(textos.t("py_automatica.o_idrac_terminar_a_tarefa", componente), textos.t("py_automatica.maintenance_job_queue_na_pagina"))
    tarefa = _acompanhar_tarefa(cliente, local, dormir, ao_ler)
    if tarefa.get("sem_resposta") and ctx is not None and reconectar_idrac(ctx, resultado, dormir):
        tarefa = _acompanhar_tarefa(cliente, local, dormir, ao_ler)  # o iDRAC reiniciou no meio: relê a mesma tarefa
    registro["tarefa"] = {k: tarefa.get(k) for k in ("id", "estado", "percentual", "mensagens", "dell_mensagem")}
    reiniciou = tarefa.get("falhou") and fila_jobs.reinicio_do_idrac(_job_da_tarefa(registro))
    if falha_explicita(tarefa) or reiniciou:
        _tentativa(resultado, registro["componente"], letra, "falhou",
                   textos.t("py_automatica.tarefa_do_idrac_falhou_job", '; '.join(tarefa.get('mensagens') or [])))  # fmt: skip
        # Revisão de 06/10/2026, B2: falha declarada pelo próprio iDRAC encerra o item (nenhum outro caminho grava o mesmo
        # pacote por cima); só a interrupção por reinício do iDRAC (RED115) vale a nova tentativa.
        return FALHOU if reiniciou else ENCERRADO
    if tarefa.get("falhou") or not tarefa.get("terminou"):
        _tentativa(resultado, registro["componente"], letra, "ok", textos.t("py_automatica.aceito_pelo_idrac_fim_da"))
        return POSSIVELMENTE
    _tentativa(resultado, registro["componente"], letra, "ok",
               textos.t("py_automatica.aplicado_pelo_idrac_sem_reinicio") if tarefa.get("concluida") else textos.t("py_automatica.agendado_no_idrac_para_o"))  # fmt: skip
    return APLICADO if tarefa.get("concluida") else AGENDADO


def _redfish_legado(ctx: Any, bmc: dict[str, Any], registro: dict[str, Any], resultado: dict[str, Any],
                    dormir: Callable[[float], None]) -> str:  # fmt: skip
    """iDRAC7/8: upload do .EXE (fica "Available"), versão conferida pelo próprio iDRAC e só então ``SimpleUpdate``."""
    from msscan.atualizacao.inventario import coletar_inventario

    cliente, componente, letra = ctx.bmc.cliente, registro["componente"], _letra(resultado)
    envio = cliente.escrever(ENVIAR_FIRMWARE_LEGADO, CAMINHO_PUSH_LEGADO, None, registro["arquivo"])
    if envio.erro_tipo == "trava":
        _tentativa(resultado, componente, letra, "falhou", envio.erro)
        return TRAVA
    disponivel = (envio.corpo or {}).get("Id") if isinstance(envio.corpo, dict) else None
    if envio.status != 201 or not isinstance(disponivel, str):
        _tentativa(resultado, componente, letra, "falhou", textos.t("py_automatica.o_idrac_recusou_o_pacote", envio.erro or envio.status))
        return FALHOU
    imagem = f"{CAMINHO_PUSH_LEGADO}/{disponivel}"
    try:
        dados = parse_item_firmware(cliente.get(imagem))
    except (FalhaColeta, ErroParser) as erro:
        _tentativa(resultado, componente, letra, "falhou", textos.t("py_automatica.nao_foi_possivel_ler_o", erro))
        return FALHOU
    inventario = resultado.get("inventario_atual") if isinstance(resultado.get("inventario_atual"), list) else coletar_inventario(ctx)[0]
    instalado = next((i for i in inventario if str(i.get("component_id") or "") == str(dados.get("software_id") or "")), None)
    situacao, motivo = comparar_versoes((instalado or {}).get("versao"), dados.get("versao"), bool((instalado or {}).get("truncada")))
    registro.update({"instalada": (instalado or {}).get("versao"), "disponivel": dados.get("versao"), "component_id": dados.get("software_id")})
    if situacao != ATUALIZACAO:
        _tentativa(resultado, componente, letra, "recusado", textos.t("py_automatica.idrac_pacote_nada_foi_aplicado", dados.get('versao'), motivo))
        return RECUSADO
    instalacao = cliente.escrever(SIMPLE_UPDATE, bmc["simple_update"], corpo_simple_update(imagem))
    if instalacao.erro_tipo == "trava":
        _tentativa(resultado, componente, letra, "falhou", instalacao.erro)
        return TRAVA
    if instalacao.status not in (200, 202) or not instalacao.local:
        if not _recusa_explicita(instalacao):
            return _incerto(resultado, componente, letra, f"SimpleUpdate ({instalacao.erro or instalacao.status})")
        _tentativa(resultado, componente, letra, "falhou", textos.t("py_automatica.o_idrac_recusou_o_simpleupdate", instalacao.erro or instalacao.status))
        return FALHOU
    return _acompanhar(cliente, instalacao.local, registro, resultado, letra, dormir, ctx)


def _esperar_racadm(ctx: Any, resultado: dict[str, Any]) -> None:
    """CLAUDE.md 20: com o download do iDRAC Tools em andamento, espera terminar em vez de falhar por falta do racadm."""
    from msscan.deteccao import idrac_tools

    if idrac.racadm(ctx) is None and idrac_tools.esperar_download(ctx):
        info = ctx.cache.get("idrac_tools") or {}
        _passo(resultado, "racadm", textos.t("py_automatica.esperado_o_download_do_idrac") + (textos.t("py_automatica.racadm_pronto") if idrac.racadm(ctx)
               else textos.t("py_automatica.nao_ficou_pronto", info.get('mensagem') or textos.t("py_automatica.motivo_desconhecido"))), "ok" if idrac.racadm(ctx) else "aviso")  # fmt: skip


ESPERA_JOB_RACADM_S = 1800.0
"""Até 30 min acompanhando o job criado pelo ``racadm update`` (revisão de 06/10/2026, M2)."""


def _ids_na_fila(ctx: Any) -> set[str] | None:
    fila = fila_jobs.ler_fila(ctx)
    return {j["id"] for j in fila["jobs"]} if fila["origem"] else None


def _acompanhar_job_racadm(ctx: Any, antes: set[str] | None, registro: dict[str, Any], resultado: dict[str, Any],
                           dormir: Callable[[float], None], limite_s: float = ESPERA_JOB_RACADM_S) -> str:  # fmt: skip
    """Revisão de 06/10/2026, M2: o ``racadm update`` só cria o job; o desfecho vem do job (fila relida), nunca da saída.
    Sem job novo: com a fila lida antes e depois → ``FALHOU`` (nada foi agendado); sem conseguir ler a fila →
    ``POSSIVELMENTE`` (o pacote pode ter sido aceito; nada é reenviado)."""
    componente, prog = registro["componente"], prog_mod.de(resultado)
    esperado = 0.0
    while True:
        fila = fila_jobs.ler_fila(ctx)
        novos = [j for j in fila["jobs"] if antes is not None and j["id"] not in antes] if fila["origem"] else []
        job = novos[-1] if novos else None
        if job is None:
            if esperado >= 120 and antes is not None and fila["origem"]:
                # R620 (iDRAC7 2.65), 19/10/2026: racadm saiu com 0, sem saída, e a fila (lida) ficou vazia: nada foi agendado.
                _tentativa(resultado, componente, "d", "falhou", textos.t("py_automatica.racadm_sem_tarefa_na_fila"))
                return FALHOU
            if esperado >= 120:
                _tentativa(resultado, componente, "d", "falhou", textos.t("py_automatica.o_racadm_aceitou_o_pacote"))  # fmt: skip
                return POSSIVELMENTE
        else:
            registro["tarefa"] = {"id": job["id"], "estado": job["estado"], "percentual": job.get("percentual"),
                                  "mensagens": [job.get("mensagem") or ""], "dell_mensagem": job.get("codigo") or ""}  # fmt: skip
            prog_mod.do_job(prog, componente, job["estado"], job.get("percentual"), fila_jobs.traduzir(job))
            if job["estado"] in fila_jobs.AGENDADOS:
                _tentativa(resultado, componente, "d", "ok", textos.t("py_automatica.job_agendado_no_idrac_para", job['id']))
                return AGENDADO
            if job["estado"] in fila_jobs.FALHAS:
                _tentativa(resultado, componente, "d", "falhou", textos.t("py_automatica.job_falhou_no_idrac", job['id'], fila_jobs.traduzir(job)))
                return FALHOU
            if job["estado"] not in fila_jobs.ATIVOS:
                _tentativa(resultado, componente, "d", "ok", textos.t("py_automatica.job_concluido_no_idrac", job['id'], job['estado']))
                return APLICADO
        if esperado >= limite_s:
            _tentativa(resultado, componente, "d", "falhou", textos.t("py_automatica.job_sem_desfecho_em_min", (job or {}).get('id') or '?', int(limite_s // 60)))  # fmt: skip
            return POSSIVELMENTE
        dormir(fila_jobs.INTERVALO_JOB_S)
        esperado += fila_jobs.INTERVALO_JOB_S


def _texto_racadm(saida: str, erro: str) -> str:
    texto = " ".join((saida or erro or "").split())
    if "ERROR" in texto:  # R620, 16/10/2026: progresso antes do erro ("g Update.... Depending on ... ERROR: RAC923 ...")
        texto = texto[texto.index("ERROR"):]
    return texto


def _racadm(ctx: Any, executor: Any, registro: dict[str, Any], resultado: dict[str, Any],
            dormir: Callable[[float], None] = time.sleep) -> str | None:  # fmt: skip
    _esperar_racadm(ctx, resultado)
    caminho = idrac.racadm(ctx)
    if not caminho:
        _tentativa(resultado, registro["componente"], "d", "indisponivel", textos.t("py_automatica.racadm_nao_esta_em_vendor"))
        return None
    if not registro["arquivo"].lower().endswith(EXTENSOES_RACADM_UPDATE):
        _tentativa(resultado, registro["componente"], "d", "indisponivel", textos.t("py_automatica.formato_nao_aceito_pelo_racadm"))
        return None
    prog_mod.de(resultado).etapa(registro["componente"], prog_mod.ENVIANDO, textos.t("py_automatica.racadm_update_enviando_o_pacote"))
    antes = _ids_na_fila(ctx)
    rc, saida, erro = executor.atualizar_racadm(caminho, registro["arquivo"])
    texto = _texto_racadm(saida, erro)
    if "RAC989" in texto and registro["arquivo"].lower().endswith(".exe"):
        # Arquivo recusado (nada gravado): a imagem nativa do iDRAC dentro do mesmo .EXE (R620, iDRAC7 1.40.40).
        imagem = imagem_nativa_racadm(registro["arquivo"])
        if imagem:
            _tentativa(resultado, registro["componente"], "d", "aviso", textos.t("py_automatica.racadm_tentando_imagem_nativa", texto[-200:]))
            rc, saida, erro = executor.atualizar_racadm(caminho, imagem)
            texto = _texto_racadm(saida, erro)
    if rc == -1 and erro == MOTIVO_TRAVA:
        _tentativa(resultado, registro["componente"], "d", "falhou", erro)
        return TRAVA
    # O racadm pode sair com 0 e escrever ERROR (MOCKUP(rodada final): conferir a saída real).
    if rc != 0 or "ERROR" in texto.upper():
        _tentativa(resultado, registro["componente"], "d", "falhou", textos.t("py_automatica.racadm_update_terminou_com", rc, texto[-300:] or textos.t("py_automatica.sem_saida")))
        return FALHOU
    _tentativa(resultado, registro["componente"], "d", "ok", textos.t("py_automatica.aceito_pelo_racadm", texto[-200:] or textos.t("py_automatica.sem_saida")))
    return _acompanhar_job_racadm(ctx, antes, registro, resultado, dormir)


def _dup(executor: Any, registro: dict[str, Any], arquivo: str, resultado: dict[str, Any], letra: str = "e") -> str:
    componente = registro["componente"]
    if registro.get("checado") and arquivo == registro["arquivo"]:
        rc, checagem = 0, {"aplicavel": True}  # pacote manual: -c já rodou na análise (depois do SHA-256)
    else:
        prog_mod.de(resultado).etapa(componente, prog_mod.VALIDANDO, textos.t("py_automatica.checagem_do_pacote_c_sem"))
        rc, saida, erro = executor.checar_dup(arquivo)
        checagem = manual.interpretar_dup(rc)
        if rc == -1 and not saida:
            _tentativa(resultado, componente, letra, "falhou", textos.t("py_automatica.checagem_c_nao_executou", erro))
            return FALHOU
    if not checagem["aplicavel"]:
        # O próprio fabricante disse que não se aplica (ou é igual/mais antigo): nunca forçar, nenhum outro caminho.
        _tentativa(resultado, componente, letra, "recusado", textos.t("py_automatica.checagem_do_pacote_c_codigo", checagem['texto'], rc))
        return RECUSADO
    prog_mod.de(resultado).etapa(componente, prog_mod.INSTALANDO, textos.t("py_automatica.o_pacote_linux_esta_gravando"), gravando=True)
    rc, saida, erro = executor.aplicar_dup(arquivo)
    if rc == -1 and erro == MOTIVO_TRAVA:
        _tentativa(resultado, componente, letra, "falhou", erro)
        return TRAVA
    codigo = manual.interpretar_dup(rc)
    registro["codigo_dup"] = {k: codigo[k] for k in ("codigo", "nome", "texto")}
    if not codigo["aplicavel"]:
        _tentativa(resultado, componente, letra, "falhou", f"-q terminou com {rc} ({codigo['nome']}): {codigo['texto']}")
        return FALHOU
    _tentativa(resultado, componente, letra, "ok", f"-q terminou com {rc} ({codigo['nome']}): {codigo['texto']}")
    return AGENDADO if codigo["categoria"] == "reinicio" else APLICADO


def outro_formato(registro: dict[str, Any], catalogo: dict[str, Any] | None, system_id: str | None) -> dict[str, Any] | None:
    """Pacote ``.BIN`` oficial do mesmo componente e da mesma versão no catálogo (caminho f), para este modelo.

    Procura no catálogo em uso e nos ``bin_linux`` (catálogo Linux da Dell). Mesmo componente = mesmo release ID do
    ``.EXE`` (a Dell publica os dois formatos com o mesmo, ex.: BIOS 2.29.2 do R640 = JJ09G) ou mesmo ``componentID``
    (BIOS e iDRAC lidos sem Redfish não têm ``componentID``)."""
    release = registro.get("release") or (registro.get("pacote") or {}).get("release")
    if not catalogo or not (registro.get("component_id") or release) or not registro.get("disponivel") or not system_id:
        return None  # revisão de 06/10/2026, B1: sem o systemID deste servidor, nenhum pacote alternativo (modelo não conferido)
    base = (catalogo.get("base") or "downloads.dell.com").strip("/")
    for comp in (catalogo.get("componentes") or []) + (catalogo.get("bin_linux") or []):
        if not comp.get("caminho", "").lower().endswith(".bin") or comp.get("versao") != registro["disponivel"]:
            continue
        if not any(s["system_id"] == system_id for s in comp.get("sistemas") or []):
            continue
        if not ((release and comp.get("pacote") == release) or (registro.get("component_id") and any(
                str(d.get("component_id")) == str(registro["component_id"]) for d in comp.get("dispositivos") or []))):
            continue
        sha = comp["hash"] if comp.get("algoritmo_hash") == "SHA256" else ""
        if not sha and not comp.get("md5"):
            continue
        return {"url": f"https://{(comp.get('base') or base).strip('/')}/{comp['caminho'].lstrip('/')}", "sha256": sha,
                "md5": comp.get("md5") or "", "tamanho": comp.get("tamanho") or 0}  # fmt: skip
    return None


def aplicar_item(ctx: Any, executor: Any, raiz: Path, bmc: dict[str, Any], registro: dict[str, Any], resultado: dict[str, Any],
                 dormir: Callable[[float], None], catalogo: dict[str, Any] | None, system_id: str | None,
                 monitor: Any = None, avisar: Any = None) -> str:  # fmt: skip
    """Um item, caminho por caminho, até um funcionar. Nunca levanta exceção (o chamador também protege)."""
    componente = registro["componente"]
    motivo = hash_ainda_confere(registro["arquivo"], *registro["hash_conferido"])
    if motivo:
        _tentativa(resultado, componente, "g", "falhou", motivo)
        return FALHOU
    eh_bin = registro["arquivo"].lower().endswith(".bin")
    if eh_bin:
        _tentativa(resultado, componente, "a", "indisponivel", textos.t("py_automatica.pacote_linux_bin_aplicado_pelo"))
    elif _admin(bmc) and bmc.get("multipart") and not (registro.get("legado") and bmc.get("simple_update")):
        desfecho = _redfish(ctx, bmc, registro, resultado, dormir)
        if desfecho in (APLICADO, AGENDADO, POSSIVELMENTE, TRAVA, RECUSADO, ENCERRADO):
            return FALHOU if desfecho == ENCERRADO else desfecho
    elif _admin(bmc) and bmc.get("simple_update"):
        desfecho = _redfish_legado(ctx, bmc, registro, resultado, dormir)
        if desfecho in (APLICADO, AGENDADO, POSSIVELMENTE, TRAVA, RECUSADO, ENCERRADO):
            return FALHOU if desfecho == ENCERRADO else desfecho
    else:
        _tentativa(resultado, componente, "a", "indisponivel", motivo_metodo(bmc)["texto"] or textos.t("py_automatica.sem_redfish_de_administrador"))
    desfecho = None if eh_bin else _racadm(ctx, executor, registro, resultado, dormir)
    if desfecho in (APLICADO, AGENDADO, TRAVA, POSSIVELMENTE):
        return desfecho
    if eh_bin:
        return _dup(executor, registro, registro["arquivo"], resultado)
    _tentativa(resultado, componente, "e", "indisponivel", textos.t("py_automatica.o_pacote_baixado_nao_e"))
    alternativo = outro_formato(registro, catalogo, system_id)
    if alternativo is None:
        _tentativa(resultado, componente, "f", "indisponivel", textos.t("py_automatica.o_catalogo_nao_traz_outro"))
    else:
        item = {"id": registro["id"], "componente": componente, "pacote": alternativo}
        arquivo, detalhe = _baixar_e_conferir(executor, item, raiz / PASTA_PACOTES, monitor, avisar)
        if arquivo is None:
            _tentativa(resultado, componente, "f", "falhou", f"{alternativo['url'].rsplit('/', 1)[-1]}: {detalhe}")
        else:
            _tentativa(resultado, componente, "f", "ok", f"{arquivo.name}: {detalhe}")
            registro["arquivo_alternativo"] = str(arquivo)
            return _dup(executor, registro, str(arquivo), resultado)
    _tentativa(resultado, componente, "g", "falhou", explicar_falta(ctx, bmc, resultado, componente))
    return FALHOU


def explicar_falta(ctx: Any, bmc: dict[str, Any], resultado: dict[str, Any] | None = None, componente: str = "") -> str:
    """Caminho g: exatamente o que faltou e o que o técnico pode fazer. A recusa real do racadm local (caminho d) vem
    primeiro: no R620 (16/10/2026) a causa era o RAC923 e o texto só falava do acesso pela rede."""
    faltas = [textos.t("py_automatica.o_racadm_local_recusou", t["detalhe"]) for t in (resultado or {}).get("tentativas") or []
              if t.get("caminho") == "d" and t.get("situacao") == "falhou" and t.get("componente") == componente][-1:]  # fmt: skip
    texto = motivo_metodo(bmc)["texto"]
    if texto or not _admin(bmc):
        faltas.append(texto or textos.t("py_automatica.sem_acesso_de_administrador_ao"))
    if not getattr(getattr(ctx, "ipmi", None), "disponivel", False):
        faltas.append(textos.t("py_automatica.sem_ipmi_local_dev_ipmi0"))
    if not idrac.racadm(ctx):
        faltas.append(textos.t("py_automatica.sem_o_racadm_em_vendor"))  # fmt: skip
    pagina = manual.url_drivers(getattr(ctx, "fabricante", None), getattr(ctx, "modelo", None), None)  # sem service tag: vai ao relatório
    faltas.append(textos.t("py_automatica.ou_baixe_o_pacote_linux", pagina["url"] or pagina["explicacao"]))
    return textos.t("py_automatica.nenhum_caminho_funcionou_o_que") + "; ".join(faltas) + "."


# --- Execução do plano confirmado -------------------------------------------------------------------------------------


def _estado(ctx: Any, resultado: dict[str, Any], enviados: list[dict[str, Any]], pendentes: list[dict[str, Any]], situacao: str,
            modo: str) -> dict[str, Any]:  # fmt: skip
    return {"versao": 1, "automatico": True, "modo": modo, "situacao": situacao, "metodo": resultado.get("metodo") or "catalogo",
            "criado": datetime.now().isoformat(timespec="seconds"),
            "confirmado": resultado.get("confirmado") or datetime.now().isoformat(timespec="seconds"),
            "equipamento": {"fabricante": getattr(ctx, "fabricante", None), "modelo": getattr(ctx, "modelo", None),
                            "sku": getattr(ctx, "sku", None)},
            "system_id": resultado.get("system_id"),
            "enviados": list(enviados), "pendentes": list(pendentes),
            "riscos_assumidos": resultado.get("riscos_assumidos") or {"assumidos": False, "aceitos": [], "checagens": []},
            "parcial": {k: resultado.get(k) for k in ("passos", "tentativas", "itens_concluidos")}}  # fmt: skip


def formatos_exportacao(anunciados: list[str] | None) -> list[str]:
    """Ordem de tentativa do formato da exportação (CLAUDE.md 1.1, detectar primeiro): o que o iDRAC anuncia; sem anúncio,
    JSON e, se o iDRAC recusar o pedido (400), XML — o padrão do script oficial da Dell em todas as gerações. Bancada de
    19/10/2026: o iDRAC7 2.65.65.65 do R620 respondeu 400 ao JSON em todas as tentativas (motivo não gravado no log)."""
    if anunciados:
        return [f for f in ("JSON", "XML") if f in anunciados] or anunciados[:1]
    return ["JSON", "XML"]


def mensagem_redfish(corpo: Any) -> str:
    """Primeira mensagem de erro de uma resposta Redfish (``error.@Message.ExtendedInfo`` ou ``error.message``)."""
    erro = corpo.get("error") if isinstance(corpo, dict) else None
    if not isinstance(erro, dict):
        return ""
    for info in erro.get("@Message.ExtendedInfo") or []:
        if isinstance(info, dict) and isinstance(info.get("Message"), str):
            return info["Message"][:300]
    return str(erro.get("message") or "")[:300]


def _exportar(cliente: Any, bmc: dict[str, Any], raiz: Path, dormir: Callable[[float], None], resultado: dict[str, Any]) -> bool:
    """Exporta a configuração do iDRAC/BIOS antes de instalar (CLAUDE.md 11.7). ``False`` só se a trava recusou."""
    if not bmc.get("exportacao"):
        _passo(resultado, "exportar", textos.t("py_automatica.exportacao_da_configuracao_nao_anunciada"), "aviso")
        return True
    for formato in formatos_exportacao(bmc.get("exportacao_formatos")):
        corpo = {"ExportFormat": formato, "ShareParameters": {"Target": ["ALL"]}}  # payload do script oficial da Dell (12ª–16ª)
        resposta = cliente.escrever(EXPORTAR_CONFIGURACAO, bmc["exportacao"], corpo)
        if resposta.erro_tipo == "trava":
            _passo(resultado, "exportar", resposta.erro, "bloqueado")
            return False
        if resposta.ok and resposta.local:
            break
        motivo = " ".join(filter(None, [str(resposta.erro or resposta.status), mensagem_redfish(resposta.corpo), f"({formato})"]))
        _passo(resultado, "exportar", textos.t("py_automatica.exportacao_nao_concluida_exporte_pela", motivo), "aviso")
        if resposta.status != 400:  # só "pedido recusado" (nada foi feito) justifica tentar o outro formato
            return True
    else:
        return True
    tarefa = _acompanhar_tarefa(cliente, resposta.local, dormir)
    backup = raiz / PASTA_BACKUP / f"configuracao_{datetime.now():%Y-%m-%d_%H%M%S}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(tarefa, ensure_ascii=False, indent=2), encoding="utf-8")
    # MOCKUP(rodada final): só o resumo da tarefa fica no pendrive (o arquivo da configuração depende do destino do iDRAC).
    _passo(resultado, "exportar", textos.t("py_automatica.exportacao_pedida_ao_idrac_resumo", textos.t("py_automatica.concluida") if not tarefa.get('falhou') else textos.t("py_automatica.nao_confirmada"), backup), "aviso")  # fmt: skip
    return True


ETAPA_FINAL = {APLICADO: prog_mod.CONCLUIDO, AGENDADO: prog_mod.AGENDADO, POSSIVELMENTE: prog_mod.AGENDADO,
               FALHOU: prog_mod.FALHOU, RECUSADO: prog_mod.FALHOU, TRAVA: prog_mod.FALHOU}  # fmt: skip


def _marcar_desfecho(resultado: dict[str, Any], item: dict[str, Any], desfecho: str) -> None:
    """Etapa final do componente na tela (21.1), com o motivo quando falha."""
    detalhe = {APLICADO: "aplicado", AGENDADO: textos.t("py_automatica.o_idrac_aplica_no_proximo"),
               POSSIVELMENTE: textos.t("py_automatica.aceito_pelo_idrac_fim_da_2")}.get(desfecho, "")  # fmt: skip
    if desfecho in (FALHOU, RECUSADO, TRAVA):
        detalhe = " | ".join(f"{t['caminho']}) {t['detalhe']}" for t in resultado.get("tentativas") or []
                             if t["componente"] == item["componente"] and t["situacao"] in ("falhou", "recusado"))  # fmt: skip
        if len(detalhe) > 400:  # a causa real vem primeiro; o "o que faltou" (g), longo, fica inteiro no relatório
            detalhe = detalhe[:399] + "…"
    prog_mod.de(resultado).etapa(item["componente"], ETAPA_FINAL.get(desfecho, prog_mod.FALHOU), detalhe)


def _instalar_em_ordem(ctx: Any, executor: Any, raiz: Path, bmc: dict[str, Any], resultado: dict[str, Any], fila: list[dict[str, Any]],
                       dormir: Callable[[float], None], modo: str, catalogo: dict[str, Any] | None, monitor: Any,
                       avisar: Any) -> str:  # fmt: skip
    """Caminhos a–g, um componente por vez, na ordem do plano (CLAUDE.md 21.3):

    - itens que dependem do reinício ficam AGENDADOS juntos e o servidor reinicia UMA vez no fim;
    - o firmware do iDRAC vai sozinho: aplicado na hora → espera o iDRAC voltar pelo Redfish; agendado → reinicia já;
    - "Parar depois do componente atual" (21.2): não começa o próximo; grava o estado ("parado").

    Devolve ``"reiniciar"`` (estado em ``resultado["estado"]``) ou ``"fim"`` (resultado preenchido)."""
    prog = prog_mod.de(resultado)
    concluidos = resultado.setdefault("itens_concluidos", [])
    agendados: list[dict[str, Any]] = []
    from msscan.atualizacao.aviso import sozinho

    for posicao, item in enumerate(fila):
        if prog.parar_pedido and posicao > 0:
            return _parar(ctx, raiz, resultado, agendados, fila[posicao:], modo)
        if agendados and sozinho(item):
            # Reserva: o iDRAC vai primeiro (26/10/2026); se algo já estiver agendado antes dele (ordem recebida de fora), é
            # aplicado num reinício e conferido na volta; só então o iDRAC começa.
            _passo(resultado, textos.t("py_automatica.reinicio"), textos.t("py_automatica.e_instalado_sozinho_antes_o", item['componente'], len(agendados)))  # fmt: skip
            for seguinte in fila[posicao:]:
                prog.etapa(seguinte["componente"], prog_mod.AGUARDANDO, textos.t("py_automatica.depois_do_reinicio_retomada_automatica"))
            return _agendar_reinicio(ctx, bmc, resultado, agendados, fila[posicao:])
        try:
            caminho = salvar_estado(raiz, _estado(ctx, resultado, agendados + [item], fila[posicao + 1:], "enviando", modo))
        except OSError as erro:
            resultado["situacao"], resultado["mensagem"] = "bloqueada", textos.t("py_automatica.estado_da_atualizacao_nao_gravado", erro)
            _passo(resultado, "estado", resultado["mensagem"], "bloqueado")
            return "fim"
        prog.etapa(item["componente"], prog_mod.PREPARANDO, textos.t("py_automatica.escolhendo_o_caminho_de_instalacao"))
        desfecho = aplicar_item(ctx, executor, raiz, bmc, item, resultado, dormir, catalogo, resultado.get("system_id"), monitor, avisar)
        item["situacao"] = {RECUSADO: "abortado", TRAVA: "abortado"}.get(desfecho, desfecho)
        _marcar_desfecho(resultado, item, desfecho)
        if desfecho == APLICADO:
            concluidos.append(item)
            if item.get("idrac"):
                _passo(resultado, "iDRAC", textos.t("py_automatica.firmware_do_idrac_aplicado_esperando"))
                if not esperar_idrac_atualizado(ctx, resultado, dormir, item):
                    return _idrac_sem_volta(ctx, raiz, resultado, item, fila[posicao + 1:], modo, caminho)
                bmc = ler_bmc(ctx)  # sessão reaberta depois do reinício do iDRAC
            if agendados:
                salvar_estado(raiz, _estado(ctx, resultado, agendados, fila[posicao + 1:], "enviando", modo))
            else:
                _apagar_estado(caminho)
            continue
        if desfecho == AGENDADO:
            agendados.append(item)
            if item.get("idrac"):  # sozinho: reinicia agora; o resto continua na volta
                return _agendar_reinicio(ctx, bmc, resultado, agendados, fila[posicao + 1:])
            continue
        if desfecho == TRAVA:
            _apagar_estado(caminho)
            resultado["situacao"], resultado["mensagem"] = "bloqueada_trava", MOTIVO_TRAVA
            return "fim"
        if desfecho == POSSIVELMENTE:
            from msscan.atualizacao.aplicacao import MENSAGEM_POSSIVELMENTE_AGENDADO

            resultado["situacao"] = POSSIVELMENTE
            resultado["mensagem"] = MENSAGEM_POSSIVELMENTE_AGENDADO.format(componente=item["componente"], motivo=textos.t("py_automatica.tarefa_sem_resposta"))
            _passo(resultado, "estado", textos.t("py_automatica.retomada_salva_em_o_proximo", caminho))
            return "fim"
        motivos = [t["detalhe"] if desfecho == RECUSADO else f"{t['caminho']}) {t['detalhe']}" for t in resultado.get("tentativas") or []
                   if t["componente"] == item["componente"] and t["situacao"] in (("recusado",) if desfecho == RECUSADO else ("falhou", "recusado"))]  # fmt: skip
        resultado["situacao"] = "bloqueada" if desfecho == RECUSADO else "falhou"
        resultado["mensagem"] = (f"{item['componente']}: " + ("" if desfecho == RECUSADO else textos.t("py_automatica.nao_instalado")) + " | ".join(motivos)
                                 + (textos.t("py_automatica.os_itens_seguintes_nao_foram") if desfecho == RECUSADO
                                    else textos.t("py_automatica.os_itens_seguintes_nao_foram_2")))  # fmt: skip
        for seguinte in fila[posicao + 1:]:
            prog.etapa(seguinte["componente"], prog_mod.PARADO, textos.t("py_automatica.nao_iniciado_um_item_anterior"))
        if agendados:  # os já agendados (plano confirmado) são aplicados no reinício; nada depois do que falhou
            resultado["mensagem"] += (textos.t("py_automatica.item_ns_ja_agendado_s", len(agendados)))  # fmt: skip
            situacao, mensagem = resultado["situacao"], resultado["mensagem"]
            _agendar_reinicio(ctx, bmc, resultado, agendados, [])
            resultado["situacao_item_falho"], resultado["mensagem_item_falho"] = situacao, mensagem
            return "reiniciar"
        _apagar_estado(caminho)
        return "fim"
    if agendados:
        return _agendar_reinicio(ctx, bmc, resultado, agendados, [])
    resultado["situacao"], resultado["mensagem"] = "aplicada", textos.t("py_automatica.todos_os_itens_instalados")
    return "fim"


def _idrac_sem_volta(ctx: Any, raiz: Path, resultado: dict[str, Any], item: dict[str, Any], pendentes: list[dict[str, Any]],
                     modo: str, caminho: Path) -> str:  # fmt: skip
    """09/10/2026: o iDRAC não voltou com a versão nova no limite. Nada segue; o estado fica "parado" com o iDRAC como
    enviado: "Continuar de onde parou" confere a versão dele e retoma a partir do próximo (BIOS), sem reenviar o iDRAC."""
    prog = prog_mod.de(resultado)
    for seguinte in pendentes:
        prog.etapa(seguinte["componente"], prog_mod.PARADO, textos.t("py_automatica.nao_iniciado_esperando_o_idrac"))
    prog.marcar_parado(pendentes[0]["componente"] if pendentes else None)
    resultado["situacao"] = "falhou"
    resultado["mensagem"] = (textos.t("py_automatica.o_firmware_do_idrac_foi", item.get('disponivel'), int(ESPERA_IDRAC_NOVO_S // 60)))  # fmt: skip
    if not pendentes:
        _apagar_estado(caminho)
        resultado["mensagem"] += textos.t("py_automatica.depois_refaca_o_diagnostico_para")
        return "fim"
    try:
        salvar_estado(raiz, _estado(ctx, resultado, [item], pendentes, PARADO, modo))
        resultado["situacao"] = PARADO
        resultado["mensagem"] += (textos.t("py_automatica.depois_use_continuar_de_onde", pendentes[0]['componente']))  # fmt: skip
    except OSError as erro:
        resultado["mensagem"] += textos.t("py_automatica.estado_nao_gravado_depois_refaca", erro)
    _passo(resultado, "iDRAC", resultado["mensagem"], "aviso")
    return "fim"


def _agendar_reinicio(ctx: Any, bmc: dict[str, Any], resultado: dict[str, Any], agendados: list[dict[str, Any]],
                      pendentes: list[dict[str, Any]]) -> str:  # fmt: skip
    if len(agendados) > 1:
        _passo(resultado, textos.t("py_automatica.reinicio"), textos.t("py_automatica.itens_agendados_para_o_mesmo", len(agendados))
               + ", ".join(i["componente"] for i in agendados))  # fmt: skip
    _preparar_reinicio(ctx, bmc, resultado, _estado(ctx, resultado, agendados, pendentes, "agendado", "aplicar"))
    return "reiniciar"


def _parar(ctx: Any, raiz: Path, resultado: dict[str, Any], agendados: list[dict[str, Any]], pendentes: list[dict[str, Any]],
           modo: str) -> str:  # fmt: skip
    """"Parar depois do componente atual" (21.2): nada novo começa; o estado fica gravado para "Continuar de onde parou"."""
    prog = prog_mod.de(resultado)
    prog.marcar_parado(pendentes[0]["componente"] if pendentes else None)
    try:
        salvar_estado(raiz, _estado(ctx, resultado, agendados, pendentes, PARADO, modo))
        gravado = textos.t("py_automatica.estado_gravado_no_pendrive_na")
    except OSError as erro:
        gravado = textos.t("py_automatica.estado_nao_gravado_na_proxima", erro)
    resultado["situacao"] = PARADO
    resultado["mensagem"] = (textos.t("py_automatica.parado_a_pedido_depois_do", len(pendentes), gravado)
                             + (textos.t("py_automatica.item_ns_ja_agendado_s_2", len(agendados)) if agendados else "") + textos.t("py_automatica.seguro_desligar_agora"))  # fmt: skip
    _passo(resultado, "parar", resultado["mensagem"], "aviso")
    return "fim"


PARADO = "parado"


def aplicar_automatico(ctx: Any, executor: Any, raiz: Path, resultado: dict[str, Any], dormir: Callable[[float], None],
                       modo: str = "aplicar", catalogo: dict[str, Any] | None = None, monitor: Any = None,
                       avisar: Any = None, bmc_inicial: dict[str, Any] | None = None) -> None:  # fmt: skip
    """Instala ``resultado["itens"]`` (já baixados e conferidos), sem perguntar nada (CLAUDE.md 18.2, 21.3).

    Ordem (26/10/2026): iDRAC primeiro e sozinho; depois placas de rede, demais componentes e BIOS por último. Lotes sem
    iDRAC podem ir pelo repositório do iDRAC (18.7); senão, caminhos a–g componente por componente, com os itens que dependem
    de reinício agendados juntos para UM reinício (boot pelo pendrive, mudanças temporárias desfeitas, estado salvo; a
    retomada confere cada um e continua sozinha, parando na primeira falha). Ao fim (ou ao parar), tudo o que foi mudado
    temporariamente no iDRAC é desfeito.
    """
    pasta_dados = str(raiz)
    restricao = liberacao.avaliar(executor, resultado["itens"])
    if restricao["bloqueios"]:
        # Liberação restrita (06/10/2026): conferida de novo aqui, antes de qualquer mudança no iDRAC — catálogo, pacote
        # manual e retomada passam por este ponto.
        resultado["situacao"], resultado["mensagem"] = "bloqueada", " ".join(restricao["bloqueios"])
        _passo(resultado, textos.t("py_automatica.liberacao"), resultado["mensagem"], "bloqueado")
        return
    registro = temp.Temporarias(pasta_dados)
    reiniciar = False
    prog = prog_mod.de(resultado)
    try:
        so_bin = all(str(i.get("arquivo") or "").lower().endswith(".bin") for i in resultado["itens"])
        for item in resultado["itens"]:
            if item["componente"] not in prog.itens:
                prog.etapa(item["componente"], prog_mod.AGUARDANDO, textos.t("py_automatica.na_fila"))
        primeiro = resultado["itens"][0]["componente"] if resultado["itens"] else textos.t("py_automatica.acesso_ao_idrac")
        prog.etapa(primeiro, prog_mod.PREPARANDO, textos.t("py_automatica.acesso_ao_idrac_e_exportacao"))
        prog.esperando(textos.t("py_automatica.o_acesso_ao_idrac_credenciais"),
                       textos.t("py_automatica.a_caixa_acesso_ao_idrac"))  # fmt: skip
        if bmc_inicial is not None and _admin(bmc_inicial):
            bmc = bmc_inicial  # já lido por quem chamou (pacote manual), com administrador validado
            _tentativa(resultado, None, "a", "ok", textos.t("py_automatica.credenciais_de_administrador_informadas_pelo"))
        else:
            bmc = ler_bmc(ctx) if so_bin else garantir_acesso(ctx, executor, pasta_dados, registro, resultado, dormir)
        if _admin(bmc):
            prog.resposta_idrac()
        if not _aplicados_conferidos(ctx, resultado):
            return
        if _admin(bmc) and not resultado.get("retomada_de") and not _exportar(ctx.bmc.cliente, bmc, raiz, dormir, resultado):
            resultado["situacao"], resultado["mensagem"] = "bloqueada_trava", MOTIVO_TRAVA
            return
        fila = list(resultado["itens"])
        from msscan.atualizacao.aviso import sozinho

        # Repositório só para lotes sem iDRAC: ele vai sozinho, antes dos outros (26/10/2026).
        if not so_bin and resultado.get("metodo") != "manual" and modo == "aplicar" and not any(sozinho(i) for i in fila):
            desfecho_repo = _por_repositorio(ctx, executor, raiz, bmc, resultado, dormir, registro, fila)
            if desfecho_repo is not None:
                reiniciar = desfecho_repo == "reiniciar"
                return
        reiniciar = _instalar_em_ordem(ctx, executor, raiz, bmc, resultado, fila, dormir, modo, catalogo, monitor, avisar) == "reiniciar"
    finally:
        resultado.pop("inventario_atual", None)
        prog.esperando(textos.t("py_automatica.desfazer_as_mudancas_temporarias_no"))
        resultado["temporarias"] = temp.desfazer_tudo(ctx, executor, pasta_dados, registro, dormir)
        _passo(resultado, textos.t("py_automatica.mudancas_temporarias"), resumo_temporarias(resultado["temporarias"]),
               "ok" if all(m["situacao"] == temp.DESFEITA for m in resultado["temporarias"]) else "aviso")  # fmt: skip
        if reiniciar:
            _reiniciar(ctx, executor, raiz, resultado, dormir)
            if resultado.get("situacao_item_falho"):
                resultado.pop("situacao_item_falho")
                resultado["mensagem"] = resultado.pop("mensagem_item_falho") + " " + resultado["mensagem"]
        prog.esperando("")


def _aplicados_conferidos(ctx: Any, resultado: dict[str, Any]) -> bool:
    """09/10/2026: "Continuar de onde parou" depois de o iDRAC demorar a voltar — antes de enviar o próximo, confere pelo
    Redfish (acesso já garantido, sessão nova) que o que foi aplicado está na versão nova. Senão, nada é enviado e o plano
    continua parado (o estado no pendrive fica como está)."""
    from msscan.atualizacao.inventario import inventario_redfish
    from msscan.atualizacao.retomada import conferir

    aplicados = resultado.pop("conferir_antes", None)
    if not aplicados:
        return True
    inventario = inventario_redfish(ctx)
    cliente = getattr(getattr(ctx, "bmc", None), "cliente", None)
    if not inventario and cliente is not None and cliente.reconectar():  # a sessão morreu no reinício do iDRAC
        inventario = inventario_redfish(ctx)
    falha = falha_na_volta(conferir({"enviados": aplicados}, inventario, "Redfish (FirmwareInventory)"), prefixo=False)
    if not falha:
        _passo(resultado, textos.t("py_automatica.conferencia"), textos.t("py_automatica.versao_nova_confirmada_pelo_redfish") + ", ".join(str(i.get("componente")) for i in aplicados))
        return True
    resultado["situacao"] = PARADO
    resultado["mensagem"] = (textos.t("py_automatica.ainda_nao_da_para_continuar", falha))  # fmt: skip
    _passo(resultado, textos.t("py_automatica.conferencia"), resultado["mensagem"], "bloqueado")
    return False


def _preparar_reinicio(ctx: Any, bmc: dict[str, Any], resultado: dict[str, Any], estado: dict[str, Any]) -> None:
    """Boot único pelo USB (ainda com a sessão do iDRAC) e o estado da retomada; o reinício vem depois de desfazer."""
    if _admin(bmc) and bmc.get("sistema") and (bmc.get("boot_usb") or bmc.get("boot_uefi_usb")):
        corpo = CORPO_BOOT_USB if bmc.get("boot_usb") else corpo_boot_uefi(bmc["boot_uefi_usb"])
        boot = ctx.bmc.cliente.escrever(BOOT_UNICO_USB, bmc["sistema"], corpo)
        _passo(resultado, textos.t("py_automatica.boot_unico"), textos.t("py_automatica.no_proximo_boot", 'USB' if bmc.get('boot_usb') else textos.t("py_automatica.pendrive_pelo_caminho_uefi"), 'ok' if boot.ok else boot.erro or boot.status) + ("" if boot.ok else textos.t("py_automatica.se_o_servidor_iniciar_pelo")), "ok" if boot.ok else "aviso")  # fmt: skip
        usb = boot.ok
    else:
        usb = False
        _passo(resultado, textos.t("py_automatica.boot_unico"), textos.t("py_automatica.o_idrac_nao_esta_acessivel"), "aviso")  # fmt: skip
    resultado["estado"] = estado
    resultado["usb_no_proximo_boot"] = usb


POUCOS_PARA_DIRETO = 2
"""22.2: com até 2 componentes, o envio direto (MultipartUpload) vem primeiro: um envio por pacote, sem os dois jobs do
repositório (comparar + instalar) e sem depender de o iDRAC alcançar este servidor. O repositório fica para lotes maiores."""
PING = ["ping", "-c", "2", "-W", "2"]


def _rede_local_valida(ip: str | None) -> bool:
    """IP da rede local do cliente (privado), nunca loopback nem o link-local do pass-through."""
    import ipaddress

    try:
        endereco = ipaddress.ip_address(str(ip))
    except ValueError:
        return False
    return endereco.is_private and not endereco.is_loopback and not endereco.is_link_local


def _dns_do_idrac(cliente: Any) -> list[str] | None:
    """Servidores DNS do iDRAC (Redfish ``EthernetInterfaces``). ``None`` se não deu para ler; ``[]`` = sem DNS (comum
    com IP fixo posto pelo ipmitool, que não configura DNS): o iDRAC não acha downloads.dell.com.
    MOCKUP(rodada final): ``NameServers``/``StaticNameServers`` reais do iDRAC9 com IP fixo."""
    try:
        base = "/redfish/v1/Managers/iDRAC.Embedded.1/EthernetInterfaces"
        servidores: list[str] = []
        for membro in membros(cliente.get(base)):
            dados = cliente.get(membro)
            servidores += [str(s) for s in (dados.get("NameServers") or []) + (dados.get("StaticNameServers") or [])
                           if str(s) not in ("", "0.0.0.0", "::")]  # fmt: skip
        return servidores
    except (FalhaColeta, ErroParser, AttributeError):
        return None


def _por_repositorio(ctx: Any, executor: Any, raiz: Path, bmc: dict[str, Any], resultado: dict[str, Any],
                     dormir: Callable[[float], None], registro: temp.Temporarias,
                     itens: list[dict[str, Any]] | None = None) -> str | None:  # fmt: skip
    """Método por repositório (18.7, 22.2). ``None`` = não usado ou falhou ANTES de aplicar (segue para o envio direto e os
    caminhos a–g); ``"reiniciar"`` = jobs agendados; ``"fim"`` = concluído ou parado (resultado preenchido).

    Caminhos, nesta ordem, cada um só com o caminho CONFERIDO antes de criar a tarefa no iDRAC (interface com IP e o iDRAC
    respondendo ao ping a partir deste servidor): rede local (iDRAC na mesma rede do servidor; firewall só para o IP dele),
    pass-through, repositório online da Dell (só com DNS no iDRAC)."""
    from msscan.atualizacao import repositorio as repo
    from msscan.atualizacao.catalogo import caminho_em_uso

    def anotar(situacao: str, detalhe: str, letra: str = "r") -> None:
        _tentativa(resultado, None, letra, situacao, detalhe)

    if not _admin(bmc):
        anotar("indisponivel", textos.t("py_automatica.sem_redfish_de_administrador_no"))
        return None
    cliente = ctx.bmc.cliente
    if not repo.servico_disponivel(cliente):
        anotar("indisponivel", textos.t("py_automatica.o_idrac_nao_anuncia_o"))
        return None
    itens = resultado["itens"] if itens is None else itens
    if not itens:
        return None
    if len(itens) <= POUCOS_PARA_DIRETO and bmc.get("multipart"):
        anotar("indisponivel", textos.t("py_automatica.nao_usado_com_componente_s", len(itens)))  # fmt: skip
        return None
    prog = prog_mod.de(resultado)
    for item in itens:
        prog.etapa(item["componente"], prog_mod.PREPARANDO, textos.t("py_automatica.repositorio_no_pendrive_para_o"))

    def progresso(job: dict[str, Any]) -> None:
        """Cada leitura de job → etapa dos componentes (o job do repositório vale para todos; os por componente, pelo nome)."""
        prog.resposta_idrac()
        nome = str(job.get("nome") or "")
        alvos = [i for i in itens if i["componente"].lower() in nome.lower()] or itens
        estado = str(job.get("estado") or "")
        detalhe = f"{nome or job.get('id')}: {fila_jobs.traduzir(job)}"
        if nome.lower().startswith("repository") and estado in ("Running", "Downloading", "New", "Scheduled"):
            estado = "Downloading"  # o job do repositório só baixa e compara: nada é gravado nele
        for item in alvos:
            prog_mod.do_job(prog, item["componente"], estado, job.get("percentual"), detalhe)

    def reconectar() -> bool:
        return reconectar_idrac(ctx, resultado, dormir)

    criados: dict[str, dict[str, Any]] = {}

    def ao_criar(job: str) -> None:
        """22.4: cada job do repositório é uma mudança temporária até terminar (cancelado no fim ou no próximo boot)."""
        try:
            criados[job] = registro.registrar(temp.TAREFA, textos.t("py_automatica.job_do_repositorio_no_idrac", job), {"job": job})
            registro.marcar(criados[job], temp.FEITA)
        except (OSError, ValueError):
            pass

    def conferir_caminho(alvo: str, via: str) -> str | None:
        """IP deste servidor na rede de ``alvo`` e o iDRAC respondendo ao ping por ela; senão anota e devolve ``None``."""
        ip_host = repo.ip_local_na_rede_de(executor, alvo)
        if not ip_host:
            anotar("indisponivel", textos.t("py_automatica.nenhuma_interface_deste_servidor_tem", via, alvo))
            return None
        if executor.rodar(PING + [alvo], timeout=15)[0] != 0:
            anotar("indisponivel", textos.t("py_automatica.o_idrac_nao_respondeu_ao", via, alvo, ip_host))
            return None
        return ip_host

    def servir(ip_host: str, alvo: str, via: str, origem: str | None) -> dict[str, Any] | None:
        try:
            montado = repo.montar(raiz, itens, caminho_em_uso(str(raiz)).read_bytes(), "downloads.dell.com", resultado.get("system_id"))
        except (OSError, ValueError, ErroParser) as erro:
            anotar("falhou", textos.t("py_automatica.repositorio_nao_montado_no_pendrive", erro))
            return None
        anotar("ok", textos.t("py_automatica.repositorio_montado_em_pacote_s", montado['pasta'], len(montado['pacotes'])))
        ativo = executor.rodar(["nft", "list", "table", "inet", "msscan_quiosque"], timeout=10)[0] == 0
        firewall = repo.FirewallRepositorio(executor, ativo, origem)
        motivo = firewall.abrir(repo.PORTA)
        if motivo:
            anotar("falhou", motivo)
            return None
        try:
            with repo.ServidorRepositorio(montado["pasta"], ip_host, repo.PORTA) as servidor:
                anotar("ok", textos.t("py_automatica.repositorio_servido_em_http_pela", ip_host, servidor.porta, via)
                       + (textos.t("py_automatica.firewall_porta_so_para_so", repo.PORTA, origem) if origem else ""))  # fmt: skip
                prog.esperando(textos.t("py_automatica.o_idrac_buscar_o_repositorio", via),
                               textos.t("py_automatica.se_o_idrac_nao_buscar"))  # fmt: skip

                def caminho_ok() -> str:
                    resultado["repositorio_pedidos"] = [repo.texto_pedido(p) for p in servidor.pedidos[-50:]]
                    if repo.ip_local_na_rede_de(executor, alvo) != ip_host:
                        return textos.t("py_automatica.a_interface_com_o_ip", ip_host, via)
                    return ""

                desfecho = repo.instalar(cliente, ip_host, itens, anotar, dormir, progresso, pedidos=lambda: len(servidor.pedidos),
                                         reconectar=reconectar, caminho_ok=caminho_ok, ao_criar=ao_criar, via=via)  # fmt: skip
                caminho_ok()
        except OSError as erro:
            anotar("falhou", textos.t("py_automatica.servidor_do_repositorio_nao_abriu", ip_host, erro))
            return None
        finally:
            firewall.fechar(repo.PORTA)
        if desfecho["desfecho"] == repo.FALHOU_ANTES:
            prog.avisar_agora(textos.t("py_automatica.repositorio_pela_seguindo_para_o", via, desfecho['mensagem']))
        return desfecho

    desfecho: dict[str, Any] | None = None
    # 1. Rede local: o iDRAC na mesma rede do servidor (22.2) — preferido ao pass-through (nada muda no iDRAC).
    ip_idrac = str(getattr(getattr(ctx, "redfish", None), "endereco", "") or "")
    if _rede_local_valida(ip_idrac) and _rede_local_valida(repo.ip_local_na_rede_de(executor, ip_idrac)):
        ip_host = conferir_caminho(ip_idrac, textos.t("py_automatica.rede_local"))
        if ip_host:
            desfecho = servir(ip_host, ip_idrac, textos.t("py_automatica.rede_local"), ip_idrac)
    else:
        anotar("indisponivel", textos.t("py_automatica.rede_local_o_idrac_nao"))
    # 2. Pass-through (rede interna servidor–iDRAC).
    if desfecho is None or desfecho["desfecho"] == repo.FALHOU_ANTES:
        pt = idrac.ler_passthrough(ctx)
        if pt.get("lido") and not (pt.get("ativo") and pt.get("modo") == "usb-p2p"):
            _ligar_passthrough(ctx, executor, str(raiz), registro, resultado, dormir)
            pt = idrac.ler_passthrough(ctx)
        ip_host = conferir_caminho(pt["ip_usb"], textos.t("py_automatica.rede_interna_do_pass_through")) if pt.get("ativo") and pt.get("ip_usb") else None
        if not pt.get("ativo"):
            anotar("indisponivel", textos.t("py_automatica.pass_through_nao_ligado") + (pt.get("motivo") or textos.t("py_automatica.o_idrac_nao_ligou")) + ")")
        if ip_host:
            desfecho = servir(ip_host, pt["ip_usb"], textos.t("py_automatica.rede_interna_do_pass_through"), None)
    # 3. Repositório online da Dell (só com DNS no iDRAC).
    if desfecho is None or desfecho["desfecho"] == repo.FALHOU_ANTES:
        dns = _dns_do_idrac(cliente)
        if dns == []:
            anotar("indisponivel", textos.t("py_automatica.repositorio_online_o_idrac_esta"), "o")  # fmt: skip
        else:
            online = repo.instalar(cliente, "", itens, lambda s, d: anotar(s, d, "o"), dormir, progresso, online=True,
                                   reconectar=reconectar, ao_criar=ao_criar)  # fmt: skip
            if online["desfecho"] != repo.FALHOU_ANTES:
                desfecho = online
    for job, mudanca in criados.items():
        if not _job_ativo(cliente, job):
            registro.descartar(mudanca)  # terminou (ou foi cancelado): nada a desfazer
    if desfecho is None or desfecho["desfecho"] == repo.FALHOU_ANTES:
        return None
    resultado["repositorio"] = {k: desfecho[k] for k in ("lista", "jobs", "mensagem")}
    if desfecho["desfecho"] == repo.TRAVA:
        resultado["situacao"], resultado["mensagem"] = "bloqueada_trava", MOTIVO_TRAVA
        return "fim"
    if desfecho["desfecho"] == repo.FALHOU_DEPOIS:
        resultado["situacao"] = POSSIVELMENTE
        resultado["mensagem"] = (textos.t("py_automatica.o_idrac_aceitou_instalar_pelo") + desfecho["mensagem"]
                                 + textos.t("py_automatica.antes_de_devolver_o_servidor"))  # fmt: skip
        for item in itens:
            item["situacao"] = POSSIVELMENTE
        return "fim"
    if desfecho["desfecho"] == repo.APLICADO:
        for item in itens:
            item["situacao"] = APLICADO
        resultado.setdefault("itens_concluidos", []).extend(itens)
        resultado["situacao"], resultado["mensagem"] = "aplicada", textos.t("py_automatica.instalado_pelo_idrac_a_partir") + desfecho["mensagem"] + "."
        return "fim"
    for item in itens:
        item["situacao"] = AGENDADO
    estado = _estado(ctx, resultado, itens, [], "agendado", "aplicar")
    estado["jobs"] = [j.get("id") for j in desfecho["jobs"]]
    _preparar_reinicio(ctx, bmc, resultado, estado)
    return "reiniciar"


def _job_ativo(cliente: Any, job: str) -> bool:
    from msscan.atualizacao import repositorio as repo

    try:
        return repo.ler_job(cliente, job)["estado"] not in repo.TERMINAIS
    except (FalhaColeta, ErroParser):
        return False  # fora da fila


def resumo_temporarias(mudancas: list[dict[str, Any]]) -> str:
    if not mudancas:
        return textos.t("py_automatica.nenhuma_mudanca_temporaria_no_idrac")
    return "; ".join(f"{m['descricao']}: {m.get('texto') or temp.TEXTOS[m['situacao']]}" for m in mudancas)


def _reiniciar(ctx: Any, executor: Any, raiz: Path, resultado: dict[str, Any], dormir: Callable[[float], None] = time.sleep) -> None:
    estado = resultado.pop("estado")
    estado["parcial"] = {k: resultado.get(k) for k in ("passos", "tentativas", "itens_concluidos", "temporarias")}
    try:
        caminho = salvar_estado(raiz, estado)
    except OSError as erro:
        resultado["situacao"] = "aguardando_reinicio_manual"
        resultado["mensagem"] = textos.t("py_automatica.firmware_agendado_mas_o_estado", erro)
        return
    _passo(resultado, "estado", textos.t("py_automatica.retomada_salva_em_depois_do", caminho))
    # Revisão de 06/10/2026, M2: nunca reiniciar com um job do iDRAC em andamento (baixando ou gravando); os agendados
    # para o reinício são justamente o que o reinício aplica.
    prog = prog_mod.de(resultado)
    livre, detalhe = fila_jobs.esperar_sem_ativos(ctx, dormir, ao_esperar=lambda jobs: prog.esperando(
        textos.t("py_automatica.as_tarefas_em_andamento_no", jobs), textos.t("py_automatica.maintenance_job_queue_na_pagina")))  # fmt: skip
    if not livre:
        resultado["situacao"] = "aguardando_reinicio_manual"
        resultado["mensagem"] = (textos.t("py_automatica.o_servidor_nao_foi_reiniciado", detalhe))  # fmt: skip
        _passo(resultado, textos.t("py_automatica.reinicio"), resultado["mensagem"], "aviso")
        return
    if detalhe:
        _passo(resultado, textos.t("py_automatica.reinicio"), detalhe, "aviso")
    prog.gravar_log()
    ok, erro = executor.reiniciar_para_aplicar()
    resultado["situacao"] = "aguardando_reinicio" if ok else "aguardando_reinicio_manual"
    resultado["mensagem"] = (textos.t("py_automatica.reiniciando_para_aplicar_o_msscan") if ok
                             else textos.t("py_automatica.reinicie_o_servidor_para_aplicar", erro))  # fmt: skip


# --- Retomada automática ----------------------------------------------------------------------------------------------


def falha_na_volta(conferencia: dict[str, Any] | None, prefixo: bool = True) -> str:
    """Parar na primeira falha (09/10/2026): algum componente agendado antes do reinício não está na versão nova agora →
    o motivo; ``""`` se todos confirmaram (ou nada foi conferido)."""
    from msscan.atualizacao.retomada import APLICADA

    falhos = [i for i in (conferencia or {}).get("itens") or [] if i.get("resultado") != APLICADA]
    if not falhos:
        return ""
    return ((textos.t("py_automatica.a_atualizacao_parou_na_primeira") if prefixo else "") + "; ".join(f"{i.get('componente')} ({i.get('texto')})" for i in falhos)
            + textos.t("py_automatica.os_componentes_seguintes_nao_foram"))  # fmt: skip


def pode_continuar(estado: dict[str, Any] | None, ctx: Any, agora: datetime | None = None,
                   conferencia: dict[str, Any] | None = None) -> str:  # fmt: skip
    """``""`` se o plano confirmado pode continuar sozinho neste boot; senão, o motivo (o técnico confirma de novo).

    ``conferencia``: antes × depois dos agendados (``retomada.conferir``); qualquer um sem a versão nova para tudo."""
    if not estado or not estado.get("automatico"):
        return textos.t("py_automatica.plano_sem_continuacao_automatica")
    if not estado.get("pendentes"):
        return textos.t("py_automatica.nenhum_item_pendente")
    falha = falha_na_volta(conferencia)
    if falha:
        return falha
    if estado.get("situacao") == PARADO:
        return textos.t("py_automatica.parado_a_pedido_do_tecnico")
    if estado.get("situacao") != "agendado":
        return textos.t("py_automatica.o_ultimo_envio_nao_foi")
    equipamento = estado.get("equipamento") or {}
    atual = {"modelo": getattr(ctx, "modelo", None), "sku": getattr(ctx, "sku", None)}
    if any(equipamento.get(k) != atual[k] for k in atual):
        return textos.t("py_automatica.o_estado_salvo_e_de")
    try:
        confirmado = datetime.fromisoformat(str(estado.get("confirmado")))
    except ValueError:
        return textos.t("py_automatica.data_da_confirmacao_ilegivel")
    if (agora or datetime.now()) - confirmado > VALIDADE_RETOMADA:
        return textos.t("py_automatica.a_confirmacao_tem_mais_de")
    return ""


def pode_continuar_parado(estado: dict[str, Any] | None, ctx: Any, agora: datetime | None = None,
                          conferencia: dict[str, Any] | None = None) -> str:  # fmt: skip
    """"Continuar de onde parou" (21.2): mesmas regras da continuação sozinha, mas para um plano parado a pedido."""
    if not estado or estado.get("situacao") != PARADO:
        return textos.t("py_automatica.nada_parado_a_pedido")
    return pode_continuar({**estado, "situacao": "agendado"}, ctx, agora, conferencia)


def continuar(ctx: Any, executor: Any, pasta_dados: str, estado: dict[str, Any], hardware: list[dict[str, str]],
              dormir: Callable[[float], None] = time.sleep, parcial: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """Continua os itens pendentes de um plano já confirmado (sem perguntar). ``hardware``: relido agora (só leitura).

    M5 (revisão de 06/10/2026): para se aparecer condição de hardware que não foi aceita na confirmação (bloqueio novo,
    redundância perdida agora, evento crítico novo); as aceitas (inclusive os riscos assumidos) continuam aceitas."""
    from msscan.atualizacao.aviso import novos_na_volta

    resultado: dict[str, Any] = parcial if parcial is not None else {}
    resultado.update({"modo": estado.get("modo") or "aplicar", "situacao": "", "mensagem": "", "passos": [], "tentativas": [],
                      "itens": [dict(i) for i in estado.get("pendentes") or []], "retomada_de": estado.get("criado"),
                      "confirmado": estado.get("confirmado"), "system_id": estado.get("system_id"),
                      "riscos_assumidos": estado.get("riscos_assumidos"),
                      "anterior": estado.get("parcial") or {}, "inicio": datetime.now().isoformat(timespec="seconds"),
                      "conferir_antes": [i for i in estado.get("enviados") or [] if i.get("situacao") == APLICADO]})  # fmt: skip
    from msscan.atualizacao.aplicacao import componentes_do_plano, encerrar_progresso

    if not isinstance(resultado.get("_acompanhamento"), prog_mod.Progresso):
        resultado["_acompanhamento"] = prog_mod.Progresso(componentes_do_plano(resultado["itens"]))
    try:
        aceitos = (estado.get("riscos_assumidos") or {}).get("aceitos") if "riscos_assumidos" in estado else None
        bloqueios = novos_na_volta(hardware, aceitos)
        if bloqueios:
            resultado["situacao"] = "bloqueada"
            resultado["mensagem"] = (textos.t("py_automatica.retomada_interrompida_o_hardware_mudou") + "; ".join(bloqueios))  # fmt: skip
            _passo(resultado, "hardware", resultado["mensagem"], "bloqueado")
            encerrar_progresso(resultado)
            return resultado
        _passo(resultado, "hardware", textos.t("py_automatica.relido_na_volta_do_reinicio"))
        # 21.4: jobs do reinício ainda rodando no iDRAC → espera; nunca cria tarefa nova por cima deles.
        prog = prog_mod.de(resultado)
        livre, detalhe = fila_jobs.esperar_sem_ativos(ctx, dormir, ao_esperar=lambda jobs: prog.esperando(
            textos.t("py_automatica.os_jobs_do_idrac_terminarem", jobs), textos.t("py_automatica.maintenance_job_queue_na_pagina")))  # fmt: skip
        if not livre:
            resultado["situacao"] = "bloqueada"
            resultado["mensagem"] = (textos.t("py_automatica.retomada_parada_job_do_idrac", detalhe))  # fmt: skip
            _passo(resultado, textos.t("py_automatica.jobs_do_idrac"), resultado["mensagem"], "bloqueado")
            encerrar_progresso(resultado)
            return resultado
        if detalhe:
            _passo(resultado, textos.t("py_automatica.jobs_do_idrac"), detalhe, "aviso")
        if resultado["modo"] != "aplicar":
            resultado["situacao"], resultado["mensagem"] = "simulada", textos.t("py_automatica.retomada_modo_interno_de_teste")
            encerrar_progresso(resultado)
            return resultado
        from msscan.atualizacao.catalogo import catalogo_em_cache

        aplicar_automatico(ctx, executor, Path(pasta_dados), resultado, dormir, "aplicar", catalogo_em_cache(pasta_dados))
    except Exception as erro:  # noqa: BLE001 - nunca derruba o programa
        resultado["situacao"], resultado["mensagem"] = "falhou", textos.t("py_automatica.erro_inesperado_na_retomada", type(erro).__name__, erro)
    resultado["fim"] = datetime.now().isoformat(timespec="seconds")
    encerrar_progresso(resultado)
    return resultado


__all__ = ["CAMINHOS", "aplicar_automatico", "continuar", "explicar_falta", "falha_na_volta", "garantir_acesso", "outro_formato",
           "pode_continuar", "prever_temporarias"]  # fmt: skip

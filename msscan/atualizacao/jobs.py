"""Fila de jobs do iDRAC (CLAUDE.md 21.4): leitura, tradução dos códigos e o que fazer com o que ficou de antes.

Leitura só: Redfish (``/redfish/v1/Managers/iDRAC.Embedded.1/Jobs``, com sessão) ou ``racadm jobqueue view`` local (sem
credenciais). Usos:

- ao iniciar: jobs agendados, em andamento ou com falha de uma sessão anterior aparecem na tela, traduzidos, com o que
  acontece no próximo reinício; instalação nova bloqueada enquanto houver job ativo (nunca duplicar tarefas);
- antes de mexer no iDRAC (pass-through, mudanças temporárias): ``ha_ativo`` — nada que possa reiniciar o iDRAC com um
  job em andamento (21.3);
- RED115 / reinício do iDRAC no meio: ``reinicio_do_idrac`` decide a nova tentativa (uma só).

MOCKUP(rodada final): formato do ``racadm jobqueue view`` (blocos ``[Job ID=...]``, ``Status=``, ``Message=[COD: texto]``,
``Percent Complete=[n]``) pelo guia RACADM do iDRAC9, sem coleta real; os textos das mensagens vêm do próprio iDRAC.
"""

from __future__ import annotations

from msscan import textos

import re
from typing import Any, Callable

from msscan.core.fallback import FalhaColeta
from msscan.parsers import ErroParser
from msscan.parsers.redfish import membros

JOBS = "/redfish/v1/Managers/iDRAC.Embedded.1/Jobs"
ATIVOS = {"Running", "Downloading", "Downloaded", "New", "Scheduling", "Waiting", "ReadyForExecution", "Paused",
          "Starting", "Pending"}  # fmt: skip
"""O iDRAC está trabalhando neles agora (nada que reinicie o iDRAC, nenhuma tarefa nova)."""
AGENDADOS = {"Scheduled"}
"""Esperando o próximo reinício do servidor (o Lifecycle Controller aplica)."""
FALHAS = {"Failed", "CompletedWithErrors", "Cancelled", "Killed", "Failed."}
MAX_JOBS = 60
_CODIGO = re.compile(r"\b([A-Z]{2,4}\d{3,4})\b")

TRADUCOES = {
    "RED115": textos.t("py_jobs.o_idrac_reiniciou_durante_o"),
    "RED014": textos.t("py_jobs.ja_existe_um_job_para"),
}  # fmt: skip
"""Códigos conferidos (mensagem real do iDRAC do usuário: RED115; artigo da Dell sobre RED014). Os demais são traduzidos
pelo sentido da mensagem do próprio iDRAC (``traduzir``), sempre com o código e o texto original ao lado."""


def codigo(job: dict[str, Any]) -> str:
    achado = job.get("codigo") or _CODIGO.search(str(job.get("mensagem") or ""))
    return achado if isinstance(achado, str) else achado.group(1) if achado else ""


def traduzir(job: dict[str, Any]) -> str:
    """O que o job diz, em português (sem inventar: sem tradução conhecida, repete a mensagem do iDRAC)."""
    cod, mensagem = codigo(job), str(job.get("mensagem") or "")
    texto = mensagem.lower()
    if cod in TRADUCOES:
        explicacao = TRADUCOES[cod]
    elif "restart" in texto and "interrupt" in texto:
        explicacao = TRADUCOES["RED115"]
    elif "already" in texto and ("job" in texto or "present" in texto):
        explicacao = TRADUCOES["RED014"]
    elif "scheduled" in texto or job.get("estado") in AGENDADOS:
        explicacao = textos.t("py_jobs.agendado_sera_aplicado_pelo_lifecycle")
    elif "completed successfully" in texto or "successfully" in texto and job.get("estado") == "Completed":
        explicacao = textos.t("py_jobs.concluido_com_sucesso")
    elif any(p in texto for p in ("unable to transfer", "unable to download", "network", "share")):
        explicacao = textos.t("py_jobs.o_idrac_nao_conseguiu_buscar")
    elif job.get("estado") in ATIVOS:
        explicacao = textos.t("py_jobs.em_andamento_no_idrac")
    elif job.get("estado") in FALHAS:
        explicacao = textos.t("py_jobs.falhou_no_idrac")
    else:
        explicacao = textos.t("py_jobs.mensagem_do_idrac")
    return f"{explicacao} ({cod + ': ' if cod else ''}{mensagem or job.get('estado') or 'sem mensagem'})"


def gravando(job: dict[str, Any]) -> bool:
    """Job de firmware que pode estar GRAVANDO agora (nunca apagar nem reiniciar o iDRAC por cima dele). O job do repositório
    (``Repository Update``) só baixa e compara: não grava nada."""
    nome = f"{job.get('nome') or ''} {job.get('tipo') or ''}".lower()
    if "repository" in nome or "export" in nome or "import" in nome:
        return False
    return job.get("estado") in {"Running", "Starting"} and ("firmware" in nome or "update" in nome or not nome.strip())


def reinicio_do_idrac(job: dict[str, Any]) -> bool:
    """Falha causada por um reinício do iDRAC no meio (RED115 e afins): vale UMA nova tentativa."""
    texto = str(job.get("mensagem") or "").lower()
    return codigo(job) == "RED115" or ("restart" in texto and "interrupt" in texto)


def _normalizar(dados: dict[str, Any]) -> dict[str, Any]:
    mensagem = str(dados.get("Message") or "")
    return {"id": str(dados.get("Id") or ""), "nome": str(dados.get("Name") or ""), "tipo": str(dados.get("JobType") or ""),
            "estado": str(dados.get("JobState") or ""), "percentual": dados.get("PercentComplete"), "mensagem": mensagem,
            "codigo": str(dados.get("MessageId") or "").rsplit(".", 1)[-1] or (codigo({"mensagem": mensagem})),
            "inicio": str(dados.get("StartTime") or ""), "fim": str(dados.get("CompletionTime") or "")}  # fmt: skip


def parse_jobqueue(texto: str) -> list[dict[str, Any]]:
    """``racadm jobqueue view`` → jobs. Função pura. MOCKUP(rodada final): formato do guia, sem coleta real."""
    jobs = []
    for bloco in re.split(r"\[Job ID=", texto or "")[1:]:
        ident = bloco.split("]", 1)[0].strip()
        campos = dict(re.findall(r"^\s*([A-Za-z ]+?)=\[?(.*?)\]?\s*$", bloco, re.M))
        mensagem = campos.get("Message", "")
        cod = _CODIGO.match(mensagem)
        percentual = re.search(r"\d+", campos.get("Percent Complete", ""))
        jobs.append({"id": ident, "nome": campos.get("Job Name", ""), "tipo": "", "estado": campos.get("Status", ""),
                     "percentual": int(percentual.group()) if percentual else None,
                     "mensagem": mensagem.split(":", 1)[1].strip() if cod and ":" in mensagem else mensagem,
                     "codigo": cod.group(1) if cod else "", "inicio": campos.get("Start Time", ""), "fim": ""})  # fmt: skip
    if texto and texto.strip() and not jobs and "no job" not in texto.lower() and "job id" in texto.lower():
        raise ErroParser(textos.t("py_jobs.racadm_jobqueue_view_formato_inesperado"))
    return jobs


def ler_fila(ctx: Any) -> dict[str, Any]:
    """``{"origem": "redfish"|"racadm"|"", "jobs": [...], "motivo": ""}``. Só leitura; nunca levanta exceção."""
    from msscan.acoes import idrac

    bmc = getattr(ctx, "bmc", None)
    if bmc is not None and getattr(bmc, "validado", False):
        try:
            cliente = bmc.cliente
            ids = [m.rstrip("/").rsplit("/", 1)[-1] for m in membros(cliente.get(JOBS))][-MAX_JOBS:]
            return {"origem": "redfish", "jobs": [_normalizar(cliente.get(f"{JOBS}/{j}")) for j in ids], "motivo": ""}
        except (FalhaColeta, ErroParser, AttributeError) as erro:
            motivo = textos.t("py_jobs.fila_de_jobs_pelo_redfish", erro)
    else:
        motivo = textos.t("py_jobs.sem_sessao_redfish")
    caminho = idrac.racadm(ctx)
    if caminho:
        rc, saida, erro = ctx.executor.rodar([caminho, "jobqueue", "view"], timeout=60)
        if rc == 0:
            try:
                return {"origem": "racadm", "jobs": parse_jobqueue(saida)[-MAX_JOBS:], "motivo": ""}
            except ErroParser as falha:
                motivo += f"; {falha}"
        else:
            motivo += textos.t("py_jobs.racadm_jobqueue_view_0", (erro or saida or '').strip()[:200] or rc)
    return {"origem": "", "jobs": [], "motivo": motivo + ("" if caminho else textos.t("py_jobs.sem_racadm"))}


def resumir(fila: dict[str, Any]) -> dict[str, Any]:
    """O que mostrar ao iniciar: ativos, agendados e falhas (traduzidos) e o que acontece/bloqueia."""
    jobs = fila.get("jobs") or []
    ativos = [j for j in jobs if j["estado"] in ATIVOS]
    agendados = [j for j in jobs if j["estado"] in AGENDADOS]
    falhas = [j for j in jobs if j["estado"] in FALHAS][-5:]
    linhas = [{**j, "texto": traduzir(j), "grupo": g} for g, lista in (("ativo", ativos), ("agendado", agendados), ("falha", falhas))
              for j in lista]  # fmt: skip
    explicacao = []
    if ativos:
        explicacao.append(textos.t("py_jobs.0_job_s_em_andamento", len(ativos)))  # fmt: skip
    if agendados:
        explicacao.append(textos.t("py_jobs.0_job_s_agendado_s", len(agendados)))  # fmt: skip
    if falhas:
        explicacao.append(textos.t("py_jobs.falhas_anteriores_nada_delas_fica"))
    bloqueio = (textos.t("py_jobs.ha_job_s_do_idrac")) if ativos or agendados else ""  # fmt: skip
    return {"origem": fila.get("origem"), "motivo": fila.get("motivo"), "jobs": linhas, "explicacao": explicacao,
            "bloqueio": bloqueio, "ativos": len(ativos), "agendados": len(agendados)}  # fmt: skip


def ha_ativo(ctx: Any, ignorar: set[str] | frozenset[str] = frozenset()) -> tuple[bool | None, str]:
    """Algum job em andamento agora (fora de ``ignorar``)? ``(None, motivo)`` se não deu para ler a fila."""
    fila = ler_fila(ctx)
    if not fila["origem"]:
        return None, fila["motivo"]
    ativos = [j for j in fila["jobs"] if j["estado"] in ATIVOS and j["id"] not in ignorar]
    return bool(ativos), "; ".join(f"{j['id']} {j['nome']} ({j['estado']})" for j in ativos)


ESPERA_JOB_S = 900.0
"""Até 15 min esperando um job do iDRAC terminar antes de mexer no iDRAC (21.3); depois, a mudança fica adiada."""
INTERVALO_JOB_S = 30.0


def esperar_sem_ativos(ctx: Any, dormir: Callable[[float], None], limite_s: float = ESPERA_JOB_S,
                       ao_esperar: Callable[[str], None] | None = None,
                       ignorar: set[str] | frozenset[str] = frozenset()) -> tuple[bool, str]:  # fmt: skip
    """Antes de qualquer ação que possa reiniciar o iDRAC (21.3): espera os jobs em andamento terminarem.

    ``(True, "")`` livre; ``(True, motivo)`` se a fila não pôde ser lida (sem Redfish nem racadm: quem chama segue, como
    antes da 21.3); ``(False, jobs)`` se ainda há job em andamento depois de ``limite_s`` (a ação fica adiada)."""
    esperado = 0.0
    while True:
        ativo, detalhe = ha_ativo(ctx, ignorar)
        if ativo is None:
            return True, textos.t("py_jobs.fila_de_jobs_nao_lida", detalhe)
        if not ativo:
            return True, ""
        if esperado >= limite_s:
            return False, detalhe
        if ao_esperar is not None:
            ao_esperar(detalhe)
        dormir(INTERVALO_JOB_S)
        esperado += INTERVALO_JOB_S


# --- Job órfão (pedido do usuário, 09/10/2026) -------------------------------------------------------------------------

SEM_PROGRESSO_ORFAO_S = 600.0
"""Job de repositório ou de atualização sem NENHUMA mudança (estado, percentual, mensagem) por mais de 10 minutos."""
TEXTO_ORFAO = textos.t("py_jobs.job_travado_de_uma_sessao")
ESPERA_FORCADA_INICIAL_S = 60.0
"""Depois do ``JID_CLEARALL_FORCE``, o script oficial da Dell espera 60 s antes de consultar o iDRAC de novo."""
ESPERA_FORCADA_S = 600.0
INTERVALO_FORCADA_S = 20.0


def _de_repositorio(job: dict[str, Any]) -> bool:
    return "repository" in f"{job.get('nome') or ''} {job.get('tipo') or ''}".lower()


def _de_atualizacao(job: dict[str, Any]) -> bool:
    texto = f"{job.get('nome') or ''} {job.get('tipo') or ''}".lower()
    return _de_repositorio(job) or "update" in texto or "firmware" in texto


def marcar_orfaos(resumo: dict[str, Any], vistos: dict[str, dict[str, Any]], agora: float, servindo_repositorio: bool,
                  criados: set[str] | frozenset[str] = frozenset(), primeira_leitura: bool = False) -> dict[str, Any]:  # fmt: skip
    """Marca os jobs órfãos do ``resumo`` (``orfao``, ``motivo_orfao``, ``texto_orfao``). Função pura sobre o histórico.

    Órfão = job de repositório ou de atualização, ainda "ativo" no iDRAC, que não foi criado nesta sessão do MSscan e:
    - é de repositório e o MSscan não está servindo nenhum repositório agora (ex.: o do teste de 06/10/2026, sempre em
      "RED110: Downloading the Catalog.xml", procurando um repositório que já não existe); ou
    - já estava na fila na primeira leitura desta sessão (criado antes do boot atual) e é de repositório; ou
    - está sem nenhum progresso há mais de 10 minutos (estado, percentual e mensagem iguais).
    Um job de firmware ativo na primeira leitura NÃO é órfão por isso (o iDRAC pode estar aplicando o que o reinício pediu):
    só pela falta de progresso. ``vistos``: id → primeira vez com o mesmo conteúdo (atualizado aqui)."""
    for job in resumo.get("jobs") or []:
        assinatura = (job.get("estado"), job.get("percentual"), job.get("mensagem"))
        anterior = vistos.get(job["id"])
        if anterior is None or anterior["assinatura"] != assinatura:
            vistos[job["id"]] = anterior = {"assinatura": assinatura, "desde": agora,
                                            "inicial": primeira_leitura if anterior is None else anterior["inicial"]}  # fmt: skip
        motivo = ""
        if job.get("grupo") == "ativo" and job["id"] not in criados and _de_atualizacao(job):
            if _de_repositorio(job) and not servindo_repositorio:
                motivo = textos.t("py_jobs.aponta_para_um_repositorio_que")
            elif _de_repositorio(job) and anterior["inicial"]:
                motivo = textos.t("py_jobs.foi_criado_antes_do_boot")
            elif agora - anterior["desde"] >= SEM_PROGRESSO_ORFAO_S:
                motivo = textos.t("py_jobs.esta_sem_nenhum_progresso_ha")
        job["orfao"] = bool(motivo)
        job["motivo_orfao"] = motivo
        job["texto_orfao"] = TEXTO_ORFAO.format(desde=textos.data_br(job.get("inicio"))) if motivo else ""
    resumo["orfaos"] = sum(1 for j in resumo.get("jobs") or [] if j.get("orfao"))
    if resumo["orfaos"]:
        resumo["bloqueio"] = (textos.t("py_jobs.a_fila_de_tarefas_do"))  # fmt: skip
    return resumo


def limpar_orfao(ctx: Any, job_id: str, forcar: bool, dormir: Callable[[float], None]) -> dict[str, Any]:
    """Limpa um job órfão (09/10/2026). Nunca levanta exceção. Passos (todos em ``passos``, para o relatório e o log):

    1. relê a fila: o job ainda está lá e nenhum firmware está gravando agora;
    2. tenta apagar SÓ esse job (``DeleteJobQueue`` com o ``JobID``);
    3. se o iDRAC recusar: sem ``forcar``, devolve ``precisa_forcar`` (a tela avisa que a fila TODA será apagada e os
       serviços do iDRAC reiniciam por alguns minutos); com ``forcar``, ``JID_CLEARALL_FORCE`` e espera o iDRAC voltar.
    Nunca o forçado com uma atualização de firmware realmente gravando."""
    from datetime import datetime

    from msscan.atualizacao import repositorio as repo
    from msscan.core.executor import CAMINHO_CANCELAR_JOB, CORPO_LIMPAR_FILA_FORCADA, LIMPAR_FILA_FORCADA

    registro: dict[str, Any] = {"job": job_id, "inicio": datetime.now().isoformat(timespec="seconds"), "passos": [],
                                "situacao": "", "mensagem": "", "forcada": False}  # fmt: skip

    def passo(acao: str, detalhe: str, situacao: str = "ok") -> None:
        registro["passos"].append({"acao": acao, "detalhe": detalhe, "situacao": situacao,
                                   "quando": datetime.now().isoformat(timespec="seconds")})  # fmt: skip

    def fim(situacao: str, mensagem: str) -> dict[str, Any]:
        registro["situacao"], registro["mensagem"] = situacao, mensagem
        registro["fim"] = datetime.now().isoformat(timespec="seconds")
        return registro

    if not re.fullmatch(r"JID_[0-9]{6,20}", job_id or ""):
        return fim("falhou", textos.t("py_jobs.identificador_de_job_invalido_nenhuma"))
    bmc = getattr(ctx, "bmc", None)
    if bmc is None or not getattr(bmc, "validado", False) or getattr(bmc, "cliente", None) is None:
        return fim("falhou", textos.t("py_jobs.nao_ha_sessao_com_o"))  # fmt: skip
    fila = ler_fila(ctx)
    if not fila["origem"]:
        return fim("falhou", textos.t("py_jobs.a_fila_de_tarefas_nao", fila['motivo']))  # fmt: skip
    job = next((j for j in fila["jobs"] if j["id"] == job_id), None)
    if job is None or job["estado"] not in ATIVOS | AGENDADOS:
        passo("conferir", textos.t("py_jobs.job_0_ja_nao_esta", job_id, (job or {}).get('estado') or 'fora da fila'))
        return fim("limpo", textos.t("py_jobs.o_job_ja_nao_estava"))
    gravando_agora = [j for j in fila["jobs"] if gravando(j) and j["id"] != job_id]
    if gravando(job):
        return fim("recusado", textos.t("py_jobs.o_job_0_esta_gravando", job_id))  # fmt: skip
    passo("conferir", textos.t("py_jobs.job_0_1_2_nenhum", job_id, job.get('nome'), job['estado'])
          if not gravando_agora else textos.t("py_jobs.job_0_ha_firmware_gravando", job_id, ', '.join(j['id'] for j in gravando_agora)))  # fmt: skip
    apagado, detalhe = repo.cancelar_job(bmc.cliente, job_id)
    passo("apagar o job", detalhe, "ok" if apagado else "aviso")
    if apagado:
        return fim("limpo", textos.t("py_jobs.o_job_travado_0_foi", job_id))
    if not forcar:
        return fim("precisa_forcar", textos.t("py_jobs.o_controlador_de_gerenciamento_recusou"))  # fmt: skip
    if gravando_agora:
        return fim("recusado", textos.t("py_jobs.ha_atualizacao_de_firmware_gravando"))  # fmt: skip
    resposta = bmc.cliente.escrever(LIMPAR_FILA_FORCADA, CAMINHO_CANCELAR_JOB, dict(CORPO_LIMPAR_FILA_FORCADA))
    registro["forcada"] = True
    if resposta.erro_tipo in ("trava", "bloqueado") or (resposta.status is not None and resposta.status >= 400):
        passo("limpar a fila (JID_CLEARALL_FORCE)", str(resposta.erro or resposta.status), "falhou")
        return fim("falhou", textos.t("py_jobs.o_controlador_de_gerenciamento_nao"))  # fmt: skip
    passo("limpar a fila (JID_CLEARALL_FORCE)", textos.t("py_jobs.pedido_aceito_0_servicos_do", resposta.status or 'sem resposta'))
    dormir(ESPERA_FORCADA_INICIAL_S)
    esperado = ESPERA_FORCADA_INICIAL_S
    while esperado <= ESPERA_FORCADA_S:
        if bmc.cliente.reconectar():
            relida = ler_fila(ctx)
            if relida["origem"] and not any(j["id"] == job_id and j["estado"] in ATIVOS for j in relida["jobs"]):
                passo(textos.t("py_jobs.idrac_de_volta"), textos.t("py_jobs.o_idrac_voltou_a_responder", int(esperado), job_id))
                return fim("limpo", textos.t("py_jobs.a_fila_de_tarefas_foi", job_id))  # fmt: skip
        dormir(INTERVALO_FORCADA_S)
        esperado += INTERVALO_FORCADA_S
    passo(textos.t("py_jobs.idrac_de_volta"), textos.t("py_jobs.sem_resposta_em_0_min", int(ESPERA_FORCADA_S // 60)), "falhou")
    return fim("falhou", textos.t("py_jobs.a_limpeza_foi_pedida_mas"))  # fmt: skip


__all__ = ["AGENDADOS", "ATIVOS", "FALHAS", "TRADUCOES", "codigo", "esperar_sem_ativos", "gravando", "ha_ativo", "ler_fila",
           "limpar_orfao", "marcar_orfaos", "parse_jobqueue", "reinicio_do_idrac", "resumir", "traduzir"]  # fmt: skip

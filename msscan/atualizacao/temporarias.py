"""Mudanças TEMPORÁRIAS no iDRAC feitas para viabilizar a atualização, desfeitas sozinhas (CLAUDE.md 18.4).

Cada mudança (usuário administrador temporário, OS to iDRAC Pass-through) é registrada em
``MSSCAN/estado/mudancas_temporarias.json`` ANTES de ser feita (gravação conferida no pendrive; sem o registro, nada é
alterado) e desfeita ao final da atualização, antes de cada reinício e, se o MSscan fechar ou o servidor cair no meio,
no próximo boot pelo pendrive (``desfazer_tudo`` chamado na inicialização). O relatório lista cada mudança e confirma
que foi desfeita (relendo o iDRAC).

Nunca muda a rede do iDRAC (IP, máscara, gateway, porta) nem a senha de usuários existentes: só os tipos abaixo. 22.4: os
jobs "Repository Update" criados pelo MSscan também são registrados (desfazer = cancelar, se ainda não terminaram), para
nunca ficar sobra na Job Queue nem um job travado adiando o desligamento do pass-through para sempre.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from msscan.acoes import idrac
from msscan.acoes.comum import gravar_conferido

ARQUIVO = "estado/mudancas_temporarias.json"
USUARIO, PASSTHROUGH, TAREFA = "usuario_temporario", "passthrough", "job_repositorio"
TIPOS = (USUARIO, PASSTHROUGH, TAREFA)
PENDENTE, FEITA, DESFEITA, NAO_DESFEITA = "pendente", "feita", "desfeita", "nao_desfeita"
"""``pendente``: registrada, ainda não confirmada pelo iDRAC (pode ter entrado ou não: desfazer é idempotente)."""
TEXTOS = {PENDENTE: "registrada (o iDRAC pode não ter aplicado)", FEITA: "ativa", DESFEITA: "DESFEITA (conferido no iDRAC)",
          NAO_DESFEITA: "NÃO desfeita: o MSscan tenta de novo no próximo boot; confira no iDRAC"}  # fmt: skip


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Temporarias:
    """Registro das mudanças temporárias desta atualização (e das que sobraram de um boot anterior)."""

    def __init__(self, pasta_dados: str | None) -> None:
        self.pasta_dados = pasta_dados
        self.mudancas: list[dict[str, Any]] = []
        arquivo = self.arquivo
        if arquivo is not None and arquivo.is_file():
            try:
                dados = json.loads(arquivo.read_text(encoding="utf-8"))
                self.mudancas = [m for m in dados.get("mudancas") or [] if isinstance(m, dict) and m.get("tipo") in TIPOS]
            except (OSError, ValueError, AttributeError):
                self.mudancas = []

    @property
    def arquivo(self) -> Path | None:
        return Path(self.pasta_dados) / ARQUIVO if self.pasta_dados else None

    def salvar(self) -> None:
        """Grava e confere no pendrive. Levanta ``OSError``/``ValueError`` (quem chama não faz a mudança sem isso)."""
        if self.arquivo is None:
            raise OSError("sem local gravável no pendrive para registrar a mudança temporária")
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        conteudo = {"versao": 1, "atualizado": _agora(), "mudancas": self.mudancas}
        gravar_conferido(self.arquivo, (json.dumps(conteudo, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))

    def registrar(self, tipo: str, descricao: str, antes: dict[str, Any] | None = None) -> dict[str, Any]:
        """Registra a intenção ANTES de mudar o iDRAC. Levanta ``OSError``/``ValueError`` se não gravou."""
        mudanca = {"id": f"m{len(self.mudancas) + 1}", "tipo": tipo, "descricao": descricao, "antes": antes or {},
                   "situacao": PENDENTE, "criada": _agora(), "desfeita_em": None, "detalhe": ""}  # fmt: skip
        self.mudancas.append(mudanca)
        try:
            self.salvar()
        except (OSError, ValueError):
            self.mudancas.remove(mudanca)
            raise
        return mudanca

    def marcar(self, mudanca: dict[str, Any], situacao: str, detalhe: str = "") -> None:
        mudanca["situacao"], mudanca["detalhe"] = situacao, detalhe
        if situacao == DESFEITA:
            mudanca["desfeita_em"] = _agora()
        try:
            self.salvar()
        except (OSError, ValueError):
            pass  # a mudança já está registrada como pendente/feita: desfazer de novo é seguro

    def descartar(self, mudanca: dict[str, Any]) -> None:
        """Job do repositório que terminou sozinho (ou foi cancelado na hora): não há o que desfazer."""
        if mudanca in self.mudancas:
            self.mudancas.remove(mudanca)
            try:
                self.salvar()
            except (OSError, ValueError):
                pass

    def pendentes(self) -> list[dict[str, Any]]:
        return [m for m in self.mudancas if m["situacao"] != DESFEITA]

    def arquivar(self) -> None:
        """Tudo desfeito: o registro sai de "estado" (não se repete no próximo boot) e fica como histórico."""
        if self.arquivo is not None and self.arquivo.is_file() and not self.pendentes():
            try:
                os.replace(self.arquivo, self.arquivo.with_name(f"mudancas_desfeitas_{datetime.now():%Y-%m-%d_%H%M%S}.json"))
            except OSError:
                pass


def ha_pendentes(pasta_dados: str | None) -> bool:
    return bool(Temporarias(pasta_dados).pendentes())


ALTERADO_POR_FORA = "alterado fora do MSscan (estado anterior desconhecido)"
TENTATIVAS_RACADM = 3
INTERVALO_RACADM_S = 20.0
"""21.7: no boot seguinte o racadm acabou de ser preparado e o serviço dele pode ainda não falar com o iDRAC: o desfazer
tenta de novo (3 vezes, 20 s entre elas) em vez de deixar o pass-through ligado."""


def _desfazer_passthrough(ctx: Any, executor: Any, mudanca: dict[str, Any],
                          dormir: Callable[[float], None] = time.sleep) -> tuple[bool, str]:  # fmt: skip
    caminho = idrac.racadm(ctx)
    if not caminho:
        return False, "sem o racadm em vendor/ para desligar o pass-through"
    antes = mudanca.get("antes") or {}
    valores = {"AdminState": str(antes.get("AdminState", "0")), "PTMode": str(antes.get("PTMode", "0"))}
    # Nunca às cegas (revisão de 06/10/2026, M4; pedido do usuário): lê o estado real e só restaura se o iDRAC está
    # exatamente como o MSscan deixou (ligado, USB). Diferente = alterado por fora: estado anterior desconhecido, não mexe.
    agora = idrac.ler_passthrough(ctx)
    if not agora.get("lido"):
        return False, "não foi possível ler o pass-through agora (racadm): nada alterado; fica para a próxima sessão"
    if not agora.get("ativo"):
        return True, "o pass-through já estava desligado (nada a desfazer)"
    if agora.get("modo") != "usb-p2p":
        return True, (f"{ALTERADO_POR_FORA}: o pass-through está ligado em outro modo ({agora.get('modo')}), não no que o "
                      "MSscan deixou; nada alterado")  # fmt: skip
    for atributo, valor in valores.items():
        for tentativa in range(1, TENTATIVAS_RACADM + 1):
            rc, _, erro = executor.acao_comando([caminho, "set", f"iDRAC.OS-BMC.{atributo}", valor])
            if rc == 0:
                break
            if tentativa < TENTATIVAS_RACADM:
                dormir(INTERVALO_RACADM_S)
        if rc != 0:
            return False, f"o iDRAC recusou restaurar {atributo} ({(erro or '').strip() or rc}; {TENTATIVAS_RACADM} tentativas)"
    pt = idrac.ler_passthrough(ctx)
    if not pt.get("lido"):
        return False, "restaurado, mas não foi possível reler o pass-through para conferir"
    ativo = "1" if pt.get("ativo") else "0"
    if ativo != valores["AdminState"]:
        return False, f"relido: pass-through {'ligado' if pt.get('ativo') else 'desligado'} (esperado o valor anterior)"
    return True, "pass-through de volta ao estado anterior (" + ("ligado" if ativo == "1" else "desligado") + "), relido no iDRAC"


def _cancelar_tarefa(ctx: Any, executor: Any, mudanca: dict[str, Any]) -> tuple[bool, str]:
    """Job do repositório criado pelo MSscan: cancelado se ainda não terminou (Redfish; sem ele, ``racadm jobqueue delete``)."""
    from msscan.atualizacao import repositorio as repo

    job = str((mudanca.get("antes") or {}).get("job") or "")
    bmc = getattr(ctx, "bmc", None)
    if bmc is not None and getattr(bmc, "validado", False) and getattr(bmc, "cliente", None) is not None:
        estado = repo.estado_job(bmc.cliente, job)
        if estado is None:  # sem resposta: "não sei" — o registro fica para a próxima vez (C1)
            return False, f"o iDRAC não respondeu sobre o job {job}: fica para a próxima sessão (ou apague-o em Maintenance → Job Queue)"
        if not estado or estado in repo.TERMINAIS:
            return True, f"job {job} já tinha terminado ({estado or 'fora da fila'})"
        return repo.cancelar_job(bmc.cliente, job)
    caminho = idrac.racadm(ctx)
    if not caminho:
        return False, f"sem Redfish nem racadm para cancelar o job {job}: apague-o em Maintenance → Job Queue"
    # Revisão de 06/10/2026, B3: estado lido antes de apagar (``racadm jobqueue view``); nunca um job gravando firmware.
    from msscan.atualizacao import jobs as fila_jobs

    fila = fila_jobs.ler_fila(ctx)
    if not fila["origem"]:
        return False, f"a fila de tarefas do iDRAC não pôde ser lida ({fila['motivo']}): o job {job} fica para a próxima sessão"
    atual = next((j for j in fila["jobs"] if j["id"] == job), None)
    if atual is None or atual["estado"] not in fila_jobs.ATIVOS | fila_jobs.AGENDADOS:
        return True, f"job {job} já tinha terminado ({(atual or {}).get('estado') or 'fora da fila'})"
    if fila_jobs.gravando(atual):
        return False, f"o job {job} está gravando firmware agora: não é apagado (fica para a próxima sessão)"
    rc, _, erro = executor.acao_comando([caminho, "jobqueue", "delete", "-i", job])
    return (True, f"job {job} apagado da Job Queue (racadm)") if rc == 0 else (False, f"racadm não apagou o job {job} ({(erro or '').strip() or rc})")


def desfazer_tudo(ctx: Any, executor: Any, pasta_dados: str | None, registro: Temporarias | None = None,
                  dormir: Callable[[float], None] = time.sleep, espera_job_s: float | None = None) -> list[dict[str, Any]]:  # fmt: skip
    """Desfaz todas as mudanças temporárias ainda não desfeitas (da mais nova para a mais antiga). Nunca levanta.

    21.3: desligar o pass-through mexe na rede interna do iDRAC; com um job do iDRAC em andamento, espera ele terminar (até
    ``espera_job_s``; padrão 15 min, 0 = não espera) e, se continuar, deixa ADIADO para o próximo boot (registro mantido).
    O usuário temporário é apagado sempre (não reinicia nada e não pode ficar para trás).
    Devolve a lista de mudanças (para o relatório) com a situação final de cada uma.
    """
    from msscan.atualizacao import jobs as fila_jobs

    temporarias = registro or Temporarias(pasta_dados)
    adiar = ""
    sobra: set[str] = set()
    if any(m["tipo"] != USUARIO for m in temporarias.pendentes()) and idrac.racadm(ctx) is None:
        from msscan.deteccao import idrac_tools

        idrac_tools.esperar_download(ctx)  # 22.4: no boot seguinte o racadm ainda pode estar sendo baixado
    for mudanca in [m for m in temporarias.pendentes() if m["tipo"] == TAREFA]:
        try:
            ok, detalhe = _cancelar_tarefa(ctx, executor, mudanca)
        except Exception as erro:  # noqa: BLE001 - desfazer nunca derruba o programa
            ok, detalhe = False, f"erro inesperado: {type(erro).__name__}: {erro}"
        temporarias.marcar(mudanca, DESFEITA if ok else NAO_DESFEITA, detalhe)
        if not ok:
            sobra.add(str((mudanca.get("antes") or {}).get("job")))
    if any(m["tipo"] == PASSTHROUGH for m in temporarias.pendentes()):
        limite = fila_jobs.ESPERA_JOB_S if espera_job_s is None else espera_job_s
        # Um job do repositório do próprio MSscan que não foi cancelado não grava firmware: não adia o pass-through.
        livre, detalhe = fila_jobs.esperar_sem_ativos(ctx, dormir, limite, ignorar=sobra)
        if not livre:
            adiar = (f"adiado: job do iDRAC em andamento ({detalhe}); desligar o pass-through agora poderia interromper o job. "
                     "O MSscan desfaz no próximo boot (ou ao desligar pela tela)")  # fmt: skip
    for mudanca in reversed([m for m in temporarias.pendentes() if m["tipo"] != TAREFA]):
        if adiar and mudanca["tipo"] == PASSTHROUGH:
            temporarias.marcar(mudanca, NAO_DESFEITA, adiar)
            continue
        try:
            if mudanca["tipo"] == USUARIO:
                feito = idrac.apagar(ctx, executor, pasta_dados)
                ok = feito["situacao"] in ("apagado", "nada")
                detalhe = feito["mensagem"]
            else:
                ok, detalhe = _desfazer_passthrough(ctx, executor, mudanca, dormir)
        except Exception as erro:  # noqa: BLE001 - desfazer nunca derruba o programa
            ok, detalhe = False, f"erro inesperado: {type(erro).__name__}: {erro}"
        temporarias.marcar(mudanca, DESFEITA if ok else NAO_DESFEITA, detalhe)
    temporarias.arquivar()
    return [dict(m, texto=TEXTOS[m["situacao"]]) for m in temporarias.mudancas]

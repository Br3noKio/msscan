"""Progresso visível da atualização e segurança para desligar (CLAUDE.md 21.1 e 21.2).

Um ``Progresso`` por execução, guardado em ``resultado["_acompanhamento"]`` (a interface lê ``para_dict()`` ao vivo):

- etapas nomeadas por componente, com o tempo decorrido em cada uma e o percentual (download, bytes enviados ao iDRAC,
  ``PercentComplete`` do job);
- barra geral, tempo restante estimado (estimativa do plano corrigida pela velocidade real), última resposta do iDRAC e o
  aviso "sem progresso há X min" com o que o MSscan espera e o que conferir;
- ``gravando``: só quando um firmware está sendo gravado AGORA (faixa vermelha "NÃO desligue"); fora disso, "Seguro desligar";
- ``pedir_parada``: "Parar depois do componente atual" (o laço da instalação confere ``parar_pedido`` entre componentes).

``ao_mudar`` é chamado a cada mudança de ETAPA (não a cada percentual) e, numa espera longa, a cada ``LOG_ESPERA_S`` com o
iDRAC respondendo (22.1: os 13 min parados de 06/10 não deixaram log): a sessão grava o log técnico no pendrive (21.7).
"""

from __future__ import annotations

from msscan import textos

import time
from datetime import datetime
from typing import Any, Callable

BAIXANDO, CONFERIDO, PREPARANDO, ENVIANDO, VALIDANDO, AGENDADO, INSTALANDO, CONCLUIDO, FALHOU, PARADO, AGUARDANDO = (
    "baixando", "conferido", "preparando", "enviando", "validando", "agendado", "instalando", "concluido", "falhou", "parado",
    "aguardando")  # fmt: skip
NOMES = {AGUARDANDO: "Aguardando", BAIXANDO: "Baixando", CONFERIDO: textos.t("py_progresso.pacote_conferido"), PREPARANDO: "Preparando",
         ENVIANDO: textos.t("py_progresso.enviando_para_o_idrac"), VALIDANDO: textos.t("py_progresso.idrac_validando_o_pacote"),
         AGENDADO: textos.t("py_progresso.agendado_para_o_proximo_reinicio"), INSTALANDO: "Instalando", CONCLUIDO: textos.t("py_progresso.concluido"), FALHOU: "Falhou",
         PARADO: textos.t("py_progresso.nao_iniciado_parado_a_pedido")}  # fmt: skip
FINAIS = {AGENDADO, CONCLUIDO, FALHOU, PARADO}
FAIXAS = {AGUARDANDO: (0, 0), BAIXANDO: (0, 15), CONFERIDO: (15, 15), PREPARANDO: (15, 20), ENVIANDO: (20, 45),
          VALIDANDO: (45, 50), INSTALANDO: (50, 99), AGENDADO: (100, 100), CONCLUIDO: (100, 100), FALHOU: (100, 100),
          PARADO: (100, 100)}  # fmt: skip
"""Parte da barra do componente que cada etapa ocupa (o percentual da etapa anda dentro da faixa)."""
SEM_PROGRESSO_S = 180
"""Sem mudança por 3 min: aviso "sem progresso", com o que o MSscan espera e o que conferir."""
ESTIMATIVA_PADRAO_S = 600
LOG_ESPERA_S = 60.0


class Progresso:
    def __init__(self, componentes: list[dict[str, Any]] | None = None, ao_mudar: Callable[[], None] | None = None,
                 relogio: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
        """``componentes``: ``[{"componente", "estimativa_s"}]`` na ordem do plano."""
        self._relogio = relogio
        self.ao_mudar = ao_mudar
        self.inicio = relogio()
        self.itens: dict[str, dict[str, Any]] = {}
        for c in componentes or []:
            self._item(c["componente"], c.get("estimativa_s"))
        self.ultima_resposta: float | None = None
        self.ultima_mudanca = self.inicio
        self.espera = {"texto": "", "conferir": ""}
        self.gravando: str | None = None
        self.parar_pedido = False
        self.parado_em: str | None = None
        self.aviso_imediato = ""
        self._ultimo_log = self.inicio

    def _mudou(self) -> None:
        self._ultimo_log = self._relogio()
        if self.ao_mudar:
            try:
                self.ao_mudar()
            except Exception:  # noqa: BLE001 - gravar o log nunca para a atualização
                pass

    def _item(self, componente: str, estimativa: float | None = None) -> dict[str, Any]:
        if componente not in self.itens:
            self.itens[componente] = {"componente": componente, "etapa": AGUARDANDO, "percentual": None, "detalhe": "",
                                      "estimativa_s": float(estimativa or ESTIMATIVA_PADRAO_S), "etapas": [],
                                      "inicio": None}  # fmt: skip
        return self.itens[componente]

    # --- Escrita (thread da atualização) -------------------------------------------------------------------------------

    def etapa(self, componente: str, etapa: str, detalhe: str = "", percentual: float | None = None,
              gravando: bool = False) -> None:  # fmt: skip
        agora = self._relogio()
        item = self._item(componente)
        if item["etapas"] and item["etapas"][-1]["fim"] is None:
            item["etapas"][-1]["fim"] = agora
        if item["inicio"] is None:
            item["inicio"] = agora
        item["etapas"].append({"etapa": etapa, "inicio": agora, "fim": agora if etapa in FINAIS else None, "detalhe": detalhe})
        item.update(etapa=etapa, detalhe=detalhe, percentual=percentual)
        self.gravando = componente if gravando else (None if self.gravando == componente else self.gravando)
        self.ultima_mudanca = agora
        self._mudou()

    def percentual(self, componente: str, valor: float | None, detalhe: str | None = None) -> None:
        item = self._item(componente)
        if valor is not None and valor != item["percentual"]:
            item["percentual"] = max(0.0, min(100.0, float(valor)))
            self.ultima_mudanca = self._relogio()
        if detalhe is not None and detalhe != item["detalhe"]:
            item["detalhe"] = detalhe
            self.ultima_mudanca = self._relogio()

    def resposta_idrac(self) -> None:
        self.ultima_resposta = self._relogio()
        if self.ultima_resposta - self._ultimo_log >= LOG_ESPERA_S:
            self._mudou()

    def esperando(self, texto: str, conferir: str = "") -> None:
        self.espera = {"texto": texto, "conferir": conferir}

    def avisar_agora(self, texto: str) -> None:
        """Pedido não entregue ao iDRAC (21.1): aparece na hora, sem esperar o "sem progresso", e fica até o próximo aviso."""
        self.aviso_imediato = texto
        self._mudou()

    def pedir_parada(self) -> None:
        self.parar_pedido = True

    def marcar_parado(self, componente: str | None) -> None:
        self.parado_em = componente or ""
        for item in self.itens.values():
            if item["etapa"] == AGUARDANDO:
                self.etapa(item["componente"], PARADO)

    # --- Leitura (interface) -------------------------------------------------------------------------------------------

    def _pct(self, item: dict[str, Any]) -> float:
        inicio, fim = FAIXAS.get(item["etapa"], (0, 0))
        pct = item["percentual"]
        return float(inicio if pct is None else inicio + (fim - inicio) * pct / 100)

    def _restante(self, item: dict[str, Any], agora: float) -> float:
        pct = self._pct(item)
        if pct >= 100:
            return 0.0
        decorrido = agora - item["inicio"] if item["inicio"] is not None else 0.0
        if pct >= 5 and decorrido > 0:
            return decorrido * (100 - pct) / pct  # velocidade real
        return max(item["estimativa_s"] - decorrido, item["estimativa_s"] * (100 - pct) / 100 * 0.5)

    def para_dict(self) -> dict[str, Any]:
        agora = self._relogio()
        itens = []
        for item in self.itens.values():
            etapas = [{"etapa": e["etapa"], "nome": NOMES.get(e["etapa"], e["etapa"]), "detalhe": e["detalhe"],
                       "segundos": round((e["fim"] if e["fim"] is not None else agora) - e["inicio"])} for e in item["etapas"]]  # fmt: skip
            itens.append({"componente": item["componente"], "etapa": item["etapa"], "nome": NOMES.get(item["etapa"], item["etapa"]),
                          "detalhe": item["detalhe"], "percentual": round(self._pct(item)), "etapas": etapas,
                          "gravando": self.gravando == item["componente"]})  # fmt: skip
        geral = round(sum(self._pct(i) for i in self.itens.values()) / len(self.itens)) if self.itens else 0
        parado_ha = agora - self.ultima_mudanca
        terminou = bool(self.itens) and all(i["etapa"] in FINAIS for i in self.itens.values())
        sem_progresso = ""
        if not terminou and parado_ha >= SEM_PROGRESSO_S:
            sem_progresso = textos.t("py_progresso.sem_progresso_ha_0_min", int(parado_ha // 60)) + (
                textos.t("py_progresso.o_msscan_esta_esperando_0", self.espera['texto']) if self.espera["texto"] else "") + (
                f" Confira: {self.espera['conferir']}" if self.espera["conferir"] else "")  # fmt: skip
        return {
            "geral": geral,
            "decorrido_s": round(agora - self.inicio),
            "restante_s": None if terminou else round(sum(self._restante(i, agora) for i in self.itens.values())),
            "ultima_resposta_s": None if self.ultima_resposta is None else round(agora - self.ultima_resposta),
            "sem_progresso": sem_progresso,
            "aviso_imediato": self.aviso_imediato,
            "esperando": self.espera["texto"],
            "gravando": self.gravando,
            "seguro": self.gravando is None,
            "faixa": (textos.t("py_progresso.nao_desligue_gravando_firmware_0", self.gravando) if self.gravando else
                      textos.t("py_progresso.seguro_desligar_agora_nada_esta") + ("." if terminou or not self.itens else
                      textos.t("py_progresso.o_que_esta_em_andamento"))),  # fmt: skip
            "parar_pedido": self.parar_pedido,
            "parado_em": self.parado_em,
            "terminou": terminou,
            "itens": itens,
            "atualizado": datetime.now().isoformat(timespec="seconds"),
        }



def do_job(progresso: Progresso, componente: str, estado: str, percentual: Any = None, detalhe: str = "") -> None:
    """Estado de um job do iDRAC (``JobState``) → etapa. "Gravando" só com ``Running`` (o iDRAC aplicando agora)."""
    pct = float(percentual) if isinstance(percentual, (int, float)) else None
    etapa = {"Downloading": ENVIANDO, "Running": INSTALANDO, "Scheduled": AGENDADO, "Completed": CONCLUIDO,
             "Failed": FALHOU, "Failed.": FALHOU, "CompletedWithErrors": FALHOU, "Cancelled": FALHOU,
             "Killed": FALHOU}.get(estado, VALIDANDO)  # fmt: skip
    gravando = etapa == INSTALANDO
    atual = progresso.itens.get(componente) or {}
    if atual.get("etapa") != etapa or bool(progresso.gravando == componente) != gravando:
        progresso.etapa(componente, etapa, detalhe, pct, gravando=gravando)
    else:
        progresso.percentual(componente, pct, detalhe or None)


def da_tarefa(progresso: Progresso, componente: str, tarefa: dict[str, Any]) -> None:
    """Tarefa do ``TaskService`` (``parse_tarefa``) → etapa. Sem o ``JobState`` da Dell, "Running" fica como "iDRAC
    validando" (com ``OnReset`` o pacote só é gravado no reinício)."""
    detalhe = "; ".join(tarefa.get("mensagens") or []) or str(tarefa.get("dell_mensagem") or "")
    if tarefa.get("agendada"):
        estado = "Scheduled"
    elif tarefa.get("concluida"):
        estado = "Completed"
    elif tarefa.get("falhou") and tarefa.get("terminou"):
        estado = "Failed"
    else:
        estado = tarefa.get("dell_estado") or ""
    do_job(progresso, componente, estado, tarefa.get("percentual"), detalhe)


def de(resultado: dict[str, Any] | None) -> Progresso:
    """O ``Progresso`` desta execução, ou um avulso (quem chama nunca precisa testar ``None``)."""
    progresso = (resultado or {}).get("_acompanhamento")
    return progresso if isinstance(progresso, Progresso) else Progresso()


__all__ = ["AGENDADO", "BAIXANDO", "CONCLUIDO", "CONFERIDO", "ENVIANDO", "FALHOU", "INSTALANDO", "PARADO", "PREPARANDO",
           "Progresso", "VALIDANDO", "da_tarefa", "de", "do_job"]  # fmt: skip

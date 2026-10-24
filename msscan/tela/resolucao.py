"""Resolução da interface gráfica (CLAUDE.md 12c, itens 1, 2, 3, 5 e 6).

Cadeia: ``msscan.resolucao=LxA`` (se houver) → com EDID: modo preferido do monitor → maior modo
suportado até 1920x1080 → 1280x1024 → 1024x768 → 800x600. Sem EDID (monitor antigo, KVM, console
virtual do BMC): modo seguro, 1024x768 → 800x600. Se o ``xrandr`` recusar um modo, desce para o próximo.

No boot (``sessao-x``, com o X no ar): ``python3 -m msscan.tela.resolucao --auto --estado <arquivo>``.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from msscan.core.executor import Executor, ExecutorReal
from msscan.parsers import ErroParser
from msscan.parsers.procfs import parametro_boot, parse_cmdline
from msscan.parsers.xrandr import parse_xrandr

MAXIMO_AUTOMATICO = (1920, 1080)
DEGRAUS = ((1280, 1024), (1024, 768), (800, 600))
SEGUROS_SEM_EDID = ((1024, 768), (800, 600))
ESPERA_CONFIRMACAO_S = 15
COMANDO_CONSULTA = ["xrandr", "--props"]


def nome_modo(modo: tuple[int, int]) -> str:
    return f"{modo[0]}x{modo[1]}"


def parse_resolucao(texto: str | None) -> tuple[int, int] | None:
    """``"1920x1080"`` → ``(1920, 1080)``; inválido → ``None``."""
    if not texto:
        return None
    partes = texto.strip().lower().split("x")
    if len(partes) != 2 or not all(p.isdigit() for p in partes):
        return None
    largura, altura = int(partes[0]), int(partes[1])
    return (largura, altura) if 320 <= largura <= 16384 and 200 <= altura <= 16384 else None


def resolucao_forcada(cmdline: str | None) -> tuple[int, int] | None:
    """Valor de ``msscan.resolucao=LxA`` (ou do antigo ``serverdiag.resolucao``) em ``/proc/cmdline``."""
    valor = parametro_boot(parse_cmdline(cmdline or ""), "resolucao")
    return parse_resolucao(valor) if valor is not None else None


def saida_principal(xrandr: dict[str, Any]) -> dict[str, Any] | None:
    """Saída conectada (a principal, se houver)."""
    conectadas = [s for s in xrandr["saidas"] if s["conectada"] and s["modos"]]
    return next((s for s in conectadas if s["principal"]), conectadas[0] if conectadas else None)


def cadeia_de_modos(saida: dict[str, Any], forcada: tuple[int, int] | None = None) -> list[tuple[int, int]]:
    """Modos a tentar, em ordem, sem repetição (só os que a saída anuncia)."""
    disponiveis = [(m["largura"], m["altura"]) for m in saida["modos"] if not m["entrelacado"]]
    cadeia: list[tuple[int, int]] = []

    def incluir(modo: tuple[int, int] | None) -> None:
        if modo and modo in disponiveis and modo not in cadeia:
            cadeia.append(modo)

    incluir(forcada)
    if saida["edid"]:
        incluir(next(((m["largura"], m["altura"]) for m in saida["modos"] if m["preferido"]), None))
        dentro = [m for m in disponiveis if m[0] <= MAXIMO_AUTOMATICO[0] and m[1] <= MAXIMO_AUTOMATICO[1]]
        incluir(max(dentro, key=lambda m: (m[0] * m[1], m[0]), default=None))
        for modo in DEGRAUS:
            incluir(modo)
    else:
        for modo in SEGUROS_SEM_EDID:
            incluir(modo)
    return cadeia


def consultar(executor: Executor) -> dict[str, Any] | None:
    rc, saida, _ = executor.rodar(COMANDO_CONSULTA, timeout=15)
    if rc != 0:
        return None
    try:
        return parse_xrandr(saida)
    except ErroParser:
        return None


def aplicar_modo(executor: Executor, saida: str, modo: tuple[int, int]) -> tuple[bool, str]:
    """``xrandr --output S --mode LxA`` e confere que o modo ficou ativo."""
    rc, _, erro = executor.rodar(["xrandr", "--output", saida, "--mode", nome_modo(modo)], timeout=20)
    if rc != 0:
        return False, (erro.strip().splitlines() or [f"código {rc}"])[-1]
    depois = consultar(executor)
    atual = next((s["atual"] for s in (depois or {}).get("saidas", []) if s["nome"] == saida), None)
    if atual != modo:
        return False, f"o modo ativo ficou {nome_modo(atual) if atual else '?'}"
    return True, ""


def ajustar_automaticamente(executor: Executor, cmdline: str | None) -> dict[str, Any]:
    """Aplica o primeiro modo da cadeia que funcionar. Nunca levanta exceção."""
    xrandr = consultar(executor)
    if xrandr is None:
        return {"ok": False, "mensagem": "xrandr indisponível: resolução mantida", "tentativas": []}
    saida = saida_principal(xrandr)
    if saida is None:
        return {"ok": False, "mensagem": "nenhuma saída de vídeo conectada", "tentativas": []}
    forcada = resolucao_forcada(cmdline)
    tentativas = []
    if forcada and forcada not in [(m["largura"], m["altura"]) for m in saida["modos"]]:
        tentativas.append(f"{nome_modo(forcada)} (msscan.resolucao): não anunciado por esta saída")
    for modo in cadeia_de_modos(saida, forcada):
        ok, motivo = aplicar_modo(executor, saida["nome"], modo)
        tentativas.append(f"{nome_modo(modo)}: {'ok' if ok else motivo}")
        if ok:
            return {"ok": True, "saida": saida["nome"], "modo": nome_modo(modo), "edid": saida["edid"],
                    "forcada": nome_modo(forcada) if forcada else None, "tentativas": tentativas,
                    "mensagem": f"{nome_modo(modo)} em {saida['nome']}" + ("" if saida["edid"] else " (monitor sem EDID: modo seguro)")}  # fmt: skip
    atual = nome_modo(saida["atual"]) if saida["atual"] else "?"
    return {"ok": False, "saida": saida["nome"], "modo": atual, "edid": saida["edid"], "tentativas": tentativas,
            "mensagem": f"nenhum modo da cadeia foi aceito: mantido {atual}"}  # fmt: skip


class ControleTela:
    """Resolução pela interface: lista, troca e confirmação (volta sozinha se não confirmar em 15 s)."""

    def __init__(self, executor: Executor, arquivo_estado: str | None = None,
                 temporizador: Callable[[float, Callable[[], None]], Any] | None = None) -> None:  # fmt: skip
        self.executor = executor
        self.arquivo_estado = arquivo_estado
        self._temporizador = temporizador or (lambda s, f: _iniciar_timer(s, f))
        self._trava = threading.Lock()
        self._pendente: dict[str, Any] | None = None
        self._timer: Any = None
        self.mensagem = ""

    def estado(self) -> dict[str, Any]:
        xrandr = consultar(self.executor)
        saida = saida_principal(xrandr) if xrandr else None
        automatico = None
        if self.arquivo_estado:
            texto = self.executor.ler_arquivo(self.arquivo_estado)
            try:
                automatico = json.loads(texto) if texto else None
            except ValueError:
                automatico = None
        if saida is None:
            return {"disponivel": False, "motivo": "resolução só pode ser trocada na interface em tela cheia (X local)",
                    "automatico": automatico}  # fmt: skip
        modos = []
        for m in saida["modos"]:
            nome = f"{m['largura']}x{m['altura']}" + ("i" if m["entrelacado"] else "")
            if nome not in [x["nome"] for x in modos]:
                modos.append({"nome": nome, "atual": m["atual"], "preferido": m["preferido"], "entrelacado": m["entrelacado"]})
        with self._trava:
            pendente = dict(self._pendente) if self._pendente else None
        if pendente:
            pendente["restante_s"] = max(0, round(pendente.pop("segundos") - (time.monotonic() - pendente.pop("inicio"))))
        return {"disponivel": True, "saida": saida["nome"], "edid": saida["edid"],
                "atual": nome_modo(saida["atual"]) if saida["atual"] else None, "modos": modos,
                "automatico": automatico, "pendente": pendente, "mensagem": self.mensagem}  # fmt: skip

    def trocar(self, modo_texto: str) -> tuple[bool, str]:
        """Aplica o modo e espera confirmação; sem ela em 15 s, volta ao anterior."""
        modo = parse_resolucao(modo_texto)
        xrandr = consultar(self.executor)
        saida = saida_principal(xrandr) if xrandr else None
        if modo is None or saida is None:
            return False, "resolução inválida ou vídeo indisponível"
        if modo not in [(m["largura"], m["altura"]) for m in saida["modos"] if not m["entrelacado"]]:
            return False, f"{modo_texto} não é anunciado por {saida['nome']}"
        anterior = saida["atual"]
        ok, motivo = aplicar_modo(self.executor, saida["nome"], modo)
        if not ok:
            self.mensagem = f"{nome_modo(modo)} recusado ({motivo}); resolução mantida"
            if anterior:
                aplicar_modo(self.executor, saida["nome"], anterior)
            return False, self.mensagem
        with self._trava:
            self._cancelar_timer()
            self._pendente = {"modo": nome_modo(modo), "anterior": nome_modo(anterior) if anterior else None,
                              "saida": saida["nome"], "segundos": ESPERA_CONFIRMACAO_S, "inicio": time.monotonic()}  # fmt: skip
            self._timer = self._temporizador(ESPERA_CONFIRMACAO_S, self._reverter)
        self.mensagem = f"{nome_modo(modo)} aplicado: confirme em {ESPERA_CONFIRMACAO_S} s ou volta para o anterior"
        return True, self.mensagem

    def confirmar(self) -> bool:
        with self._trava:
            if not self._pendente:
                return False
            self.mensagem = f"{self._pendente['modo']} mantido"
            self._pendente = None
            self._cancelar_timer()
            return True

    def reverter(self) -> bool:
        return self._reverter()

    def _reverter(self) -> bool:
        with self._trava:
            pendente, self._pendente = self._pendente, None
            self._cancelar_timer()
        if not pendente or not pendente["anterior"]:
            return False
        aplicar_modo(self.executor, pendente["saida"], parse_resolucao(pendente["anterior"]) or (1024, 768))
        self.mensagem = f"voltou para {pendente['anterior']} (sem confirmação)"
        return True

    def _cancelar_timer(self) -> None:
        if self._timer is not None and hasattr(self._timer, "cancel"):
            self._timer.cancel()
        self._timer = None


def _iniciar_timer(segundos: float, funcao: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(segundos, funcao)
    timer.daemon = True
    timer.start()
    return timer


def main(argv: list[str] | None = None) -> int:
    """``--auto [--estado ARQUIVO]``: ajusta a resolução no boot e grava o resultado (para a interface)."""
    argumentos = sys.argv[1:] if argv is None else argv
    if "--auto" not in argumentos:
        print("Uso: python3 -m msscan.tela.resolucao --auto [--estado ARQUIVO]")
        return 2
    executor = ExecutorReal()
    resultado = ajustar_automaticamente(executor, executor.ler_arquivo("/proc/cmdline"))
    if "--estado" in argumentos:
        destino = argumentos[argumentos.index("--estado") + 1]
        try:
            Path(destino).write_text(json.dumps(resultado, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError:
            pass
    print("Resolução: " + resultado["mensagem"] + " | " + "; ".join(resultado["tentativas"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

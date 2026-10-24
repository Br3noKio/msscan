"""Monitoramento contínuo da rede (CLAUDE.md 5b), depois da verificação inicial obrigatória.

Em segundo plano, sem travar a interface: link e IP a cada ``intervalo`` segundos (leituras locais,
baratas) e a internet (HTTPS) a cada ``intervalo_internet`` segundos ou logo que link/IP voltam. Só
declara "Reconectando..." depois de ``falhas_para_cair`` verificações seguidas com falha, para um
pacote perdido não piscar a tela. O NetworkManager reconecta e faz o DHCP; aqui só se verifica.

As leituras passam por um ``ExecutorSilencioso``: mesmas salvaguardas do executor, mas fora do
histórico de comandos (senão o relatório teria um ``ip addr`` a cada 5 segundos).

Quem depende de rede (IA, catálogo, downloads) usa ``esperar`` e ``baixar_retomando``: espera a rede
voltar e retoma sozinho; o download continua de onde parou e o hash é sempre recalculado do arquivo inteiro.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from msscan.core.executor import Executor, RespostaDownload
from msscan.rede.verificacao import SEM_CABO, SEM_INTERNET, SEM_IP, SEM_PLACA, ResultadoRede, verificar_rede

OK, RECONECTANDO = "ok", "reconectando"
CURTOS = {SEM_PLACA: "sem placa", SEM_CABO: "sem cabo", SEM_IP: "sem DHCP", SEM_INTERNET: "sem internet"}
"""Motivo curto para o cabeçalho da interface (cabe em 800x600)."""
MOTIVOS = {
    SEM_PLACA: "nenhuma placa de rede utilizável",
    SEM_CABO: "cabo de rede desconectado (sem link)",
    SEM_IP: "sem endereço IP (DHCP não respondeu)",
    SEM_INTERNET: "sem acesso à internet",
}
INTERVALO_RELOGIO_S = 300
"""Sem internet, o monitor confere o relógio no máximo a cada 5 min (NTP, HTTP e iDRAC pesam; B2)."""
ESPERA_MAXIMA_S = 900
"""Quanto IA/catálogo/download esperam a rede voltar antes de desistir (a tela segue livre para o resto)."""
ERROS_DE_REDE = {"conexao", "dns", "timeout", "proxy", "tls"}
"""``erro_tipo`` de download que indicam rede fora (vale esperar e retomar)."""


COMANDOS_QUE_ALTERAM = {"date", "nmcli"}
"""Mudanças feitas pelo monitor (relógio do sistema, DNS/rota da sessão) vão para o histórico do executor base (B1)."""


class _SemRegistro(list):
    """Histórico do executor silencioso: descarta as leituras; repassa ao base os comandos que alteram algo."""

    def __init__(self, base: list | None = None) -> None:
        super().__init__()
        self.base = base

    def append(self, item: Any) -> None:  # noqa: D102
        comando = getattr(item, "comando", None) or []
        if self.base is not None and comando and comando[0] in COMANDOS_QUE_ALTERAM:
            self.base.append(item)


class ExecutorSilencioso(Executor):
    """Mesmas leituras (e validações) do executor base, sem registrar no histórico."""

    def __init__(self, base: Executor) -> None:
        super().__init__()
        self.base = base
        self.origem = base.origem
        self.historico = _SemRegistro(base.historico)
        self.modos_tls_bmc = base.modos_tls_bmc

    def _rodar(self, comando: list[str], timeout: float) -> tuple[int, str, str, float]:
        return self.base._rodar(comando, timeout)

    def _ler_arquivo(self, caminho: str) -> str | None:
        return self.base._ler_arquivo(caminho)

    def _existe(self, caminho: str) -> bool:
        return self.base._existe(caminho)

    def _listar(self, caminho: str) -> list[str]:
        return self.base._listar(caminho)

    def _tem_comando(self, nome: str) -> bool:
        return self.base._tem_comando(nome)

    def _http_get(self, url: str, timeout: float):  # noqa: ANN202 - mesmo tipo do base
        return self.base._http_get(url, timeout)


class MonitorRede:
    """Estado da rede atualizado em segundo plano. Métodos seguros para várias threads."""

    def __init__(
        self,
        executor: Executor,
        intervalo: float = 5.0,
        intervalo_internet: float = 30.0,
        falhas_para_cair: int = 2,
        ao_mudar: Callable[[dict[str, Any]], None] | None = None,
        relogio: Callable[[], float] = time.monotonic,
        inicial: ResultadoRede | None = None,
    ) -> None:
        """``inicial``: a verificação inicial. Sem internet nela (28.1: a rede não bloqueia mais), o monitor começa em
        "Reconectando..." com o motivo real e avisa quando a internet chegar."""
        self.executor = ExecutorSilencioso(executor)
        self.intervalo = intervalo
        self.intervalo_internet = intervalo_internet
        self.falhas_para_cair = max(1, falhas_para_cair)
        self.ao_mudar = ao_mudar
        self._relogio = relogio
        self._trava = threading.Lock()
        self._conectado = threading.Event()
        self._conectado.set()  # começa depois da verificação inicial, que já passou
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        self._falhas = 0
        self._ultima_internet = relogio()
        self._ultimo_relogio = -INTERVALO_RELOGIO_S
        self._estado: dict[str, Any] = {"situacao": OK, "problema": "", "motivo": "", "titulo": "Rede OK", "desde": time.time()}
        self._ao_voltar: list[Callable[[], None]] = []
        self.ultimo: ResultadoRede | None = inicial if inicial is not None and inicial.ok else None
        """Última verificação completa (com o teste de internet) que deu certo: quem começou sem internet a usa."""
        if inicial is not None and not inicial.ok and not inicial.sem_rede_por_parametro:
            self._falhas = self.falhas_para_cair
            self._estado = self._estado_fora(inicial, time.time())
            self._conectado.clear()

    # Ciclo ------------------------------------------------------------------

    def iniciar(self) -> None:
        """Começa a verificar em segundo plano (uma thread; chamar de novo não duplica)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._parar.clear()
        self._thread = threading.Thread(target=self._laco, name="msscan-monitor-rede", daemon=True)
        self._thread.start()

    def parar(self) -> None:
        self._parar.set()

    def _laco(self) -> None:
        while not self._parar.wait(self.intervalo if self.ok else min(self.intervalo, 3.0)):
            try:
                self.verificar_agora()
            except Exception:  # noqa: BLE001 - o monitor nunca derruba o programa
                pass

    def verificar_agora(self, forcar_internet: bool = False) -> dict[str, Any]:
        """Uma verificação (a internet só quando é hora, quando a rede estava fora ou forçada). Devolve o estado."""
        agora = self._relogio()
        testar_internet = forcar_internet or not self.ok or agora - self._ultima_internet >= self.intervalo_internet
        # Relógio só conferido com a rede fora, no máximo a cada 5 min (uma vez por sessão; depois de acertado, não refaz).
        acertar = not self.ok and agora - self._ultimo_relogio >= INTERVALO_RELOGIO_S
        if acertar and testar_internet:
            self._ultimo_relogio = agora
        resultado = verificar_rede(self.executor, timeout_http=8, testar_internet=testar_internet, acertar_relogio=acertar)
        if testar_internet and resultado.ok:
            self._ultima_internet = agora
            self.ultimo = resultado
        return self._registrar(resultado)

    @staticmethod
    def _estado_fora(resultado: ResultadoRede, desde: float) -> dict[str, Any]:
        motivo = resultado.causa if resultado.problema == SEM_INTERNET and resultado.causa else MOTIVOS.get(resultado.problema, resultado.titulo)
        return {"situacao": RECONECTANDO, "problema": resultado.problema, "motivo": motivo,
                "curto": CURTOS.get(resultado.problema, "rede fora"), "titulo": resultado.titulo, "desde": desde}  # fmt: skip

    def _registrar(self, resultado: ResultadoRede) -> dict[str, Any]:
        mudou = None
        with self._trava:
            if resultado.ok:
                self._falhas = 0
                if self._estado["situacao"] != OK:
                    self._estado = {"situacao": OK, "problema": "", "motivo": "", "titulo": "Rede OK", "desde": time.time()}
                    mudou = dict(self._estado)
            else:
                self._falhas += 1
                caiu = self._estado["situacao"] == OK and self._falhas >= self.falhas_para_cair
                trocou_motivo = self._estado["situacao"] == RECONECTANDO and self._estado["problema"] != resultado.problema
                if caiu or trocou_motivo:
                    desde = self._estado["desde"] if trocou_motivo else time.time()
                    self._estado = self._estado_fora(resultado, desde)
                    mudou = dict(self._estado)
            conectado = self._estado["situacao"] == OK
            estado = dict(self._estado)
        if conectado:
            self._conectado.set()
        else:
            self._conectado.clear()
        pendentes: list[Callable[[], None]] = []
        if conectado and self._ao_voltar:
            with self._trava:
                pendentes, self._ao_voltar = self._ao_voltar, []
        for funcao in pendentes:
            try:
                funcao()
            except Exception:  # noqa: BLE001 - quem espera a rede não para o monitor
                pass
        if mudou is not None and self.ao_mudar is not None:
            try:
                self.ao_mudar(mudou)
            except Exception:  # noqa: BLE001 - quem acompanha não para o monitor
                pass
        return estado

    def confirmar_queda(self) -> bool:
        """Um consumidor (IA, download) falhou por rede: verifica agora contando essa falha como evidência.

        ``True`` se a rede está fora (aí vale esperar); ``False`` se a rede está de pé (o problema é do servidor).
        """
        with self._trava:
            self._falhas = max(self._falhas, self.falhas_para_cair - 1)
        self.verificar_agora(forcar_internet=True)
        return not self.ok

    def quando_voltar(self, funcao: Callable[[], None]) -> None:
        """Roda ``funcao`` uma vez, na thread do monitor, quando a rede estiver OK (na hora, se já estiver)."""
        with self._trava:
            if not self._conectado.is_set():
                self._ao_voltar.append(funcao)
                return
        funcao()

    # Leitura e espera ----------------------------------------------------------

    @property
    def ok(self) -> bool:
        return self._conectado.is_set()

    def estado(self) -> dict[str, Any]:
        with self._trava:
            return dict(self._estado)

    def esperar(self, timeout: float | None = None) -> bool:
        """Bloqueia até a rede voltar (``True``) ou o tempo acabar (``False``). Retorna na hora se já está OK."""
        return self._conectado.wait(timeout)


def texto_status(estado: dict[str, Any]) -> str:
    """Linha de status do modo texto (e texto do cabeçalho da interface)."""
    if estado.get("situacao") == RECONECTANDO:
        return f"Reconectando... ({estado.get('motivo') or 'rede fora'})"
    return "Rede OK"


def esperar_rede(monitor: MonitorRede | None, avisar: Callable[[str], None] | None = None,
                 timeout: float | None = None) -> bool:  # fmt: skip
    """Se a rede está fora, avisa e espera ela voltar. Sem monitor: segue na hora."""
    if monitor is None or monitor.ok:
        return True
    if avisar is not None:
        avisar(f"Sem rede ({monitor.estado().get('motivo') or 'rede fora'}): aguardando a reconexão para continuar...")
    return monitor.esperar(timeout)


def baixar_retomando(
    executor: Executor,
    url: str,
    destino: str,
    limite_bytes: int,
    timeout: float = 120,
    monitor: MonitorRede | None = None,
    tentativas: int = 4,
    espera_maxima: float | None = ESPERA_MAXIMA_S,
    avisar: Callable[[str], None] | None = None,
) -> RespostaDownload:
    """``executor.baixar`` que, se a rede cair no meio, espera ela voltar e continua de onde parou.

    O executor mantém o ``.parcial`` quando o erro é de rede e retoma com ``Range``; o SHA-256 é
    recalculado sobre o arquivo INTEIRO (quem chama sempre confere com o hash oficial).
    """
    resposta = executor.baixar(url, destino, limite_bytes, timeout=timeout)
    for _ in range(tentativas - 1):
        if resposta.ok or resposta.erro_tipo not in ERROS_DE_REDE or monitor is None:
            return resposta
        if not monitor.confirmar_queda():
            return resposta  # a rede está de pé: o problema é do servidor, não adianta esperar
        if not esperar_rede(monitor, avisar, espera_maxima):
            return resposta
        if avisar is not None:
            avisar("Rede de volta: retomando o download de onde parou.")
        resposta = executor.baixar(url, destino, limite_bytes, timeout=timeout)
    return resposta

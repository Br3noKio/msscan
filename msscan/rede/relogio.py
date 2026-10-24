"""Relógio do sistema antes de qualquer HTTPS (CLAUDE.md 28.1).

Caso real (R530): o relógio do BIOS estava 6 meses atrasado, os certificados da internet pareciam "do futuro" e o MSscan
dizia só "SEM INTERNET: tls". Ordem, parando na primeira fonte que funcionar:

1. NTP (SNTP por UDP 123), vários servidores ao mesmo tempo, tempo curto (o NTP do DHCP da rede do cliente primeiro);
   respostas sem sincronia (alarme, estrato 16) ou que não ecoam o pedido são descartadas;
2. cabeçalho ``Date`` de respostas HTTP **sem TLS** de mais de um site (só lê a hora e confere a resposta conhecida de
   cada site; redirecionamento ou conteúdo diferente = portal/proxy, descartado) — vale quando dois concordam;
3. hora do iDRAC (``ipmitool -Z sel time get``, em UTC), se for plausível e não for a mesma do sistema (o iDRAC costuma
   herdar a hora do BIOS: igual à do sistema não confirma nada);
4. nada funcionou e a hora do sistema é impossível (anterior à data desta versão): ``precisa_tecnico`` — a interface e o
   modo texto perguntam a data e hora ao técnico, com a hora do sistema já preenchida.

Só o relógio do SISTEMA (Linux) é ajustado (``date -u -s @<segundos>``). **Nunca o relógio do BIOS** (sem ``hwclock``, e a
ISO não usa o systemd-timesyncd, que deixaria o kernel copiar a hora para o BIOS a cada 11 minutos).

Concorrência (revisão independente, A2): medir e aplicar ficam sob uma trava única, e cada fonte é guardada como
deslocamento em relação a ``time.monotonic()`` (imune a outra thread mudar o relógio no meio): a correção nunca é
aplicada duas vezes.
"""

from __future__ import annotations

import os
import re
import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from statistics import median
from typing import Any

from msscan import __data_versao__

SERVIDORES_NTP = ("pool.ntp.org", "a.st1.ntp.br", "time.google.com", "time.cloudflare.com", "time.windows.com",
                  "0.debian.pool.ntp.org")  # fmt: skip
URLS_DATA_HTTP = ("http://connectivitycheck.gstatic.com/generate_204", "http://detectportal.firefox.com/success.txt",
                  "http://www.msftconnecttest.com/connecttest.txt", "http://captive.apple.com/")  # fmt: skip
RESPOSTA_ESPERADA = {
    URLS_DATA_HTTP[0]: (204, b""),
    URLS_DATA_HTTP[1]: (200, b"success"),
    URLS_DATA_HTTP[2]: (200, b"Microsoft Connect Test"),
    URLS_DATA_HTTP[3]: (200, b"Success"),
}
"""Status e começo do corpo de cada site: outra coisa é um portal ou proxy respondendo no lugar dele (M3)."""
TEMPO_NTP_S = 2.5
TEMPO_HTTP_S = 4.0
NTP_SUFICIENTES = 3
"""Com três servidores NTP respondendo, não se espera os demais (B3)."""
CONCORDANCIA_HTTP_S = 300
"""Sites HTTP só valem se pelo menos dois concordarem até 5 min."""
MESMA_ORIGEM_S = 120
"""iDRAC com a hora a menos de 2 min da do sistema: mesma origem (o BIOS), não confirma nada (A3)."""
AJUSTAR_ACIMA_S = 30
"""Diferença a partir da qual o relógio do sistema é ajustado."""
AVISAR_ACIMA_S = 86400
"""Diferença a partir da qual se recomenda corrigir a data no BIOS (um dia)."""
_NTP_1970 = 2208988800

_trava = threading.RLock()
"""Medir + aplicar (sincronizar, conferir, técnico) sob uma trava só: nunca duas correções somadas (A2)."""
_ultimo: ResultadoRelogio | None = None


def data_minima() -> float:
    """Início do dia da versão do MSscan (UTC): uma hora anterior a isso é impossível."""
    ano, mes, dia = (int(p) for p in __data_versao__.split("-"))
    return datetime(ano, mes, dia, tzinfo=timezone.utc).timestamp()


def consultar_ntp(servidor: str, tempo: float = TEMPO_NTP_S, porta: int = 123) -> float | None:
    """Hora (segundos desde 1970, UTC) de um servidor NTP por SNTP; ``None`` sem resposta válida e sincronizada."""
    marca = os.urandom(8)  # "transmit timestamp" aleatório: o servidor tem de ecoá-lo (resposta é para este pedido)
    pedido = b"\x23" + 39 * b"\0" + marca  # LI=0, versão 4, modo 3 (cliente)
    try:
        for familia, tipo, proto, _, endereco in socket.getaddrinfo(servidor, porta, socket.AF_INET, socket.SOCK_DGRAM):
            with socket.socket(familia, tipo, proto) as s:
                s.settimeout(tempo)
                enviado = time.monotonic()
                s.sendto(pedido, endereco)
                resposta, _ = s.recvfrom(512)
                atraso = (time.monotonic() - enviado) / 2
            if len(resposta) < 48 or resposta[24:32] != marca:
                continue
            li, modo, estrato = resposta[0] >> 6, resposta[0] & 0x07, resposta[1]
            if li == 3 or modo not in (4, 5) or not 1 <= estrato <= 15:  # alarme / sem sincronia (M2)
                continue
            segundos, fracao = struct.unpack("!II", resposta[40:48])
            if segundos == 0:
                continue
            return segundos - _NTP_1970 + fracao / 2**32 + atraso
    except (OSError, UnicodeError):
        return None
    return None


def data_http(url: str, tempo: float = TEMPO_HTTP_S) -> float | None:
    """Hora do cabeçalho ``Date`` de uma resposta HTTP sem TLS (proxy das variáveis de ambiente respeitado).

    Sem seguir redirecionamentos e só se a resposta for a conhecida do site (``RESPOSTA_ESPERADA``): um portal ou proxy
    transparente respondendo pelos quatro sites não vale como "sites concordando". Lê no máximo 64 bytes do corpo.
    """
    import urllib.error
    import urllib.request

    class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_a: Any, **_k: Any) -> None:  # noqa: D102 - 3xx vira HTTPError
            return None

    abridor = urllib.request.build_opener(_SemRedirecionar)
    pedido = urllib.request.Request(url, headers={"User-Agent": "MSscan"})
    status_esperado, corpo_esperado = RESPOSTA_ESPERADA.get(url, (200, b""))
    try:
        with abridor.open(pedido, timeout=tempo) as resposta:  # noqa: S310 - URL fixa, só http
            if resposta.status != status_esperado or not resposta.read(64).startswith(corpo_esperado):
                return None
            cabecalho = resposta.headers.get("Date")
    except (OSError, ValueError, urllib.error.HTTPError):
        return None
    try:
        return parsedate_to_datetime(cabecalho).timestamp() if cabecalho else None
    except (TypeError, ValueError):
        return None


_RE_SEL_TIME = re.compile(r"^(\d{2})/(\d{2})/(\d{2}|\d{4}) (\d{2}):(\d{2}):(\d{2})(?: (GMT|UTC|[+-]\d{2}(?::?\d{2})?))?$")


def parse_sel_time(texto: str) -> float | None:
    """Saída do ``ipmitool sel time get`` → segundos UTC. Só com o fuso explícito (``GMT``/``UTC`` do ``-Z``, ou ``-03``).

    Saída real (ipmitool 1.8.19, R640): ``10/14/26 15:11:03 GMT`` com ``-Z`` e ``10/14/26 12:11:03 -03`` sem. Sem fuso não
    dá para saber se é hora local ou UTC: ``None`` (nunca adivinhar 3 horas).
    """
    linhas = texto.strip().splitlines()
    achado = _RE_SEL_TIME.match(linhas[-1].strip()) if linhas else None
    if not achado or not achado.group(7):
        return None
    mes, dia, ano, hora, minuto, segundo = (int(achado.group(i)) for i in range(1, 7))
    fuso = achado.group(7)
    if fuso in ("GMT", "UTC"):
        deslocamento = timedelta(0)
    else:
        numeros = fuso.replace(":", "")
        sinal = -1 if numeros[0] == "-" else 1
        deslocamento = sinal * timedelta(hours=int(numeros[1:3]), minutes=int(numeros[3:5] or 0))
    try:
        quando = datetime(ano + 2000 if ano < 100 else ano, mes, dia, hora, minuto, segundo, tzinfo=timezone(deslocamento))
    except ValueError:
        return None
    return quando.timestamp()


def hora_idrac(executor: Any) -> float | None:
    """Hora do BMC em UTC (``ipmitool -Z sel time get``; sem o ``-Z``, só se a saída trouxer o fuso). ``None`` se não houver."""
    for comando in (["ipmitool", "-Z", "sel", "time", "get"], ["ipmitool", "sel", "time", "get"]):
        rc, saida, _ = executor.rodar(comando, timeout=15)
        if rc == 0 and (hora := parse_sel_time(saida)) is not None:
            return hora
    return None


@dataclass
class ResultadoRelogio:
    """O que se fez com o relógio (vai para a tela, o relatório e o log técnico)."""

    ok: bool = False
    """Hora confiável obtida (o relógio ficou certo, ajustado ou não)."""
    fonte: str = ""
    diferenca_s: float = 0.0
    """Hora correta − hora do sistema (positivo = o sistema estava atrasado)."""
    ajustado: bool = False
    mensagem: str = ""
    precisa_tecnico: bool = False
    hora_sistema: str = ""
    tentativas: list[str] = field(default_factory=list)

    def para_dict(self) -> dict[str, Any]:
        return asdict(self)


def _paralelo(funcao: Callable[[str], float | None], alvos: list[str], limite: float, suficientes: int | None = None) -> dict[str, float]:
    """Roda ``funcao`` em todos os alvos ao mesmo tempo, até ``limite`` segundos (ou até ``suficientes`` respostas).

    Cada resposta é guardada como deslocamento em relação a ``time.monotonic()``: hora certa agora = monotonic() + valor.
    """
    respostas: dict[str, float] = {}
    trava = threading.Lock()
    bastou = threading.Event()

    def um(alvo: str) -> None:
        valor = funcao(alvo)
        if valor is not None:
            with trava:
                respostas[alvo] = valor - time.monotonic()
                if suficientes and len(respostas) >= suficientes:
                    bastou.set()

    threads = [threading.Thread(target=um, args=(a,), daemon=True) for a in alvos]
    for t in threads:
        t.start()
    fim = time.monotonic() + limite
    for t in threads:
        if bastou.is_set():
            break
        t.join(max(0.0, fim - time.monotonic()))
    with trava:
        return dict(respostas)


def descrever_diferenca(diferenca_s: float) -> str:
    """``atrasado 183 dias`` / ``adiantado 2 horas``."""
    sentido = "atrasado" if diferenca_s > 0 else "adiantado"
    s = abs(diferenca_s)
    if s >= 86400:
        quantidade, unidade = round(s / 86400), "dia"
    elif s >= 3600:
        quantidade, unidade = round(s / 3600), "hora"
    else:
        quantidade, unidade = max(1, round(s / 60)), "minuto"
    return f"{sentido} {quantidade} {unidade}{'s' if quantidade != 1 else ''}"


def sincronizar(
    executor: Any,
    servidores_dhcp: list[str] | None = None,
    ntp: Callable[[str], float | None] = consultar_ntp,
    http: Callable[[str], float | None] = data_http,
    agora: Callable[[], float] = time.time,
) -> ResultadoRelogio:
    """Acerta o relógio do sistema pela primeira fonte que funcionar. Nunca levanta exceção; nunca toca no BIOS."""
    resultado = ResultadoRelogio()
    with _trava:
        try:
            _sincronizar(executor, resultado, servidores_dhcp or [], ntp, http, agora)
        except Exception as erro:  # noqa: BLE001 - o relógio nunca derruba a verificação de rede
            resultado.tentativas.append(f"erro inesperado: {type(erro).__name__}: {erro}")
    resultado.hora_sistema = datetime.fromtimestamp(agora()).strftime("%d/%m/%Y %H:%M")
    return resultado


def _sincronizar(executor: Any, resultado: ResultadoRelogio, dhcp: list[str], ntp: Callable[[str], float | None],
                 http: Callable[[str], float | None], agora: Callable[[], float]) -> None:  # fmt: skip
    if getattr(executor, "origem", "real") != "real":
        resultado.tentativas.append("relógio não conferido (replay/simulação: nada é alterado nesta máquina)")
        resultado.ok = True
        return
    servidores = list(dict.fromkeys([*dhcp, *SERVIDORES_NTP]))
    respostas = _paralelo(ntp, servidores, TEMPO_NTP_S + 1, NTP_SUFICIENTES)
    if respostas:
        resultado.tentativas.append(f"NTP: {len(respostas)} de {len(servidores)} servidor(es) responderam")
        _aplicar(executor, resultado, "NTP (" + ", ".join(sorted(respostas)[:3]) + ")", median(respostas.values()), agora)
        return
    resultado.tentativas.append(f"NTP: nenhum dos {len(servidores)} servidores respondeu (porta UDP 123 bloqueada?)")
    datas = _paralelo(http, list(URLS_DATA_HTTP), TEMPO_HTTP_S + 1)
    if len(datas) >= 2:
        valores = sorted(datas.values())
        meio = median(valores)
        concordam = [v for v in valores if abs(v - meio) <= CONCORDANCIA_HTTP_S]
        if len(concordam) >= 2:
            resultado.tentativas.append(f"HTTP: {len(concordam)} site(s) concordam na hora (cabeçalho Date)")
            _aplicar(executor, resultado, "cabeçalho Date de sites HTTP", median(concordam), agora)
            return
        resultado.tentativas.append("HTTP: os sites não concordam na hora (proxy ou portal da rede?)")
    else:
        resultado.tentativas.append(f"HTTP: {len(datas)} site(s) responderam como esperado; são precisos dois que concordem")
    hora = hora_idrac(executor)
    if hora is not None and hora >= data_minima() and abs(hora - agora()) >= MESMA_ORIGEM_S:
        resultado.tentativas.append("iDRAC: hora lida pelo IPMI (ipmitool -Z sel time get)")
        _aplicar(executor, resultado, "relógio do iDRAC", hora - time.monotonic(), agora)
        return
    if hora is None:
        resultado.tentativas.append("iDRAC: hora indisponível")
    elif hora < data_minima():
        resultado.tentativas.append("iDRAC: a hora dele também é anterior a esta versão do MSscan (ignorada)")
    else:
        resultado.tentativas.append("iDRAC: mesma hora do sistema (provavelmente herdada do BIOS: não confirma nada)")
    if agora() < data_minima():
        resultado.precisa_tecnico = True
        resultado.mensagem = ("Não foi possível acertar o relógio pela rede e a data do servidor está errada (anterior a esta "
                              "versão do MSscan). Informe a data e a hora corretas.")  # fmt: skip
    else:
        resultado.mensagem = "Relógio não conferido pela rede (NTP, HTTP e iDRAC sem resposta); a data do sistema parece plausível."


def _aplicar(executor: Any, resultado: ResultadoRelogio, fonte: str, deslocamento: float, agora: Callable[[], float]) -> None:
    """``deslocamento``: hora certa − ``time.monotonic()``. A diferença é medida agora, sob a trava (A2)."""
    diferenca = time.monotonic() + deslocamento - agora()
    resultado.ok, resultado.fonte, resultado.diferenca_s = True, fonte, round(diferenca, 1)
    if abs(diferenca) < AJUSTAR_ACIMA_S:
        return
    alvo = int(agora() + diferenca)
    rc, _, erro = executor.rodar(["date", "-u", "-s", f"@{alvo}"], timeout=10)
    if rc != 0:
        resultado.ok = False
        resultado.mensagem = f"Hora certa obtida por {fonte}, mas o relógio do sistema não pôde ser ajustado: {erro.strip() or rc}"
        return
    resultado.ajustado = True
    if abs(diferenca) >= AVISAR_ACIMA_S:
        resultado.mensagem = (f"O relógio do servidor estava {descrever_diferenca(diferenca)} e foi ajustado ({fonte}). "
                              "Recomenda-se corrigir a data no BIOS.")  # fmt: skip
    else:
        resultado.mensagem = f"Relógio do sistema ajustado ({descrever_diferenca(diferenca)}; {fonte})."


def servidores_ntp_dhcp(executor: Any) -> list[str]:
    """NTP informado pelo DHCP da rede do cliente (``nmcli``), nas placas físicas com IP. Nunca levanta exceção."""
    from msscan.acoes.ntp import SERVIDOR_PADRAO, sugestoes

    try:
        return [s["servidor"] for s in sugestoes(executor) if s["servidor"] != SERVIDOR_PADRAO]
    except Exception:  # noqa: BLE001
        return []


def conferir(executor: Any, forcar: bool = False, **kwargs: Any) -> ResultadoRelogio:
    """Relógio conferido uma vez por sessão: depois de um acerto, só refaz com ``forcar`` ("Tentar novamente").

    Uma nova conferência que não achou fonte nunca apaga um acerto anterior (NTP, HTTP, iDRAC ou técnico) (M1).
    """
    global _ultimo
    with _trava:
        if _ultimo is not None and _ultimo.ok and not forcar:
            return _ultimo
        if getattr(executor, "origem", "real") == "real":
            kwargs.setdefault("servidores_dhcp", servidores_ntp_dhcp(executor))
        resultado = sincronizar(executor, **kwargs)
        if not resultado.ok and _ultimo is not None and _ultimo.ok:
            _ultimo.tentativas.append("nova conferência sem fonte de hora; vale o acerto anterior")
            return _ultimo
        _ultimo = resultado
        return resultado


def registrar_tecnico(resultado: ResultadoRelogio) -> None:
    """Hora informada pelo técnico vale como acerto da sessão (não pergunta de novo)."""
    global _ultimo
    with _trava:
        _ultimo = resultado


def esquecer() -> None:
    """Para os testes: esquece o acerto desta sessão."""
    global _ultimo
    with _trava:
        _ultimo = None


def ajustar_pelo_tecnico(executor: Any, texto: str, agora: Callable[[], float] = time.time) -> ResultadoRelogio:
    """Data e hora digitadas pelo técnico (``dd/mm/aaaa hh:mm``, hora local do sistema). Só o relógio do sistema."""
    resultado = ResultadoRelogio(fonte="técnico")
    try:
        informada = datetime.strptime(texto.strip(), "%d/%m/%Y %H:%M").timestamp()
    except ValueError:
        resultado.mensagem = "Data e hora inválidas: use dd/mm/aaaa hh:mm (ex.: 13/10/2026 14:30)."
        return resultado
    if informada < data_minima():
        resultado.mensagem = f"Data anterior a esta versão do MSscan ({date.fromisoformat(__data_versao__):%d/%m/%Y}): confira."
        return resultado
    with _trava:
        _aplicar(executor, resultado, "data e hora informadas pelo técnico", informada - time.monotonic(), agora)
        resultado.tentativas.append("data e hora informadas pelo técnico")
        resultado.hora_sistema = datetime.fromtimestamp(agora()).strftime("%d/%m/%Y %H:%M")
        if resultado.ok:
            registrar_tecnico(resultado)
    return resultado


__all__ = ["ResultadoRelogio", "ajustar_pelo_tecnico", "conferir", "consultar_ntp", "data_http", "data_minima",
           "descrever_diferenca", "esquecer", "hora_idrac", "parse_sel_time", "servidores_ntp_dhcp", "sincronizar"]

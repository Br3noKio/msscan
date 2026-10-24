"""Relógio antes de qualquer HTTPS (CLAUDE.md 28.1 e 28.3): atrasado dias/meses/anos, NTP e HTTP bloqueados, iDRAC, técnico."""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from msscan.core.executor import validar_dados_cliente
from msscan.rede import relogio

AGORA_REAL = 1_791_900_000.0  # 13/10/2026 (UTC), depois da data desta versão
DIA = 86400


class ExecutorFalso:
    """Guarda os comandos; ``date`` sempre funciona; ``ipmitool sel time get`` devolve ``sel``."""

    origem = "real"

    def __init__(self, sel: str | None = None, date_rc: int = 0) -> None:
        self.comandos: list[list[str]] = []
        self.sel = sel
        self.date_rc = date_rc

    def rodar(self, cmd: list[str], timeout: float = 10) -> tuple[int, str, str]:
        self.comandos.append(cmd)
        if cmd[0] == "date":
            return self.date_rc, "", "date: sem permissão" if self.date_rc else ""
        if cmd[:4] == ["ipmitool", "sel", "time", "get"]:
            return (0, self.sel, "") if self.sel else (1, "", "sem BMC")
        return 1, "", "inesperado"


def _fonte(hora: float | None):
    """Fonte (NTP ou HTTP) que responde ``hora`` para qualquer alvo; ``None`` = bloqueada."""
    return lambda _alvo: hora


def _sincronizar(ex: ExecutorFalso, sistema: float, ntp=None, http=None) -> relogio.ResultadoRelogio:
    # O sistema está em ``sistema``; time.time() real é usado nas diferenças das fontes, então a fonte devolve
    # time.time() + (AGORA_REAL - sistema) para simular "hora certa" quando o sistema está deslocado.
    deslocamento = AGORA_REAL - sistema
    return relogio.sincronizar(
        ex,
        ntp=ntp or _fonte(None),
        http=http or _fonte(None),
        agora=lambda: time.time() - deslocamento,  # hora (errada) do sistema
    )


def _certa(_alvo: str) -> float:
    """Fonte que sabe a hora certa (a do processo de teste)."""
    return time.time()


@pytest.mark.parametrize("atraso_dias", [3, 183, 3 * 365])
def test_ntp_acerta_relogio_atrasado(atraso_dias: int) -> None:
    ex = ExecutorFalso()
    sistema = AGORA_REAL - atraso_dias * DIA
    r = _sincronizar(ex, sistema, ntp=_certa)
    assert r.ok and r.ajustado and r.fonte.startswith("NTP")
    assert abs(r.diferenca_s - atraso_dias * DIA) < 5
    assert ex.comandos[0][:3] == ["date", "-u", "-s"]
    assert abs(int(ex.comandos[0][3].lstrip("@")) - time.time()) < 5  # o relógio vai para a hora CERTA
    assert f"atrasado {atraso_dias} dias" in r.mensagem and "corrigir a data no BIOS" in r.mensagem
    assert not any(c[0] in ("hwclock", "timedatectl") for c in ex.comandos)


def test_relogio_certo_nao_mexe() -> None:
    ex = ExecutorFalso()
    r = relogio.sincronizar(ex, ntp=lambda _a: time.time() + 2, http=_fonte(None))
    assert r.ok and not r.ajustado and ex.comandos == []


def test_ntp_bloqueado_usa_http_com_dois_sites_concordando() -> None:
    ex = ExecutorFalso()
    sistema = AGORA_REAL - 183 * DIA
    r = _sincronizar(ex, sistema, http=_certa)
    assert r.ok and r.ajustado and "HTTP" in r.fonte
    assert any("NTP: nenhum" in t for t in r.tentativas)


def test_http_um_site_so_nao_muda_o_relogio() -> None:
    """Portal cativo sozinho (um site com hora qualquer) nunca muda o relógio."""
    ex = ExecutorFalso()
    respostas = {relogio.URLS_DATA_HTTP[0]: time.time() + 999 * DIA}
    r = relogio.sincronizar(ex, ntp=_fonte(None), http=lambda url: respostas.get(url), agora=time.time)
    assert not r.ajustado and not any(c[0] == "date" for c in ex.comandos)


def test_http_sites_discordando_nao_muda_o_relogio() -> None:
    ex = ExecutorFalso()
    horas = iter([0, 400 * DIA, 800 * DIA, 1200 * DIA])
    deslocamentos = {url: next(horas) for url in relogio.URLS_DATA_HTTP}
    r = relogio.sincronizar(ex, ntp=_fonte(None), http=lambda url: time.time() + deslocamentos[url], agora=time.time)
    assert not r.ajustado
    assert any("não concordam" in t for t in r.tentativas)


def test_sem_ntp_nem_http_usa_hora_do_idrac() -> None:
    """Formato REAL do ipmitool 1.8.19 com -Z (R640): ano com 2 dígitos e GMT."""
    from datetime import datetime, timezone

    sel = datetime.fromtimestamp(time.time(), timezone.utc).strftime("%m/%d/%y %H:%M:%S GMT")
    ex = ExecutorFalso(sel=sel)
    r = _sincronizar(ex, AGORA_REAL - 183 * DIA)
    assert r.ok and r.ajustado and r.fonte == "relógio do iDRAC"
    assert ["ipmitool", "-Z", "sel", "time", "get"] in ex.comandos


def test_idrac_com_a_mesma_hora_do_sistema_nao_confirma() -> None:
    """A3: o iDRAC herda a hora do BIOS; igual à do sistema não é confirmação."""
    from datetime import datetime, timezone

    ex = ExecutorFalso(sel=datetime.fromtimestamp(time.time(), timezone.utc).strftime("%m/%d/%y %H:%M:%S GMT"))
    r = relogio.sincronizar(ex, ntp=_fonte(None), http=_fonte(None), agora=time.time)
    assert not r.ok and any("mesma hora do sistema" in t for t in r.tentativas)


@pytest.mark.parametrize(("saida", "esperado"), [
    ("10/14/26 15:11:03 GMT", "2026-10-14T15:11:03+00:00"),  # saída real com -Z (R640)
    ("10/14/26 12:11:03 -03", "2026-10-14T15:11:03+00:00"),  # saída real sem -Z (R640, fuso da ISO)
    ("10/14/2026 15:11:03 UTC", "2026-10-14T15:11:03+00:00"),
    ("10/14/26 12:11:03", None),  # sem fuso: local ou UTC? nunca adivinhar
    ("lixo", None),
])
def test_parse_sel_time(saida: str, esperado: str | None) -> None:
    from datetime import datetime, timezone

    valor = relogio.parse_sel_time(saida)
    assert (datetime.fromtimestamp(valor, timezone.utc).isoformat() if valor is not None else None) == esperado


def test_idrac_tambem_atrasado_e_ignorado_e_pergunta_ao_tecnico() -> None:
    ex = ExecutorFalso(sel="01/01/20 00:00:00 GMT")
    sistema = relogio.data_minima() - 183 * DIA
    r = relogio.sincronizar(ex, ntp=_fonte(None), http=_fonte(None), agora=lambda: sistema)
    assert not r.ok and r.precisa_tecnico and not r.ajustado
    assert any("anterior a esta versão" in t for t in r.tentativas)
    assert not any(c[0] == "date" for c in ex.comandos)


def test_sem_fonte_com_data_plausivel_nao_pergunta() -> None:
    ex = ExecutorFalso()
    r = relogio.sincronizar(ex, ntp=_fonte(None), http=_fonte(None), agora=time.time)
    assert not r.ok and not r.precisa_tecnico


def test_date_falhou_nao_finge_sucesso() -> None:
    ex = ExecutorFalso(date_rc=1)
    r = _sincronizar(ex, AGORA_REAL - 183 * DIA, ntp=_certa)
    assert not r.ok and not r.ajustado and "não pôde ser ajustado" in r.mensagem


def test_replay_nunca_mexe_no_relogio() -> None:
    ex = ExecutorFalso()
    ex.origem = "replay"
    r = relogio.sincronizar(ex, ntp=_certa)
    assert r.ok and ex.comandos == []


def test_ajuste_pelo_tecnico() -> None:
    ex = ExecutorFalso()
    assert "inválidas" in relogio.ajustar_pelo_tecnico(ex, "ontem").mensagem
    assert "anterior a esta versão" in relogio.ajustar_pelo_tecnico(ex, "01/01/2020 10:00").mensagem
    assert ex.comandos == []
    r = relogio.ajustar_pelo_tecnico(ex, "13/10/2030 10:00", agora=lambda: relogio.data_minima() - 183 * DIA)
    assert r.ok and r.ajustado and ex.comandos[0][:3] == ["date", "-u", "-s"]


def test_executor_recusa_mexer_no_relogio_do_bios() -> None:
    assert "proibido" in validar_dados_cliente(["hwclock", "--systohc"])
    assert "proibido" in validar_dados_cliente(["/usr/bin/timedatectl", "set-time", "2026-01-01"])
    assert validar_dados_cliente(["date", "-u", "-s", "@1791900000"]) == ""


def test_duas_threads_nunca_somam_a_correcao() -> None:
    """A2: monitor e "Tentar novamente" ao mesmo tempo: a correção de 183 dias é aplicada uma vez só."""
    import threading

    estado = {"sistema": -183 * DIA}  # deslocamento do relógio do sistema em relação à hora certa

    class ExecutorRelogio(ExecutorFalso):
        def rodar(self, cmd, timeout=10):
            if cmd[0] == "date":
                estado["sistema"] = int(cmd[3][1:]) - time.time()
            return super().rodar(cmd, timeout)

    ex = ExecutorRelogio()

    def ntp_lento(_alvo: str) -> float:
        time.sleep(0.05)
        return time.time()

    threads = [threading.Thread(target=relogio.sincronizar, args=(ex,),
                                kwargs={"ntp": ntp_lento, "http": _fonte(None), "agora": lambda: time.time() + estado["sistema"]})
               for _ in range(2)]  # fmt: skip
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert abs(estado["sistema"]) < 5
    assert sum(c[0] == "date" for c in ex.comandos) == 1


def test_falha_nova_nao_apaga_acerto_anterior() -> None:
    """M1: "Tentar novamente" sem fonte de hora mantém o acerto do técnico."""
    ex = ExecutorFalso()
    relogio.registrar_tecnico(relogio.ResultadoRelogio(ok=True, fonte="técnico"))
    r = relogio.conferir(ex, forcar=True, ntp=_fonte(None), http=_fonte(None), servidores_dhcp=[])
    assert r.ok and r.fonte == "técnico"


def _servidor_ntp(resposta_de: Callable):
    """Servidor UDP local que responde ao pedido com ``resposta_de(pedido)``. Devolve a porta."""
    import socket
    import threading

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))

    def atender() -> None:
        pedido, origem = s.recvfrom(512)
        s.sendto(resposta_de(pedido), origem)
        s.close()

    threading.Thread(target=atender, daemon=True).start()
    return s.getsockname()[1]


def _pacote_ntp(pedido: bytes, li: int = 0, estrato: int = 2, eco: bool = True) -> bytes:
    import struct

    segundos = int(time.time()) + 2208988800
    primeiro = (li << 6) | (4 << 3) | 4
    return (bytes([primeiro, estrato]) + 22 * b"\0" + (pedido[40:48] if eco else 8 * b"\1") + 8 * b"\0"
            + struct.pack("!II", segundos, 0))  # fmt: skip


@pytest.mark.parametrize(("kwargs", "valido"), [
    ({}, True),
    ({"li": 3}, False),  # alarme: servidor sem sincronia
    ({"estrato": 16}, False),  # estrato 16: sem sincronia
    ({"estrato": 0}, False),  # "kiss of death"
    ({"eco": False}, False),  # não é resposta a este pedido
])
def test_ntp_recusa_servidor_sem_sincronia(kwargs: dict, valido: bool) -> None:
    porta = _servidor_ntp(lambda pedido: _pacote_ntp(pedido, **kwargs))
    hora = relogio.consultar_ntp("127.0.0.1", tempo=2, porta=porta)
    assert (hora is not None and abs(hora - time.time()) < 5) is valido


def test_http_portal_que_responde_tudo_nao_vale() -> None:
    """M3: proxy transparente/portal com o mesmo Date em todos os sites, mas sem a resposta conhecida de cada um."""
    import http.server
    import threading

    class Portal(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html>Bem-vindo ao Wi-Fi</html>")

        def log_message(self, *_a) -> None:
            return None

    servidor = http.server.HTTPServer(("127.0.0.1", 0), Portal)
    threading.Thread(target=servidor.handle_request, daemon=True).start()
    url = f"http://127.0.0.1:{servidor.server_port}/success.txt"
    relogio.RESPOSTA_ESPERADA[url] = (200, b"success")
    try:
        assert relogio.data_http(url, tempo=3) is None
    finally:
        relogio.RESPOSTA_ESPERADA.pop(url)
        servidor.server_close()


def test_descrever_diferenca() -> None:
    assert relogio.descrever_diferenca(183 * DIA) == "atrasado 183 dias"
    assert relogio.descrever_diferenca(-2 * 3600) == "adiantado 2 horas"
    assert relogio.descrever_diferenca(60) == "atrasado 1 minuto"

"""Internet em qualquer servidor (CLAUDE.md 28.1 e 28.3): relógio antes do HTTPS, DNS alternativo, outra placa,
IPv4 preferido, sem internet sem travar."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from msscan.core.executor import RespostaHttp
from msscan.rede import caminho, relogio
from msscan.rede.relogio import ResultadoRelogio
from msscan.rede.verificacao import SEM_INTERNET, verificar_rede
from tests.test_rede import URL_DELL, URL_IA, ReplayAlterado

ISO = Path(__file__).resolve().parent.parent / "iso" / "config"


class RedeReal(ReplayAlterado):
    """Replay do R640 fingindo ser a máquina real (os consertos do caminho só rodam nela); ``nmcli`` aceito."""

    origem = "real"

    def __init__(self, http_antes: dict, http_depois: dict | None = None) -> None:
        super().__init__(http=http_antes)
        self.http_depois = http_depois
        self.nmcli: list[list[str]] = []

    def _rodar(self, comando, timeout):
        if comando[:3] == ["nmcli", "device", "modify"]:
            self.nmcli.append(comando)
            if self.http_depois is not None:
                self.troca_http = self.http_depois
            return 0, "", "", 0.0
        return super()._rodar(comando, timeout)


def _falha(tipo: str) -> dict:
    return {url: RespostaHttp(url, None, tipo, "x") for url in (URL_DELL, URL_IA)}


OK = {url: RespostaHttp(url, 200) for url in (URL_DELL, URL_IA)}


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    """Sem rede de verdade nos testes: o relógio devolve o que cada teste mandar."""
    estado = {"r": ResultadoRelogio(ok=True, fonte="NTP")}
    monkeypatch.setattr(relogio, "conferir", lambda _ex, forcar=False, **_k: estado["r"])
    return estado


def test_relogio_atrasado_meses_e_acertado_e_avisado(relogio_fixo) -> None:
    relogio_fixo["r"] = ResultadoRelogio(ok=True, ajustado=True, fonte="NTP", diferenca_s=183 * 86400,
                                         mensagem="O relógio do servidor estava atrasado 183 dias e foi ajustado (NTP). "
                                                  "Recomenda-se corrigir a data no BIOS.")  # fmt: skip
    resultado = verificar_rede(RedeReal(OK))
    assert resultado.ok
    assert any("atrasado 183 dias" in d and "BIOS" in d for d in resultado.detalhes)
    assert resultado.relogio["ajustado"] is True


def test_relogio_impossivel_sem_fonte_pede_hora_ao_tecnico(relogio_fixo) -> None:
    relogio_fixo["r"] = ResultadoRelogio(ok=False, precisa_tecnico=True, hora_sistema="14/04/2026 08:00")
    resultado = verificar_rede(RedeReal(_falha("tls")))
    assert resultado.problema == SEM_INTERNET and resultado.precisa_hora
    assert "relógio" in resultado.causa


def test_dns_da_rede_falhou_usa_dns_alternativo(monkeypatch) -> None:
    monkeypatch.setattr(caminho, "consultar_dns", lambda servidor, *a, **k: servidor == "8.8.8.8")
    monkeypatch.setattr(caminho, "alcanca_internet", lambda *_a, **_k: False)
    executor = RedeReal(_falha("dns"), http_depois=OK)
    resultado = verificar_rede(executor)
    assert resultado.ok
    assert executor.nmcli == [["nmcli", "device", "modify", "eno3", "ipv4.dns", "8.8.8.8"]]
    assert any("DNS alternativo 8.8.8.8" in d for d in resultado.detalhes)


def test_dns_bloqueado_de_todo_jeito_explica(monkeypatch) -> None:
    monkeypatch.setattr(caminho, "consultar_dns", lambda *_a, **_k: False)
    resultado = verificar_rede(RedeReal(_falha("dns")))
    assert resultado.problema == SEM_INTERNET and "DNS" in resultado.titulo
    assert any("bloqueia DNS externo" in d for d in resultado.detalhes)


def test_replay_nunca_mexe_na_rede(monkeypatch) -> None:
    chamado = []
    monkeypatch.setattr(caminho, "consultar_dns", lambda *_a, **_k: chamado.append(1) or True)
    resultado = verificar_rede(ReplayAlterado(http=_falha("dns")))
    assert not resultado.ok and chamado == []


class ExecutorNmcli:
    origem = "real"

    def __init__(self) -> None:
        self.comandos: list[list[str]] = []

    def rodar(self, cmd, timeout=10):
        self.comandos.append(cmd)
        return 0, "", ""


def test_varias_placas_a_primeira_com_internet_vira_rota_padrao() -> None:
    ex = ExecutorNmcli()
    placa, detalhes = caminho.escolher_placa(ex, ["eno1", "eno3", "eno4"], testar=lambda p: p != "eno1")
    assert placa == "eno3"
    assert ex.comandos == [["nmcli", "device", "modify", "eno3", "ipv4.route-metric", "10"]]
    assert detalhes[0] == "eno1: sem saída para a internet"


def test_uma_placa_so_nao_mexe_na_rota() -> None:
    ex = ExecutorNmcli()
    assert caminho.escolher_placa(ex, ["eno3"], testar=lambda _p: True) == (None, [])
    assert ex.comandos == []


def test_iso_prefere_ipv4_e_dhcp_espera_mais() -> None:
    """IPv6 quebrado não atrasa nada: IPv4 primeiro (gai.conf); DHCP de 90 s com retentativas sem fim."""
    gai = (ISO / "includes.chroot/etc/gai.conf").read_text()
    assert any(linha.split() == ["precedence", "::ffff:0:0/96", "100"] for linha in gai.splitlines())
    nm = (ISO / "includes.chroot/etc/NetworkManager/conf.d/10-msscan.conf").read_text()
    assert "ipv4.dhcp-timeout=90" in nm and "autoconnect-retries-default=0" in nm
    assert "may-fail" not in nm.split("[connection]")[1]  # não é padrão global aceito pelo NetworkManager


def test_iso_nunca_sincroniza_o_relogio_do_bios() -> None:
    """Sem systemd-timesyncd, chrony, ntpsec ou openntpd (sincronizam o kernel, que copia a hora para o BIOS a cada
    11 min) e sem hwclock/timedatectl em nada que a ISO roda."""
    pacotes = [l.strip() for l in (ISO / "package-lists/msscan.list.chroot").read_text().splitlines()]
    assert not {"systemd-timesyncd", "chrony", "ntpsec", "ntp", "openntpd"} & set(pacotes)
    raiz = ISO.parent.parent
    for arquivo in [*ISO.rglob("*"), *(raiz / "msscan").rglob("*.py"), raiz / "main.py"]:
        if not arquivo.is_file() or arquivo.suffix in (".png", ".jpg", ".svg", ".woff2", ".gz"):
            continue
        texto = arquivo.read_text(encoding="utf-8", errors="ignore")
        for linha in texto.splitlines():
            if re.search(r"\b(hwclock|timedatectl)\b", linha) and not re.search(r"(nunca|sem |proibido|nada de|recusa)", linha, re.I):
                if arquivo.name != "executor.py" or "if base in" not in linha:
                    raise AssertionError(f"{arquivo}: {linha.strip()}")
    gancho = (ISO / "hooks/normal/9000-msscan.hook.chroot").read_text()
    assert "systemctl mask systemd-timesyncd.service" in gancho and "enable systemd-timesyncd" not in gancho


def test_iso_tem_firmware_das_placas_de_rede() -> None:
    pacotes = set((ISO / "package-lists/msscan.list.chroot").read_text().split())
    assert {"firmware-bnx2", "firmware-bnx2x", "firmware-misc-nonfree", "firmware-intel-misc", "firmware-qlogic",
            "firmware-netxen", "firmware-cavium", "firmware-realtek"} <= pacotes  # fmt: skip


def test_relogio_plausivel_mas_certificado_do_futuro_pede_a_hora(relogio_fixo) -> None:
    """A3: sem fonte de hora e data "plausível", mas o erro TLS diz que o certificado ainda não vale: é o relógio."""
    relogio_fixo["r"] = ResultadoRelogio(ok=False, precisa_tecnico=False, hora_sistema="14/04/2027 08:00")
    http = {url: RespostaHttp(url, None, "tls", "[SSL: CERTIFICATE_VERIFY_FAILED] certificate is not yet valid")
            for url in (URL_DELL, URL_IA)}  # fmt: skip
    resultado = verificar_rede(RedeReal(http))
    assert "relógio do servidor está errado" in resultado.causa and resultado.precisa_hora

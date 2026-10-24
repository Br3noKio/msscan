"""Nova interface da 1.0.0 (pedido de 09/10/2026): visão do servidor, sensores ao vivo, msscan-fetch, estrutura em
mosaico, papel de parede e contraste do vidro. Nada aqui grava no servidor: tudo vem do que o diagnóstico já leu ou de
um ``ipmitool sensor`` (só leitura, fora do histórico)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from msscan.core.executor import ExecutorReplay
from msscan.interface import fetch, visao
from msscan.interface.sessao import Sessao
from tests.test_contraste import razao, rgb, variavel
from tests.test_interface import R640, sessao_concluida

RAIZ = Path(__file__).parent.parent
ESTATICOS = RAIZ / "msscan/interface/estaticos"
HTML = (ESTATICOS / "index.html").read_text(encoding="utf-8")
CSS = (ESTATICOS / "app.css").read_text(encoding="utf-8")


def test_sensores_separados_e_nomes_repetidos_numerados():
    lista = [{"nome": "Temp", "valor": 37.0, "unidade": "degrees C", "discreto": False, "status": "ok", "limiares": {"ucr": 102.0}},
             {"nome": "Temp", "valor": 39.0, "unidade": "degrees C", "discreto": False, "status": "ok", "limiares": {"ucr": 102.0}},
             {"nome": "Fan1A", "valor": 6960.0, "unidade": "RPM", "discreto": False, "status": "ok", "limiares": {"lcr": 600.0}},
             {"nome": "Pwr Consumption", "valor": 96.0, "unidade": "Watts", "discreto": False, "status": "ok", "limiares": {"ucr": 912.0}},
             {"nome": "PS Redundancy", "valor": None, "unidade": "discrete", "discreto": True, "status": "0x0180", "limiares": {}}]  # fmt: skip
    s = visao.sensores(lista)
    assert [t["nome"] for t in s["temperaturas"]] == ["Temp 1", "Temp 2"] and s["temperatura_maxima"] == 39.0
    assert [v["nome"] for v in s["ventoinhas"]] == ["Fan1A"] and s["consumo"][0]["limite"] == 912.0


def test_visao_e_sensores_da_sessao_sem_encher_o_historico():
    sessao = sessao_concluida()
    try:
        antes = len(sessao.executor.historico)
        v = sessao.visao()
        servidor = v["servidor"]
        assert servidor["modelo"] == "PowerEdge R640" and servidor["geracao"] and servidor["bios"]
        assert len(servidor["processadores"]) == 2 and servidor["memoria"]["total_gb"] == 16
        assert [m["slot"] for m in servidor["memoria"]["slots"] if m["ocupado"]] == ["A1", "A2"]
        assert v["sensores"]["temperaturas"] and v["sensores"]["ventoinhas"]
        assert len(sessao.executor.historico) == antes  # leitura ao vivo fora do log técnico
    finally:
        sessao.encerrar()


def test_sensores_ao_vivo_pausam_durante_a_gravacao(monkeypatch):
    sessao = sessao_concluida()
    try:
        chamadas = []
        original = sessao.executor.rodar
        monkeypatch.setattr(sessao.executor, "rodar", lambda *a, **k: (chamadas.append(a[0]), original(*a, **k))[1])
        monkeypatch.setattr(sessao, "_motivo_energia_bloqueada", lambda: "gravação de firmware em andamento")
        sessao._sensores_lidos = 0.0
        v = sessao.visao()
        assert ["ipmitool", "sensor"] not in chamadas and v["sensores"]["pausa"] == "gravação de firmware em andamento"
    finally:
        sessao.encerrar()


def test_executor_registrar_false_so_tira_do_historico():
    executor = ExecutorReplay(R640)
    executor.rodar(["ipmitool", "sensor"], registrar=False)
    assert executor.historico == []
    executor.rodar(["ipmitool", "raw", "0x06", "0x01"], registrar=False)  # escrita recusada: sempre registrada
    assert executor.historico and executor.historico[-1].rc != 0


def test_msscan_fetch_alinhado_e_sem_inventar_dados():
    texto = fetch.montar({"fabricante": "Dell Inc.", "modelo": "PowerEdge R640", "processadores": [], "memoria": {"slots": []}},
                         "Debian GNU/Linux 13 (trixie)", "1.0.0", cor=False)  # fmt: skip
    linhas = texto.splitlines()
    assert "msscan@poweredge-r640" in linhas[0] and any("Processadores  —" in l for l in linhas)
    assert {len(l) for l in fetch.RACK} == {fetch.LARGURA_RACK}
    colorido = fetch.montar(None, "", cor=True)
    assert "\x1b[38;2;88;166;255m" in colorido and "\r\n" in colorido


def test_terminal_mostra_o_msscan_fetch_ao_abrir(tmp_path):
    from msscan.interface.terminal import Terminal

    terminal = Terminal("1.0.0", comando=("/bin/sh", "-c", "sleep 1"), apresentacao=lambda: "RESUMO-DA-MAQUINA", liberado=True)
    try:
        assert terminal.abrir()[0]
        dados, _, _ = terminal.ler(0, 2)
        assert b"RESUMO-DA-MAQUINA" in bytes(dados)
    finally:
        terminal.encerrar()


def test_estrutura_em_mosaico_com_todas_as_funcoes():
    espacos = re.findall(r'<span class="num" aria-hidden="true">(\d)</span><span data-icone="[a-z-]+" aria-hidden="true"></span>'
                         r'<span class="rotulo-espaco" data-t="[a-z_.]+"', HTML)  # fmt: skip
    assert espacos == list("12345678")  # 22/10/2026: aba Discos (CLAUDE.md 31) na 3ª posição
    ordem = re.findall(r'data-tela="([a-z]+)" id="aba-', HTML)
    assert ordem == ["inicio", "diagnostico", "discos", "ia", "chat", "atualizacoes", "relatorios", "terminal", "sobre"]
    for id_ in ("painel-servidor", "painel-rapido", "doca", "sensores-vivo", "notif-silenciar", "notif-limpar", "pilula-temp", "pilula-fontes",
                "pilula-idrac", "pilula-ia", "pilula-ssh", "menu-energia", "botao-desligar", "botao-reiniciar", "relogio", "doca-refazer",
                "doca-relatorio", "doca-terminal", "doca-drivers", "doca-downloads", "doca-config", "faixa-seguranca", "faixa-ssh"):
        assert f'id="{id_}"' in HTML, id_


def test_sem_backdrop_filter_e_com_modo_leve():
    assert "backdrop-filter" not in CSS.replace("nada de backdrop-filter", "")
    assert "body.leve .painel" in CSS and "prefers-reduced-motion" in CSS


def test_papel_de_parede_pronto_e_leve():
    for nome, limite in (("fundo.jpg", 400_000), ("fundo-desfocado.jpg", 150_000)):
        assert 0 < (ESTATICOS / nome).stat().st_size < limite, nome


@pytest.mark.parametrize("texto", ["texto", "texto-sec", "azul-link", "ok", "alerta", "falha"])
def test_contraste_sobre_o_vidro_no_pior_ponto_do_fundo(texto):
    pil = pytest.importorskip("PIL.Image")
    for imagem, camada in (("fundo-desfocado.jpg", "vidro"), ("fundo.jpg", "vidro-pilula")):
        pixels = pil.open(ESTATICOS / imagem).convert("RGB").getdata()
        mais_claro = max(pixels, key=lambda c: 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2])
        fundo = rgb(variavel(camada), tuple(float(c) for c in mais_claro))
        assert razao(rgb(variavel(texto)), fundo) >= 4.5, (imagem, texto)


def test_fontes_e_comando_na_iso():
    pacotes = (RAIZ / "iso/config/package-lists/msscan.list.chroot").read_text(encoding="utf-8")
    assert "fonts-inter" in pacotes and "fonts-jetbrains-mono" in pacotes
    comando = RAIZ / "iso/config/includes.chroot/usr/local/bin/msscan-fetch"
    assert comando.stat().st_mode & 0o111 and "msscan.interface.fetch" in comando.read_text(encoding="utf-8")

"""Chave da IA pelo celular (CLAUDE.md 10d): HTTPS com certificado gerado na hora, token de uso único, código de
verificação, limite de tentativas, 5 minutos, sinal de vida da tela, firewall do quiosque só durante o pareamento e
nenhuma chave em estado, registro ou relatório.

Servidor HTTPS real em 127.0.0.1 (porta livre); o ``nft`` é simulado pelo ``ExecutorFalso``.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from msscan.interface import pareamento as modulo
from msscan.interface.pareamento import (
    ACEITA,
    AGUARDANDO,
    DURACAO_S,
    ENCERRADA,
    ERRO,
    EXPIRADA,
    MAX_TENTATIVAS,
    RECEBIDA,
    RECUSADA,
    SINAL_S,
    TENTATIVAS,
    FirewallQuiosque,
    Pareamento,
    gerar_certificado,
)
from tests.conftest import ExecutorFalso

CHAVE = "AIzaSy-CHAVE-DO-CELULAR-DE-TESTE-123"
LINK = "https://aistudio.google.com/apikey?x=1&y=<2>"


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def nft(porta: int, acao: str) -> tuple[str, ...]:
    elemento = f"{{ {porta} timeout {DURACAO_S}s }}" if acao == "add" else f"{{ {porta} }}"
    return ("nft", acao, "element", "inet", "msscan_quiosque", "pareamento", elemento)


class Relogio:
    def __init__(self):
        self.agora = 1000.0

    def __call__(self):
        return self.agora


def aguardar(condicao, segundos: float = 5.0) -> bool:
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.05)
    return condicao()


@pytest.fixture
def novo(tmp_path):
    """Fábrica de pareamentos já iniciados (encerrados no fim do teste)."""
    criados: list[Pareamento] = []

    def fabricar(ativo: bool = True, nft_rc: int = 0, relogio=None) -> tuple[Pareamento, ExecutorFalso, list]:
        porta = porta_livre()
        executor = ExecutorFalso({nft(porta, "add"): (nft_rc, "", "Error: No such file or directory" if nft_rc else ""),
                                  nft(porta, "delete"): (0, "", "")})  # fmt: skip
        fins: list[Pareamento] = []
        p = Pareamento("gemini", "Google Gemini", LINK, "127.0.0.1", FirewallQuiosque(executor, ativo), porta=porta,
                       relogio=relogio or time.monotonic, ao_terminar=fins.append)  # fmt: skip
        criados.append(p)
        return p, executor, fins

    yield fabricar
    for p in criados:
        p.encerrar()


def pedir(p: Pareamento, metodo: str, caminho: str, corpo: str | None = None) -> tuple[int, dict[str, str], str]:
    contexto = ssl.create_default_context()
    contexto.check_hostname, contexto.verify_mode = False, ssl.CERT_NONE
    conexao = http.client.HTTPSConnection("127.0.0.1", p.porta, timeout=5, context=contexto)
    cabecalhos = {"Content-Type": "application/x-www-form-urlencoded"} if corpo is not None else {}
    conexao.request(metodo, caminho, body=corpo, headers=cabecalhos)
    resposta = conexao.getresponse()
    dados = resposta.read().decode("utf-8")
    conexao.close()
    return resposta.status, {k.lower(): v for k, v in resposta.getheaders()}, dados


def porta_fechada(p: Pareamento) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", p.porta), timeout=0.5):
            return False
    except OSError:
        return True


def comandos(executor: ExecutorFalso) -> list[tuple[str, ...]]:
    return [tuple(e.comando) for e in executor.historico]


# --- Abertura --------------------------------------------------------------------------------------


def test_abre_https_com_token_codigo_e_firewall(novo):
    p, executor, _ = novo()
    assert p.iniciar() == (True, "aguardando o celular")
    estado = p.estado()
    assert estado["situacao"] == AGUARDANDO and estado["url"] == f"https://127.0.0.1:{p.porta}/p/{p.token}"
    assert len(p.token) >= 43 and len(estado["codigo"]) == 6 and estado["codigo"].isdigit()
    assert 0 < estado["restante_s"] <= DURACAO_S and len(estado["impressao"]) == 23  # 8 bytes "AB:CD:..."
    assert comandos(executor) == [nft(p.porta, "add")]
    # certificado autoassinado gerado na hora, com o IP no SAN
    der = ssl.get_server_certificate(("127.0.0.1", p.porta))
    assert "BEGIN CERTIFICATE" in der


def test_nao_faz_dns_reverso_do_ip(novo, monkeypatch):
    """HTTPServer.server_bind chama socket.getfqdn(ip): sem DNS reverso na rede, travava ~10 s (visto no QEMU)."""
    monkeypatch.setattr(socket, "getfqdn", lambda *_: (_ for _ in ()).throw(AssertionError("DNS reverso")))
    p, _, _ = novo()
    assert p.iniciar() == (True, "aguardando o celular")
    p.encerrar()


def test_firewall_fora_da_iso_nao_mexe_em_nada(novo):
    p, executor, _ = novo(ativo=False)
    assert p.iniciar()[0] and comandos(executor) == []
    p.encerrar()
    assert comandos(executor) == []


def test_firewall_falhou_nao_abre(novo):
    p, executor, fins = novo(nft_rc=1)
    ok, mensagem = p.iniciar()
    assert not ok and "não foi possível abrir a porta" in mensagem and p.situacao == ERRO
    assert porta_fechada(p) and fins == [p] and p._pasta is None


def test_porta_ocupada(novo):
    p, _, _ = novo()
    with socket.socket() as ocupante:
        ocupante.bind(("127.0.0.1", p.porta))
        ocupante.listen()
        ok, mensagem = p.iniciar()
    assert not ok and "não foi possível abrir o pareamento" in mensagem and p.situacao == ERRO


# --- Página e envio --------------------------------------------------------------------------------


def test_pagina_com_codigo_link_escapado_e_cabecalhos(novo):
    p, _, _ = novo()
    p.iniciar()
    status, cabecalhos, html = pedir(p, "GET", f"/p/{p.token}")
    assert status == 200 and f"{p.codigo[:3]} {p.codigo[3:]}" in html
    assert "y=&lt;2&gt;" in html and "<2>" not in html and "<script" not in html
    assert cabecalhos["cache-control"] == "no-store" and "default-src 'none'" in cabecalhos["content-security-policy"]
    assert cabecalhos["referrer-policy"] == "no-referrer" and 'type=\'password\'' in html


def test_token_errado_conta_tentativa_e_fecha_no_limite(novo):
    p, executor, fins = novo()
    p.iniciar()
    for caminho in ("/p/errado", "/", f"/p/{p.token}x", f"/x/{p.token}"):
        assert pedir(p, "GET", caminho)[0] == 404
    assert p.estado()["tentativas_invalidas"] == 4 and p.situacao == AGUARDANDO
    for _ in range(MAX_TENTATIVAS - 3):
        try:
            pedir(p, "GET", "/p/errado")
        except OSError:
            break
    assert aguardar(lambda: p.situacao == TENTATIVAS) and aguardar(lambda: porta_fechada(p))
    assert fins == [p] and nft(p.porta, "delete") in comandos(executor)


def test_chave_recebida_fecha_a_porta_e_espera_o_tecnico(novo):
    p, executor, fins = novo()
    p.iniciar()
    status, _, html = pedir(p, "POST", f"/p/{p.token}", "chave=" + quote(f"  {CHAVE}\r\n"))
    assert status == 200 and "Chave enviada" in html and CHAVE not in html
    assert p.situacao == RECEBIDA and p.estado()["ip_celular"] == "127.0.0.1" and p.estado()["url"] == ""
    assert aguardar(lambda: porta_fechada(p)) and aguardar(lambda: nft(p.porta, "delete") in comandos(executor))
    assert fins == []  # ainda falta a decisão do técnico
    assert CHAVE not in json.dumps(p.estado()) and CHAVE not in json.dumps(p.registro())
    assert p.aceitar() == CHAVE and p.aceitar() is None
    assert p.situacao == ACEITA and fins == [p] and p.registro()["resultado"] == ACEITA
    assert CHAVE not in json.dumps(executor.historico_dict())


def test_uso_unico(novo):
    p, _, _ = novo()
    p.iniciar()
    assert p._receber(CHAVE, "10.0.0.5") == ""
    assert "já foi usado" in p._receber("outra-chave", "10.0.0.6") and p.ip_celular == "10.0.0.5"


def test_recusar_descarta_a_chave(novo):
    p, _, fins = novo()
    p.iniciar()
    p._receber(CHAVE, "10.0.0.5")
    p.recusar()
    assert p.situacao == RECUSADA and p._chave is None and p.aceitar() is None and fins == [p]


@pytest.mark.parametrize(("corpo", "mensagem"), [
    ("chave=", "Cole a chave"), ("chave=" + quote("chave com ç"), "caracteres inválidos"),
    ("chave=" + "a" * 600, "caracteres inválidos"), ("outro=1", "Cole a chave"),
])  # fmt: skip
def test_chave_invalida_nao_e_aceita(novo, corpo, mensagem):
    p, _, _ = novo()
    p.iniciar()
    status, _, html = pedir(p, "POST", f"/p/{p.token}", corpo)
    assert status == 400 and mensagem in html and p.situacao == AGUARDANDO


def test_corpo_grande_demais_conta_tentativa(novo):
    p, _, _ = novo()
    p.iniciar()
    status, _, _ = pedir(p, "POST", f"/p/{p.token}", "chave=" + "a" * (modulo.MAX_CORPO + 10))
    assert status == 400 and p.estado()["tentativas_invalidas"] == 1 and p.situacao == AGUARDANDO


# --- Tempo: 5 minutos e sinal de vida da tela ------------------------------------------------------


def test_expira_em_5_minutos(novo):
    relogio = Relogio()
    p, _, fins = novo(relogio=relogio)
    p.iniciar()
    relogio.agora += DURACAO_S - 1
    assert p.manter()  # tela aberta: continua
    relogio.agora += 2
    assert aguardar(lambda: p.situacao == EXPIRADA) and aguardar(lambda: porta_fechada(p)) and fins == [p]
    assert not p.manter()


def test_tela_fechada_sem_sinal_de_vida(novo):
    relogio = Relogio()
    p, _, _ = novo(relogio=relogio)
    p.iniciar()
    relogio.agora += SINAL_S + 1
    assert aguardar(lambda: p.situacao == ENCERRADA) and "fechada" in p.mensagem and aguardar(lambda: porta_fechada(p))


def test_encerrar_e_idempotente_e_apaga_o_certificado(novo):
    p, _, fins = novo()
    p.iniciar()
    pasta = p._pasta
    assert pasta is not None and (pasta / "chave.pem").exists()
    p.encerrar()
    p.encerrar()
    assert not pasta.exists() and fins == [p] and porta_fechada(p)  # fechada ao voltar de encerrar()


# --- Certificado ------------------------------------------------------------------------------------


def test_certificado_real(tmp_path):
    certificado, chave, impressao = gerar_certificado(tmp_path, "192.168.77.202")
    texto = subprocess.run(["openssl", "x509", "-in", str(certificado), "-noout", "-text"], capture_output=True, text=True).stdout
    assert "IP Address:192.168.77.202" in texto and "prime256v1" in texto
    assert (chave.stat().st_mode & 0o777) == 0o600 and len(impressao) == 64


def test_estado_e_registro_nunca_tem_a_chave(novo):
    p, _, _ = novo()
    p.iniciar()
    p._receber(CHAVE, "10.0.0.5")
    assert set(p.registro()) == {"ia", "ia_nome", "inicio", "fim", "resultado", "ip_celular", "tentativas_invalidas"}
    assert CHAVE not in repr(p.estado()) + repr(p.registro())


def test_modulo_nao_registra_url(novo, capsys):
    p, _, _ = novo()
    p.iniciar()
    pedir(p, "GET", f"/p/{p.token}")
    pedir(p, "GET", "/p/errado")
    saida = capsys.readouterr()
    assert p.token not in saida.out + saida.err


def test_pagina_sem_javascript():
    fonte = Path(modulo.__file__).read_text(encoding="utf-8")
    assert "<script" not in fonte and "script-src" not in modulo.CSP

"""Envio automático dos dados anonimizados ao banco da MSServer (CLAUDE.md 33) e o receptor do banco."""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from msscan import envio_dados
from msscan.core.executor import URL_BANCO_DADOS, Executor, ExecutorReplay, RespostaIA
from msscan.interface.sessao import CONCLUIDO, Sessao
from tests.conftest import R640

RAIZ = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("receptor", RAIZ / "ferramentas/banco_dados/receptor.py")
receptor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(receptor)

RELATORIO = {
    "equipamento": {"fabricante": "Dell Inc.", "modelo": "PowerEdge R640", "versao_bios": "2.21.2"},
    "veredito": "parcialmente diagnosticado",
    "medido": [{"componente": "memoria", "titulo": "Memória", "status": "OK", "fonte": "dmidecode", "mensagem": "ok",
                "dados": {"pentes": [{"slot": "A1", "serial": "36ABC1234", "tamanho": "32 GB"}]}},
               {"componente": "rede", "titulo": "Rede", "status": "OK", "fonte": "ip", "mensagem": "eno3 192.168.0.85",
                "dados": {"portas": [{"nome": "eno3", "mac": "e4:43:4b:a0:f8:88"}]}}],
    "historico_comandos": [{"comando": ["ipmitool", "lan", "print"]}],
}  # fmt: skip


class ExecutorEnvio(Executor):
    origem = "real"

    def __init__(self, status: int | None = 201) -> None:
        super().__init__()
        self.enviados: list[dict] = []
        self.status = status

    def _enviar_dados_servidor(self, corpo, timeout):
        self.enviados.append(corpo)
        return RespostaIA(self.status, None, erro="sem conexão" if self.status is None else "")


def test_anonimizado_sem_historico_e_sem_identificadores():
    texto = json.dumps(envio_dados.montar(RELATORIO, ["r640-lab"]), ensure_ascii=False)
    for proibido in ("36ABC1234", "e4:43:4b:a0:f8:88", "192.168.0.85", "ipmitool"):
        assert proibido not in texto, proibido
    assert "PowerEdge R640" in texto and "2.21.2" in texto and "32 GB" in texto


def test_envia_uma_vez_por_conteudo_e_tenta_de_novo_depois_de_falhar():
    executor = ExecutorEnvio(status=None)
    impressao, situacao = envio_dados.enviar(executor, RELATORIO, [], None)
    assert impressao is None and situacao == "sem conexão"
    executor.status = 201
    impressao, situacao = envio_dados.enviar(executor, RELATORIO, [], None)
    assert situacao == "enviado" and executor.enviados[-1]["formato"] == envio_dados.FORMATO
    assert envio_dados.enviar(executor, RELATORIO, [], impressao) == (impressao, "ja_enviado") and len(executor.enviados) == 2
    assert executor.historico[-1].comando == ["POST", URL_BANCO_DADOS, "(dados anonimizados omitidos)"]


def test_replay_nunca_envia():
    sessao = Sessao(ExecutorReplay(R640), None, replay=True)
    sessao.iniciar()
    sessao.aguardar(30)
    assert sessao.iniciar_diagnostico()
    sessao.aguardar(30)
    assert sessao.etapa == CONCLUIDO and sessao.envio_dados["situacao"] == "nao_enviado"
    assert ExecutorReplay(R640).enviar_dados_servidor({"x": 1}).status is None


def test_receptor_aceita_so_o_formato_e_guarda(tmp_path):
    srv = receptor.Receptor(("127.0.0.1", 0), str(tmp_path / "coletas.db"))
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(caminho: str, corpo: bytes) -> int:
        pedido = urllib.request.Request(url + caminho, data=corpo, method="POST", headers={"Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(pedido, timeout=5).status
        except urllib.error.HTTPError as erro:
            return erro.code

    try:
        valido = json.dumps(envio_dados.corpo(envio_dados.montar(RELATORIO, []))).encode()
        assert post("/msscan/v1/coletas", valido) == 201
        assert post("/v1/coletas", json.dumps({"formato": "outro"}).encode()) == 400
        assert post("/v1/coletas", b"{nao json") == 400
        assert post("/outra", valido) == 404
        linhas = srv.conexao.execute("SELECT modelo, dados FROM coletas").fetchall()
        assert len(linhas) == 1 and linhas[0][0] == "PowerEdge R640" and "36ABC1234" not in linhas[0][1]
        assert "1 coletas" in receptor.resumo(str(tmp_path / "coletas.db"))
    finally:
        srv.shutdown()
        srv.server_close()


def test_receptor_limita_envios_por_origem(tmp_path):
    srv = receptor.Receptor(("127.0.0.1", 0), str(tmp_path / "c.db"))
    assert all(srv.permitido("1.2.3.4") for _ in range(receptor.LIMITE_POR_HORA))
    assert not srv.permitido("1.2.3.4") and srv.permitido("5.6.7.8")
    srv.server_close()

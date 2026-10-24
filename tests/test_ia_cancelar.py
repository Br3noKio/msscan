"""IA (pedido de 01/10/2026, CLAUDE.md 17.5): Flash-Lite como principal, "Cancelar envio" e "Remover chave"."""

from __future__ import annotations

import threading
import time

from msscan.core.executor import RespostaIA
from msscan.ia.catalogo import carregar_catalogo
from msscan.ia.config import atualizar_texto, chave_de, ler_config, remover_chave_do_texto
from tests.conftest import ExecutorFalso


def test_flash_lite_e_o_modelo_principal_do_gemini():
    catalogo = carregar_catalogo(None)
    for identificador in ("gemini_gratuito", "gemini_pago"):
        entrada = catalogo.obter(identificador)
        assert entrada.modelo_padrao == "gemini-3.5-flash-lite" and entrada.modelo_alternativo == "gemini-3.8-flash"


class ExecutorLento(ExecutorFalso):
    """Resposta da IA que demora (o cancelamento tem de voltar antes)."""

    def __init__(self):
        super().__init__()
        self.liberar = threading.Event()

    def _post_ia(self, url, cabecalhos, corpo, timeout):
        self.liberar.wait(5)
        return RespostaIA(200, {})


def test_cancelar_envio_volta_na_hora_e_bloqueia_as_proximas_tentativas():
    executor = ExecutorLento()
    resultado = {}
    tarefa = threading.Thread(target=lambda: resultado.setdefault("r", executor.post_ia({}, {}, url="https://api.anthropic.com/v1/messages")))
    tarefa.start()
    time.sleep(0.2)
    inicio = time.monotonic()
    executor.cancelar_ia()
    tarefa.join(2)
    assert not tarefa.is_alive() and time.monotonic() - inicio < 1
    assert resultado["r"].erro_tipo == "cancelado"
    assert executor.post_ia({}, {}, url="https://api.anthropic.com/v1/messages").erro_tipo == "cancelado"
    executor.liberar.set()
    executor.ia_cancelada.clear()  # novo envio do técnico
    assert executor.post_ia({}, {}, url="https://api.anthropic.com/v1/messages").status == 200


def test_analise_cancelada_nao_passa_para_a_reserva(tmp_path):
    from msscan.ia.analise import solicitar_analise
    from tests.test_ia_config import dois_provedores

    executor = ExecutorFalso()
    executor.ia_cancelada.set()
    resultado = solicitar_analise(executor, dois_provedores(tmp_path), {"componentes": []})
    assert not resultado.ok and "cancelado" in resultado.mensagem.lower() and len(resultado.tentativas) == 1
    assert executor.ia_urls == []


def test_remover_chave_esvazia_so_a_do_provedor(tmp_path):
    (tmp_path / "config").mkdir()
    texto = atualizar_texto("", "gemini_gratuito", "groq", {"gemini_gratuito": "AIzaChave1234", "groq": "gsk_outra9876"})
    novo = remover_chave_do_texto(texto, "gemini_gratuito")
    (tmp_path / "config" / "ia.conf").write_text(novo)
    config, catalogo = ler_config(str(tmp_path)), carregar_catalogo(None)
    assert chave_de(str(tmp_path), config, catalogo.obter("gemini_gratuito")) == ""
    assert chave_de(str(tmp_path), config, catalogo.obter("groq")) == "gsk_outra9876"
    assert "AIzaChave1234" not in novo and "principal = gemini_gratuito" in novo

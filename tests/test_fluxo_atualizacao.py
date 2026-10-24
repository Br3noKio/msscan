"""Atualizações (pedido de 01/10/2026): mensagens com a causa real (3c), download prévio (3d) e o "Próximo passo"."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from msscan.atualizacao.aplicacao import CREDENCIAIS, IDRAC_INACESSIVEL, NAO_SUPORTADO_AQUI, baixar_e_verificar, motivo_metodo
from msscan.atualizacao.manual import metodo_aplicacao

APP_JS = Path(__file__).resolve().parents[1] / "msscan" / "interface" / "estaticos" / "app.js"


def test_motivo_metodo_diz_a_causa_real():
    assert motivo_metodo({"redfish": True, "administrador": True, "multipart": "/x"}) == {"codigo": "", "texto": ""}
    leitura = motivo_metodo({"redfish": True, "administrador": False, "multipart": "/x"})
    assert leitura["codigo"] == CREDENCIAIS and leitura["texto"].startswith("Precisa de um usuário administrador do iDRAC")
    assert motivo_metodo({"redfish": False, "alcancavel": True})["codigo"] == CREDENCIAIS
    inacessivel = motivo_metodo({"redfish": False, "alcancavel": False, "dell_ipmi": True})
    assert inacessivel["codigo"] == IDRAC_INACESSIVEL and inacessivel["texto"].startswith("iDRAC inacessível pela rede (cabo, IP, porta)")
    assert "não acessível" not in inacessivel["texto"] and "acessível só por dentro do servidor (IPMI)" in inacessivel["texto"]
    # 28.2 (R530): com o racadm local respondendo, nunca "inacessível" como causa — a instalação usa o racadm.
    interno = motivo_metodo({"redfish": False, "alcancavel": False, "dell_ipmi": True, "racadm": True})
    assert interno["codigo"] == "interno" and "acessível só por dentro do servidor (racadm/IPMI)" in interno["texto"]
    assert motivo_metodo({"redfish": False})["codigo"] == NAO_SUPORTADO_AQUI
    # 18.3: o acesso ao iDRAC é resolvido sozinho na instalação; o .EXE só é recusado pela versão não confirmada.
    metodo, texto = metodo_aplicacao({"linux": False}, {"redfish": False, "dell_ipmi": True})
    assert metodo is None and "versão do pacote .EXE não confirmada" in texto and "não suportada" not in texto
    assert metodo_aplicacao({"linux": False}, {"redfish": False, "dell_ipmi": True}, versao_confirmada=True)[0] == "automatico"


class ExecutorDownload:
    """Grava um conteúdo fixo no destino e conta os downloads."""

    def __init__(self, conteudo: bytes):
        self.conteudo, self.downloads = conteudo, 0

    def baixar(self, url, destino, limite_bytes, timeout=120):
        from msscan.core.executor import RespostaDownload

        self.downloads += 1
        Path(destino).write_bytes(self.conteudo)
        return RespostaDownload(url, 200, sha256=hashlib.sha256(self.conteudo).hexdigest())


def test_sem_idrac_aponta_o_pacote_linux_manual():
    """Pedido de 20/10/2026: sem iDRAC daqui e só .EXE no catálogo, avisa antes do ATUALIZAR com a página de drivers."""
    from msscan.atualizacao.automatica import explicar_falta
    from msscan.atualizacao.verificacao import sem_idrac
    from msscan.deteccao.contexto import Contexto
    from tests.conftest import ExecutorFalso

    def item(nome, url, cid):
        return {"componente": nome, "situacao": "atualizacao", "component_id": cid, "disponivel": "2.9.0",
                "pacote": {"url": url}}  # fmt: skip

    secao = {"system_id": "04CE", "acesso": {"codigo": IDRAC_INACESSIVEL}, "itens": [
        item("BIOS", "https://downloads.dell.com/FOLDER1/BIOS_X_WN64_2.9.0.EXE", "159"),
        item("PERC H710", "https://downloads.dell.com/FOLDER2/SAS-RAID_Y_LN_21.3.5.BIN", "160"),
        item("NIC", "https://downloads.dell.com/FOLDER3/Network_Z_WN64_2.9.0.EXE", "161"),
        {**item("iDRAC", "https://downloads.dell.com/FOLDER4/iDRAC.EXE", "162"), "situacao": "incerta"}]}  # fmt: skip
    catalogo = {"base": "downloads.dell.com", "componentes": [  # .BIN oficial da mesma versão só para a NIC (caminho f)
        {"caminho": "FOLDER5/Network_Z_LN_2.9.0.BIN", "versao": "2.9.0", "sistemas": [{"system_id": "04CE"}],
         "dispositivos": [{"component_id": "161"}], "algoritmo_hash": "SHA256", "hash": "a" * 64}]}  # fmt: skip
    aviso = sem_idrac(secao, catalogo, "Dell Inc.", "PowerEdge R620")
    assert aviso["componentes"] == ["BIOS"]
    assert aviso["url"] == "https://www.dell.com/support/product-details/pt-br/product/poweredge-r620/drivers"
    assert aviso["url"] in aviso["texto"] and "MSSCAN/firmware/manual" in aviso["texto"] and "Lifecycle Controller" in aviso["texto"]
    assert sem_idrac({**secao, "acesso": {"codigo": NAO_SUPORTADO_AQUI}}, catalogo, "Dell Inc.", "PowerEdge R620")["texto"] != aviso["texto"]
    for codigo in ("", CREDENCIAIS, "interno"):  # iDRAC acessível (ou só faltam credenciais): nada a avisar
        assert sem_idrac({**secao, "acesso": {"codigo": codigo}}, catalogo, "Dell Inc.", "PowerEdge R620") is None
    ctx = Contexto(executor=ExecutorFalso())
    ctx.fabricante, ctx.modelo = "Dell Inc.", "PowerEdge R620"
    assert aviso["url"] in explicar_falta(ctx, {}, {"tentativas": []}, "BIOS")


def test_baixar_e_verificar_reusa_o_arquivo_so_com_hash_conferido(tmp_path):
    conteudo = b"pacote oficial"
    item = {"id": "item1", "componente": "BIOS", "instalada": "2.7.0", "disponivel": "2.9.0",
            "pacote": {"url": "https://downloads.dell.com/FOLDER1/BIOS_X_WN64_2.9.0.EXE", "sha256": hashlib.sha256(conteudo).hexdigest()}}  # fmt: skip
    executor = ExecutorDownload(conteudo)
    [feito] = baixar_e_verificar([item], executor, str(tmp_path), "0716")
    assert feito["ok"] and feito["versao"] == "2.7.0 → 2.9.0 (mais nova)" and "0716" in feito["modelo"]
    [de_novo] = baixar_e_verificar([item], executor, str(tmp_path), "0716")
    assert de_novo["ok"] and "baixado antes" in de_novo["hash"] and executor.downloads == 1
    (tmp_path / "firmware/pacotes/BIOS_X_WN64_2.9.0.EXE").write_bytes(b"adulterado")
    [refeito] = baixar_e_verificar([item], executor, str(tmp_path), "0716")
    assert refeito["ok"] and executor.downloads == 2  # hash diferente: apagado e baixado de novo


@pytest.mark.skipif(shutil.which("node") is None, reason="node ausente")
def test_proximo_passo_da_interface():
    """A função pura ``passoAtualizacao`` do app.js, rodada no node com estados típicos."""
    js = APP_JS.read_text(encoding="utf-8")
    trecho = js[js.index("const PASSOS_ATU"):js.index("const BOTAO_ACESSO")]
    casos = {
        "sem_verificacao": [{"disponivel": True}, []],
        "nada_escolhido": [{"secao": {"situacao": "verificada", "itens": [{"situacao": "atualizacao"}]}}, []],
        "baixar": [{"secao": {"situacao": "verificada", "itens": []}}, ["item1"]],
        "sem_idrac": [{"secao": {"situacao": "verificada", "acesso": {"codigo": "idrac", "texto": "iDRAC não acessível"}},
                       "preparo": {"ids": ["item1"], "situacao": "pronto"}}, ["item1"]],
        "aviso": [{"secao": {"situacao": "verificada", "acesso": {"codigo": ""}}, "preparo": {"ids": ["item1"], "situacao": "pronto"}}, ["item1"]],
        "manual_sha": [{"manual": {"pacotes": [{"id": "p1", "nome": "BIOS.BIN", "etapa": "sha"}]}}, []],
        "instalando": [{"aplicacao": {"situacao": "executando"}}, []],
    }
    from msscan.textos import script_js

    programa = script_js().replace("window.TEXTOS", "const TEXTOS") + js[js.index("function T("):js.index("/* Textos fixos do index.html")] + trecho + f"const casos = {json.dumps(casos)};\n" + \
        "const r = {}; for (const [k, [a, e]] of Object.entries(casos)) r[k] = passoAtualizacao(a, e); console.log(JSON.stringify(r));"
    saida = json.loads(subprocess.run(["node"], input=programa, capture_output=True, text=True, check=True).stdout)
    # 18.2: o acesso ao iDRAC não é mais uma etapa do técnico (resolvido sozinho depois do ATUALIZAR): o próximo passo é o plano.
    assert {k: v["n"] for k, v in saida.items()} == {"sem_verificacao": 1, "nada_escolhido": 1, "baixar": 2, "sem_idrac": 4,
                                                      "aviso": 4, "manual_sha": 3, "instalando": 6}  # fmt: skip
    assert "acesso" not in saida["sem_idrac"] and saida["baixar"]["acao"] == "baixar" and "Plano" in saida["aviso"]["botao"].title()


@pytest.mark.skipif(shutil.which("node") is None, reason="node ausente")
def test_lista_de_componentes_por_gravidade():
    """``ordemPorGravidade`` do app.js: Falha, Atenção, Não verificado, Normal, Não aplicável; empate na ordem do núcleo;
    em andamento e aguardando no fim."""
    js = APP_JS.read_text(encoding="utf-8")
    trecho = js[js.index("function statusDoComponente"):js.index("function reordenarComponentes")]
    componentes = [{"componente": "sensores", "status": "OK"}, {"componente": "fontes", "status": "ALERTA"},
                   {"componente": "raid", "status": "NAO_APLICAVEL"}, {"componente": "memoria", "status": "FALHA"},
                   {"componente": "discos", "status": "NAO_VERIFICADO"}, {"componente": "sel", "status": "ALERTA"},
                   {"componente": "rede", "em_andamento": True}, {"componente": "cpu", "status": "OK"}]  # fmt: skip
    programa = trecho + f"console.log(JSON.stringify(ordemPorGravidade({json.dumps(componentes)})));"
    saida = json.loads(subprocess.run(["node"], input=programa, capture_output=True, text=True, check=True).stdout)
    assert saida == ["memoria", "fontes", "sel", "discos", "sensores", "cpu", "raid", "rede"]

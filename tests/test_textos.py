"""Pedido de 09/10/2026: todos os textos da interface num único arquivo (``msscan/textos/pt_BR.json``) e o padrão de
escrita de ``docs/guia_de_escrita.md``. Estes testes FALHAM se algum texto da interface ficar escrito direto no código."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from msscan import textos
from tests import textos_codigo as tc

RAIZ = Path(__file__).parent.parent
ESTATICOS = RAIZ / "msscan/interface/estaticos"
CENTRAL = textos.carregar()
MODULOS_PY = ["msscan/interface/sessao.py", "msscan/interface/manual.py", "msscan/interface/servidor.py", "msscan/acesso_idrac.py",
              "msscan/atualizacao/jobs.py", "msscan/atualizacao/progresso.py", "msscan/atualizacao/liberacao.py",
              "msscan/atualizacao/aviso.py", "main.py", "msscan/atualizacao/automatica.py", "msscan/atualizacao/aplicacao.py",
              "msscan/atualizacao/verificacao.py", "msscan/discos.py"] + sorted(str(p.relative_to(RAIZ)) for p in (RAIZ / "msscan/relatorio").glob("*.py"))  # fmt: skip
"""Módulos cujos textos chegam direto à interface (respostas da API, caixas, plano de atualização, faixa de segurança), ao modo
texto (``main.py``), aos relatórios e às mensagens do motor de atualização."""
PERMITIDOS_PY = {
    "msscan/interface/servidor.py": set(),
    "msscan/acesso_idrac.py": {"o Redfish não respondeu", "sem o pacote iDRAC Tools em vendor/"},
    "msscan/atualizacao/jobs.py": {"sem resposta", "fora da fila", "unable to transfer", "unable to download"},
    "msscan/atualizacao/aviso.py": {"fila de jobs não lida ({0}: {1})", "as fontes", "a memória", "este item","Fontes", "Memória", "Configuração de memória", "RAID", "Temperatura e sensores", "Ventoinhas",
                                    "Hardware", "Plano de atualização", "integrated dell remote access", "serial ata"},
}
"""Nomes dos itens de hardware também são chaves da lógica (mapa de riscos e riscos aceitos na retomada) e trechos de
categorias da Dell usados para ordenar: ficam no código. Qualquer outro texto vai para o arquivo central."""


def test_app_js_sem_texto_fora_do_arquivo_central():
    assert tc.textos_fora((ESTATICOS / "app.js").read_text(encoding="utf-8")) == []


def test_index_html_sem_texto_fora_do_arquivo_central():
    html = (ESTATICOS / "index.html").read_text(encoding="utf-8")
    corpo = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", "", html[html.index("<body"):], flags=re.S)
    soltos = [t.strip() for t in re.findall(r">([^<>]+)<", corpo) if re.search(r"[A-Za-zÀ-ÿ]", t)]
    assert soltos == []
    atributos = re.findall(r'\s(?:aria-label|title|placeholder|alt)="([^"]*[A-Za-zÀ-ÿ][^"]*)"', corpo)
    assert atributos == []


@pytest.mark.parametrize("modulo", MODULOS_PY)
def test_modulos_da_interface_sem_texto_fora_do_arquivo_central(modulo):
    achados = [(linha, texto) for linha, texto in tc.textos_fora_py((RAIZ / modulo).read_text(encoding="utf-8"))
               if texto not in PERMITIDOS_PY.get(modulo, set()) and "charset=" not in texto and "-src '" not in texto]  # fmt: skip
    assert achados == []


def _chaves_e_argumentos(js: str) -> list[tuple[str, int]]:
    saida = []
    for m in re.finditer(r'\bT\("([^"]+)"', js):
        profundidade, n, i = 0, 0, m.end()
        while True:
            c = js[i]
            if c in "([{":
                profundidade += 1
            elif c in ")]}":
                if profundidade == 0:
                    break
                profundidade -= 1
            elif c == "," and profundidade == 0:
                n += 1
            elif c in "\"'`":
                i += 1
                while js[i] != c:
                    i += 2 if js[i] == "\\" else 1
            i += 1
        saida.append((m.group(1), n))
    return saida


def test_toda_chave_usada_existe_e_os_valores_batem():
    js = re.sub(r"/\*.*?\*/", "", (ESTATICOS / "app.js").read_text(encoding="utf-8"), flags=re.S)
    html = (ESTATICOS / "index.html").read_text(encoding="utf-8")
    for chave, n in _chaves_e_argumentos(js):
        assert chave in CENTRAL, chave
        assert {int(x) for x in re.findall(r"\{(\d+)\}", CENTRAL[chave])} == set(range(n)), chave
    for chave in re.findall(r'data-t(?:-[a-z-]+)?="([^"]+)"', html):
        assert chave in CENTRAL, chave
    for arquivo in MODULOS_PY:
        for chave in re.findall(r'textos\.t\(["\']([^"\']+)["\']', (RAIZ / arquivo).read_text(encoding="utf-8")):
            assert chave in CENTRAL or chave.startswith("py_aviso.risco_") or "{" in chave, (arquivo, chave)


def test_servidor_entrega_os_textos_e_t_troca_os_valores():
    assert textos.script_js().startswith("window.TEXTOS = {") and '"rodape.versao": "MSscan {0}"' in textos.script_js()
    assert textos.t("rodape.versao", "1.2.3") == "MSscan 1.2.3" and textos.t("chave.inexistente") == "chave.inexistente"


def test_datas_no_padrao_brasileiro():
    assert textos.data_br("2026-10-06T14:25:00") == "06/10/2026 14:25" and textos.data_br("August 11, 2026") == "11/08/2026"
    assert textos.data_br("2026-10-06", com_hora=False) == "06/10/2026" and textos.data_br("") == "data desconhecida"


TERMOS_PROIBIDOS = {r"\bupdate\b": "atualização de firmware", r"\bdrivers?\b": "atualização de firmware", r"\brollback\b": "reversão",
                    r"\breplay\b": "modo de demonstração", r"\bpass-through\b": "acesso interno ao iDRAC", r"\bchat\b": "conversa"}  # fmt: skip


def test_glossario_e_tom():
    """Guia de escrita: terminologia única, sem exclamações (a página de site bloqueado é HTML do navegador integrado)."""
    for chave, texto in CENTRAL.items():
        if chave.startswith(("sobre.", "py_servidor.doctype")):
            continue  # licenças em inglês e a página HTML de site bloqueado
        sem_caminhos = re.sub(r"Maintenance > Job Queue|Job Queue", "", texto)
        for termo, oficial in TERMOS_PROIBIDOS.items():
            assert not re.search(termo, sem_caminhos, re.I), f"{chave}: use \"{oficial}\" ({texto})"
        assert "!" not in texto, chave


def test_botoes_com_verbo_e_so_a_primeira_letra_maiuscula():
    html = (ESTATICOS / "index.html").read_text(encoding="utf-8")
    for chave in re.findall(r'<button[^>]*>(?:<span[^>]*></span>)*<span data-t="([^"]+)"', html) + re.findall(r'<button[^>]*data-t="([^"]+)"', html):
        texto = CENTRAL[chave]
        palavras = [p for p in re.findall(r"[A-Za-zÀ-ÿ]+", texto)[1:] if p not in {"iDRAC", "IA", "IP", "SHA", "MSscan", "BIOS", "SSH",
                                                                                     "SFTP", "Ctrl", "Esc", "D", "CORRIGIR", "DESFAZER", "ATUALIZAR", "NTP", "QR", "Code"}]  # fmt: skip
        assert not any(p[0].isupper() for p in palavras), f"{chave}: {texto}"


def test_guia_de_escrita_existe_com_o_glossario():
    guia = (RAIZ / "docs/guia_de_escrita.md").read_text(encoding="utf-8")
    assert "## Glossário" in guia and "controlador de gerenciamento" in guia and "Detalhes técnicos" in guia
    assert json.loads((RAIZ / "msscan/textos/pt_BR.json").read_text(encoding="utf-8"))

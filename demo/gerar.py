"""Gera a demonstração estática do MSscan (GitHub Pages) em ``docs/demo/``.

Roda a sessão REAL da interface em modo replay sobre a fixture anonimizada do R640, com:
- o recorte real do catálogo Dell (``tests/fixtures/dell/catalogo``) no lugar do download;
- respostas de IA GRAVADAS (``demo/respostas_ia.py``): nenhuma API é chamada, nenhuma chave existe.

Grava os estados que a interface pede à API (``dados.js``), o relatório HTML e cópias dos arquivos da interface com
os caminhos relativos (o Pages serve em ``/<repositório>/``). O ``demo.js`` responde ao ``fetch`` da interface com
esses estados: a interface é a mesma do sistema, mas nada é executado.

Uso: ``python3 demo/gerar.py [--xterm /usr/share/nodejs]``
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from msscan.atualizacao.aplicacao import MODO_APLICAR  # noqa: E402
from msscan.core.executor import ExecutorReplay, RespostaDownload, RespostaIA  # noqa: E402
from msscan.interface.servidor import ARQUIVOS_THREE  # noqa: E402
from msscan.interface import fetch  # noqa: E402
from msscan.interface.sessao import Sessao  # noqa: E402
from msscan.rede.verificacao import ResultadoRede  # noqa: E402
import respostas_ia  # noqa: E402

FIXTURE = RAIZ / "tests/fixtures/dell/poweredge_r640"
RECORTE = RAIZ / "tests/fixtures/dell/catalogo/Catalog_recorte_r640.xml.gz"
ESTATICOS = RAIZ / "msscan/interface/estaticos"
DESTINO = RAIZ / "docs/demo"
ARQUIVOS_XTERM = {"xterm.js": "xterm/lib/xterm.js", "xterm.css": "xterm/css/xterm.css",
                  "xterm-addon-fit.js": "xterm-addon-fit/lib/xterm-addon-fit.js",
                  "xterm-addon-canvas.js": "xterm-addon-canvas/lib/xterm-addon-canvas.js"}  # fmt: skip
# Comandos do terminal simulado → saída gravada na fixture (anonimizada).
COMANDOS_TERMINAL = {
    "ipmitool sdr type \"Power Supply\"": "ipmitool__sdr__type__Power_Supply.stdout",
    "ipmitool sdr type Fan": "ipmitool__sdr__type__Fan.stdout",
    "ipmitool sel elist": "ipmitool__sel__elist.stdout",
    "ipmitool mc info": "ipmitool__mc__info.stdout",
    "dmidecode -t 17": "dmidecode__-t__17.stdout",
    "dmidecode -s system-product-name": "dmidecode__-s__system-product-name.stdout",
    "lspci -nnk": "lspci__-nnk.stdout",
    "ethtool eno3": "ethtool__eno3.stdout",
}


class ExecutorDemo(ExecutorReplay):
    """Replay do R640 + catálogo do recorte + IA gravada (fila: a análise e depois as perguntas do chat, na ordem)."""

    def __init__(self) -> None:
        super().__init__(str(FIXTURE))
        self.fila_ia = [respostas_ia.ANALISE] + [respostas_ia.CHAT[s] for s in respostas_ia.CHAT]

    def _baixar(self, url, destino, limite, timeout):  # noqa: ANN001
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        Path(destino).write_bytes(RECORTE.read_bytes())
        return RespostaDownload(url, 200, destino, RECORTE.stat().st_size, "demo")

    def _post_ia(self, url, cabecalhos, corpo, timeout):  # noqa: ANN001
        return RespostaIA(200, respostas_ia.gemini(self.fila_ia.pop(0)))


PASTA_DADOS = {"atual": ""}


def _limpar(dados):  # noqa: ANN001
    """Cópia JSON (datas viram texto) sem caminhos locais da máquina que gerou a demo."""
    texto = json.dumps(dados, ensure_ascii=False, default=str)
    if PASTA_DADOS["atual"]:
        texto = texto.replace(PASTA_DADOS["atual"], "MSSCAN")
    return json.loads(texto)


def _esperar(condicao, limite: float = 60) -> None:  # noqa: ANN001
    import time

    fim = time.monotonic() + limite
    while not condicao():
        assert time.monotonic() < fim, "tempo esgotado esperando a sessão"
        time.sleep(0.1)


def gerar(xterm: Path, three: Path = Path("/usr/share/javascript/three")) -> None:
    pasta = Path(tempfile.mkdtemp(prefix="msscan-demo-"))
    PASTA_DADOS["atual"] = str(pasta)
    sessao = Sessao(ExecutorDemo(), str(pasta), replay=False, sem_ia=False, modo_aplicacao=MODO_APLICAR)
    sessao.iniciar()
    sessao.aguardar(60)
    sessao.rede = ResultadoRede(True)
    assert sessao.salvar_ia("gemini_gratuito", None, {"gemini_gratuito": "CHAVE-DE-DEMONSTRACAO"})[0]
    estados = {"pronto": _limpar(sessao.estado())}
    visoes = {"pronto": _limpar(sessao.visao())}
    assert sessao.iniciar_diagnostico()
    sessao.aguardar(120)
    estados["diagnostico"] = _limpar(sessao.estado())
    visoes["diagnostico"] = _limpar(sessao.visao())
    sessao.discos()  # aba Discos (CLAUDE.md 31): a primeira consulta dispara a releitura do SMART
    _esperar(lambda: not sessao._discos_lendo and sessao._discos_lidos)
    discos = _limpar(sessao.discos())
    componentes = {c["componente"]: _limpar(sessao.componente(c["componente"]))
                   for c in estados["diagnostico"]["diagnostico"]["componentes"]}  # fmt: skip
    previa = _limpar(sessao.previa_ia())
    config_ia = _limpar(sessao.configuracao_ia())
    qrs = {e["id"]: sessao.qr_ia(e["id"]) for e in config_ia.get("catalogo", []) if isinstance(e, dict) and e.get("id")}

    assert sessao.solicitar_ia()
    sessao.aguardar(120)
    ia = _limpar(sessao.estado()["ia"])
    chat = [{"estado": _limpar(sessao.estado()["chat"]), "mensagens": _limpar(sessao.mensagens_chat())}]
    for sugestao in respostas_ia.CHAT:
        ok, motivo = sessao.enviar_chat(sugestao)
        assert ok, motivo
        _esperar(lambda: sessao.estado()["chat"]["situacao"] not in ("digitando", "aguardando_rede"))
        chat.append({"pergunta": sugestao, "estado": _limpar(sessao.estado()["chat"]), "mensagens": _limpar(sessao.mensagens_chat())})

    assert sessao.verificar_atualizacoes()
    sessao.aguardar(120)
    atualizacoes = _limpar(sessao.estado()["atualizacoes"])
    disponiveis = [i["id"] for i in (atualizacoes.get("secao") or {}).get("itens", []) if i.get("situacao") == "atualizacao"]
    avisos = {}
    if disponiveis:
        assert sessao.preparar_aviso("catalogo", disponiveis[:2])[0]
        sessao.aguardar(120)
        avisos["catalogo"] = _limpar(sessao.aviso)
    relatorio_html = (sessao.html_relatorio() or "").replace(str(pasta), "MSSCAN")
    sessao_visao = _limpar(sessao.visao_servidor())
    final = _limpar(sessao.estado())
    sessao.encerrar()

    terminal = {cmd: (FIXTURE / "comandos" / arquivo).read_text(encoding="utf-8", errors="replace")
                for cmd, arquivo in COMANDOS_TERMINAL.items()}  # fmt: skip
    dados = {"estados": estados, "componentes": componentes, "previa_ia": previa, "config_ia": config_ia, "qr": qrs,
             "ia": ia, "chat": chat, "atualizacoes": atualizacoes, "avisos": avisos, "final": final,
             "terminal": terminal, "versao": final.get("versao"), "visao": visoes, "discos": discos,
             "fetch": fetch.montar(sessao_visao, "Debian GNU/Linux 13 (trixie)", final.get("versao"))}  # fmt: skip
    texto = json.dumps(dados, ensure_ascii=False)
    for proibido in ("CHAVE-DE-DEMONSTRACAO", str(pasta)):
        assert proibido not in texto, f"vazou na demonstração: {proibido}"

    if DESTINO.exists():
        shutil.rmtree(DESTINO)
    (DESTINO / "estaticos" / "xterm").mkdir(parents=True)
    (DESTINO / "estaticos" / "fontes").mkdir(parents=True)
    for nome in ("tema.css", "app.css", "icones.js", "logo-msserver.png", *(f.relative_to(ESTATICOS).as_posix() for f in (ESTATICOS / "fontes").iterdir())):
        shutil.copy(ESTATICOS / nome, DESTINO / "estaticos" / nome)
    from msscan import textos as textos_centrais

    (DESTINO / "estaticos" / "textos.js").write_text(textos_centrais.script_js(), encoding="utf-8")  # 09/10/2026: textos centrais
    app = (ESTATICOS / "app.js").read_text(encoding="utf-8")
    app = app.replace('"/estaticos/', '"estaticos/').replace('"/relatorio.html', '"relatorio.html')
    (DESTINO / "estaticos" / "app.js").write_text(app, encoding="utf-8")
    modelo = (ESTATICOS / "modelo3d.js").read_text(encoding="utf-8").replace('"/estaticos/', '"estaticos/')
    (DESTINO / "estaticos" / "modelo3d.js").write_text(modelo, encoding="utf-8")
    # three.js r111 (MIT) do pacote Debian libjs-three, o mesmo da ISO (CLAUDE.md 18.1).
    (DESTINO / "estaticos" / "three").mkdir(parents=True)
    for nome, origem in ARQUIVOS_THREE.items():
        shutil.copy(three / origem, DESTINO / "estaticos" / "three" / nome)
    (DESTINO / "estaticos" / "three" / "LICENSE.txt").write_text(
        "three.js (https://threejs.org) — MIT License. Copyright © 2010-2019 three.js authors.\n"
        "Arquivos copiados do pacote Debian libjs-three 111+dfsg1.\n", encoding="utf-8")
    html = (ESTATICOS / "index.html").read_text(encoding="utf-8").replace('"/estaticos/', '"estaticos/')
    html = html.replace("<title>MSscan</title>", "<title>MSscan — demonstração</title>")
    html = html.replace('<link rel="stylesheet" href="estaticos/app.css">',
                        '<link rel="stylesheet" href="estaticos/app.css">\n  <link rel="stylesheet" href="demo.css">')
    html = html.replace("<body>", '<body class="demo">\n  <div class="faixa-demo" role="note">Demonstração: dados simulados. '
                        'Nada é executado.</div>', 1)  # fmt: skip
    html = html.replace('<script src="estaticos/icones.js"></script>',
                        '<script src="dados.js"></script>\n  <script src="demo.js"></script>\n  <script src="estaticos/icones.js"></script>')
    (DESTINO / "index.html").write_text(html, encoding="utf-8")
    (DESTINO / "relatorio.html").write_text(relatorio_html, encoding="utf-8")
    (DESTINO / "dados.js").write_text("window.MSSCAN_DEMO = " + texto + ";\n", encoding="utf-8")
    for nome in ("demo.js", "demo.css"):
        shutil.copy(Path(__file__).parent / nome, DESTINO / nome)
    for nome, origem in ARQUIVOS_XTERM.items():
        shutil.copy(xterm / origem, DESTINO / "estaticos" / "xterm" / nome)
    (DESTINO / "estaticos" / "xterm" / "LICENSE.txt").write_text(
        "xterm.js (https://xtermjs.org) — MIT License. Copyright (c) 2017-2019, The xterm.js authors;\n"
        "Copyright (c) 2014-2016, SourceLair Private Company; Copyright (c) 2012-2013, Christopher Jeffrey.\n"
        "Arquivos copiados do pacote Debian node-xterm 5.3.0.\n", encoding="utf-8")
    shutil.rmtree(pasta, ignore_errors=True)
    print(f"Demonstração gerada em {DESTINO} ({len(texto) // 1024} KiB de dados)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--xterm", default="/usr/share/nodejs", help="pasta do pacote node-xterm (padrão: /usr/share/nodejs)")
    parser.add_argument("--three", default="/usr/share/javascript/three", help="pasta do pacote libjs-three")
    argumentos = parser.parse_args()
    gerar(Path(argumentos.xterm), Path(argumentos.three))

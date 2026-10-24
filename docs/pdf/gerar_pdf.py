"""Gera ``MSscan-Documentacao.pdf`` a partir do README e de ``docs/``.

Uso: ``python3 docs/pdf/gerar_pdf.py <mermaid.min.js> <saida.pdf>``

Ferramentas da Debian: ``pandoc`` (Markdown → HTML) e ``chromium`` sem tela (executa o Mermaid e imprime o PDF).
O ``mermaid.min.js`` (10.9.1, cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/) é passado de fora: não fica no repositório.
"""

from __future__ import annotations

import html
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
PAGINAS = ["README.md", "docs/uso.md", "docs/diagnostico.md", "docs/atualizacao-de-firmware.md",
           "docs/compatibilidade.md", "docs/seguranca.md", "docs/desenvolvimento.md", "docs/guia_de_escrita.md",
           "docs/licencas.md"]  # fmt: skip


def _ancora(caminho: str) -> str:
    return "p-" + Path(caminho).stem.lower().replace("_", "-")


def _versao() -> str:
    texto = (RAIZ / "msscan/__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', texto).group(1)


def _pagina(caminho: str) -> tuple[str, str]:
    """(título, HTML) de uma página, com links internos virando âncoras e imagens com caminho absoluto."""
    origem = RAIZ / caminho
    corpo = subprocess.run(["pandoc", "-f", "gfm", "-t", "html5", str(origem)], capture_output=True, text=True, check=True).stdout
    titulo = re.search(r"<h1[^>]*>(.*?)</h1>", corpo, re.S)
    titulo_txt = re.sub("<[^>]+>", "", titulo.group(1)) if titulo else Path(caminho).stem
    corpo = re.sub(r'<pre class="mermaid"><code>(.*?)</code></pre>', lambda m: f'<div class="mermaid">{m.group(1).replace("flowchart LR", "flowchart TB")}</div>', corpo, flags=re.S)

    def link(m: re.Match[str]) -> str:
        alvo = m.group(1)
        if alvo.startswith(("http://", "https://", "#")) or not alvo.endswith(".md"):
            return m.group(0)
        resolvido = (origem.parent / alvo).resolve().relative_to(RAIZ).as_posix()
        return f'href="#{_ancora(resolvido)}"' if resolvido in PAGINAS else m.group(0)

    corpo = re.sub(r'href="([^"]+)"', link, corpo)
    corpo = re.sub(r'src="([^":]+)"', lambda m: f'src="{(origem.parent / m.group(1)).resolve().as_uri()}"', corpo)
    return titulo_txt, f'<section class="pagina" id="{_ancora(caminho)}">{corpo}</section>'


CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm;
        @bottom-center { content: "MSscan — Documentação · " counter(page); font: 8pt 'DejaVu Sans'; color: #57606a; } }
@page :first { margin: 0; @bottom-center { content: none; } }
body { margin: 0; font: 10pt/1.5 'DejaVu Sans', sans-serif; color: #1f2328; }
h1 { color: #0b1f3a; border-bottom: 3px solid #1f6feb; padding-bottom: 4px; font-size: 20pt; }
h2 { color: #0d419d; font-size: 14pt; margin-top: 18px; }
h3 { color: #0d419d; font-size: 11.5pt; }
a { color: #0969da; text-decoration: none; }
code { font: 8.8pt 'DejaVu Sans Mono', monospace; background: #eef2f7; padding: 0 3px; border-radius: 3px; }
pre { background: #0d1117; color: #e6edf3; padding: 10px; border-radius: 6px; white-space: pre-wrap; }
pre code { background: none; color: inherit; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 8.8pt; page-break-inside: auto; }
th { background: #0b1f3a; color: #fff; text-align: left; }
th, td { border: 1px solid #d0d7de; padding: 4px 6px; vertical-align: top; }
tr:nth-child(even) td { background: #f6f8fa; }
blockquote { border-left: 4px solid #1f6feb; background: #ddf4ff; margin: 8px 0; padding: 6px 10px; }
img { max-width: 100%; border: 1px solid #d0d7de; border-radius: 4px; }
.mermaid svg { max-height: 150mm; }
.mermaid { text-align: center; margin: 10px 0; page-break-inside: avoid; }
.pagina { page-break-before: always; }
.capa { height: 290mm; overflow: hidden; box-sizing: border-box; padding: 70mm 22mm 0; color: #fff;
        background: linear-gradient(160deg, #010409 0%, #0b1f3a 55%, #1f6feb 140%); }
.capa h1 { color: #fff; border: 0; font-size: 44pt; margin: 0; }
.capa .sub { font-size: 15pt; color: #c9d1d9; margin-top: 8px; }
.capa .linha { width: 90mm; height: 4px; background: linear-gradient(90deg, #1f6feb, #58a6ff); margin: 22px 0; }
.capa .meta { font-size: 10.5pt; color: #8b949e; margin-top: 120mm; }
.sumario ol { font-size: 11pt; line-height: 2; }
"""


def main() -> int:
    mermaid, saida = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    paginas = [_pagina(p) for p in PAGINAS]
    sumario = "".join(f'<li><a href="#{_ancora(p)}">{html.escape(t)}</a></li>' for p, (t, _) in zip(PAGINAS, paginas))
    documento = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>MSscan — Documentação</title>
<style>{CSS}</style></head><body>
<div class="capa"><h1>MSscan</h1><div class="sub">Diagnóstico de servidores físicos e atualização de firmware Dell</div>
<div class="linha"></div><div class="sub">Documentação técnica e funcional</div>
<div class="meta">Versão {_versao()} · {date.today():%d/%m/%Y}</div></div>
<section class="pagina sumario"><h1>Sumário</h1><ol>{sumario}</ol></section>
{''.join(c for _, c in paginas)}
<script src="{mermaid.as_uri()}"></script>
<script>mermaid.initialize({{startOnLoad: true, theme: "neutral", flowchart: {{htmlLabels: true}}, securityLevel: "loose"}});</script>
</body></html>"""
    with tempfile.TemporaryDirectory() as pasta:
        fonte = Path(pasta) / "documentacao.html"
        fonte.write_text(documento, encoding="utf-8")
        subprocess.run(["chromium", "--headless", "--no-sandbox", "--disable-gpu", "--allow-file-access-from-files",
                        "--no-pdf-header-footer", "--virtual-time-budget=30000", f"--print-to-pdf={saida}", fonte.as_uri()],
                       check=True, capture_output=True)  # fmt: skip
    print(saida)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

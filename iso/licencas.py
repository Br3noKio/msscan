"""Lista de licenças dos pacotes Debian da ISO, lida do chroot do último build (arquivos ``copyright`` no formato DEP-5).

Uso, depois do ``iso/build.sh``: ``python3 iso/licencas.py iso/trabalho/chroot > docs/licencas-iso.md``.
O texto integral de cada licença fica dentro da própria ISO, em ``/usr/share/doc/<pacote>/copyright``."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def pacotes(status: str) -> list[tuple[str, str]]:
    """(pacote, versão) dos instalados, do ``/var/lib/dpkg/status``."""
    saida = []
    for bloco in status.split("\n\n"):
        campos = dict(re.findall(r"^(Package|Version|Status): (.*)$", bloco, flags=re.MULTILINE))
        if campos.get("Package") and "installed" in campos.get("Status", "").split():
            saida.append((campos["Package"], campos.get("Version", "")))
    return sorted(saida)


def licencas(copyright: str) -> list[str]:
    """Licenças distintas declaradas (``License:`` do DEP-5); vazio se o arquivo não for DEP-5."""
    vistas: list[str] = []
    for nome in re.findall(r"^License: *(.+)$", copyright, flags=re.MULTILINE):
        nome = nome.strip()
        if nome and nome not in vistas:
            vistas.append(nome)
    return vistas


def main(chroot: Path) -> str:
    linhas = ["# Licenças dos componentes da ISO", "",
              "Pacotes Debian instalados na ISO do MSscan e as licenças declaradas pelos mantenedores (campo `License` dos",
              "arquivos `copyright`). O texto integral de cada licença está na ISO, em `/usr/share/doc/<pacote>/copyright`.",
              "Gerado por `iso/licencas.py` a partir do chroot do build.", "",
              "| Pacote | Versão | Licenças declaradas |", "|---|---|---|"]  # fmt: skip
    for nome, versao in pacotes((chroot / "var/lib/dpkg/status").read_text(encoding="utf-8", errors="replace")):
        arquivo = chroot / "usr/share/doc" / nome / "copyright"
        texto = arquivo.read_text(encoding="utf-8", errors="replace") if arquivo.is_file() else ""
        declaradas = ", ".join(licencas(texto)) or ("ver o arquivo copyright do pacote" if texto else "pacote sem arquivo copyright próprio")
        linhas.append(f"| `{nome}` | {versao} | {declaradas.replace('|', '/')} |")
    return "\n".join(linhas) + "\n"


if __name__ == "__main__":
    sys.stdout.write(main(Path(sys.argv[1])))

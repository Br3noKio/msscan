"""Conferência do catálogo da Dell ANTES de o técnico encontrar o problema (recomendação de 16/10/2026).

A Dell muda o catálogo sem aviso (15/10/2026: o iDRAC 2.40.40.40 foi para ``<Prerequisites>`` e o R530 virou "fora de
suporte"; gerações saem do catálogo atual). Esta ferramenta de desenvolvimento (não vai para a ISO), só leitura:

1. baixa o ``Catalog.xml.gz`` atual e roda o MESMO parser do MSscan;
2. acusa formato novo: elementos na raiz que o parser não lê, componentes sem caminho/hash/versão;
3. para cada modelo de referência (R640, R530, R620 e a frota simulada), diz de qual catálogo vêm o BIOS e o iDRAC —
   o atual ou um antigo de ``catalogos_dell.yaml`` — e acusa modelo sem catálogo nenhum;
4. baixa cada catálogo antigo fixado e confere MD5 e SHA-256.

Uso: ``python3 ferramentas/conferir_catalogo_dell.py`` (código de saída 1 = algo a conferir). Rodar toda semana e a cada
versão nova; o que aparecer vira teste com recorte real (CLAUDE.md 11: nunca presumir o XML).
"""

from __future__ import annotations

import gzip
import hashlib
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from msscan.atualizacao.catalogo import URL_CATALOGO_DELL, carregar_legados, tem_bios  # noqa: E402
from msscan.parsers.catalogo_dell import parse_catalogo_dell  # noqa: E402

# System IDs de referência: servidores reais do usuário + a frota simulada (tests/frota.py, fontes lá).
REFERENCIA = {
    "0716": "R640 (14ª, real)", "063A": "R530 (13ª, real)", "04CE": "R620 (12ª, real)", "04F7": "R320 (12ª)",
    "0737": "R740xd (14ª)", "07C9": "R440 (14ª)", "090E": "R750 (15ª)", "0A6B": "R760 (16ª)", "0C65": "R770 (17ª)",
    "0765": "MX740c (14ª)", "0757": "C6420 (14ª)", "09D4": "XR11 (15ª)",
}
SEM_CATALOGO_ESPERADO = {"0235": "R710 (11ª): sem catálogo oficial conferido, Atualização manual (CLAUDE.md 11a)"}
RAIZ_CONHECIDA = {"SoftwareComponent", "Prerequisites", "SoftwareBundle", "InventoryComponent", "ReleaseNotes"}
"""Filhos de ``<Manifest>`` já vistos. ``SoftwareBundle``/``InventoryComponent``/``ReleaseNotes`` (só um título, catálogo
26.10.08) não são usados pelo MSscan; qualquer outro é formato novo a conferir."""


def baixar(url: str) -> bytes:
    if not url.startswith("https://downloads.dell.com/"):
        raise ValueError(f"fora do domínio oficial: {url}")
    requisicao = urllib.request.Request(url, headers={"User-Agent": "MSscan-conferencia"})
    with urllib.request.urlopen(requisicao, timeout=180) as resposta:  # noqa: S310 - só https em downloads.dell.com
        return resposta.read()


def versoes(dados: dict, system_id: str) -> dict[str, str]:
    """BIOS e iDRAC mais novos do catálogo para o System ID (categoria do iDRAC pelo nome, como na interface)."""
    achados: dict[str, str] = {}
    for comp in dados["componentes"]:
        if not any(s["system_id"] == system_id for s in comp["sistemas"]):
            continue
        # iDRAC pela categoria ("iDRAC with Lifecycle Controller"), firmware só (o iSM é APAC na mesma categoria às vezes).
        chave = "BIOS" if comp["tipo"] == "BIOS" else "iDRAC" if comp["tipo"] == "FRMW" and "idrac" in comp["categoria"].lower() else ""
        if chave and comp["versao"] > achados.get(chave, ""):  # só para leitura humana; a comparação real é a do MSscan
            achados[chave] = comp["versao"]
    return achados


def conferir_formato(bruto: bytes, dados: dict) -> list[str]:
    problemas = []
    raiz = ET.fromstring(gzip.decompress(bruto))
    novos = sorted({filho.tag for filho in raiz} - RAIZ_CONHECIDA)
    if novos:
        problemas.append(f"elementos novos na raiz do catálogo (o parser não lê): {', '.join(novos)}")
    for campo in ("caminho", "versao"):
        faltando = [c["nome"] for c in dados["componentes"] if not c[campo]]
        if faltando:
            problemas.append(f"{len(faltando)} componente(s) sem {campo} (ex.: {faltando[0]})")
    sem_hash = [c["nome"] for c in dados["componentes"] if not (c["hash"] or c["md5"])]
    if sem_hash:
        problemas.append(f"{len(sem_hash)} componente(s) sem hash (o MSscan recusa sem hash; ex.: {sem_hash[0]})")
    return problemas


def main() -> int:
    problemas: list[str] = []
    print(f"Catálogo atual: {URL_CATALOGO_DELL}")
    bruto = baixar(URL_CATALOGO_DELL)
    atual = parse_catalogo_dell(bruto)
    print(f"  versão {atual['versao']} de {atual['data']}, {len(atual['componentes'])} componentes "
          f"({sum(1 for c in atual['componentes'] if c.get('prerequisito'))} em <Prerequisites>)")
    problemas += conferir_formato(bruto, atual)

    legados = []
    for entrada in carregar_legados():
        conteudo = baixar(entrada["url"])
        md5, sha = hashlib.md5(conteudo).hexdigest(), hashlib.sha256(conteudo).hexdigest()  # noqa: S324 - MD5 do índice oficial
        ok = md5 == entrada["md5"] and sha == entrada["sha256"]
        print(f"Catálogo antigo {entrada['versao']}: {'MD5 e SHA-256 conferidos' if ok else 'HASH DIFERENTE'}")
        if not ok:
            problemas.append(f"catálogo antigo {entrada['versao']} com hash diferente do fixado (o MSscan vai recusá-lo)")
            continue
        legados.append((entrada["versao"], parse_catalogo_dell(conteudo)))

    print("\nModelos de referência:")
    for system_id, nome in {**REFERENCIA, **SEM_CATALOGO_ESPERADO}.items():
        origem, dados = ("atual", atual) if tem_bios(atual, system_id) else \
            next(((f"antigo {v}", d) for v, d in legados if tem_bios(d, system_id)), (None, None))  # fmt: skip
        if dados is None:
            print(f"  {system_id} {nome}: sem catálogo")
            if system_id not in SEM_CATALOGO_ESPERADO:
                problemas.append(f"{nome} ({system_id}) não está em nenhum catálogo (saiu do atual? acrescentar um antigo)")
            continue
        v = versoes(dados, system_id)
        print(f"  {system_id} {nome}: catálogo {origem}; BIOS {v.get('BIOS', '?')}, iDRAC {v.get('iDRAC', '?')}")
        if system_id in SEM_CATALOGO_ESPERADO:
            problemas.append(f"{nome} apareceu num catálogo: rever a Atualização manual da 11ª geração")

    print("\n" + ("Nada a conferir." if not problemas else "A CONFERIR:\n- " + "\n- ".join(problemas)))
    return 1 if problemas else 0


if __name__ == "__main__":
    sys.exit(main())

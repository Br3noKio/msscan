"""Configuração da análise por IA em ``MSSCAN/config/ia.conf`` (CLAUDE.md 10c).

Formato (INI simples, pensado para o Bloco de Notas do Windows)::

    [ia]
    principal = gemini_gratuito
    reserva = openrouter_gratuito

    [gemini_gratuito]
    chave = AIza...

    [openrouter_gratuito]
    chave = sk-or-...
    ; modelo = openrouter/free        (opcional: troca o modelo do catálogo)

As seções têm o ``id`` do catálogo (``ia_provedores.yaml``). Uma seção fora do catálogo é um provedor
próprio (ex.: Ollama na rede local) e precisa de ``provedor``, ``url`` e ``modelo``.

Tolerância: fim de linha Windows (CRLF) ou Linux, BOM do Bloco de Notas, arquivo em ANSI (cp1252),
comentários com ``;`` ou ``#`` e espaços, aspas ou quebras de linha coladas junto com a chave.

Compatibilidade: ``ia.conf`` sem a seção ``[ia]`` é o formato da v0.8.2 (seções na ordem de tentativa,
``chave`` = nome de um arquivo em ``config/``); sem ``ia.conf``, vale ``config/gemini.key`` e depois
``config/ia.key``. O arquivo é lido direto do disco (nunca pelo executor): nada dele vai para coletas,
histórico de comandos, logs ou relatórios. Na tela, a chave aparece no máximo com os 4 últimos caracteres.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from msscan.ia.catalogo import Catalogo, EntradaCatalogo

ARQUIVO = "config/ia.conf"
SECAO_GERAL = "ia"
CAMPOS_LEGADO_PADRAO = (("gemini", {"provedor": "gemini"}), ("anthropic", {"provedor": "anthropic"}))
_RE_SECAO = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*$")
_RE_CAMPO = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.*?)\s*$")
_INVISIVEIS = dict.fromkeys(map(ord, "﻿​‌‍⁠ "), None)
FIM_DE_LINHA = "\r\n"
"""Gravação sempre com fim de linha Windows: o Bloco de Notas mostra certo e o MSscan lê os dois."""


@dataclass
class ConfigIA:
    """Conteúdo do ``ia.conf`` já interpretado."""

    existe: bool
    legado: bool = False
    principal: str = ""
    reserva: str = ""
    secoes: dict[str, dict[str, str]] = field(default_factory=dict)
    ordem: list[str] = field(default_factory=list)
    """Ordem das seções no arquivo (usada no formato antigo, sem ``[ia]``)."""
    avisos: list[str] = field(default_factory=list)


def decodificar(dados: bytes) -> str:
    """Texto de um arquivo salvo pelo Bloco de Notas (UTF-8 com ou sem BOM, ou ANSI/cp1252)."""
    try:
        return dados.decode("utf-8-sig")
    except UnicodeDecodeError:
        return dados.decode("cp1252", errors="replace")


def normalizar_chave(valor: str) -> str:
    """Chave como colada pelo técnico, sem espaços, quebras de linha, aspas e caracteres invisíveis."""
    texto = "".join(valor.translate(_INVISIVEIS).split())
    while len(texto) >= 2 and texto[0] == texto[-1] and texto[0] in "\"'`":
        texto = texto[1:-1]
    return texto.strip("<>")


def mascarar(chave: str) -> str:
    """Nunca mais que os 4 últimos caracteres (e nada, se a chave for curta)."""
    chave = normalizar_chave(chave)
    if not chave:
        return ""
    return "••••••••" + (chave[-4:] if len(chave) >= 12 else "")


def interpretar(texto: str) -> ConfigIA:
    """Interpreta o ``ia.conf``. Linhas que não entende viram aviso (nunca exceção)."""
    config = ConfigIA(existe=True)
    atual: str | None = None
    ultimo = ""
    for numero, linha in enumerate(texto.replace("\r\n", "\n").replace("\r", "\n").split("\n"), start=1):
        limpa = linha.translate(_INVISIVEIS).strip()
        if not limpa or limpa[0] in ";#":
            continue
        secao = _RE_SECAO.match(limpa)
        if secao:
            atual = secao.group(1).strip().lower()
            if atual not in config.secoes:
                config.secoes[atual] = {}
                config.ordem.append(atual)
            ultimo = ""
            continue
        campo = _RE_CAMPO.match(limpa)
        if not campo and atual is not None and ultimo == "chave" and not config.secoes[atual].get("chave"):
            config.secoes[atual]["chave"] = limpa  # chave colada na linha de baixo de "chave ="
            continue
        if not campo or atual is None:
            config.avisos.append(f"linha {numero} ignorada (esperado [seção] ou campo = valor)")
            continue
        nome, valor = campo.group(1).lower(), campo.group(2)
        ultimo = nome
        if nome != "chave":  # comentário no fim da linha (a chave nunca tem espaço, então nem precisa)
            valor = re.split(r"\s+[;#]", valor, maxsplit=1)[0].strip()
        config.secoes[atual][nome] = valor
    geral = config.secoes.get(SECAO_GERAL)
    if geral is None:
        config.legado = bool(config.secoes)
    else:
        config.principal = geral.get("principal", "").strip().lower()
        config.reserva = geral.get("reserva", "").strip().lower()
        if config.reserva == config.principal:
            config.reserva = ""
    return config


def ler_config(pasta_dados: str | None) -> ConfigIA:
    """Lê ``config/ia.conf`` do local de dados (``ConfigIA(existe=False)`` se não houver)."""
    if not pasta_dados:
        return ConfigIA(existe=False)
    arquivo = Path(pasta_dados) / ARQUIVO
    try:
        dados = arquivo.read_bytes()
    except FileNotFoundError:
        return ConfigIA(existe=False)
    except OSError as erro:
        return ConfigIA(existe=False, avisos=[f"config/ia.conf ilegível: {erro.strerror or erro}"])
    return interpretar(decodificar(dados))


def ler_arquivo_chave(pasta_dados: str | None, nome: str) -> str:
    """Chave num arquivo separado em ``config/`` (compatibilidade com v0.7/v0.8.2). ``""`` se não houver."""
    if not pasta_dados or not nome or "/" in nome or "\\" in nome or ".." in nome:
        return ""
    try:
        return normalizar_chave(decodificar((Path(pasta_dados) / "config" / nome).read_bytes()))
    except OSError:
        return ""


def chave_de(pasta_dados: str | None, config: ConfigIA, entrada: EntradaCatalogo) -> str:
    """Chave de um provedor do catálogo: a do ``ia.conf`` ou, se vazia, a do arquivo antigo."""
    secao = config.secoes.get(entrada.id, {}) if not config.legado else {}
    chave = normalizar_chave(secao.get("chave", ""))
    if chave:
        return chave
    arquivo = normalizar_chave(secao.get("arquivo_chave", "")) or entrada.arquivo_chave
    return ler_arquivo_chave(pasta_dados, arquivo)


# --- Modelo comentado para a primeira execução ----------------------------------------------------


def _comentar(texto: str, largura: int = 96) -> list[str]:
    linhas, atual = [], ";"
    for palavra in texto.split():
        if len(atual) + 1 + len(palavra) > largura:
            linhas.append(atual)
            atual = ";"
        atual += " " + palavra
    linhas.append(atual)
    return linhas


def modelo_ia_conf(catalogo: Catalogo, principal: str = "gemini_gratuito", reserva: str = "") -> str:
    """``ia.conf`` de exemplo, comentado em português, para preencher no Bloco de Notas."""
    linhas = [
        "; ============================================================================================",
        "; MSscan - configuração da análise por IA (opcional)",
        "; ============================================================================================",
        ";",
        "; A análise por IA é OPCIONAL: sem chave, o diagnóstico funciona normalmente.",
        "; Pode editar este arquivo no Bloco de Notas do Windows. Linhas começando com ; são comentários.",
        ";",
        "; PASSO A PASSO",
        "; 1. Escolha uma IA na lista abaixo (as GRATUITAS vêm primeiro).",
        "; 2. Abra o link \"Chave:\" dela, entre com sua conta e crie uma chave de API.",
        "; 3. Copie a chave e cole depois de \"chave =\" na seção dela, por exemplo:",
        ";        [gemini_gratuito]",
        ";        chave = AIzaSy...sua-chave...",
        ";    (espaços a mais ou a chave colada em outra linha não atrapalham)",
        "; 4. Na seção [ia], coloque o nome da seção escolhida em \"principal\".",
        ";    Opcional: outra IA em \"reserva\" - ela é usada se a principal der limite, erro ou demora.",
        "; 5. Salve o arquivo e rode o MSscan. Também dá para fazer tudo pela tela \"Configurar IA\".",
        ";",
        "; PRIVACIDADE: antes do envio o MSscan remove serial, service tag, UUID, asset tag, MAC, IP,",
        "; hostname e WWN, e mostra o que será enviado. Planos gratuitos podem usar os dados para treinar",
        "; os modelos. Nunca compartilhe este arquivo: ele contém as suas chaves.",
        "",
        "[ia]",
        f"principal = {principal}",
        f"reserva = {reserva}".rstrip(),
        "",
    ]
    for categoria, titulo in (("gratuita", "IAs GRATUITAS"), ("paga", "IAs PAGAS")):
        linhas += ["; " + "=" * 92, f"; {titulo}", "; " + "=" * 92, ""]
        for entrada in catalogo.por_categoria(categoria):
            linhas.append(f"; {entrada.nome} ({entrada.categoria}) - modelo padrão: {entrada.modelo_padrao}")
            linhas.append(f"; Chave: {entrada.link_chave}")
            linhas += _comentar(f"Limites: {entrada.limites}")
            linhas += _comentar(f"Privacidade: {entrada.privacidade}")
            linhas += [f"[{entrada.id}]", "chave =", ""]
    return FIM_DE_LINHA.join(linhas) + FIM_DE_LINHA


# --- Gravação preservando comentários ---------------------------------------------------------


def _definir(linhas: list[str], secao: str, campo: str, valor: str) -> list[str]:
    """Troca (ou cria) ``campo = valor`` dentro de ``[secao]``, sem mexer no resto do arquivo."""
    inicio = next((i for i, l in enumerate(linhas) if (m := _RE_SECAO.match(l)) and m.group(1).strip().lower() == secao), None)
    novo = f"{campo} = {valor}".rstrip()
    if inicio is None:
        while linhas and not linhas[-1].strip():
            linhas.pop()
        return linhas + ["", f"[{secao}]", novo, ""]
    fim = next((i for i in range(inicio + 1, len(linhas)) if _RE_SECAO.match(linhas[i])), len(linhas))
    for i in range(inicio + 1, fim):
        m = _RE_CAMPO.match(linhas[i])
        if m and m.group(1).lower() == campo and not linhas[i].lstrip().startswith((";", "#")):
            linhas[i] = novo
            return linhas
    posicao = inicio + 1
    for i in range(inicio + 1, fim):
        if linhas[i].strip() and not linhas[i].lstrip().startswith((";", "#")):
            posicao = i + 1
    linhas.insert(posicao, novo)
    return linhas


def atualizar_texto(texto: str, principal: str | None = None, reserva: str | None = None,
                    chaves: dict[str, str] | None = None) -> str:  # fmt: skip
    """Novo conteúdo do ``ia.conf`` com as escolhas/chaves; comentários e demais linhas preservados."""
    linhas = texto.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if linhas and linhas[-1] == "":
        linhas.pop()
    if principal is not None:
        linhas = _definir(linhas, SECAO_GERAL, "principal", principal)
    if reserva is not None:
        linhas = _definir(linhas, SECAO_GERAL, "reserva", reserva)
    for identificador, chave in (chaves or {}).items():
        linhas = _definir(linhas, identificador, "chave", normalizar_chave(chave))
    return FIM_DE_LINHA.join(linhas) + FIM_DE_LINHA


def remover_chave_do_texto(texto: str, identificador: str) -> str:
    """``ia.conf`` com a linha ``chave =`` da seção esvaziada (pedido de 01/10/2026, "Remover chave")."""
    linhas = texto.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if linhas and linhas[-1] == "":
        linhas.pop()
    return FIM_DE_LINHA.join(_definir(linhas, identificador, "chave", "")) + FIM_DE_LINHA


def gravar_texto(pasta_dados: str, texto: str) -> Path:
    """Grava o ``ia.conf`` de forma atômica (arquivo temporário + troca). Quem chama abre a janela de escrita."""
    arquivo = Path(pasta_dados) / ARQUIVO
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    temporario = arquivo.with_name(arquivo.name + ".novo")
    temporario.write_bytes(texto.encode("utf-8"))
    os.replace(temporario, arquivo)
    return arquivo


def texto_atual_ou_modelo(pasta_dados: str, catalogo: Catalogo) -> tuple[str, str]:
    """``(texto, aviso)``: o ``ia.conf`` atual ou, se não existir, o modelo comentado.

    Formato antigo (sem ``[ia]``): guardado como ``ia.conf.antigo`` e substituído pelo modelo novo
    (o aviso diz isso ao técnico; chaves em arquivos ``.key`` continuam valendo).
    """
    arquivo = Path(pasta_dados) / ARQUIVO
    try:
        texto = decodificar(arquivo.read_bytes())
    except FileNotFoundError:
        return modelo_ia_conf(catalogo), ""
    if interpretar(texto).legado:
        arquivo.with_name("ia.conf.antigo").write_bytes(texto.encode("utf-8"))
        return modelo_ia_conf(catalogo), "O ia.conf no formato antigo foi guardado como config/ia.conf.antigo."
    return texto, ""


def criar_modelo_se_faltar(pasta_dados: str | None, catalogo: Catalogo) -> bool:
    """Primeira execução: cria ``config/ia.conf`` comentado. ``True`` se criou. Nunca sobrescreve.

    Quem já usa chaves em arquivos (v0.7: ``ia.key``; v0.8.2: ``gemini.key``) continua funcionando: o modelo
    já aponta principal/reserva para elas.
    """
    if not pasta_dados or (Path(pasta_dados) / ARQUIVO).exists():
        return False
    com_chave = [e.id for e in catalogo.entradas if e.arquivo_chave and ler_arquivo_chave(pasta_dados, e.arquivo_chave)]
    principal = com_chave[0] if com_chave else "gemini_gratuito"
    reserva = com_chave[1] if len(com_chave) > 1 else ""
    try:
        gravar_texto(pasta_dados, modelo_ia_conf(catalogo, principal, reserva))
    except OSError:
        return False
    return True

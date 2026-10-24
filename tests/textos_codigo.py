"""Detectores de texto de interface escrito direto no código (usados por tests/test_textos.py; pedido de 09/10/2026).

JavaScript: tokenizador mínimo (literais fora de comentários e regex). Python: AST (literais e f-strings fora de docstrings,
comparações, chaves de dicionário e argumentos de métodos de texto). Os mesmos critérios usados na extração."""

import ast
import re

_ANTES_DE_REGEX = set("(,=:[!&|?{};+-*%<>~^") | {""}
_PALAVRAS_ANTES_DE_REGEX = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw"}


def literais(src: str) -> list[dict]:
    """[{inicio, fim, aspas, partes: [texto...], expressoes: [(ini, fim)...]}] — template com ${} vira partes + expressões."""
    saida, i, n = [], 0, len(src)
    ultimo_sig = ""  # último caractere significativo (para decidir se "/" abre regex)
    ultima_palavra = ""
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            fim = src.find("\n", i)
            i = n if fim < 0 else fim
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            i = src.index("*/", i + 2) + 2
            continue
        if c == "/" and (ultimo_sig in _ANTES_DE_REGEX or ultima_palavra in _PALAVRAS_ANTES_DE_REGEX):
            j, classe = i + 1, False
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    classe = True
                elif src[j] == "]":
                    classe = False
                elif src[j] == "/" and not classe:
                    break
                elif src[j] == "\n":
                    break
                j += 1
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            i, ultimo_sig, ultima_palavra = j, ")", ""
            continue
        if c in "\"'":
            j = i + 1
            while src[j] != c:
                j += 2 if src[j] == "\\" else 1
            saida.append({"inicio": i, "fim": j + 1, "aspas": c, "partes": [src[i + 1:j]], "expressoes": []})
            i, ultimo_sig, ultima_palavra = j + 1, ")", ""
            continue
        if c == "`":
            j, partes, expressoes, atual = i + 1, [], [], ""
            while src[j] != "`":
                if src[j] == "\\":
                    atual += src[j:j + 2]
                    j += 2
                    continue
                if src[j] == "$" and src[j + 1] == "{":
                    partes.append(atual)
                    atual = ""
                    k, profundidade = j + 2, 1
                    while profundidade:
                        if src[k] == "{":
                            profundidade += 1
                        elif src[k] == "}":
                            profundidade -= 1
                        elif src[k] in "\"'`":  # string dentro da expressão
                            q = src[k]
                            k += 1
                            while src[k] != q:
                                k += 2 if src[k] == "\\" else 1
                        k += 1
                    expressoes.append((j + 2, k - 1))
                    j = k
                    continue
                atual += src[j]
                j += 1
            partes.append(atual)
            saida.append({"inicio": i, "fim": j + 1, "aspas": "`", "partes": partes, "expressoes": expressoes})
            i, ultimo_sig, ultima_palavra = j + 1, ")", ""
            continue
        if c.isalnum() or c in "_$":
            m = re.match(r"[A-Za-z0-9_$]+", src[i:])
            ultima_palavra = m.group(0)
            ultimo_sig = "a"
            i += len(ultima_palavra)
            continue
        if not c.isspace():
            ultimo_sig, ultima_palavra = c, ""
        i += 1
    return saida


_NAO_HUMANO = re.compile(r"^[a-z0-9_.:/#\-\[\]=*\"' >+~(),^$|]*$")
_LETRA = re.compile(r"[A-Za-zÀ-ÿ]")
_PALAVRAS_PT = {"de", "do", "da", "dos", "das", "o", "os", "as", "em", "no", "na", "nos", "nas", "para", "com", "sem", "que",
                "ou", "por", "um", "uma", "se", "ao", "aos", "pelo", "pela", "ate", "desde", "entre"}


def humano(texto: str) -> bool:
    """Texto para pessoas (e não id, classe CSS, chave, rota, seletor, formato)."""
    t = texto.strip()
    if not _LETRA.search(t):
        return False
    if re.search(r"[À-ÿ]", t):
        return True
    if re.fullmatch(r"[a-zà-ÿ]+:", t):
        return True  # rótulo de uma palavra ("esperando:")
    if not re.search(r"\s", t) and re.search(r"[_.\-/=?]", t):
        return False  # identificador, chave, classe, rota
    if _NAO_HUMANO.fullmatch(t):
        # minúsculo: classes CSS, ids, rotas e seletores — a não ser que pareça frase (palavra comum do português ou
        # terminada em dois-pontos, como "restante estimado:")
        palavras = set(re.findall(r"[a-z]+", t))
        return bool(palavras & _PALAVRAS_PT) or (t.endswith(":") and " " in t)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", t) and not t[0].isupper():
        return False
    return bool(re.search(r"[A-Za-z]{2,}", t))


TECNICOS = {
    "OK", "N/A", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End", "Enter", "Escape", "Tab", "Esc", "Backspace",
    "Delete", "PageUp", "PageDown", "Shift", "Control", "Alt", "Meta", "X-MSscan-Token", "Content-Type", "GET", "POST", "HTTP ",
    "MSscan", "use strict", "Dell", "iDRAC", "IA", "Ctrl", "Add", "Subtract", "Equal", "Minus", "DOMContentLoaded",
    '\\"DejaVu Sans Mono\\", \\"Liberation Mono\\", monospace',
}
"""Literais que não são texto da interface (teclas, métodos, cabeçalhos) ou nomes próprios sem tradução."""


def de_interface(texto: str) -> bool:
    t = texto.strip()
    if t in TECNICOS or texto in TECNICOS:
        return False
    if re.fullmatch(r"[A-Z0-9_ ]+", t):
        return False  # constantes de status/código da API e palavras de confirmação digitadas (ATUALIZAR...)
    return humano(texto)


def textos_fora(src: str) -> list[tuple[int, str]]:
    """(linha, literal) de cada texto de interface escrito direto no código (fora do arquivo central)."""
    saida = []
    for l in literais(src):
        if src[max(0, l["inicio"] - 2):l["inicio"]] == "T(":
            continue  # chave do arquivo central
        if any(de_interface(p) for p in l["partes"]):
            saida.append((src.count("\n", 0, l["inicio"]) + 1, src[l["inicio"]:l["fim"]]))
    return saida


# --- Python ---

PROIBIDOS_PAI = (ast.Compare, ast.Subscript, ast.JoinedStr)


def humano_py(texto):
    t = re.sub(r"<[^<>]*>", " ", texto).strip()  # marcação HTML não é texto; o que sobra entre as tags é
    if len(t) < 4 or not re.search(r"[A-Za-zÀ-ÿ]{2}", t):
        return False
    if re.search(r"\{ |; \}", t):
        return False  # folha de estilo (CSS) do relatório HTML
    if re.fullmatch(r"[A-Za-z0-9_.:/#\-\[\]=*,%{}() ]*", t) and not re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}", t):
        return False  # ids, rotas, formatos
    if t.startswith(("/", "http", "-", "%")) or re.fullmatch(r"[A-Z0-9_ ]+", t):
        return False
    return bool(re.search(r"\s", t) or re.search(r"[À-ÿ]", t) or t[0].isupper())


def textos_fora_py(src):
    """(linha, texto) de cada texto de interface escrito direto num módulo Python (fora de ``textos.t``)."""
    arvore = ast.parse(src)
    pais = {}
    for no in ast.walk(arvore):
        for filho in ast.iter_child_nodes(no):
            pais[filho] = no
    docs = set()
    for no in ast.walk(arvore):
        if isinstance(no, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and no.body:
            primeiro = no.body[0]
            if isinstance(primeiro, ast.Expr) and isinstance(primeiro.value, (ast.Constant, ast.JoinedStr)):
                docs.add(primeiro.value)
        # docstrings de atributos: string solta depois de uma atribuição
        for corpo in (getattr(no, "body", None), getattr(no, "orelse", None)):
            if isinstance(corpo, list):
                for st in corpo:
                    if isinstance(st, ast.Expr) and isinstance(st.value, (ast.Constant, ast.JoinedStr)):
                        docs.add(st.value)
    linhas = src.splitlines(keepends=True)
    deslocamentos = [0]
    for l in linhas:
        deslocamentos.append(deslocamentos[-1] + len(l))

    def pos(linha, col):
        return deslocamentos[linha - 1] + len(linhas[linha - 1].encode()[:col].decode(errors="ignore"))

    achados = []
    for no in ast.walk(arvore):
        if no in docs or not isinstance(no, (ast.Constant, ast.JoinedStr)):
            continue
        pai = pais.get(no)
        avo, dentro_de_fstring = pai, False
        while avo is not None:
            if isinstance(avo, ast.JoinedStr):
                dentro_de_fstring = True
            avo = pais.get(avo)
        if dentro_de_fstring:
            continue
        if isinstance(pai, PROIBIDOS_PAI) or (isinstance(pai, ast.Dict) and no in pai.keys):
            continue
        if isinstance(pai, ast.Call) and isinstance(pai.func, ast.Attribute) and pai.func.attr in (
                "startswith", "endswith", "split", "replace", "strip", "rstrip", "lstrip", "index", "find", "count", "join",
                "get", "setdefault", "pop", "removeprefix", "removesuffix", "format", "fullmatch", "match", "search", "sub",
                "compile", "findall"):
            continue
        if isinstance(pai, ast.Call) and isinstance(pai.func, ast.Name) and pai.func.id in ("re", "compile", "getattr", "hasattr",
                                                                                             "isinstance", "Path", "open"):
            continue
        if isinstance(pai, ast.keyword) and pai.arg in ("encoding", "errors", "prefix", "suffix", "name", "texto_ignorado"):
            continue
        if isinstance(no, ast.Constant):
            if not isinstance(no.value, str) or not humano_py(no.value):
                continue
            texto, args = no.value, []
        else:
            partes, args, ok = [], [], True
            for v in no.values:
                if isinstance(v, ast.Constant):
                    partes.append(str(v.value).replace("{", "{{").replace("}", "}}"))
                elif isinstance(v, ast.FormattedValue) and v.conversion == -1 and v.format_spec is None:
                    partes.append("{%d}" % len(args))
                    args.append(ast.get_source_segment(src, v.value))
                else:
                    ok = False
            if not ok:
                continue
            texto = "".join(partes).replace("{{", "{").replace("}}", "}")
            if not humano_py(re.sub(r"\{\d+\}", "", texto)):
                continue
        achados.append((no.lineno, texto))
    return achados

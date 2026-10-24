"""Parsers: funções PURAS que convertem a saída de ferramentas em dados.

Regras:
- nenhum parser executa comandos ou lê arquivos; recebe texto e devolve dados;
- entrada vazia ou "nada encontrado" devolve estrutura vazia;
- entrada irreconhecível levanta ``ErroParser`` (quem chama registra a tentativa);
- todo parser tem teste com fixture real em ``tests/fixtures``.
"""


class ErroParser(ValueError):
    """A saída da ferramenta não está no formato esperado."""

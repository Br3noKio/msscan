"""Catálogo de provedores de IA (CLAUDE.md 10c), em ``ia_provedores.yaml``.

O catálogo embutido fica ao lado deste módulo. Uma cópia em ``MSSCAN/config/ia_provedores.yaml``
(no pendrive) tem prioridade quando for válida — assim ele é atualizado sem reconstruir a ISO. Cópia
inválida é ignorada com aviso (nunca derruba o programa). Nenhuma chave vive no catálogo.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from msscan.core.executor import validar_url_ia

ARQUIVO_EMBUTIDO = Path(__file__).with_name("ia_provedores.yaml")
ARQUIVO_PENDRIVE = "config/ia_provedores.yaml"
TIPOS_API = ("gemini", "openai", "anthropic")
CATEGORIAS = ("gratuita", "paga")
AVISOS = {
    "treinamento": "Planos gratuitos podem usar os dados enviados para treinar os modelos.",
    "china": "Os dados são processados e armazenados na China.",
    "limite_pequeno": "O limite do plano gratuito é menor que um diagnóstico típico: a análise pode ser recusada.",
}
_RE_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_RE_MODELO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_RE_ARQUIVO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class EntradaCatalogo:
    """Um provedor do catálogo (sem chave)."""

    id: str
    nome: str
    categoria: str
    tipo_api: str
    url: str
    modelo_padrao: str
    link_chave: str
    arquivo_chave: str
    limites: str
    privacidade: str
    avisos: tuple[str, ...]
    verificado_em: str
    fontes: tuple[str, ...] = field(default=())
    modelo_alternativo: str = ""
    """Outro modelo gratuito do mesmo provedor (mesma chave), tentado quando o padrão está sobrecarregado."""

    def url_chamada(self, modelo: str | None = None) -> str:
        """URL completa do POST para o ``modelo`` (padrão: o do catálogo)."""
        return url_chamada(self.tipo_api, self.url, modelo or self.modelo_padrao)

    def para_dict(self) -> dict[str, Any]:
        """Para a interface e o relatório."""
        return {
            "id": self.id, "nome": self.nome, "categoria": self.categoria, "tipo_api": self.tipo_api,
            "modelo_padrao": self.modelo_padrao, "link_chave": self.link_chave, "limites": self.limites,
            "privacidade": self.privacidade, "avisos": [{"id": a, "texto": AVISOS[a]} for a in self.avisos],
            "verificado_em": self.verificado_em,
        }  # fmt: skip


def url_chamada(tipo_api: str, base: str, modelo: str) -> str:
    """Endpoint de cada tipo de API a partir da URL base."""
    base = base.rstrip("/")
    if tipo_api == "gemini":
        return f"{base}/models/{modelo}:generateContent"
    if tipo_api == "anthropic":
        return f"{base}/messages"
    return f"{base}/chat/completions"


@dataclass
class Catalogo:
    """Catálogo carregado: entradas na ordem do arquivo, origem e avisos de validação."""

    entradas: list[EntradaCatalogo]
    origem: str
    avisos: list[str] = field(default_factory=list)

    def obter(self, identificador: str) -> EntradaCatalogo | None:
        return next((e for e in self.entradas if e.id == identificador), None)

    def por_categoria(self, categoria: str) -> list[EntradaCatalogo]:
        return [e for e in self.entradas if e.categoria == categoria]


class CatalogoInvalido(ValueError):
    """Catálogo fora do formato (mensagem em português, mostrada ao técnico)."""


def _texto(item: dict[str, Any], campo: str, obrigatorio: bool = True) -> str:
    valor = item.get(campo)
    if valor is None and not obrigatorio:
        return ""
    if isinstance(valor, datetime.date):  # o YAML lê 2026-09-28 como data
        return valor.isoformat()
    if not isinstance(valor, (str, int, float)) or not str(valor).strip():
        raise CatalogoInvalido(f"campo '{campo}' ausente ou vazio")
    return " ".join(str(valor).split())


def _entrada(item: Any) -> EntradaCatalogo:
    if not isinstance(item, dict):
        raise CatalogoInvalido("cada provedor deve ser um bloco com campos")
    identificador = _texto(item, "id")
    if not _RE_ID.match(identificador):
        raise CatalogoInvalido(f"id inválido: {identificador!r} (use letras minúsculas, números e _)")
    try:
        categoria = _texto(item, "categoria")
        tipo = _texto(item, "tipo_api")
        if categoria not in CATEGORIAS:
            raise CatalogoInvalido(f"categoria deve ser {' ou '.join(CATEGORIAS)}")
        if tipo not in TIPOS_API:
            raise CatalogoInvalido(f"tipo_api deve ser um de: {', '.join(TIPOS_API)}")
        modelo = _texto(item, "modelo_padrao")
        if not _RE_MODELO.match(modelo) or ".." in modelo:
            raise CatalogoInvalido("modelo_padrao inválido")
        url = _texto(item, "url")
        motivo = validar_url_ia(url_chamada(tipo, url, modelo))
        if motivo:
            raise CatalogoInvalido(f"url: {motivo}")
        alternativo = _texto(item, "modelo_alternativo", obrigatorio=False)
        if alternativo and (not _RE_MODELO.match(alternativo) or ".." in alternativo or validar_url_ia(url_chamada(tipo, url, alternativo))):
            raise CatalogoInvalido("modelo_alternativo inválido")
        link = _texto(item, "link_chave")
        if not link.startswith("https://"):
            raise CatalogoInvalido("link_chave deve ser HTTPS")
        arquivo = _texto(item, "arquivo_chave", obrigatorio=False)
        if arquivo and (not _RE_ARQUIVO.match(arquivo) or ".." in arquivo):
            raise CatalogoInvalido("arquivo_chave deve ser só um nome de arquivo (sem caminho)")
        avisos = item.get("avisos") or []
        if not isinstance(avisos, list) or any(a not in AVISOS for a in avisos):
            raise CatalogoInvalido(f"avisos devem estar entre: {', '.join(AVISOS)}")
        fontes = item.get("fontes") or []
        if not isinstance(fontes, list) or any(not str(f).startswith("https://") for f in fontes):
            raise CatalogoInvalido("fontes devem ser uma lista de links HTTPS")
        return EntradaCatalogo(
            id=identificador, nome=_texto(item, "nome"), categoria=categoria, tipo_api=tipo, url=url.rstrip("/"),
            modelo_padrao=modelo, link_chave=link, arquivo_chave=arquivo, limites=_texto(item, "limites"),
            privacidade=_texto(item, "privacidade"), avisos=tuple(avisos),
            verificado_em=_texto(item, "verificado_em"), fontes=tuple(str(f) for f in fontes),
            modelo_alternativo=alternativo,
        )  # fmt: skip
    except CatalogoInvalido as erro:
        raise CatalogoInvalido(f"[{identificador}] {erro}") from None


def interpretar_catalogo(texto: str) -> list[EntradaCatalogo]:
    """Valida o YAML do catálogo e devolve as entradas. Levanta ``CatalogoInvalido``."""
    try:
        dados = yaml.safe_load(texto)
    except yaml.YAMLError as erro:
        raise CatalogoInvalido(f"YAML inválido: {erro}") from None
    if not isinstance(dados, dict) or dados.get("versao") != 1 or not isinstance(dados.get("provedores"), list):
        raise CatalogoInvalido("esperado 'versao: 1' e a lista 'provedores'")
    entradas = [_entrada(item) for item in dados["provedores"]]
    ids = [e.id for e in entradas]
    repetidos = sorted({i for i in ids if ids.count(i) > 1})
    if repetidos:
        raise CatalogoInvalido(f"ids repetidos: {', '.join(repetidos)}")
    if not entradas:
        raise CatalogoInvalido("nenhum provedor no catálogo")
    return entradas


_CACHE: dict[tuple[str, float], list[EntradaCatalogo]] = {}


def _entradas_do_arquivo(arquivo: Path, encoding: str) -> list[EntradaCatalogo]:
    """Entradas validadas, com cache pela data de modificação (a interface consulta o estado a cada 0,5 s)."""
    chave = (str(arquivo), arquivo.stat().st_mtime)
    if chave not in _CACHE:
        _CACHE.clear() if len(_CACHE) > 16 else None
        _CACHE[chave] = interpretar_catalogo(arquivo.read_text(encoding=encoding, errors="replace"))
    return _CACHE[chave]


def carregar_catalogo(pasta_dados: str | None = None) -> Catalogo:
    """Catálogo do pendrive (se houver e for válido) ou o embutido. Nunca levanta exceção."""
    avisos: list[str] = []
    if pasta_dados:
        arquivo = Path(pasta_dados) / ARQUIVO_PENDRIVE
        if arquivo.is_file():
            try:
                return Catalogo(_entradas_do_arquivo(arquivo, "utf-8-sig"), f"pendrive ({ARQUIVO_PENDRIVE})")
            except (OSError, CatalogoInvalido) as erro:
                avisos.append(f"{ARQUIVO_PENDRIVE} ignorado: {erro}. Usando o catálogo embutido.")
    try:
        return Catalogo(_entradas_do_arquivo(ARQUIVO_EMBUTIDO, "utf-8"), "embutido", avisos)
    except (OSError, CatalogoInvalido) as erro:  # só acontece com a imagem corrompida
        return Catalogo([], "indisponível", avisos + [f"catálogo embutido inválido: {erro}"])

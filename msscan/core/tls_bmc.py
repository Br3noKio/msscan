"""Modos de criptografia para falar com o BMC (CLAUDE.md 28.2) — NUNCA usados para a internet.

iDRACs antigos (iDRAC6/7/8 com firmware antigo) oferecem só DH de 1024 bits, chaves RSA de 1024, assinaturas SHA-1 ou
TLS 1.0, que o OpenSSL 3 do Debian 13 recusa por padrão (caso real: R530/iDRAC8 2.30, ``[SSL: DH_KEY_TOO_SMALL]``).
Os modos são tentados em ordem até um funcionar; o que funcionou fica guardado por BMC na sessão (``Executor.modos_tls_bmc``).
O BMC já é acessado sem conferir o certificado (autoassinado, rede local do servidor); os modos só relaxam o que o
handshake aceita, e só para ``https://<BMC>``.
"""

from __future__ import annotations

import ssl
import warnings
from typing import Any

PADRAO, SEM_DHE, SEGURANCA_REDUZIDA, LEGADO = "padrao", "sem_dhe", "seguranca_reduzida", "legado"
MODOS = (PADRAO, SEM_DHE, SEGURANCA_REDUZIDA, LEGADO)
DESCRICAO = {
    PADRAO: "configuração moderna padrão",
    SEM_DHE: "compatibilidade 1: só cifras sem DHE (ECDHE/RSA)",
    SEGURANCA_REDUZIDA: "compatibilidade 2: nível de segurança do OpenSSL reduzido (DH e chaves fracas, SHA-1)",
    LEGADO: "compatibilidade 3: todas as cifras do OpenSSL e renegociação legada (RC4 não é suportado)",
}
_CIFRAS_SEM_DHE = "ECDHE+AESGCM:ECDHE+CHACHA20:ECDHE+AES:RSA+AESGCM:RSA+AES:!aNULL:!eNULL:!MD5:!DSS"
_OP_LEGACY_SERVER_CONNECT = getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)  # Python 3.11 não tem a constante


def contexto(modo: str) -> ssl.SSLContext | None:
    """``SSLContext`` do modo (``None`` = o padrão do requests, igual ao de sempre)."""
    if modo == PADRAO:
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if modo == SEM_DHE:
        ctx.set_ciphers(_CIFRAS_SEM_DHE)
    elif modo == SEGURANCA_REDUZIDA:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    elif modo == LEGADO:
        ctx.set_ciphers("ALL:@SECLEVEL=0")
        with warnings.catch_warnings():  # TLSv1 está "obsoleto" no Python, mas é o que o iDRAC6/7 antigo fala
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.options |= _OP_LEGACY_SERVER_CONNECT
    else:
        raise ValueError(f"modo TLS desconhecido: {modo}")
    return ctx


def montar_sessao(sessao: Any, modo: str) -> None:
    """Liga o contexto do modo a uma ``requests.Session`` (só para ``https://``)."""
    ctx = contexto(modo)
    if ctx is None:
        return
    from requests.adapters import HTTPAdapter

    class _Adaptador(HTTPAdapter):
        def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
            kwargs["ssl_context"] = ctx
            super().init_poolmanager(*args, **kwargs)

    sessao.mount("https://", _Adaptador())


def vale_outro_modo(erro_tipo: str, erro: str, metodo: str = "GET") -> bool:
    """Falha que outro modo pode resolver: handshake TLS recusado (ou conexão cortada no handshake, comum no iDRAC6).

    O servidor fechar DEPOIS de receber o pedido (``RemoteDisconnected``) não é problema de criptografia: não troca de
    modo (um iDRAC9 ocupado não vira "modo de compatibilidade"). Fora do GET, só o erro TLS troca de modo: o pedido não
    chegou a ser enviado, então um login (POST) nunca é repetido depois de entregue (revisão independente, M5).
    """
    texto = erro.lower()
    if erro_tipo == "tls":
        return True
    if metodo.upper() != "GET" or erro_tipo != "conexao" or "remotedisconnected" in texto or "remote end closed" in texto:
        return False
    return "reset" in texto or "eof" in texto


def ordem(conhecido: str | None) -> list[str]:
    """Modo já aprendido para o BMC primeiro; depois os demais, na ordem."""
    return ([conhecido] if conhecido in MODOS else []) + [m for m in MODOS if m != conhecido]


__all__ = ["DESCRICAO", "LEGADO", "MODOS", "PADRAO", "SEGURANCA_REDUZIDA", "SEM_DHE", "contexto", "montar_sessao", "ordem",
           "vale_outro_modo"]

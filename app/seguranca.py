"""
Proteção do servidor local.

Por que isto existe: o aplicativo sobe um servidor HTTP no laptop. Sem trava,
**qualquer página aberta no navegador** consegue disparar um POST para
`http://127.0.0.1:porta/emitir/iniciar` — e aqui isso significa emitir nota
fiscal de verdade. CSRF contra servidor local é ataque conhecido, não hipótese.

Três camadas:

1. O servidor escuta apenas em `127.0.0.1` (feito em `lancador.py`).
2. Toda requisição precisa vir com o header `Host` apontando para o próprio
   laptop — barra ataque de DNS rebinding, em que um domínio externo passa a
   resolver para 127.0.0.1.
3. Toda rota que altera estado exige um token aleatório, gerado a cada
   inicialização. O lançador o entrega na URL que abre no navegador, e a
   partir daí ele vive num cookie `SameSite=Strict`, que o navegador não envia
   em requisições originadas de outros sites.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

NOME_COOKIE = "emissor_token"
NOME_HEADER = "X-Emissor-Token"
PARAMETRO_URL = "t"

# Métodos que não alteram estado passam sem token.
METODOS_SEGUROS = {"GET", "HEAD", "OPTIONS"}

# Hosts aceitos (a porta é validada à parte).
HOSTS_LOCAIS = {"127.0.0.1", "localhost", "[::1]", "::1"}


class Guardiao:
    """Guarda o token da sessão e valida as requisições."""

    def __init__(self, token: str | None = None):
        # 32 bytes de entropia: inviável de adivinhar por força bruta.
        self.token = token or secrets.token_urlsafe(32)

    def token_confere(self, candidato: str | None) -> bool:
        """Compara em tempo constante, para não vazar o token por temporização."""
        if not candidato:
            return False
        return hmac.compare_digest(candidato, self.token)

    def host_e_local(self, cabecalho_host: str | None) -> bool:
        if not cabecalho_host:
            return False
        # Separa a porta, cuidando do formato IPv6 "[::1]:8000".
        host = cabecalho_host.rsplit(":", 1)[0] if ":" in cabecalho_host else cabecalho_host
        if cabecalho_host.startswith("["):
            host = cabecalho_host.split("]")[0] + "]"
        return host.lower() in HOSTS_LOCAIS

    def token_da_requisicao(self, requisicao: Request) -> str | None:
        """Aceita o token no header (fetch), no cookie ou na URL de abertura."""
        return (
            requisicao.headers.get(NOME_HEADER)
            or requisicao.cookies.get(NOME_COOKIE)
            or requisicao.query_params.get(PARAMETRO_URL)
        )


def montar_middleware(guardiao: Guardiao):
    """Devolve o middleware que aplica as três camadas acima."""

    async def middleware(requisicao: Request, proxima):
        if not guardiao.host_e_local(requisicao.headers.get("host")):
            return PlainTextResponse(
                "Acesso recusado: este aplicativo só responde ao próprio computador.",
                status_code=403,
            )

        if requisicao.method not in METODOS_SEGUROS:
            if not guardiao.token_confere(guardiao.token_da_requisicao(requisicao)):
                return JSONResponse(
                    {
                        "erro": "Sessão inválida ou expirada.",
                        "acao": "Feche esta aba e abra o aplicativo novamente pelo atalho.",
                    },
                    status_code=403,
                )

        return await proxima(requisicao)

    return middleware


def gravar_cookie(resposta, token: str) -> None:
    """Fixa o token no navegador.

    `samesite=strict` é o que impede outro site de mandar o cookie junto numa
    requisição forjada. `httponly` impede que JavaScript de terceiros o leia.
    Sem `secure`, porque em `http://127.0.0.1` não há TLS — e nem faz sentido,
    já que o tráfego não sai da máquina.
    """
    resposta.set_cookie(
        NOME_COOKIE, token, httponly=True, samesite="strict", path="/"
    )

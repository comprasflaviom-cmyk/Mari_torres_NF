"""Cliente HTTP da API da NFS-e Nacional (Sefin Nacional), autenticado por mTLS."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import Configuracao, ROTA_CONSULTA_CHAVE, ROTA_DANFSE, ROTA_EMISSAO


@dataclass
class RespostaEmissao:
    """Resultado normalizado de uma tentativa de emissão."""

    autorizada: bool
    status_http: int
    chave_acesso: str | None = None
    id_dps: str | None = None
    xml_nfse: bytes | None = None       # XML da NFS-e já descomprimido
    mensagens: list[str] = field(default_factory=list)
    corpo_bruto: dict[str, Any] | str | None = None

    @property
    def motivo_erro(self) -> str:
        return " | ".join(self.mensagens) or f"HTTP {self.status_http} sem detalhamento."


class ClienteNFSe:
    """Envolve os endpoints REST usados na emissão.

    A sessão já vem com o certificado A1 no handshake (veja `certificado.py`),
    então nenhum token ou header de autenticação adicional é necessário.
    """

    def __init__(self, config: Configuracao, sessao: requests.Session):
        self.config = config
        self.sessao = sessao
        self.base = config.url_base.rstrip("/")

    # -- Emissão ------------------------------------------------------------
    def emitir(self, dps_gzip_b64: str) -> RespostaEmissao:
        """POST /nfse — envia a DPS assinada e interpreta o retorno."""
        url = f"{self.base}{ROTA_EMISSAO}"
        resposta = self._requisitar_com_retentativa(
            "POST", url, json={"dpsXmlGZipB64": dps_gzip_b64}
        )
        return self._interpretar_emissao(resposta)

    def _interpretar_emissao(self, resposta: requests.Response) -> RespostaEmissao:
        from .assinatura import desempacotar_retorno

        try:
            corpo = resposta.json()
        except ValueError:
            corpo = resposta.text

        # 200/201 = NFS-e autorizada.
        if resposta.status_code in (200, 201) and isinstance(corpo, dict):
            xml_b64 = corpo.get("nfseXmlGZipB64")
            return RespostaEmissao(
                autorizada=True,
                status_http=resposta.status_code,
                chave_acesso=corpo.get("chaveAcesso"),
                id_dps=corpo.get("idDps"),
                xml_nfse=desempacotar_retorno(xml_b64) if xml_b64 else None,
                mensagens=_extrair_mensagens(corpo, chave="alertas"),
                corpo_bruto=corpo,
            )

        # Qualquer outro status = rejeição. O layout devolve a lista `erros`.
        return RespostaEmissao(
            autorizada=False,
            status_http=resposta.status_code,
            mensagens=_extrair_mensagens(corpo, chave="erros")
            or [str(corpo)[:500]],
            corpo_bruto=corpo,
        )

    # -- Consultas ----------------------------------------------------------
    def consultar(self, chave_acesso: str) -> dict[str, Any] | None:
        """GET /nfse/{chave} — útil para reconciliar notas já emitidas."""
        url = f"{self.base}{ROTA_CONSULTA_CHAVE.format(chave=chave_acesso)}"
        resposta = self._requisitar_com_retentativa("GET", url)
        if resposta.status_code == 200:
            return resposta.json()
        return None

    def baixar_danfse(self, chave_acesso: str) -> bytes | None:
        """GET /danfse/{chave} — PDF da DANFSe. Devolve None se indisponível."""
        url = f"{self.base}{ROTA_DANFSE.format(chave=chave_acesso)}"
        resposta = self._requisitar_com_retentativa(
            "GET", url, headers={"Accept": "application/pdf"}
        )
        if resposta.status_code == 200 and resposta.content[:4] == b"%PDF":
            return resposta.content
        return None

    # -- Infraestrutura -----------------------------------------------------
    def _requisitar_com_retentativa(self, metodo: str, url: str, **kwargs):
        """Reenvia apenas em falha de rede ou erro 5xx — nunca em rejeição fiscal.

        Rejeição (4xx) é resposta definitiva do governo: repetir só geraria
        duplicidade. Já 5xx e timeout são instabilidade do ambiente.
        """
        ultimo_erro: Exception | None = None
        for tentativa in range(1, self.config.max_tentativas + 1):
            try:
                resposta = self.sessao.request(
                    metodo, url, timeout=self.config.timeout_segundos, **kwargs
                )
                if resposta.status_code < 500:
                    return resposta
                ultimo_erro = RuntimeError(
                    f"HTTP {resposta.status_code} do servidor da Sefin Nacional."
                )
            except requests.exceptions.SSLError as exc:
                # Erro de handshake: quase sempre certificado A1 vencido,
                # revogado ou sem cadeia ICP-Brasil. Não adianta repetir.
                raise RuntimeError(
                    f"Falha no handshake mTLS — verifique o certificado A1: {exc}"
                ) from exc
            except requests.exceptions.RequestException as exc:
                ultimo_erro = exc

            if tentativa < self.config.max_tentativas:
                time.sleep(2 ** tentativa)  # backoff: 2s, 4s, 8s...

        raise RuntimeError(f"Falha ao chamar {url} após {self.config.max_tentativas} tentativas: {ultimo_erro}")


def _extrair_mensagens(corpo: Any, chave: str) -> list[str]:
    """Normaliza a lista `erros`/`alertas` do retorno em texto legível."""
    if not isinstance(corpo, dict):
        return []
    itens = corpo.get(chave) or corpo.get("Erros") or corpo.get("mensagens") or []
    if isinstance(itens, dict):
        itens = [itens]

    mensagens = []
    for item in itens:
        if isinstance(item, dict):
            codigo = item.get("codigo") or item.get("Codigo") or ""
            descricao = (
                item.get("descricao") or item.get("Descricao") or item.get("mensagem") or ""
            )
            complemento = item.get("complemento") or item.get("Complemento") or ""
            texto = " ".join(p for p in (f"[{codigo}]" if codigo else "", descricao, complemento) if p)
            mensagens.append(texto.strip())
        else:
            mensagens.append(str(item))
    return [m for m in mensagens if m]

"""
Carregamento do Certificado Digital A1 e montagem da conexão mTLS.

CONTEXTO IMPORTANTE
-------------------
O `requests` (via urllib3/OpenSSL) só aceita o par certificado+chave em
**arquivos PEM**, e a API pública `cert=("cert.pem", "key.pem")` **não aceita
senha** para a chave privada. Por isso este módulo monta um `SSLContext`
manualmente: assim funciona tanto com o `.pfx` original (com senha) quanto com
os PEMs já convertidos, cifrados ou não.

Consulte o README, seção "Convertendo o Certificado A1", para os comandos
`openssl` que geram `certificado.crt` e `chave.key` a partir do seu `.pfx`.
"""

from __future__ import annotations

import ssl
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509 import Certificate, ExtensionNotFound, OtherName
from cryptography.x509.oid import ExtensionOID

from .config import Configuracao

# OID do CNPJ do titular dentro do subjectAltName de um e-CNPJ ICP-Brasil.
OID_CNPJ_ICP_BRASIL = "2.16.76.1.3.3"


class ErroCertificado(RuntimeError):
    """Certificado ausente, com senha errada, vencido ou ilegível."""


@dataclass
class CertificadoA1:
    """Material criptográfico do A1, já em memória."""

    certificado: Certificate
    chave_privada: object
    cadeia: list[Certificate]

    # PEMs em bytes — usados tanto pelo SSLContext quanto pela assinatura XMLDSig.
    cert_pem: bytes
    chave_pem: bytes  # sempre SEM senha aqui dentro (só existe em memória)

    @property
    def titular(self) -> str:
        return self.certificado.subject.rfc4514_string()

    @property
    def cnpj_titular(self) -> str:
        """CNPJ do dono do certificado, só dígitos (vazio se não for um e-CNPJ).

        Existe porque a Sefin Nacional recusa a nota quando quem assinou não é
        o prestador declarado — e o erro que ela devolve é um genérico
        `[E0714] Arquivo enviado com erro na assinatura`, que manda procurar
        defeito na assinatura em vez de no titular do certificado.

        Num e-CNPJ ICP-Brasil o CNPJ vem no `otherName` de OID 2.16.76.1.3.3
        do subjectAltName; o sufixo do CN (`RAZAO SOCIAL:00000000000000`) é
        só o plano B, para certificados que fujam do padrão.
        """
        try:
            alternativos = self.certificado.extensions.get_extension_for_oid(
                ExtensionOID.SUBJECT_ALTERNATIVE_NAME
            ).value
        except ExtensionNotFound:
            alternativos = []

        for nome in alternativos:
            if isinstance(nome, OtherName) and nome.type_id.dotted_string == OID_CNPJ_ICP_BRASIL:
                # O valor é um DER de string; os dois primeiros bytes são tag e
                # tamanho, e o resto são os 14 dígitos.
                digitos = "".join(c for c in nome.value[2:].decode("latin-1") if c.isdigit())
                if len(digitos) == 14:
                    return digitos

        _, separador, sufixo = self.titular.partition("CN=")
        if separador:
            candidato = "".join(c for c in sufixo.split(",")[0].rpartition(":")[2] if c.isdigit())
            if len(candidato) == 14:
                return candidato
        return ""

    @property
    def valido_ate(self) -> datetime:
        return self.certificado.not_valid_after_utc

    @property
    def dias_para_vencer(self) -> int:
        return (self.valido_ate - datetime.now(timezone.utc)).days

    def validar_vigencia(self) -> None:
        agora = datetime.now(timezone.utc)
        if agora < self.certificado.not_valid_before_utc:
            raise ErroCertificado(
                f"Certificado ainda não é válido (início em {self.certificado.not_valid_before_utc})."
            )
        if agora > self.valido_ate:
            raise ErroCertificado(
                f"Certificado VENCIDO em {self.valido_ate:%d/%m/%Y}. Renove antes de emitir."
            )

    def validar_titular(self, cnpj_prestador: str) -> None:
        """Confere se quem assina é o prestador que vai na nota.

        A Sefin Nacional recusa a nota quando o titular do certificado não é o
        prestador declarado, e o erro é o genérico `[E0714] Arquivo enviado
        com erro na assinatura` — que aponta para a assinatura, não para o
        titular. Custa caro descobrir isso pelo retorno do governo, então a
        checagem acontece aqui, antes de transmitir.
        """
        esperado = "".join(c for c in cnpj_prestador if c.isdigit())
        titular = self.cnpj_titular
        if not esperado or not titular or titular == esperado:
            return
        raise ErroCertificado(
            f"O certificado é de outro CNPJ. Ele pertence a {titular}, e a nota "
            f"declara o prestador {esperado}. A Sefin recusa a nota nesse caso "
            "(erro E0714, que fala em 'erro na assinatura'). Use o certificado "
            "do próprio prestador, ou corrija o CNPJ em Configuração."
        )

    def impressao_digital(self) -> str:
        return self.certificado.fingerprint(hashes.SHA1()).hex().upper()


def carregar_certificado(config: Configuracao) -> CertificadoA1:
    """Carrega o A1 a partir do `.pfx` (preferencial) ou dos PEMs `.crt`/`.key`."""
    if config.usa_pfx:
        return _carregar_de_pfx(config.caminho_pfx, config.senha_pfx)
    return _carregar_de_pem(
        config.caminho_certificado, config.caminho_chave, config.senha_chave
    )


def _carregar_de_pfx(caminho: Path, senha: str | None) -> CertificadoA1:
    if not caminho.exists():
        raise ErroCertificado(f"Arquivo .pfx não encontrado: {caminho}")
    senha_bytes = senha.encode() if senha else None
    try:
        chave, cert, cadeia = pkcs12.load_key_and_certificates(
            caminho.read_bytes(), senha_bytes
        )
    except ValueError as exc:  # senha incorreta ou arquivo corrompido
        raise ErroCertificado(
            f"Não foi possível abrir {caminho.name}. Verifique CERT_PFX_SENHA no .env. ({exc})"
        ) from exc
    if cert is None or chave is None:
        raise ErroCertificado(f"{caminho.name} não contém um par certificado/chave válido.")

    return CertificadoA1(
        certificado=cert,
        chave_privada=chave,
        cadeia=list(cadeia or []),
        cert_pem=cert.public_bytes(serialization.Encoding.PEM),
        chave_pem=chave.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


def _carregar_de_pem(
    caminho_cert: Path | None, caminho_chave: Path | None, senha: str | None
) -> CertificadoA1:
    from cryptography import x509

    if not caminho_cert or not caminho_cert.exists():
        raise ErroCertificado(
            f"Certificado PEM não encontrado: {caminho_cert}. "
            "Gere-o a partir do .pfx (veja o README) ou aponte CERT_PFX no .env."
        )
    if not caminho_chave or not caminho_chave.exists():
        raise ErroCertificado(f"Chave privada PEM não encontrada: {caminho_chave}")

    cert = x509.load_pem_x509_certificate(caminho_cert.read_bytes())
    try:
        chave = serialization.load_pem_private_key(
            caminho_chave.read_bytes(),
            password=senha.encode() if senha else None,
        )
    except TypeError as exc:
        raise ErroCertificado(
            f"{caminho_chave.name} está protegida por senha. Preencha CERT_KEY_SENHA no .env."
        ) from exc
    except ValueError as exc:
        raise ErroCertificado(
            f"Não foi possível ler {caminho_chave.name}. Senha incorreta ou formato inválido. ({exc})"
        ) from exc

    return CertificadoA1(
        certificado=cert,
        chave_privada=chave,
        cadeia=[],
        cert_pem=cert.public_bytes(serialization.Encoding.PEM),
        chave_pem=chave.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


class AdaptadorMTLS(requests.adapters.HTTPAdapter):
    """HTTPAdapter que injeta o certificado A1 no handshake TLS.

    O par cert+chave é escrito num arquivo temporário com permissão 0600,
    carregado no SSLContext e o arquivo é apagado em seguida — a chave
    decifrada nunca fica em disco de forma persistente.
    """

    def __init__(self, cert: CertificadoA1, **kwargs):
        self._contexto = self._montar_contexto(cert)
        super().__init__(**kwargs)

    @staticmethod
    def _montar_contexto(cert: CertificadoA1) -> ssl.SSLContext:
        contexto = ssl.create_default_context()
        # A verificação da cadeia do SERVIDOR continua ligada — nunca desative.
        contexto.check_hostname = True
        contexto.verify_mode = ssl.CERT_REQUIRED

        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as tmp:
            caminho_tmp = Path(tmp.name)
            tmp.write(cert.cert_pem + b"\n" + cert.chave_pem)
        try:
            caminho_tmp.chmod(0o600)
            contexto.load_cert_chain(certfile=str(caminho_tmp))
        finally:
            caminho_tmp.unlink(missing_ok=True)
        return contexto

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._contexto
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._contexto
        return super().proxy_manager_for(*args, **kwargs)


def criar_sessao_mtls(cert: CertificadoA1) -> requests.Session:
    """Devolve uma `requests.Session` já autenticada por mTLS com o A1."""
    sessao = requests.Session()
    adaptador = AdaptadorMTLS(cert)
    sessao.mount("https://", adaptador)
    sessao.headers.update(
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "emissor-nfse-nacional/1.0",
        }
    )
    return sessao

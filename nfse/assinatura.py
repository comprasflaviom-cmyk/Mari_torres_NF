"""
Assinatura digital XMLDSig da DPS e empacotamento para o envelope JSON.

A Sefin Nacional rejeita DPS não assinada. O padrão exigido é **XMLDSig
enveloped**, com `Reference URI="#<Id do infDPS>"`, transformações
`enveloped-signature` + **C14N exclusivo** (`exc-c14n`, não o C14N 1.0
"inclusive" da NF-e clássica) e o certificado do prestador embutido em
`<X509Data>`.

Atenção, duas rejeições reais que já apareceram e o porquê da correção:

1. `[E1228] Xml declarado com prefixo de namespace` — a Sefin exige
   `xmlns="..."` sem prefixo, tanto no `<Signature>` quanto em seus filhos.
   Por isso a árvore da assinatura é montada manualmente aqui (em vez de usar
   a montagem automática de uma lib de XMLDSig): testamos que a forma
   documentada do signxml para isso (`signer.namespaces = {None: ...}`)
   produz uma assinatura que já não bate consigo mesma depois de serializada
   e reinterpretada — ou seja, o próprio documento que ela gera falha ao ser
   reverificado, o que teria trocado essa rejeição por "assinatura inválida",
   pior de diagnosticar. Construindo a árvore nós mesmos com o `c14n` do
   lxml, confirmamos que o `SignedInfo` reproduz byte a byte após
   reserializar e reinterpretar o XML, e que a assinatura RSA confere de
   forma independente de quem a gerou.

2. `[E0714] Arquivo enviado com erro na assinatura` — apareceu já com o
   prefixo corrigido e com um certificado real. A assinatura em si está
   certa: o digest e o RSA conferem tanto pela verificação própria quanto
   por uma biblioteca independente (signxml), sobre o XML exato que a Sefin
   recusou. Ou seja: o erro fala de assinatura, mas o que a Sefin recusa é
   alguma condição em volta dela.

   Causas já confirmadas e tratadas: assinar com certificado de outro CNPJ
   (veja `CertificadoA1.validar_titular`); mandar a cadeia inteira no
   `<X509Data>`, que além de não ajudar saía fora de ordem; e um bug do lxml
   que corrompia a canonicalização (veja `canonizar`) — esse último invalida
   as duas primeiras tentativas com o perfil clássico, que nunca chegaram a
   sair daqui com bytes íntegros.

   O perfil de algoritmos ficou configurável (`PERFIS`, abaixo) porque o
   schema oficial não fixa nenhum: `classico` (RSA-SHA1 + C14N 1.0), o
   padrão do ecossistema fiscal brasileiro e o default daqui, e `moderno`
   (RSA-SHA256 + exc-c14n). Trocar entre eles é `ASSINATURA_ALGORITMO` no
   `.env`, sem mexer em código.
"""

from __future__ import annotations

import base64
import gzip
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import Encoding
from lxml import etree
from lxml.etree import QName, SubElement

from .certificado import CertificadoA1
from .config import NAMESPACE_DPS

NAMESPACE_XMLDSIG = "http://www.w3.org/2000/09/xmldsig#"

C14N_CLASSICO = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
C14N_EXCLUSIVO = "http://www.w3.org/2001/10/xml-exc-c14n#"

# A declaração XML como todo o ecossistema fiscal brasileiro a escreve. O lxml
# emitiria `<?xml version='1.0' encoding='utf-8'?>`, com aspas simples e minúsculas;
# fora do padrão não é erro de XML, mas os validadores fiscais são conhecidos por
# analisar o arquivo de forma literal (é o que está por trás da recusa a prefixo
# de namespace), e não vale arriscar por causa de aspas.
DECLARACAO_XML = b'<?xml version="1.0" encoding="UTF-8"?>'


def canonizar(elemento, exclusivo: bool) -> bytes:
    """Canoniza um elemento — desviando de um bug do lxml que corrompe a assinatura.

    `etree.tostring(elemento, method="c14n")` sobre um elemento que está DENTRO
    de outra árvore emite `xmlns=""` em elementos que pertencem, sim, a um
    namespace: no nosso caso, `Transforms`, `Transform`, `DigestMethod` e
    `DigestValue`. Undeclarar o namespace deles muda o significado do XML, e a
    assinatura acaba calculada sobre bytes que nenhum outro validador
    reproduz — foi o que fez a Sefin recusar com `[E0714]` enquanto a
    verificação local dizia que estava tudo certo (as duas pontas usavam a
    mesma função defeituosa, então combinavam entre si).

    O bug só aparece no C14N inclusivo e só a partir do segundo nível abaixo do
    elemento canonizado; serializar e reinterpretar o elemento como documento
    próprio devolve a forma correta. Isso vale aqui porque a DPS não tem
    nenhum namespace de ancestral que o C14N inclusivo devesse arrastar para
    dentro do trecho assinado além do que já está declarado nele.
    """
    isolado = etree.fromstring(etree.tostring(elemento))
    return etree.tostring(isolado, method="c14n", exclusive=exclusivo)


class ErroAssinatura(RuntimeError):
    pass


def _ds(tag: str) -> QName:
    """QName do xmldsig — o namespace padrão da árvore da assinatura, sem prefixo."""
    return QName(NAMESPACE_XMLDSIG, tag)


# Dois perfis de assinatura completos. Trocar só o hash sem trocar a
# canonicalização junto não faz sentido: os validadores fiscais esperam a
# combinação inteira, e misturar as duas metades foi o que mais atrapalhou o
# diagnóstico da rejeição E0714.
#
# `classico` é o perfil da NF-e/CT-e/MDF-e, que o ecossistema fiscal brasileiro
# usa há anos e que as bibliotecas de NFS-e Nacional seguem. É o padrão aqui.
# `moderno` existe porque o schema oficial não fixa algoritmo nenhum e há
# implementações usando SHA-256; se a Sefin recusar o clássico, dá para trocar
# sem mexer em código, com ASSINATURA_ALGORITMO=moderno no .env.
PERFIS = {
    "classico": {
        "assinatura": f"{NAMESPACE_XMLDSIG}rsa-sha1",
        "digest": f"{NAMESPACE_XMLDSIG}sha1",
        "hash": hashes.SHA1,
        "c14n": C14N_CLASSICO,
        "exclusivo": False,
    },
    "moderno": {
        "assinatura": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
        "digest": "http://www.w3.org/2001/04/xmlenc#sha256",
        "hash": hashes.SHA256,
        "c14n": C14N_EXCLUSIVO,
        "exclusivo": True,
    },
}
# Nomes antigos, de quando a opção era só o hash.
PERFIS["sha1"] = PERFIS["classico"]
PERFIS["sha256"] = PERFIS["moderno"]


def assinar_dps(xml_dps: bytes, cert: CertificadoA1) -> bytes:
    """Assina o XML da DPS e devolve o XML assinado em bytes."""
    try:
        raiz = etree.fromstring(xml_dps)
    except etree.XMLSyntaxError as exc:
        raise ErroAssinatura(f"XML da DPS malformado: {exc}") from exc

    inf_dps = raiz.find(f"{{{NAMESPACE_DPS}}}infDPS")
    if inf_dps is None or not inf_dps.get("Id"):
        raise ErroAssinatura("Elemento <infDPS> sem atributo Id — não é possível assinar.")

    escolha = os.getenv("ASSINATURA_ALGORITMO", "classico").strip().lower()
    if escolha not in PERFIS:
        raise ErroAssinatura(
            f"ASSINATURA_ALGORITMO inválido: {escolha!r}. Use 'classico' ou 'moderno'."
        )
    perfil = PERFIS[escolha]
    classe_hash = perfil["hash"]
    c14n_algoritmo = perfil["c14n"]

    # 1) Digest de infDPS. A transformação enveloped-signature é inócua aqui:
    #    <Signature> é irmão de <infDPS> (não descendente) — não há nada para remover.
    id_ref = inf_dps.get("Id")
    resumo = hashes.Hash(classe_hash())
    resumo.update(canonizar(inf_dps, perfil["exclusivo"]))
    digest_value = base64.b64encode(resumo.finalize()).decode("ascii")

    # 2) Monta <Signature> já anexado à árvore real da DPS, com o xmldsig
    #    como namespace padrão — nsmap só precisa ser dado uma vez, na raiz da
    #    assinatura; o lxml reaproveita o mesmo default em todos os descendentes,
    #    sem redeclarar e sem prefixo (o que a Sefin exige).
    sig = SubElement(raiz, _ds("Signature"), nsmap={None: NAMESPACE_XMLDSIG})
    signed_info = SubElement(sig, _ds("SignedInfo"))
    SubElement(signed_info, _ds("CanonicalizationMethod"), Algorithm=c14n_algoritmo)
    SubElement(signed_info, _ds("SignatureMethod"), Algorithm=perfil["assinatura"])
    referencia = SubElement(signed_info, _ds("Reference"), URI="#" + id_ref)
    transformacoes = SubElement(referencia, _ds("Transforms"))
    SubElement(transformacoes, _ds("Transform"), Algorithm=f"{NAMESPACE_XMLDSIG}enveloped-signature")
    SubElement(transformacoes, _ds("Transform"), Algorithm=c14n_algoritmo)
    SubElement(referencia, _ds("DigestMethod"), Algorithm=perfil["digest"])
    SubElement(referencia, _ds("DigestValue")).text = digest_value

    # 3) Canonicaliza o SignedInfo já dentro da árvore real (contexto de
    #    namespace correto) e assina com a chave do certificado.
    c14n_signed_info = canonizar(signed_info, perfil["exclusivo"])
    try:
        assinatura = cert.chave_privada.sign(c14n_signed_info, padding.PKCS1v15(), classe_hash())
    except Exception as exc:  # noqa: BLE001 — chave incompatível vira erro claro, não traceback
        raise ErroAssinatura(f"Falha ao assinar com a chave do certificado: {exc}") from exc

    SubElement(sig, _ds("SignatureValue")).text = base64.b64encode(assinatura).decode("ascii")
    key_info = SubElement(sig, _ds("KeyInfo"))
    x509_data = SubElement(key_info, _ds("X509Data"))
    # Só o certificado do titular, como fazem as implementações de NFS-e
    # Nacional. Chegamos a enviar a cadeia inteira junto, tentando destravar a
    # rejeição E0714, e não mudou nada — pior: o `.pfx` devolve a cadeia fora de
    # ordem (a raiz vinha antes da AC intermediária), e caminho de certificação
    # embaralhado atrapalha mais do que ajuda. A Sefin já valida esse mesmo
    # certificado no handshake mTLS, então ela conhece a cadeia.
    SubElement(x509_data, _ds("X509Certificate")).text = base64.b64encode(
        cert.certificado.public_bytes(Encoding.DER)
    ).decode("ascii")

    return DECLARACAO_XML + etree.tostring(raiz, encoding="utf-8", xml_declaration=False)


def empacotar_para_envio(xml_assinado: bytes) -> str:
    """gzip + Base64 — é o conteúdo do campo `dpsXmlGZipB64` do POST /nfse."""
    comprimido = gzip.compress(xml_assinado)
    return base64.b64encode(comprimido).decode("ascii")


def desempacotar_retorno(conteudo_b64: str) -> bytes:
    """Inverso de `empacotar_para_envio`: usado no XML da NFS-e devolvido pela API."""
    bruto = base64.b64decode(conteudo_b64)
    try:
        return gzip.decompress(bruto)
    except (OSError, gzip.BadGzipFile):
        # Alguns retornos vêm em Base64 puro, sem compressão.
        return bruto

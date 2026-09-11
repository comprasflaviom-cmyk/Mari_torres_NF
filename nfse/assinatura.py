"""
Assinatura digital XMLDSig da DPS e empacotamento para o envelope JSON.

A Sefin Nacional rejeita DPS não assinada. O padrão exigido é o mesmo da
NF-e/NFS-e: **XMLDSig enveloped**, com `Reference URI="#<Id do infDPS>"`,
transformações `enveloped-signature` + `c14n` e o certificado do prestador
embutido em `<X509Data>`.

Atenção: a Sefin Nacional rejeita `<Signature>` com prefixo de namespace
(erro `[E1228] Xml declarado com prefixo de namespace`) — exige
`xmlns="..."` sem prefixo, tanto no `<Signature>` quanto em seus filhos.
Por isso a árvore da assinatura é montada manualmente aqui (em vez de usar a
montagem automática de uma lib de XMLDSig): testamos que a forma documentada
do signxml para isso (`signer.namespaces = {None: ...}`) produz uma
assinatura que já não bate consigo mesma depois de serializada e
reinterpretada — ou seja, o próprio documento que ela gera falha ao ser
reverificado, o que teria feito a Sefin trocar essa rejeição por outra
("assinatura inválida"), pior de diagnosticar. Construindo a árvore nós
mesmos com o `c14n` do lxml, confirmamos que o `SignedInfo` reproduz
byte a byte após reserializar e reinterpretar o XML, e que a assinatura RSA
confere de forma independente do papel que a gerou.
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
C14N_ALGORITMO = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"


class ErroAssinatura(RuntimeError):
    pass


def _ds(tag: str) -> QName:
    """QName do xmldsig — o namespace padrão da árvore da assinatura, sem prefixo."""
    return QName(NAMESPACE_XMLDSIG, tag)


# Confira o algoritmo exigido no Manual de Orientação ao Contribuinte (MOC) da
# NFS-e Nacional antes de emitir em produção. sha1 é o padrão histórico dos
# webservices fiscais brasileiros; as URIs de sha256 usam namespaces "-more"
# próprios (não há RSA-SHA256/SHA256 nativos no xmldsig-core original).
ALGORITMOS = {
    "sha1": {
        "assinatura": f"{NAMESPACE_XMLDSIG}rsa-sha1",
        "digest": f"{NAMESPACE_XMLDSIG}sha1",
        "hash": hashes.SHA1,
    },
    "sha256": {
        "assinatura": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
        "digest": "http://www.w3.org/2001/04/xmlenc#sha256",
        "hash": hashes.SHA256,
    },
}


def assinar_dps(xml_dps: bytes, cert: CertificadoA1) -> bytes:
    """Assina o XML da DPS e devolve o XML assinado em bytes."""
    try:
        raiz = etree.fromstring(xml_dps)
    except etree.XMLSyntaxError as exc:
        raise ErroAssinatura(f"XML da DPS malformado: {exc}") from exc

    inf_dps = raiz.find(f"{{{NAMESPACE_DPS}}}infDPS")
    if inf_dps is None or not inf_dps.get("Id"):
        raise ErroAssinatura("Elemento <infDPS> sem atributo Id — não é possível assinar.")

    escolha = os.getenv("ASSINATURA_ALGORITMO", "sha1").strip().lower()
    if escolha not in ALGORITMOS:
        raise ErroAssinatura(
            f"ASSINATURA_ALGORITMO inválido: {escolha!r}. Use 'sha1' ou 'sha256'."
        )
    algoritmo = ALGORITMOS[escolha]
    classe_hash = algoritmo["hash"]

    # 1) Digest de infDPS. A transformação enveloped-signature é inócua aqui:
    #    <Signature> é irmão de <infDPS> (não descendente) — não há nada para remover.
    id_ref = inf_dps.get("Id")
    resumo = hashes.Hash(classe_hash())
    resumo.update(etree.tostring(inf_dps, method="c14n"))
    digest_value = base64.b64encode(resumo.finalize()).decode("ascii")

    # 2) Monta <Signature> já anexado à árvore real da DPS, com o xmldsig
    #    como namespace padrão — nsmap só precisa ser dado uma vez, na raiz da
    #    assinatura; o lxml reaproveita o mesmo default em todos os descendentes,
    #    sem redeclarar e sem prefixo (o que a Sefin exige).
    sig = SubElement(raiz, _ds("Signature"), nsmap={None: NAMESPACE_XMLDSIG})
    signed_info = SubElement(sig, _ds("SignedInfo"))
    SubElement(signed_info, _ds("CanonicalizationMethod"), Algorithm=C14N_ALGORITMO)
    SubElement(signed_info, _ds("SignatureMethod"), Algorithm=algoritmo["assinatura"])
    referencia = SubElement(signed_info, _ds("Reference"), URI="#" + id_ref)
    transformacoes = SubElement(referencia, _ds("Transforms"))
    SubElement(transformacoes, _ds("Transform"), Algorithm=f"{NAMESPACE_XMLDSIG}enveloped-signature")
    SubElement(transformacoes, _ds("Transform"), Algorithm=C14N_ALGORITMO)
    SubElement(referencia, _ds("DigestMethod"), Algorithm=algoritmo["digest"])
    SubElement(referencia, _ds("DigestValue")).text = digest_value

    # 3) Canonicaliza o SignedInfo já dentro da árvore real (contexto de
    #    namespace correto) e assina com a chave do certificado.
    c14n_signed_info = etree.tostring(signed_info, method="c14n")
    try:
        assinatura = cert.chave_privada.sign(c14n_signed_info, padding.PKCS1v15(), classe_hash())
    except Exception as exc:  # noqa: BLE001 — chave incompatível vira erro claro, não traceback
        raise ErroAssinatura(f"Falha ao assinar com a chave do certificado: {exc}") from exc

    SubElement(sig, _ds("SignatureValue")).text = base64.b64encode(assinatura).decode("ascii")
    key_info = SubElement(sig, _ds("KeyInfo"))
    x509_data = SubElement(key_info, _ds("X509Data"))
    # O certificado do titular primeiro, seguido da cadeia até a AC raiz (se
    # houver): XMLDSig permite vários <X509Certificate> no mesmo <X509Data>, e
    # sem a cadeia o validador pode não conseguir montar o caminho de
    # certificação até uma AC confiável.
    for certificado in [cert.certificado, *cert.cadeia]:
        SubElement(x509_data, _ds("X509Certificate")).text = base64.b64encode(
            certificado.public_bytes(Encoding.DER)
        ).decode("ascii")

    return etree.tostring(raiz, encoding="utf-8", xml_declaration=True)


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

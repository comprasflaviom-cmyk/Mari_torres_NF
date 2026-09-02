"""
Gera um Certificado Digital A1 de TESTE, autoassinado.

Para que serve: o emissor assina a DPS mesmo no modo simulação, então sem
nenhum certificado o aplicativo não roda — nem o modo que não transmite nada.
Este utilitário destrava todo o fluxo local enquanto o certificado A1 de
verdade não chega.

O QUE ELE **NÃO** FAZ
---------------------
Não transmite. A Sefin Nacional exige certificado ICP-Brasil válido no
handshake mTLS **inclusive em homologação**. Com este arquivo você configura o
app, cadastra clientes, importa planilha, simula e confere o XML gerado — mas
qualquer tentativa de transmitir falha no handshake, e é assim que tem que ser.

Uso:
    python tools/gerar_certificado_teste.py
    python tools/gerar_certificado_teste.py --cnpj 11222333000181 --senha minhasenha
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

# O nome aparece na tela de Configuração do app, no botão "Testar certificado".
# Deixá-lo gritante é proposital: ninguém deve confundir isto com o A1 real.
PREFIXO_TITULAR = "NAO USAR EM PRODUCAO - CERTIFICADO DE TESTE"


def gerar(caminho: Path, cnpj: str, senha: str, dias: int) -> x509.Certificate:
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    titular = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "BR"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TESTE LOCAL - SEM VALOR FISCAL"),
        x509.NameAttribute(NameOID.COMMON_NAME, f"{PREFIXO_TITULAR}:{cnpj}"),
    ])

    agora = dt.datetime.now(dt.timezone.utc)
    certificado = (
        x509.CertificateBuilder()
        .subject_name(titular)
        .issuer_name(titular)                      # autoassinado: emissor = titular
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - dt.timedelta(days=1))
        .not_valid_after(agora + dt.timedelta(days=dias))
        # Sem CA: nada aqui deve poder assinar outros certificados.
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=True, key_encipherment=True,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(chave, hashes.SHA256())
    )

    caminho.write_bytes(
        pkcs12.serialize_key_and_certificates(
            name=b"certificado-de-teste",
            key=chave,
            cert=certificado,
            cas=None,
            encryption_algorithm=serialization.BestAvailableEncryption(senha.encode()),
        )
    )
    try:
        caminho.chmod(0o600)   # sem efeito prático no Windows, útil no Unix
    except OSError:
        pass
    return certificado


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gera um certificado A1 autoassinado para testar o emissor localmente."
    )
    parser.add_argument("--saida", type=Path, default=Path("certificado-teste.pfx"),
                        help="Arquivo a gerar (padrão: certificado-teste.pfx).")
    parser.add_argument("--cnpj", default="11222333000181",
                        help="CNPJ que aparece no titular. Use o da sua empresa para "
                             "a tela ficar parecida com a real.")
    parser.add_argument("--senha", default="teste123",
                        help="Senha do arquivo .pfx (padrão: teste123).")
    parser.add_argument("--dias", type=int, default=180,
                        help="Validade em dias (padrão: 180).")
    args = parser.parse_args()

    if args.saida.exists():
        resposta = input(f"{args.saida} já existe. Sobrescrever? (s/N) ").strip().lower()
        if resposta != "s":
            print("Cancelado.")
            return 1

    certificado = gerar(args.saida, "".join(c for c in args.cnpj if c.isdigit()),
                        args.senha, args.dias)

    print()
    print("=" * 70)
    print("  CERTIFICADO DE TESTE GERADO")
    print("=" * 70)
    print(f"  Arquivo ...... {args.saida.resolve()}")
    print(f"  Senha ........ {args.senha}")
    print(f"  Titular ...... {certificado.subject.rfc4514_string()}")
    print(f"  Válido até ... {certificado.not_valid_after_utc:%d/%m/%Y}")
    print("=" * 70)
    print()
    print("  Use na tela de Configuração do aplicativo para exercitar:")
    print("    configurar, cadastrar clientes, importar planilha, SIMULAR,")
    print("    conferir o XML gerado e testar o envio de e-mail.")
    print()
    print("  ESTE CERTIFICADO NÃO TRANSMITE NOTAS.")
    print("  A Sefin Nacional exige certificado ICP-Brasil de verdade no")
    print("  handshake, INCLUSIVE em homologação. Qualquer emissão real vai")
    print("  falhar com erro de handshake mTLS — e é o comportamento correto.")
    print()
    print("  Apague este arquivo assim que o seu A1 verdadeiro chegar.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

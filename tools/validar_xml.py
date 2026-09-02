"""
Valida um XML de DPS contra o schema oficial da NFS-e Nacional.

O checklist de produção manda conferir o XML gerado pela simulação contra o
`DPS_v1.00.xsd`. Este utilitário faz isso mostrando **em que linha e em que
campo** está o problema, em vez de só dizer que o arquivo é inválido.

O XSD não vem no repositório de propósito: é material do portal nacional, e
uma cópia versionada aqui envelheceria sem ninguém notar. Baixe o pacote de
schemas na área de documentação técnica de https://www.nfse.gov.br/

Uso:
    python tools/validar_xml.py notas/dry-run/dps_simulada_linha002.xml schemas/DPS_v1.00.xsd
    python tools/validar_xml.py "notas/dry-run/*.xml" schemas/DPS_v1.00.xsd
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

from lxml import etree


def validar(caminho_xml: Path, schema: etree.XMLSchema) -> list[str]:
    """Devolve a lista de problemas. Vazia = válido."""
    try:
        documento = etree.parse(str(caminho_xml))
    except etree.XMLSyntaxError as exc:
        return [f"XML malformado: {exc}"]

    if schema.validate(documento):
        return []
    return [
        f"linha {erro.line}: {erro.message}"
        for erro in schema.error_log
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Valida XML de DPS contra o schema oficial da NFS-e Nacional."
    )
    parser.add_argument("xml", help="Arquivo .xml, ou padrão como \"notas/dry-run/*.xml\".")
    parser.add_argument("xsd", type=Path, help="Caminho do DPS_v1.00.xsd oficial.")
    args = parser.parse_args()

    if not args.xsd.exists():
        print(f"Schema não encontrado: {args.xsd}", file=sys.stderr)
        print("Baixe o pacote de schemas em https://www.nfse.gov.br/ "
              "(documentação técnica).", file=sys.stderr)
        return 2

    try:
        schema = etree.XMLSchema(etree.parse(str(args.xsd)))
    except etree.XMLSchemaParseError as exc:
        print(f"Não foi possível carregar o schema: {exc}", file=sys.stderr)
        print("Confira se o .xsd está completo — alguns importam outros arquivos "
              "que precisam estar na mesma pasta.", file=sys.stderr)
        return 2

    arquivos = [Path(c) for c in sorted(glob.glob(args.xml))] or [Path(args.xml)]
    if not arquivos or not arquivos[0].exists():
        print(f"Nenhum XML encontrado em: {args.xml}", file=sys.stderr)
        return 2

    invalidos = 0
    for caminho in arquivos:
        problemas = validar(caminho, schema)
        if not problemas:
            print(f"[OK]      {caminho.name}")
            continue
        invalidos += 1
        print(f"[INVALIDO] {caminho.name}")
        for problema in problemas:
            print(f"           {problema}")

    print()
    print(f"{len(arquivos) - invalidos} válido(s), {invalidos} inválido(s).")
    if invalidos:
        print()
        print("Erro no XML da simulação é problema de layout ou de configuração —")
        print("não adianta tentar emitir antes de resolver. Os campos costumam")
        print("apontar para a tela de Configuração (cTribNac, município, regime).")
    return 1 if invalidos else 0


if __name__ == "__main__":
    raise SystemExit(main())

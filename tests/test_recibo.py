"""Recibo de serviço: valor por extenso e PDF."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest

from nfse.recibo import DadosRecibo, gerar_recibo, valor_por_extenso


@pytest.mark.parametrize("valor, esperado", [
    ("4500", "quatro mil e quinhentos reais"),
    ("1", "um real"),
    ("0.50", "cinquenta centavos"),
    ("1.01", "um real e um centavo"),
    ("100", "cem reais"),
    ("101", "cento e um reais"),
    ("1000", "mil reais"),
    ("1550", "mil quinhentos e cinquenta reais"),
    ("1000000", "um milhão de reais"),
    ("2500000", "dois milhões e quinhentos mil reais"),
    ("123456.78", "cento e vinte e três mil quatrocentos e cinquenta e seis reais "
                  "e setenta e oito centavos"),
])
def test_valor_por_extenso(valor, esperado):
    assert valor_por_extenso(Decimal(valor)) == esperado


def test_recibo_pdf_tem_os_dados_e_avisa_que_nao_e_nota():
    from pypdf import PdfReader

    pdf = gerar_recibo(DadosRecibo(
        numero=7, data=date(2026, 10, 7), valor=Decimal("4500.00"),
        descricao="consultoria prestada em setembro de 2026",
        tomador_nome="Cliente Alfa LTDA", tomador_documento="11444777000161",
        prestador_nome="Empresa Teste LTDA", prestador_documento="11222333000181",
        prestador_cod_municipio="3304557",
    ))
    texto = " ".join(PdfReader(BytesIO(pdf)).pages[0].extract_text().split())
    assert "RECIBO" in texto and "Nº 0007" in texto
    assert "11.444.777/0001-61" in texto
    assert "quatro mil e quinhentos reais" in texto
    assert "Rio de Janeiro, 7 de outubro de 2026" in texto
    assert "não é nota fiscal" in texto
    assert "NFS-e" not in texto

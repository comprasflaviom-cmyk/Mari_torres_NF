"""
Recibo de prestação de serviço (PDF), sem passar pela Receita.

Não é nota fiscal e não se parece com uma: não leva chave de acesso, QR code
nem o nome "NFS-e", e o rodapé diz que o documento não tem valor fiscal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import BytesIO

from reportlab.lib.colors import HexColor, black
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen.canvas import Canvas

from .danfse import _fontes, _municipio_uf

MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"]

_UNIDADES = ["", "um", "dois", "três", "quatro", "cinco", "seis", "sete", "oito", "nove",
             "dez", "onze", "doze", "treze", "quatorze", "quinze", "dezesseis", "dezessete",
             "dezoito", "dezenove"]
_DEZENAS = ["", "", "vinte", "trinta", "quarenta", "cinquenta", "sessenta", "setenta",
            "oitenta", "noventa"]
_CENTENAS = ["", "cento", "duzentos", "trezentos", "quatrocentos", "quinhentos", "seiscentos",
             "setecentos", "oitocentos", "novecentos"]
_GRUPOS = [(10**9, "bilhão", "bilhões"), (10**6, "milhão", "milhões"), (10**3, "mil", "mil")]


def _ate_999(n: int) -> str:
    if n == 100:
        return "cem"
    partes = []
    if n >= 100:
        partes.append(_CENTENAS[n // 100])
    resto = n % 100
    if resto >= 20:
        partes.append(_DEZENAS[resto // 10] + (f" e {_UNIDADES[resto % 10]}" if resto % 10 else ""))
    elif resto:
        partes.append(_UNIDADES[resto])
    return " e ".join(partes)


def _inteiro_por_extenso(n: int) -> str:
    if n == 0:
        return "zero"
    blocos: list[tuple[int, str]] = []
    for divisor, singular, plural in _GRUPOS:
        grupo, n = divmod(n, divisor)
        if grupo:
            if divisor == 1000:
                blocos.append((grupo, "mil" if grupo == 1 else f"{_ate_999(grupo)} mil"))
            else:
                blocos.append((grupo, f"{_ate_999(grupo)} {singular if grupo == 1 else plural}"))
    if n:
        blocos.append((n, _ate_999(n)))

    texto = blocos[0][1]
    for valor, palavras in blocos[1:]:
        # "mil e quinhentos", "mil e vinte" — mas "mil quinhentos e vinte".
        texto += (" e " if valor < 100 or valor % 100 == 0 else " ") + palavras
    return texto


def valor_por_extenso(valor: Decimal) -> str:
    """4500.00 → "quatro mil e quinhentos reais"."""
    centavos_total = int((Decimal(valor) * 100).quantize(Decimal("1")))
    reais, centavos = divmod(centavos_total, 100)
    partes = []
    if reais:
        texto = _inteiro_por_extenso(reais)
        if reais % 10**6 == 0:            # "um milhão de reais"
            texto += " de"
        partes.append(texto + (" real" if reais == 1 else " reais"))
    if centavos:
        partes.append(_inteiro_por_extenso(centavos) + (" centavo" if centavos == 1 else " centavos"))
    return " e ".join(partes) or "zero reais"


def moeda(valor: Decimal) -> str:
    return f"{Decimal(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_documento(documento: str) -> str:
    d = "".join(c for c in documento if c.isdigit())
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return documento


def data_por_extenso(dia: date) -> str:
    return f"{dia.day} de {MESES[dia.month - 1]} de {dia.year}"


@dataclass
class DadosRecibo:
    numero: int
    data: date
    valor: Decimal
    descricao: str
    tomador_nome: str
    tomador_documento: str
    prestador_nome: str
    prestador_documento: str
    prestador_endereco: str = ""
    prestador_cod_municipio: str = ""
    assinante: str = ""

    @property
    def cidade(self) -> str:
        return _municipio_uf(self.prestador_cod_municipio)[0] if self.prestador_cod_municipio else ""


def gerar_recibo(d: DadosRecibo) -> bytes:
    """PDF A4 com o recibo na metade de cima (cabe dobrado num envelope)."""
    f = _fontes()
    buffer = BytesIO()
    c = Canvas(buffer, pagesize=A4)
    c.setTitle(f"Recibo {d.numero:04d} - {d.tomador_nome}")
    largura, altura = A4
    x0, x1 = 2 * cm, largura - 2 * cm
    topo, base = altura - 2 * cm, altura / 2 - 0.5 * cm

    c.setStrokeColor(HexColor("#5b666f"))
    c.setLineWidth(0.8)
    c.roundRect(x0, base, x1 - x0, topo - base, 8)

    y = topo - 1.4 * cm
    c.setFont(f["titulo"], 22)
    c.drawString(x0 + 0.8 * cm, y, "RECIBO")
    c.setFont(f["titulo_normal"], 10)
    c.drawString(x0 + 0.8 * cm, y - 0.6 * cm, f"Nº {d.numero:04d}")

    # Caixa do valor, à direita.
    caixa_w, caixa_h = 5.6 * cm, 1.4 * cm
    cx, cy = x1 - 0.8 * cm - caixa_w, y - 0.75 * cm
    c.setFillColor(HexColor("#e8f0f9"))
    c.roundRect(cx, cy, caixa_w, caixa_h, 5, stroke=0, fill=1)
    c.setFillColor(black)
    c.setFont(f["titulo_normal"], 8)
    c.drawString(cx + 0.3 * cm, cy + caixa_h - 0.45 * cm, "VALOR")
    c.setFont(f["titulo"], 16)
    c.drawRightString(cx + caixa_w - 0.3 * cm, cy + 0.3 * cm, f"R$ {moeda(d.valor)}")

    # Corpo.
    tipo = "CNPJ" if len("".join(ch for ch in d.tomador_documento if ch.isdigit())) == 14 else "CPF"
    corpo = (
        f"Recebemos de {d.tomador_nome}, {tipo} {formatar_documento(d.tomador_documento)}, "
        f"a importância de R$ {moeda(d.valor)} ({valor_por_extenso(d.valor)}), "
        f"referente a {d.descricao.rstrip('.')}."
    )
    texto_w = x1 - x0 - 1.6 * cm
    y = cy - 1.2 * cm
    c.setFont(f["conteudo"], 11)
    for linha in simpleSplit(corpo, f["conteudo"], 11, texto_w)[:9]:
        c.drawString(x0 + 0.8 * cm, y, linha)
        y -= 0.62 * cm
    y -= 0.2 * cm
    c.drawString(x0 + 0.8 * cm, y, "Para maior clareza, firmamos o presente recibo.")

    local = f"{d.cidade}, " if d.cidade else ""
    c.drawRightString(x1 - 0.8 * cm, y - 1.1 * cm, f"{local}{data_por_extenso(d.data)}.")

    # Assinatura.
    ya = base + 2.6 * cm
    meio = (x0 + x1) / 2
    c.setLineWidth(0.6)
    c.setStrokeColor(black)
    c.line(meio - 5.5 * cm, ya, meio + 5.5 * cm, ya)
    c.setFont(f["titulo"], 10)
    c.drawCentredString(meio, ya - 0.5 * cm, d.assinante or d.prestador_nome)
    c.setFont(f["conteudo"], 9)
    linhas_emitente = []
    if d.assinante:
        linhas_emitente.append(d.prestador_nome)
    linhas_emitente.append(f"CNPJ {formatar_documento(d.prestador_documento)}")
    if d.prestador_endereco:
        linhas_emitente.append(d.prestador_endereco)
    for i, linha in enumerate(linhas_emitente):
        c.drawCentredString(meio, ya - (0.95 + 0.42 * i) * cm, linha)

    c.setFont(f["titulo_normal"], 7)
    c.setFillColor(HexColor("#5b666f"))
    c.drawCentredString(meio, base + 0.4 * cm, "Este recibo não é nota fiscal e não tem valor fiscal.")
    c.setFillColor(black)

    c.showPage()
    c.save()
    return buffer.getvalue()

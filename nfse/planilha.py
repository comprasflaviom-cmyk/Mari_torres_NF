"""Leitura e validação da planilha `faturamento.xlsx`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

COLUNAS_OBRIGATORIAS = [
    "CNPJ_Cliente",
    "Razao_Social",
    "Email_Cliente",
    "Valor_Servico",
    "Descricao_Servico",
]

# Colunas opcionais que, se existirem, enriquecem a DPS do tomador.
COLUNAS_OPCIONAIS = [
    "Logradouro",
    "Numero",
    "Complemento",
    "Bairro",
    "Cod_Municipio",   # IBGE, 7 dígitos
    "UF",
    "CEP",
    "Telefone",
]

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ErroPlanilha(ValueError):
    """Problema estrutural na planilha (coluna faltando, arquivo ausente)."""


@dataclass
class LinhaFaturamento:
    """Uma linha da planilha, já normalizada e validada."""

    numero_linha: int          # linha como aparece no Excel (cabeçalho = 1)
    documento_tomador: str     # somente dígitos: 14 = CNPJ, 11 = CPF
    razao_social: str
    email: str
    valor_servico: Decimal     # 2 casas decimais
    descricao: str
    extras: dict[str, str]     # colunas opcionais preenchidas

    @property
    def tipo_documento(self) -> str:
        return "CNPJ" if len(self.documento_tomador) == 14 else "CPF"


def _somente_digitos(valor: object) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _validar_dv_cnpj(cnpj: str) -> bool:
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False
    for tamanho in (12, 13):
        pesos = list(range(tamanho - 7, 1, -1)) + list(range(9, 1, -1))
        soma = sum(int(d) * p for d, p in zip(cnpj[:tamanho], pesos))
        dv = 11 - soma % 11
        dv = 0 if dv >= 10 else dv
        if dv != int(cnpj[tamanho]):
            return False
    return True


def _validar_dv_cpf(cpf: str) -> bool:
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for tamanho in (9, 10):
        soma = sum(int(d) * (tamanho + 1 - i) for i, d in enumerate(cpf[:tamanho]))
        dv = (soma * 10) % 11 % 10
        if dv != int(cpf[tamanho]):
            return False
    return True


def _converter_valor(bruto: object) -> Decimal:
    """Aceita 1234.56, "1.234,56", "R$ 1.234,56" e devolve Decimal com 2 casas."""
    if isinstance(bruto, (int, float, Decimal)):
        valor = Decimal(str(bruto))
    else:
        texto = str(bruto or "").strip().replace("R$", "").replace(" ", "")
        if "," in texto:                       # formato brasileiro
            texto = texto.replace(".", "").replace(",", ".")
        try:
            valor = Decimal(texto)
        except InvalidOperation as exc:
            raise ValueError(f"Valor_Servico inválido: {bruto!r}") from exc
    if valor <= 0:
        raise ValueError(f"Valor_Servico deve ser maior que zero (recebido: {bruto!r}).")
    return valor.quantize(Decimal("0.01"))


def ler_planilha(caminho: Path) -> pd.DataFrame:
    """Lê o .xlsx e garante a presença das colunas obrigatórias."""
    if not caminho.exists():
        raise ErroPlanilha(f"Planilha não encontrada: {caminho}")

    # dtype=str preserva CNPJs com zeros à esquerda; Valor_Servico é convertido depois.
    df = pd.read_excel(caminho, engine="openpyxl", dtype=str)
    df.columns = [str(c).strip() for c in df.columns]

    faltantes = [c for c in COLUNAS_OBRIGATORIAS if c not in df.columns]
    if faltantes:
        raise ErroPlanilha(
            "Colunas obrigatórias ausentes em "
            f"{caminho.name}: {', '.join(faltantes)}. "
            f"Esperado: {', '.join(COLUNAS_OBRIGATORIAS)}."
        )
    return df


def validar_linha(indice: int, linha: pd.Series) -> LinhaFaturamento:
    """Valida uma linha. Levanta ValueError com a causa exata em caso de erro."""
    # +2: pandas indexa a partir de 0 e a linha 1 do Excel é o cabeçalho.
    numero_linha = indice + 2

    documento = _somente_digitos(linha.get("CNPJ_Cliente"))
    if len(documento) == 14:
        if not _validar_dv_cnpj(documento):
            raise ValueError(f"CNPJ_Cliente com dígito verificador inválido: {documento}")
    elif len(documento) == 11:
        if not _validar_dv_cpf(documento):
            raise ValueError(f"CPF do cliente com dígito verificador inválido: {documento}")
    else:
        raise ValueError(
            f"CNPJ_Cliente deve ter 14 dígitos (ou 11, para CPF). Recebido: {documento or 'vazio'!r}"
        )

    razao = str(linha.get("Razao_Social") or "").strip()
    if not razao:
        raise ValueError("Razao_Social está vazia.")

    email = str(linha.get("Email_Cliente") or "").strip()
    if email and not _EMAIL.match(email):
        raise ValueError(f"Email_Cliente inválido: {email!r}")

    descricao = " ".join(str(linha.get("Descricao_Servico") or "").split())
    if len(descricao) < 1:
        raise ValueError("Descricao_Servico está vazia.")
    if len(descricao) > 2000:   # xDescServ do layout aceita até 2000 caracteres
        raise ValueError(f"Descricao_Servico excede 2000 caracteres ({len(descricao)}).")

    extras = {
        coluna: str(linha[coluna]).strip()
        for coluna in COLUNAS_OPCIONAIS
        if coluna in linha.index and pd.notna(linha[coluna]) and str(linha[coluna]).strip()
    }

    return LinhaFaturamento(
        numero_linha=numero_linha,
        documento_tomador=documento,
        razao_social=razao[:300],
        email=email,
        valor_servico=_converter_valor(linha.get("Valor_Servico")),
        descricao=descricao,
        extras=extras,
    )


def iterar_faturamento(caminho: Path):
    """Gera `(indice, LinhaFaturamento | None, erro | None)` para cada linha útil."""
    df = ler_planilha(caminho)
    for indice, linha in df.iterrows():
        # Ignora linhas totalmente em branco deixadas pelo Excel.
        if linha.isna().all():
            continue
        try:
            yield indice, validar_linha(indice, linha), None
        except ValueError as exc:
            yield indice, None, str(exc)

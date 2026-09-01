"""Gera uma `faturamento_exemplo.xlsx` com o layout esperado pelo emissor."""

from pathlib import Path

import pandas as pd

LINHAS = [
    {
        "CNPJ_Cliente": "11.222.333/0001-81",
        "Razao_Social": "Cliente Alfa Tecnologia LTDA",
        "Email_Cliente": "financeiro@clientealfa.com.br",
        "Valor_Servico": 4500.00,
        "Descricao_Servico": "Consultoria estratégica em processos comerciais - competência 09/2026.",
        # Colunas opcionais: melhoram a qualificação do tomador na DPS.
        "Logradouro": "Avenida Rio Branco",
        "Numero": "156",
        "Complemento": "sala 1201",
        "Bairro": "Centro",
        "Cod_Municipio": "3304557",
        "UF": "RJ",
        "CEP": "20040-901",
        "Telefone": "2133334444",
    },
    {
        "CNPJ_Cliente": "11222333000181",
        "Razao_Social": "Beta Serviços Empresariais S/A",
        "Email_Cliente": "contas@betaservicos.com.br",
        "Valor_Servico": "R$ 12.750,00",   # o parser aceita o formato brasileiro
        "Descricao_Servico": "Diagnóstico organizacional e plano de reestruturação.",
        "Cod_Municipio": "3550308",
        "UF": "SP",
    },
]

if __name__ == "__main__":
    destino = Path(__file__).resolve().parent.parent / "faturamento_exemplo.xlsx"
    pd.DataFrame(LINHAS).to_excel(destino, index=False, sheet_name="Faturamento")
    print(f"Planilha de exemplo gerada em: {destino}")

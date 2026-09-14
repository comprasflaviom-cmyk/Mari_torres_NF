"""Decide quais clientes recorrentes já venceram este mês.

Fica separado de `clientes.py` (que é sobre o banco) de propósito: esta é a
única peça que precisa ser fácil de testar sem SQLite — dada uma lista de
clientes e uma data, diz quem está em dia para o app montar a nota sozinho.
"""

from __future__ import annotations

from datetime import date

from .clientes import Cliente


def competencia_de(dia: date) -> str:
    return dia.strftime("%Y-%m")


def clientes_vencidos(clientes: list[Cliente], hoje: date) -> list[Cliente]:
    """Clientes ativos e recorrentes cujo dia de emissão deste mês já chegou.

    `dia_emissao_recorrente` é limitado a 1-28 no cadastro (nem todo mês tem
    29, 30 ou 31), então "hoje.dia >= dia" sempre tem chance de disparar.
    """
    return [
        cliente for cliente in clientes
        if cliente.ativo and cliente.recorrente and hoje.day >= cliente.dia_emissao_recorrente
    ]

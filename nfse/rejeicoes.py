"""
Rejeições da Sefin explicadas em português simples.

A mensagem oficial continua aparecendo (é ela que o suporte precisa); isto só
acrescenta o que ela significa e o que fazer. Cada explicação vem de uma
rejeição real já resolvida neste projeto — código desconhecido fica só com a
mensagem oficial, em vez de um palpite.
"""

from __future__ import annotations

import re

EXPLICACOES = {
    "E0008": (
        "O relógio deste computador está adiantado em relação ao da Receita. "
        "Acerte a hora do Windows (Configurações → Hora e idioma → Sincronizar agora) "
        "e emita de novo."
    ),
    "E0010": (
        "A série da DPS está numa faixa reservada a outro tipo de emissor (a 70000 é "
        "do portal gov.br). Em Configuração, use uma série entre 1 e 49999."
    ),
    "E0310": (
        "O código de tributação nacional não existe do jeito que foi enviado. Use só "
        "os 6 números, sem pontos (ex.: 170303)."
    ),
    "E0312": (
        "O município não aceita esse serviço do jeito que foi enviado. Confira o código "
        "de tributação nacional e o código complementar municipal (ex.: 001) — os "
        "mesmos que aparecem numa nota emitida no portal."
    ),
    "E0712": (
        "Empresa do Simples (ME/EPP) precisa informar o percentual do Simples Nacional. "
        "Preencha em Configuração com o percentual que o contador mandou neste mês."
    ),
    "E0714": (
        "A assinatura não confere — quase sempre é o certificado de outra empresa. Em "
        "Configuração, clique em Testar certificado e confira se o titular é o prestador."
    ),
    "E1228": (
        "A nota saiu com um detalhe técnico de formato que a Receita não aceita. "
        "Isso é do aplicativo, não do seu cadastro: avise o suporte."
    ),
}

FECHO = "A nota não foi emitida e o número não foi gasto: corrija e emita de novo."


def explicar(mensagens: list[str]) -> str:
    """Explicação simples das rejeições reconhecidas, ou "" se nenhuma for."""
    codigos: list[str] = []
    for mensagem in mensagens:
        for codigo in re.findall(r"E\d{4}", mensagem):
            if codigo in EXPLICACOES and codigo not in codigos:
                codigos.append(codigo)
    if not codigos:
        return ""
    return " ".join(EXPLICACOES[c] for c in codigos) + " " + FECHO

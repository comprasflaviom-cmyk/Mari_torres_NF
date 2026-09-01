"""
Controle de numeração da DPS e proteção contra emissão duplicada.

Por que isso existe: `nDPS` precisa ser **único e sequencial** por série. Se o
script for executado duas vezes sobre a mesma planilha, sem controle você emite
a mesma nota de novo (e cancelar NFS-e dá trabalho). Este módulo guarda, num
JSON local, o último número usado e a impressão digital de cada linha já
emitida com sucesso.
"""

from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import dataclass
from pathlib import Path

from .planilha import LinhaFaturamento


class ConflitoDeMaquina(RuntimeError):
    """Duas máquinas usando a mesma série da DPS.

    `nDPS` é único por série. Se dois computadores emitem na mesma série, cada
    um mantém sua própria contagem e os números se repetem — rejeição na melhor
    das hipóteses, nota duplicada na pior. A saída correta é dar uma série
    diferente a cada máquina.
    """


def impressao_da_linha(linha: LinhaFaturamento, competencia: str) -> str:
    """Identidade lógica da linha: mesmo cliente + valor + descrição + competência."""
    bruto = "|".join(
        [
            linha.documento_tomador,
            str(linha.valor_servico),
            linha.descricao,
            competencia,
        ]
    )
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()[:32]


@dataclass
class ControleEmissao:
    """Livro-caixa local das emissões. Um arquivo por ambiente e série."""

    caminho: Path
    ultimo_numero: int
    emitidas: dict[str, dict]
    # Nome do computador que criou este controle. Ver `conflito_de_maquina`.
    maquina: str = ""

    @classmethod
    def carregar(cls, diretorio: Path, ambiente: str, serie: str, numero_inicial: int) -> "ControleEmissao":
        diretorio.mkdir(parents=True, exist_ok=True)
        caminho = diretorio / f"controle_{ambiente}_serie{serie}.json"
        if caminho.exists():
            dados = json.loads(caminho.read_text(encoding="utf-8"))
            return cls(
                caminho=caminho,
                ultimo_numero=int(dados.get("ultimo_numero", numero_inicial - 1)),
                emitidas=dados.get("emitidas", {}),
                maquina=dados.get("maquina", ""),
            )
        return cls(caminho=caminho, ultimo_numero=numero_inicial - 1, emitidas={})

    # -- Uma série por computador ------------------------------------------
    def conflito_de_maquina(self) -> str | None:
        """Devolve o nome da outra máquina, se este controle veio dela.

        Como o arquivo é local, isto só pega o caso em que ele foi copiado
        entre computadores (restauração de backup, pasta compartilhada). Não
        detecta duas máquinas emitindo em paralelo com arquivos separados —
        para isso, o que protege é cada uma ter a sua série.
        """
        if self.maquina and self.maquina != nome_da_maquina():
            return self.maquina
        return None

    def assumir_maquina(self) -> None:
        """Passa o controle para este computador, ciente do risco."""
        self.maquina = nome_da_maquina()

    def proximo_numero(self) -> int:
        self.ultimo_numero += 1
        return self.ultimo_numero

    def ja_emitida(self, impressao: str) -> dict | None:
        return self.emitidas.get(impressao)

    def registrar(self, impressao: str, numero_dps: int, chave_acesso: str | None) -> None:
        self.emitidas[impressao] = {
            "numero_dps": numero_dps,
            "chave_acesso": chave_acesso,
        }

    def salvar(self) -> None:
        """Grava de forma atômica: escreve num .tmp e renomeia."""
        if not self.maquina:
            self.maquina = nome_da_maquina()
        temporario = self.caminho.with_suffix(".tmp")
        temporario.write_text(
            json.dumps(
                {
                    "maquina": self.maquina,
                    "ultimo_numero": self.ultimo_numero,
                    "emitidas": self.emitidas,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporario.replace(self.caminho)


def nome_da_maquina() -> str:
    """Identifica o computador. Usado para detectar duas máquinas na mesma série."""
    try:
        return socket.gethostname()
    except OSError:
        return "desconhecida"

"""Log de execução: console + arquivo .log + relatório CSV por lote."""

from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

CAMPOS_RELATORIO = [
    "linha_planilha",
    "situacao",
    "documento_tomador",
    "razao_social",
    "valor_servico",
    "numero_dps",
    "chave_acesso",
    "detalhe",
    "email",
    "arquivo_xml",
]


@dataclass
class RegistroLinha:
    """Uma linha do relatório final do lote."""

    linha_planilha: int
    situacao: str            # AUTORIZADA | REJEITADA | INVALIDA | ERRO_LOCAL | PULADA
    documento_tomador: str = ""
    razao_social: str = ""
    valor_servico: str = ""
    numero_dps: str = ""
    chave_acesso: str = ""
    detalhe: str = ""
    email: str = ""          # resultado do envio ao cliente
    arquivo_xml: str = ""


@dataclass
class Relatorio:
    """Acumula o resultado de cada linha e escreve o CSV ao final."""

    registros: list[RegistroLinha] = field(default_factory=list)

    def adicionar(self, registro: RegistroLinha) -> None:
        self.registros.append(registro)

    def contagem(self, situacao: str) -> int:
        return sum(1 for r in self.registros if r.situacao == situacao)

    def salvar_csv(self, diretorio: Path, marca_tempo: str) -> Path:
        diretorio.mkdir(parents=True, exist_ok=True)
        caminho = diretorio / f"relatorio_{marca_tempo}.csv"
        with caminho.open("w", newline="", encoding="utf-8-sig") as arquivo:
            escritor = csv.DictWriter(arquivo, fieldnames=CAMPOS_RELATORIO, delimiter=";")
            escritor.writeheader()
            for registro in self.registros:
                escritor.writerow(asdict(registro))
        return caminho

    def resumo(self) -> str:
        return (
            f"{self.contagem('AUTORIZADA')} autorizada(s), "
            f"{self.contagem('REJEITADA')} rejeitada(s), "
            f"{self.contagem('INVALIDA')} inválida(s) na planilha, "
            f"{self.contagem('ERRO_LOCAL')} com erro local, "
            f"{self.contagem('PULADA')} pulada(s)."
        )


def configurar_logger(diretorio: Path, marca_tempo: str) -> logging.Logger:
    """Console (INFO) + arquivo (DEBUG, com stack trace completo)."""
    diretorio.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("nfse")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formato_arquivo = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    manipulador_arquivo = logging.FileHandler(
        diretorio / f"emissao_{marca_tempo}.log", encoding="utf-8"
    )
    manipulador_arquivo.setLevel(logging.DEBUG)
    manipulador_arquivo.setFormatter(formato_arquivo)

    manipulador_console = logging.StreamHandler()
    manipulador_console.setLevel(logging.INFO)
    manipulador_console.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(manipulador_arquivo)
    logger.addHandler(manipulador_console)
    return logger


def marca_tempo_execucao() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")

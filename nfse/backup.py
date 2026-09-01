"""
Espelhamento das notas e do controle de numeração para uma pasta de backup.

Por que isto importa: instalar o emissor no laptop de uma pessoa cria um ponto
único de falha. Se a máquina morrer, vão junto as notas emitidas **e** o
`controle_*.json` — e perder o controle é perder a sequência da numeração da
DPS, que precisa ser única por série.

O espelho copia arquivo, não mantém banco aberto. Por isso uma pasta do
OneDrive ou do Google Drive serve bem aqui (ao contrário do SQLite do
cadastro, que corromperia).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

NOME_MARCADOR = "ultimo-backup.json"


@dataclass
class Espelho:
    """Copia arquivos para a pasta de backup, preservando a estrutura."""

    destino: Path | None

    @property
    def ativo(self) -> bool:
        return self.destino is not None

    def copiar(self, arquivos: list[Path], raiz: Path) -> list[Path]:
        """Copia os arquivos mantendo o caminho relativo a `raiz`.

        Falha de cópia nunca derruba a emissão: a nota já está autorizada na
        Sefin e gravada no disco local. O erro sobe como exceção para o
        chamador registrar, não para abortar o lote.
        """
        if not self.ativo:
            return []

        copiados: list[Path] = []
        for origem in arquivos:
            origem = Path(origem)
            if not origem.exists():
                continue
            try:
                relativo = origem.relative_to(raiz)
            except ValueError:
                relativo = Path(origem.name)   # fora da raiz: copia solto
            alvo = self.destino / relativo
            alvo.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origem, alvo)
            copiados.append(alvo)
        return copiados

    def registrar_sucesso(self, quantidade: int) -> None:
        """Grava a marca do último backup, para o painel mostrar."""
        if not self.ativo:
            return
        self.destino.mkdir(parents=True, exist_ok=True)
        (self.destino / NOME_MARCADOR).write_text(
            json.dumps(
                {
                    "em": datetime.now().isoformat(timespec="seconds"),
                    "arquivos": quantidade,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def ultimo_backup(self) -> datetime | None:
        if not self.ativo:
            return None
        marcador = self.destino / NOME_MARCADOR
        if not marcador.exists():
            return None
        try:
            return datetime.fromisoformat(
                json.loads(marcador.read_text(encoding="utf-8"))["em"]
            )
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            return None


def espelhar_nota(
    espelho: Espelho,
    arquivos_da_nota: dict[str, str],
    diretorio_notas: Path,
    caminho_controle: Path,
) -> int:
    """Copia os arquivos de uma nota e o controle de numeração.

    O controle vai junto a cada nota de propósito: é o arquivo cuja perda custa
    mais caro, e ele é pequeno.
    """
    if not espelho.ativo:
        return 0

    copiados = espelho.copiar(
        [Path(c) for c in arquivos_da_nota.values()], diretorio_notas
    )
    if caminho_controle and Path(caminho_controle).exists():
        copiados += espelho.copiar([Path(caminho_controle)], Path(caminho_controle).parent)

    espelho.registrar_sucesso(len(copiados))
    return len(copiados)

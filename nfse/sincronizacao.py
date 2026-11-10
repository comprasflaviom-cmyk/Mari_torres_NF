"""
Compartilhamento dos dados entre computadores por uma pasta do Google Drive.

Por que assim, e não o banco direto no Drive: o Drive copia arquivo aberto, e
um SQLite sincronizado no meio de uma gravação corrompe. Então cada computador
continua com o seu banco local (rápido, funciona sem internet) e, na pasta
compartilhada, cada um escreve **só no seu próprio arquivo de eventos**:

    <pasta>/computadores/serie-1/eventos.jsonl     ← só a série 1 escreve
    <pasta>/computadores/serie-1/dados-backup.db   ← cópia segura do banco
    <pasta>/computadores/serie-2/eventos.jsonl     ← só a série 2 escreve

Ninguém escreve no arquivo de outro, então o Drive nunca tem conflito. Cada
computador lê os eventos dos outros e aplica no banco local. A série da DPS já
é única por computador (exigência da numeração), por isso serve de identidade.

Tudo aqui é no-op enquanto nenhuma pasta estiver configurada.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PASTA_COMPUTADORES = "computadores"
ARQ_EVENTOS = "eventos.jsonl"
ARQ_SNAPSHOT = "dados-backup.db"
ARQ_CONFIG = "config.json"

# Ordem de "avanço" de uma nota mensal: estado mais avançado nunca regride.
_RANK_RECORRENCIA = {"aguardando_valor": 0, "aguardando_aprovacao": 1, "pulada": 2, "emitida": 3}

_trava = threading.RLock()
_ativo: "Sincronizador | None" = None


@dataclass
class Sincronizador:
    pasta: Path          # a pasta compartilhada (Drive)
    serie: str           # identidade deste computador
    banco: Path          # o SQLite local
    config: Path | None = None   # config.json local, copiado junto com o backup

    @property
    def minha_pasta(self) -> Path:
        return self.pasta / PASTA_COMPUTADORES / f"serie-{self.serie}"

    @property
    def saida(self) -> Path:
        """Fila local: eventos esperando a pasta compartilhada ficar disponível."""
        return self.banco.parent / "sync-saida.jsonl"

    def disponivel(self) -> bool:
        return self.pasta.is_dir()

    def conectar(self) -> sqlite3.Connection:
        conexao = sqlite3.connect(self.banco, timeout=10)
        conexao.row_factory = sqlite3.Row
        return conexao


# ---------------------------------------------------------------------------
# Liga / desliga
# ---------------------------------------------------------------------------
def ativar(pasta: Path, serie: str, banco: Path, config: Path | None = None) -> Sincronizador:
    global _ativo
    with _trava:
        _ativo = Sincronizador(Path(pasta), str(serie), Path(banco), Path(config) if config else None)
        return _ativo


def desativar() -> None:
    global _ativo
    with _trava:
        _ativo = None


def ativo() -> Sincronizador | None:
    return _ativo


# ---------------------------------------------------------------------------
# Publicação (chamada pelos repositórios a cada mudança)
# ---------------------------------------------------------------------------
def publicar(banco: Path, tipo: str, dados: dict) -> None:
    """Põe o evento na fila local. Só vale para o banco principal do app."""
    sinc = _ativo
    if sinc is None or Path(banco).resolve() != sinc.banco.resolve():
        return
    evento = {
        "id": uuid.uuid4().hex, "serie": sinc.serie, "tipo": tipo,
        "em": datetime.now().isoformat(timespec="seconds"), "dados": dados,
    }
    with _trava:
        with sinc.saida.open("a", encoding="utf-8") as arquivo:
            arquivo.write(json.dumps(evento, ensure_ascii=False, default=str) + "\n")


def descarregar() -> int:
    """Leva a fila local para o arquivo deste computador na pasta compartilhada."""
    sinc = _ativo
    if sinc is None or not sinc.disponivel():
        return 0
    with _trava:
        if not sinc.saida.exists():
            return 0
        conteudo = sinc.saida.read_text(encoding="utf-8")
        if not conteudo.strip():
            return 0
        sinc.minha_pasta.mkdir(parents=True, exist_ok=True)
        with (sinc.minha_pasta / ARQ_EVENTOS).open("a", encoding="utf-8") as destino:
            destino.write(conteudo)
            destino.flush()
            os.fsync(destino.fileno())
        sinc.saida.write_text("", encoding="utf-8")
        return conteudo.count("\n")


def pendentes_na_fila() -> int:
    sinc = _ativo
    if sinc is None or not sinc.saida.exists():
        return 0
    return sinc.saida.read_text(encoding="utf-8").count("\n")


def publicar_tudo() -> bool:
    """Na primeira ativação, publica o que já existia antes de compartilhar."""
    sinc = _ativo
    if sinc is None:
        return False
    marca = f"publicado:serie-{sinc.serie}"
    with sinc.conectar() as conexao:
        if _ler_estado(conexao, marca):
            return False
        linhas = {
            "cliente": conexao.execute("SELECT * FROM clientes").fetchall(),
            "cliente_excluido": conexao.execute("SELECT * FROM clientes_excluidos").fetchall(),
            "nota": conexao.execute("SELECT * FROM emissoes").fetchall(),
            "recibo": conexao.execute("SELECT * FROM recibos").fetchall(),
            "recorrencia": conexao.execute("SELECT * FROM recorrencias").fetchall(),
        }
    for tipo, registros in linhas.items():
        for linha in registros:
            publicar(sinc.banco, tipo, carga(tipo, dict(linha)))
    with sinc.conectar() as conexao:
        _gravar_estado(conexao, marca, datetime.now().isoformat(timespec="seconds"))
    return True


def carga(tipo: str, linha: dict) -> dict:
    """O que vai no evento: a linha do banco sem o que só faz sentido aqui."""
    dados = dict(linha)
    if tipo == "nota":
        dados.pop("pasta", None)
        dados.pop("arquivos", None)
    elif tipo == "recibo":
        dados.pop("numero", None)                     # id local
        dados["arquivo_nome"] = Path(dados.pop("arquivo", "") or "").name
    elif tipo == "recorrencia":
        dados.pop("id", None)
    elif tipo == "cliente_excluido":
        dados = {"documento": dados["documento"], "em": dados["em"]}
    return dados


# ---------------------------------------------------------------------------
# Importação (eventos dos outros computadores)
# ---------------------------------------------------------------------------
def importar(incluir_propria: bool = False) -> int:
    """Aplica os eventos novos dos outros computadores. Devolve quantos."""
    sinc = _ativo
    if sinc is None or not sinc.disponivel():
        return 0
    raiz = sinc.pasta / PASTA_COMPUTADORES
    if not raiz.is_dir():
        return 0

    total = 0
    with _trava, sinc.conectar() as conexao:
        for pasta in sorted(raiz.glob("serie-*")):
            serie = pasta.name.removeprefix("serie-")
            if serie == sinc.serie and not incluir_propria:
                continue
            arquivo = pasta / ARQ_EVENTOS
            if not arquivo.is_file():
                continue
            chave = f"offset:serie-{serie}"
            offset = int(_ler_estado(conexao, chave) or 0)
            tamanho = arquivo.stat().st_size
            if tamanho < offset:
                offset = 0   # arquivo recriado/truncado: relê tudo (aplicar é idempotente)
            if tamanho == offset:
                continue
            with arquivo.open("rb") as entrada:
                entrada.seek(offset)
                bloco = entrada.read()
            # O Drive pode estar no meio da cópia: só linhas completas.
            fim = bloco.rfind(b"\n")
            if fim < 0:
                continue
            for bruta in bloco[: fim + 1].splitlines():
                try:
                    evento = json.loads(bruta.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                aplicar(conexao, evento, sinc)
                total += 1
            _gravar_estado(conexao, chave, str(offset + fim + 1))
        _gravar_estado(conexao, "ultima_sincronizacao", datetime.now().isoformat(timespec="seconds"))
    return total


def aplicar(conexao: sqlite3.Connection, evento: dict, sinc: Sincronizador) -> None:
    tipo, dados = evento.get("tipo"), evento.get("dados") or {}
    if tipo == "cliente":
        _aplicar_cliente(conexao, dados)
    elif tipo == "cliente_excluido":
        _aplicar_exclusao(conexao, dados)
    elif tipo == "nota":
        _aplicar_nota(conexao, dados)
    elif tipo == "recibo":
        _aplicar_recibo(conexao, dados, sinc)
    elif tipo == "recorrencia":
        _aplicar_recorrencia(conexao, dados)


def _colunas(conexao, tabela: str) -> list[str]:
    return [linha["name"] for linha in conexao.execute(f"PRAGMA table_info({tabela})")]


def _upsert(conexao, tabela: str, dados: dict) -> None:
    colunas = [c for c in _colunas(conexao, tabela) if c in dados]
    conexao.execute(
        f"INSERT OR REPLACE INTO {tabela} ({', '.join(colunas)}) "
        f"VALUES ({', '.join('?' for _ in colunas)})",
        [dados[c] for c in colunas],
    )


def _aplicar_cliente(conexao, dados: dict) -> None:
    documento = dados.get("documento")
    if not documento:
        return
    remoto = dados.get("atualizado_em") or ""
    local = conexao.execute("SELECT atualizado_em FROM clientes WHERE documento = ?", (documento,)).fetchone()
    if local and (local["atualizado_em"] or "") >= remoto:
        return   # a versão daqui é igual ou mais nova
    lapide = conexao.execute("SELECT em FROM clientes_excluidos WHERE documento = ?", (documento,)).fetchone()
    if lapide and lapide["em"] >= remoto:
        return   # foi excluído depois desta edição
    _upsert(conexao, "clientes", dados)
    conexao.execute("DELETE FROM clientes_excluidos WHERE documento = ?", (documento,))


def _aplicar_exclusao(conexao, dados: dict) -> None:
    documento, em = dados.get("documento"), dados.get("em") or ""
    if not documento:
        return
    local = conexao.execute("SELECT atualizado_em FROM clientes WHERE documento = ?", (documento,)).fetchone()
    if local and (local["atualizado_em"] or "") > em:
        return   # editado depois da exclusão: a edição vence
    conexao.execute("DELETE FROM clientes WHERE documento = ?", (documento,))
    conexao.execute(
        "INSERT INTO clientes_excluidos (documento, em) VALUES (?, ?) "
        "ON CONFLICT(documento) DO UPDATE SET em = MAX(em, excluded.em)",
        (documento, em),
    )


def _aplicar_nota(conexao, dados: dict) -> None:
    if not dados.get("chave_acesso"):
        return
    colunas = [c for c in _colunas(conexao, "emissoes") if c in dados]
    conexao.execute(
        f"INSERT OR IGNORE INTO emissoes ({', '.join(colunas)}, arquivos, pasta) "
        f"VALUES ({', '.join('?' for _ in colunas)}, '{{}}', '')",
        [dados[c] for c in colunas],
    )


def caminho_recibo_espelhado(pasta: Path, data: str, nome: str) -> Path:
    return Path(pasta) / "recibos" / (data or "0000")[:4] / nome


def _aplicar_recibo(conexao, dados: dict, sinc: Sincronizador) -> None:
    serie, numero_serie = dados.get("serie") or "", int(dados.get("numero_serie") or 0)
    if not serie or not numero_serie:
        return
    nome = dados.get("arquivo_nome") or ""
    espelhado = str(caminho_recibo_espelhado(sinc.pasta, dados.get("data") or "", nome)) if nome else ""
    local = conexao.execute(
        "SELECT numero, arquivo, enviado_em FROM recibos WHERE serie = ? AND numero_serie = ?",
        (serie, numero_serie),
    ).fetchone()
    if local:
        arquivo = local["arquivo"] if local["arquivo"] and Path(local["arquivo"]).exists() else espelhado
        conexao.execute(
            "UPDATE recibos SET email = ?, enviado_em = ?, arquivo = ? WHERE numero = ?",
            (dados.get("email") or "", max(local["enviado_em"] or "", dados.get("enviado_em") or ""),
             arquivo, local["numero"]),
        )
        return
    conexao.execute(
        """INSERT INTO recibos (documento, tomador, email, valor, descricao, data, arquivo,
                                enviado_em, criado_em, serie, numero_serie)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (dados.get("documento") or "", dados.get("tomador") or "", dados.get("email") or "",
         dados.get("valor") or "0", dados.get("descricao") or "", dados.get("data") or "",
         espelhado, dados.get("enviado_em") or "", dados.get("criado_em") or "", serie, numero_serie),
    )


def _aplicar_recorrencia(conexao, dados: dict) -> None:
    documento, competencia = dados.get("documento"), dados.get("competencia")
    if not documento or not competencia:
        return
    local = conexao.execute(
        "SELECT id, estado, atualizado_em FROM recorrencias WHERE documento = ? AND competencia = ?",
        (documento, competencia),
    ).fetchone()
    if local is None:
        _upsert(conexao, "recorrencias", dados)
        return
    rank_local = _RANK_RECORRENCIA.get(local["estado"], 0)
    rank_remoto = _RANK_RECORRENCIA.get(dados.get("estado"), 0)
    mais_nova = (dados.get("atualizado_em") or "") > (local["atualizado_em"] or "")
    if rank_remoto > rank_local or (rank_remoto == rank_local and mais_nova):
        conexao.execute(
            "UPDATE recorrencias SET estado = ?, valor = ?, descricao = ?, chave_acesso = ?, "
            "atualizado_em = ? WHERE id = ?",
            (dados.get("estado"), dados.get("valor") or "", dados.get("descricao") or "",
             dados.get("chave_acesso") or "", dados.get("atualizado_em") or "", local["id"]),
        )


# ---------------------------------------------------------------------------
# Backup do banco e restauração
# ---------------------------------------------------------------------------
def snapshot() -> Path | None:
    """Cópia consistente do banco (API de backup do SQLite) na pasta compartilhada."""
    sinc = _ativo
    if sinc is None or not sinc.disponivel():
        return None
    sinc.minha_pasta.mkdir(parents=True, exist_ok=True)
    destino = sinc.minha_pasta / ARQ_SNAPSHOT
    temporario = destino.with_suffix(".tmp")
    _copiar_banco(sinc.banco, temporario)
    os.replace(temporario, destino)
    if sinc.config and sinc.config.exists():
        shutil.copy2(sinc.config, sinc.minha_pasta / ARQ_CONFIG)
    with sinc.conectar() as conexao:
        _gravar_estado(conexao, "ultimo_snapshot", datetime.now().isoformat(timespec="seconds"))
    return destino


def _copiar_banco(origem: Path, destino: Path, somente_leitura: bool = False) -> None:
    """Cópia consistente de um SQLite para outro (fecha as duas conexões)."""
    fonte = (sqlite3.connect(f"file:{origem}?mode=ro", uri=True) if somente_leitura
             else sqlite3.connect(origem, timeout=10))
    alvo = sqlite3.connect(destino, timeout=10)
    try:
        fonte.backup(alvo)
    finally:
        alvo.close()
        fonte.close()


def backups_disponiveis() -> list[dict]:
    sinc = _ativo
    if sinc is None or not (sinc.pasta / PASTA_COMPUTADORES).is_dir():
        return []
    achados = []
    for arquivo in sorted((sinc.pasta / PASTA_COMPUTADORES).glob(f"serie-*/{ARQ_SNAPSHOT}")):
        achados.append({
            "serie": arquivo.parent.name.removeprefix("serie-"),
            "em": datetime.fromtimestamp(arquivo.stat().st_mtime).isoformat(timespec="minutes"),
        })
    return achados


def restaurar(serie: str) -> int:
    """Troca o banco local pelo backup de uma série e completa com os eventos."""
    sinc = _ativo
    if sinc is None:
        raise ValueError("Configure a pasta compartilhada antes de restaurar.")
    origem = sinc.pasta / PASTA_COMPUTADORES / f"serie-{serie}" / ARQ_SNAPSHOT
    if not origem.is_file():
        raise ValueError(f"Não há backup do computador da série {serie} nesta pasta.")
    with _trava:
        _copiar_banco(origem, sinc.banco, somente_leitura=True)
        with sinc.conectar() as conexao:
            conexao.execute("DELETE FROM sync_estado WHERE chave LIKE 'offset:%'")
    # Eventos gravados depois do backup (inclusive deste computador) completam o que faltar.
    return importar(incluir_propria=True)


# ---------------------------------------------------------------------------
# Situação, para a tela
# ---------------------------------------------------------------------------
def situacao() -> dict:
    sinc = _ativo
    if sinc is None:
        return {"ativa": False}
    estado = {"ativa": True, "pasta": str(sinc.pasta), "serie": sinc.serie,
              "disponivel": sinc.disponivel(), "na_fila": pendentes_na_fila(),
              "computadores": [], "ultima": None, "ultimo_backup": None}
    try:
        with sinc.conectar() as conexao:
            estado["ultima"] = _ler_estado(conexao, "ultima_sincronizacao")
            estado["ultimo_backup"] = _ler_estado(conexao, "ultimo_snapshot")
    except sqlite3.Error:
        pass
    raiz = sinc.pasta / PASTA_COMPUTADORES
    if raiz.is_dir():
        estado["computadores"] = sorted(p.name.removeprefix("serie-") for p in raiz.glob("serie-*"))
    return estado


def _ler_estado(conexao, chave: str) -> str | None:
    linha = conexao.execute("SELECT valor FROM sync_estado WHERE chave = ?", (chave,)).fetchone()
    return linha["valor"] if linha else None


def _gravar_estado(conexao, chave: str, valor: str) -> None:
    conexao.execute("INSERT OR REPLACE INTO sync_estado (chave, valor) VALUES (?, ?)", (chave, valor))

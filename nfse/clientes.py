"""
Cadastro de clientes e histórico de emissões, em SQLite local.

Duas tabelas com papéis bem diferentes:

* **clientes** — dado próprio do aplicativo. É a fonte da verdade.
* **emissoes** — apenas um **modelo de leitura** para a tela de histórico,
  reconstruível a partir da pasta de notas. A autoridade sobre a numeração
  continua sendo o `controle_*.json` de `nfse/estado.py`; ter dois donos do
  mesmo número seria pedir divergência.

Sobre as duas chaves de cada cliente:

* `ativo` — se ele entra no faturamento.
* `receber_por_email` — se a NFS-e é enviada automaticamente para ele.

São coisas distintas de propósito. Um cliente pode pedir para não receber por
e-mail (o contador dele busca o arquivo) e continuar sendo faturado
normalmente; se fosse uma chave só, desligar o e-mail o tiraria do faturamento.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .planilha import LinhaFaturamento, somente_digitos, validar_dv_cnpj, validar_dv_cpf

ESQUEMA = """
CREATE TABLE IF NOT EXISTS clientes (
    documento               TEXT PRIMARY KEY,
    razao_social            TEXT NOT NULL,
    email                   TEXT DEFAULT '',
    logradouro              TEXT DEFAULT '',
    numero                  TEXT DEFAULT '',
    complemento             TEXT DEFAULT '',
    bairro                  TEXT DEFAULT '',
    cod_municipio           TEXT DEFAULT '',
    uf                      TEXT DEFAULT '',
    cep                     TEXT DEFAULT '',
    telefone                TEXT DEFAULT '',
    ativo                   INTEGER NOT NULL DEFAULT 1,
    receber_por_email       INTEGER NOT NULL DEFAULT 1,
    observacao              TEXT DEFAULT '',
    valor_recorrente        TEXT DEFAULT '',
    descricao_recorrente    TEXT DEFAULT '',
    dia_emissao_recorrente  INTEGER NOT NULL DEFAULT 0,
    criado_em               TEXT NOT NULL,
    atualizado_em           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS emissoes (
    chave_acesso   TEXT PRIMARY KEY,
    documento      TEXT,
    tomador        TEXT,
    valor_servico  TEXT,
    numero_dps     TEXT,
    emitida_em     TEXT,
    ambiente       TEXT,
    pasta          TEXT,
    arquivos       TEXT
);

-- Uma pendência por cliente e competência: o dia de emissão recorrente pode
-- ser conferido de novo (reabrir o app, verificação periódica) sem duplicar
-- nada, porque a UNIQUE abaixo faz a segunda tentativa virar no-op.
CREATE TABLE IF NOT EXISTS recorrencias (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    documento      TEXT NOT NULL,
    competencia    TEXT NOT NULL,
    valor          TEXT NOT NULL DEFAULT '',
    descricao      TEXT NOT NULL DEFAULT '',
    estado         TEXT NOT NULL,
    chave_acesso   TEXT DEFAULT '',
    criado_em      TEXT NOT NULL,
    atualizado_em  TEXT NOT NULL,
    UNIQUE(documento, competencia)
);

CREATE INDEX IF NOT EXISTS idx_emissoes_documento    ON emissoes(documento);
CREATE INDEX IF NOT EXISTS idx_clientes_ativo        ON clientes(ativo);
CREATE INDEX IF NOT EXISTS idx_recorrencias_estado   ON recorrencias(estado);
"""

# Colunas que podem não existir ainda num banco criado antes delas existirem —
# `CREATE TABLE IF NOT EXISTS` não altera tabela já criada, então isso cobre
# quem atualiza o app com um cadastro antigo.
_COLUNAS_NOVAS_CLIENTES = {
    "valor_recorrente": "TEXT DEFAULT ''",
    "descricao_recorrente": "TEXT DEFAULT ''",
    "dia_emissao_recorrente": "INTEGER NOT NULL DEFAULT 0",
}


def _migrar_colunas_novas(conexao: sqlite3.Connection) -> None:
    existentes = {linha["name"] for linha in conexao.execute("PRAGMA table_info(clientes)")}
    for coluna, tipo in _COLUNAS_NOVAS_CLIENTES.items():
        if coluna not in existentes:
            conexao.execute(f"ALTER TABLE clientes ADD COLUMN {coluna} {tipo}")


class ErroCadastro(ValueError):
    """Dado inválido no cadastro."""


@dataclass
class Cliente:
    documento: str                      # somente dígitos: 14 = CNPJ, 11 = CPF
    razao_social: str
    email: str = ""
    logradouro: str = ""
    numero: str = ""
    complemento: str = ""
    bairro: str = ""
    cod_municipio: str = ""             # IBGE, 7 dígitos
    uf: str = ""
    cep: str = ""
    telefone: str = ""
    ativo: bool = True                  # entra no faturamento
    receber_por_email: bool = True      # recebe a NFS-e automaticamente
    observacao: str = ""
    # Recorrência: gera nota sozinho todo mês, sem planilha. Ver nfse/recorrencia.py.
    valor_recorrente: str = ""          # decimal como texto; vazio = valor varia todo mês
    descricao_recorrente: str = ""
    dia_emissao_recorrente: int = 0     # dia do mês (1-28); 0 = não é recorrente
    criado_em: str = ""
    atualizado_em: str = ""

    @property
    def recorrente(self) -> bool:
        return self.dia_emissao_recorrente > 0

    def validar(self) -> None:
        documento = somente_digitos(self.documento)
        if len(documento) == 14:
            if not validar_dv_cnpj(documento):
                raise ErroCadastro(f"CNPJ com dígito verificador inválido: {documento}")
        elif len(documento) == 11:
            if not validar_dv_cpf(documento):
                raise ErroCadastro(f"CPF com dígito verificador inválido: {documento}")
        else:
            raise ErroCadastro("Informe um CNPJ (14 dígitos) ou CPF (11 dígitos).")
        self.documento = documento

        if not self.razao_social.strip():
            raise ErroCadastro("Razão social é obrigatória.")
        if self.receber_por_email and not self.email.strip():
            raise ErroCadastro(
                "Cliente marcado para receber por e-mail, mas sem endereço de e-mail."
            )
        if self.cod_municipio and len(somente_digitos(self.cod_municipio)) != 7:
            raise ErroCadastro("O código do município (IBGE) tem 7 dígitos.")

        if self.dia_emissao_recorrente:
            if not (1 <= self.dia_emissao_recorrente <= 28):
                raise ErroCadastro("O dia de emissão recorrente deve ser entre 1 e 28.")
            if not self.descricao_recorrente.strip():
                raise ErroCadastro(
                    "Descreva o serviço recorrente — é o que vai na nota que o app monta sozinho."
                )
        if self.valor_recorrente.strip():
            bruto = self.valor_recorrente.strip()
            try:
                valor = Decimal(bruto.replace(".", "").replace(",", ".")) if "," in bruto else Decimal(bruto)
                if valor <= 0:
                    raise InvalidOperation
            except InvalidOperation:
                raise ErroCadastro(f"Valor mensal recorrente inválido: {bruto!r}")
            self.valor_recorrente = f"{valor.quantize(Decimal('0.01')):f}"

    @property
    def tipo_documento(self) -> str:
        return "CNPJ" if len(self.documento) == 14 else "CPF"

    def extras_para_dps(self) -> dict[str, str]:
        """Converte o endereço no formato que `dps.montar_dps` espera."""
        mapa = {
            "Logradouro": self.logradouro, "Numero": self.numero,
            "Complemento": self.complemento, "Bairro": self.bairro,
            "Cod_Municipio": self.cod_municipio, "UF": self.uf,
            "CEP": self.cep, "Telefone": self.telefone,
        }
        return {chave: valor.strip() for chave, valor in mapa.items() if valor and valor.strip()}


class BancoLocal:
    """Conexão SQLite com o banco do aplicativo.

    Abre uma conexão por operação: a emissão roda em outra thread, e conexão
    SQLite não é compartilhável entre threads.
    """

    def __init__(self, caminho: Path):
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        with self._conectar() as conexao:
            conexao.executescript(ESQUEMA)
            _migrar_colunas_novas(conexao)

    def _conectar(self) -> sqlite3.Connection:
        conexao = sqlite3.connect(self.caminho, timeout=10)
        conexao.row_factory = sqlite3.Row
        conexao.execute("PRAGMA foreign_keys = ON")
        return conexao


# ---------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------
COLUNAS_CLIENTE = [c.name for c in fields(Cliente)]


class RepositorioClientes:
    def __init__(self, banco: BancoLocal):
        self.banco = banco

    def salvar(self, cliente: Cliente) -> Cliente:
        """Cria ou atualiza pelo documento."""
        cliente.validar()
        agora = datetime.now().isoformat(timespec="seconds")
        existente = self.buscar(cliente.documento)
        cliente.criado_em = existente.criado_em if existente else agora
        cliente.atualizado_em = agora

        valores = asdict(cliente)
        valores["ativo"] = int(cliente.ativo)
        valores["receber_por_email"] = int(cliente.receber_por_email)

        marcadores = ", ".join(f":{coluna}" for coluna in COLUNAS_CLIENTE)
        with self.banco._conectar() as conexao:
            conexao.execute(
                f"INSERT OR REPLACE INTO clientes ({', '.join(COLUNAS_CLIENTE)}) "
                f"VALUES ({marcadores})",
                valores,
            )
        return cliente

    def buscar(self, documento: str) -> Cliente | None:
        with self.banco._conectar() as conexao:
            linha = conexao.execute(
                "SELECT * FROM clientes WHERE documento = ?", (somente_digitos(documento),)
            ).fetchone()
        return _linha_para_cliente(linha) if linha else None

    def listar(self, busca: str = "", apenas_ativos: bool = False) -> list[Cliente]:
        consulta = "SELECT * FROM clientes"
        condicoes, parametros = [], []
        if apenas_ativos:
            condicoes.append("ativo = 1")
        if busca.strip():
            condicoes.append("(razao_social LIKE ? OR documento LIKE ? OR email LIKE ?)")
            alvo = f"%{busca.strip()}%"
            parametros += [alvo, alvo, alvo]
        if condicoes:
            consulta += " WHERE " + " AND ".join(condicoes)
        consulta += " ORDER BY ativo DESC, razao_social COLLATE NOCASE"

        with self.banco._conectar() as conexao:
            return [_linha_para_cliente(l) for l in conexao.execute(consulta, parametros)]

    def definir_chave(self, documento: str, coluna: str, valor: bool) -> None:
        """Liga/desliga `ativo` ou `receber_por_email`."""
        if coluna not in ("ativo", "receber_por_email"):
            raise ErroCadastro(f"Coluna não alternável: {coluna}")
        with self.banco._conectar() as conexao:
            conexao.execute(
                f"UPDATE clientes SET {coluna} = ?, atualizado_em = ? WHERE documento = ?",
                (int(valor), datetime.now().isoformat(timespec="seconds"), somente_digitos(documento)),
            )

    def excluir(self, documento: str) -> None:
        with self.banco._conectar() as conexao:
            conexao.execute("DELETE FROM clientes WHERE documento = ?", (somente_digitos(documento),))

    # -- Compartilhamento entre máquinas -----------------------------------
    def exportar(self) -> list[dict]:
        """Cadastro em JSON, para levar para outro laptop.

        Exportar e importar é deliberadamente manual: colocar o arquivo SQLite
        numa pasta sincronizada (OneDrive, Drive) corrompe o banco, porque a
        sincronização copia arquivo aberto.
        """
        return [asdict(cliente) for cliente in self.listar()]

    def importar(self, registros: list[dict]) -> tuple[int, int, list[str]]:
        """Devolve (criados, atualizados, erros)."""
        criados = atualizados = 0
        erros: list[str] = []
        conhecidos = set(COLUNAS_CLIENTE)

        for registro in registros:
            try:
                dados = {k: v for k, v in registro.items() if k in conhecidos}
                cliente = Cliente(**{
                    **dados,
                    "ativo": bool(dados.get("ativo", True)),
                    "receber_por_email": bool(dados.get("receber_por_email", True)),
                })
                ja_existia = self.buscar(cliente.documento) is not None
                self.salvar(cliente)
                atualizados += ja_existia
                criados += not ja_existia
            except (ErroCadastro, TypeError) as exc:
                erros.append(f"{registro.get('razao_social', registro.get('documento', '?'))}: {exc}")
        return criados, atualizados, erros


def _linha_para_cliente(linha: sqlite3.Row) -> Cliente:
    dados = dict(linha)
    dados["ativo"] = bool(dados["ativo"])
    dados["receber_por_email"] = bool(dados["receber_por_email"])
    return Cliente(**dados)


# ---------------------------------------------------------------------------
# Histórico (modelo de leitura)
# ---------------------------------------------------------------------------
class RepositorioEmissoes:
    def __init__(self, banco: BancoLocal):
        self.banco = banco

    def registrar(self, dados: dict) -> None:
        """Grava depois que a nota já foi arquivada em disco."""
        if not dados.get("chave_acesso"):
            return
        with self.banco._conectar() as conexao:
            conexao.execute(
                """INSERT OR REPLACE INTO emissoes
                   (chave_acesso, documento, tomador, valor_servico, numero_dps,
                    emitida_em, ambiente, pasta, arquivos)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    dados.get("chave_acesso"),
                    dados.get("documento_tomador", ""),
                    dados.get("tomador", ""),
                    str(dados.get("valor_servico", "")),
                    str(dados.get("numero_dps", "")),
                    dados.get("emitida_em", ""),
                    dados.get("ambiente", ""),
                    dados.get("pasta", ""),
                    json.dumps(dados.get("arquivos", {}), ensure_ascii=False),
                ),
            )

    def listar(self, busca: str = "", limite: int = 500) -> list[dict]:
        consulta = "SELECT * FROM emissoes"
        parametros: list = []
        if busca.strip():
            consulta += " WHERE tomador LIKE ? OR documento LIKE ? OR chave_acesso LIKE ?"
            alvo = f"%{busca.strip()}%"
            parametros = [alvo, alvo, alvo]
        consulta += " ORDER BY emitida_em DESC LIMIT ?"
        parametros.append(limite)

        with self.banco._conectar() as conexao:
            resultados = []
            for linha in conexao.execute(consulta, parametros):
                registro = dict(linha)
                registro["arquivos"] = json.loads(registro.get("arquivos") or "{}")
                resultados.append(registro)
            return resultados

    def reconstruir(self, diretorio_notas: Path) -> int:
        """Refaz a tabela varrendo os `*_retorno.json` da pasta de notas.

        Existe para provar que esta tabela é descartável: se ela se perder ou
        divergir, os arquivos em disco continuam sendo a verdade.
        """
        diretorio_notas = Path(diretorio_notas)
        if not diretorio_notas.exists():
            return 0

        with self.banco._conectar() as conexao:
            conexao.execute("DELETE FROM emissoes")

        total = 0
        for caminho in diretorio_notas.rglob("*_retorno.json"):
            try:
                dados = json.loads(caminho.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            dados["pasta"] = str(caminho.parent)
            self.registrar(dados)
            total += 1
        return total


# ---------------------------------------------------------------------------
# Recorrência (ver nfse/recorrencia.py para quem decide "quem venceu")
# ---------------------------------------------------------------------------
ESTADOS_PENDENTES = ("aguardando_valor", "aguardando_aprovacao")


@dataclass
class Recorrencia:
    """Uma pendência de nota recorrente: um cliente, uma competência.

    `estado` conta a história: `aguardando_valor` (cliente sem valor fixo
    cadastrado — alguém precisa digitar antes de emitir), `aguardando_aprovacao`
    (valor já conhecido, falta o clique — ou a tentativa automática, se o modo
    estiver ligado), `emitida` ou `pulada`.
    """

    id: int
    documento: str
    competencia: str            # "AAAA-MM"
    valor: str
    descricao: str
    estado: str
    chave_acesso: str
    criado_em: str
    atualizado_em: str


class RepositorioRecorrencias:
    def __init__(self, banco: BancoLocal):
        self.banco = banco

    def criar_pendencia(
        self, documento: str, competencia: str, valor: str, descricao: str, estado: str
    ) -> tuple[Recorrencia, bool]:
        """Cria a pendência do mês para o cliente, se ainda não existir uma.

        Idempotente por causa do UNIQUE(documento, competencia): chamar de novo
        para o mesmo cliente na mesma competência não duplica nada — só devolve
        o que já existia. `criada_agora` diz qual dos dois casos aconteceu.
        """
        agora = datetime.now().isoformat(timespec="seconds")
        with self.banco._conectar() as conexao:
            cursor = conexao.execute(
                """INSERT OR IGNORE INTO recorrencias
                   (documento, competencia, valor, descricao, estado, criado_em, atualizado_em)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (documento, competencia, valor, descricao, estado, agora, agora),
            )
            criada_agora = cursor.rowcount > 0
            linha = conexao.execute(
                "SELECT * FROM recorrencias WHERE documento = ? AND competencia = ?",
                (documento, competencia),
            ).fetchone()
        return _linha_para_recorrencia(linha), criada_agora

    def listar_pendentes(self) -> list[Recorrencia]:
        marcadores = ", ".join("?" * len(ESTADOS_PENDENTES))
        with self.banco._conectar() as conexao:
            linhas = conexao.execute(
                f"SELECT * FROM recorrencias WHERE estado IN ({marcadores}) "
                "ORDER BY competencia, documento",
                ESTADOS_PENDENTES,
            ).fetchall()
        return [_linha_para_recorrencia(l) for l in linhas]

    def listar_recentes(self, limite: int = 100) -> list[Recorrencia]:
        with self.banco._conectar() as conexao:
            linhas = conexao.execute(
                "SELECT * FROM recorrencias ORDER BY criado_em DESC LIMIT ?", (limite,)
            ).fetchall()
        return [_linha_para_recorrencia(l) for l in linhas]

    def buscar(self, id_: int) -> Recorrencia | None:
        with self.banco._conectar() as conexao:
            linha = conexao.execute("SELECT * FROM recorrencias WHERE id = ?", (id_,)).fetchone()
        return _linha_para_recorrencia(linha) if linha else None

    def definir_valor(self, id_: int, valor: str) -> None:
        with self.banco._conectar() as conexao:
            conexao.execute(
                "UPDATE recorrencias SET valor = ?, atualizado_em = ? WHERE id = ?",
                (valor, datetime.now().isoformat(timespec="seconds"), id_),
            )

    def marcar_emitida(self, id_: int, chave_acesso: str) -> None:
        with self.banco._conectar() as conexao:
            conexao.execute(
                "UPDATE recorrencias SET estado = 'emitida', chave_acesso = ?, atualizado_em = ? "
                "WHERE id = ?",
                (chave_acesso, datetime.now().isoformat(timespec="seconds"), id_),
            )

    def marcar_pulada(self, id_: int) -> None:
        with self.banco._conectar() as conexao:
            conexao.execute(
                "UPDATE recorrencias SET estado = 'pulada', atualizado_em = ? WHERE id = ?",
                (datetime.now().isoformat(timespec="seconds"), id_),
            )


def _linha_para_recorrencia(linha: sqlite3.Row) -> Recorrencia:
    return Recorrencia(**dict(linha))


# ---------------------------------------------------------------------------
# Ponte entre cadastro e emissão
# ---------------------------------------------------------------------------
def completar_com_cadastro(
    linha: LinhaFaturamento, cliente: Cliente | None
) -> tuple[LinhaFaturamento, list[str]]:
    """Preenche na linha da planilha o que só o cadastro tem.

    Endereço incompleto do tomador é causa comum de rejeição, e a planilha
    raramente o traz. O que veio na planilha tem precedência: se a pessoa
    digitou algo lá, foi de propósito.

    Devolve a linha ajustada e a lista do que foi completado, para a interface
    mostrar o que mudou.
    """
    if cliente is None:
        return linha, []

    completados: list[str] = []
    for chave, valor in cliente.extras_para_dps().items():
        if not linha.extras.get(chave):
            linha.extras[chave] = valor
            completados.append(chave)

    if not linha.email and cliente.email:
        linha.email = cliente.email
        completados.append("Email_Cliente")

    # A chave do cadastro decide o envio; sem ela, a nota é emitida e arquivada
    # normalmente, mas não sai e-mail para o cliente.
    linha.enviar_email = cliente.receber_por_email
    return linha, completados

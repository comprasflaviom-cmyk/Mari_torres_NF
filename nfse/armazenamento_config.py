"""
Configuração do aplicativo instalado — substitui o `.env` na interface gráfica.

A separação é deliberada:

* **Dados não sensíveis** (CNPJ, município, `cTribNac`, série, pastas, servidor
  SMTP) vão para um `config.json` na pasta de dados do usuário.
* **Senhas** (do `.pfx` e do SMTP) vão para o cofre de credenciais do sistema
  operacional — Gerenciador de Credenciais no Windows, Keychain no macOS,
  Secret Service no Linux — através da biblioteca `keyring`. **Nunca** em
  arquivo de texto.

O `.env` continua existindo e funcionando para quem usa a linha de comando;
os dois caminhos produzem o mesmo dataclass `Configuracao`, então nada rio
abaixo precisa saber de onde a configuração veio.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal
from pathlib import Path

from .config import Configuracao, ParametrosServico, Prestador
from .email_envio import CORPO_PADRAO, ConfiguracaoEmail

NOME_APP = "EmissorNFSe"
VERSAO_SCHEMA = 1

# Identificadores usados no cofre de credenciais do sistema.
SERVICO_KEYRING = "EmissorNFSe"
CHAVE_SENHA_CERTIFICADO = "senha_certificado_a1"
CHAVE_SENHA_SMTP = "senha_smtp"


class ErroConfiguracaoApp(RuntimeError):
    """Configuração ausente, corrompida ou incompleta."""


# ---------------------------------------------------------------------------
# Localização dos arquivos
# ---------------------------------------------------------------------------
def diretorio_dados() -> Path:
    """Pasta de dados do aplicativo, por sistema operacional.

    Windows:  %APPDATA%\\EmissorNFSe
    macOS:    ~/Library/Application Support/EmissorNFSe
    Linux:    $XDG_CONFIG_HOME/EmissorNFSe (ou ~/.config/EmissorNFSe)

    `EMISSOR_NFSE_DIR` sobrepõe tudo — usado pelos testes e por instalações
    portáteis (rodar de um pendrive, por exemplo).
    """
    if sobreposicao := os.getenv("EMISSOR_NFSE_DIR"):
        destino = Path(sobreposicao)
    elif sys.platform == "win32":
        base = os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming")
        destino = Path(base) / NOME_APP
    elif sys.platform == "darwin":
        destino = Path.home() / "Library" / "Application Support" / NOME_APP
    else:
        base = os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config")
        destino = Path(base) / NOME_APP
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def caminho_config() -> Path:
    return diretorio_dados() / "config.json"


# ---------------------------------------------------------------------------
# Cofre de senhas
# ---------------------------------------------------------------------------
def _keyring():
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - dependência declarada
        raise ErroConfiguracaoApp(
            "Biblioteca 'keyring' não instalada. Rode: pip install keyring"
        ) from exc
    return keyring


def guardar_senha(chave: str, senha: str) -> None:
    """Grava uma senha no cofre do sistema. Senha vazia remove a entrada."""
    keyring = _keyring()
    if not senha:
        remover_senha(chave)
        return
    try:
        keyring.set_password(SERVICO_KEYRING, chave, senha)
    except Exception as exc:  # backend indisponível (Linux headless, por ex.)
        raise ErroConfiguracaoApp(
            f"Não foi possível gravar a senha no cofre do sistema: {exc}"
        ) from exc


def ler_senha(chave: str) -> str | None:
    """Lê uma senha do cofre. Devolve None se não houver ou se o cofre falhar.

    Falha de cofre não derruba o app: a interface pede a senha na hora e a
    mantém apenas em memória durante a sessão.
    """
    try:
        return _keyring().get_password(SERVICO_KEYRING, chave)
    except Exception:
        return None


def remover_senha(chave: str) -> None:
    try:
        _keyring().delete_password(SERVICO_KEYRING, chave)
    except Exception:
        pass  # já não existia


def cofre_disponivel() -> bool:
    """Diz se dá para guardar senhas no sistema — a interface avisa se não der."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as CofreInutil

        return not isinstance(keyring.get_keyring(), CofreInutil)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Configuração persistida
# ---------------------------------------------------------------------------
@dataclass
class ConfiguracaoApp:
    """Tudo que a interface salva — sem nenhuma senha."""

    # Ambiente
    ambiente: str = "homologacao"

    # Prestador
    prestador_cnpj: str = ""
    prestador_im: str = ""
    prestador_cod_municipio: str = "3304557"       # Rio de Janeiro/RJ
    prestador_simples_nacional: int = 3
    prestador_regime_especial: int = 0

    # Serviço
    servico_ctribnac: str = "170101"               # LC 116, 17.01 — consultoria
    servico_cod_municipio: str = "3304557"
    servico_trib_issqn: int = 1
    servico_ret_issqn: int = 1
    servico_ind_tot_trib: int = 0
    iss_aliquota: str = ""                         # vazio = não informa (Simples)

    # Certificado A1 (a SENHA fica no cofre, não aqui)
    caminho_certificado_pfx: str = ""

    # Pastas
    diretorio_notas: str = ""
    diretorio_logs: str = ""
    ultima_planilha: str = ""
    # Pasta de espelhamento: OneDrive, Drive ou unidade de rede servem.
    pasta_backup: str = ""

    # Numeração
    serie_dps: str = "1"
    numero_dps_inicial: int = 1

    # HTTP
    http_timeout: int = 60
    http_max_tentativas: int = 3

    # E-mail (a SENHA fica no cofre)
    email_enviar: bool = False
    email_smtp_servidor: str = "smtp.gmail.com"
    email_smtp_porta: int = 587
    email_smtp_starttls: bool = True
    email_smtp_usuario: str = ""
    email_remetente: str = ""
    email_remetente_nome: str = ""
    email_bcc: list[str] = field(default_factory=list)
    email_permitir_homologacao: bool = False
    email_teste_destino: str = ""
    email_assunto: str = "NFS-e {chave_curta} - {competencia}"
    email_corpo: str = ""

    versao_schema: int = VERSAO_SCHEMA

    # -- Conversão para os dataclasses do núcleo ---------------------------
    def para_configuracao(self, senha_certificado: str | None = None) -> Configuracao:
        """Devolve o mesmo `Configuracao` que o `.env` produziria."""
        base = diretorio_dados()
        return Configuracao(
            ambiente=self.ambiente,
            prestador=Prestador(
                cnpj=self.prestador_cnpj,
                inscricao_municipal=self.prestador_im,
                codigo_municipio=self.prestador_cod_municipio,
                opcao_simples_nacional=self.prestador_simples_nacional,
                regime_especial=self.prestador_regime_especial,
            ),
            servico=ParametrosServico(
                codigo_tributacao_nacional=self.servico_ctribnac,
                codigo_municipio_prestacao=self.servico_cod_municipio,
                tributacao_issqn=self.servico_trib_issqn,
                tipo_retencao_issqn=self.servico_ret_issqn,
                indicador_total_tributos=self.servico_ind_tot_trib,
                aliquota_iss=Decimal(self.iss_aliquota) if self.iss_aliquota.strip() else None,
            ),
            caminho_pfx=Path(self.caminho_certificado_pfx) if self.caminho_certificado_pfx else None,
            senha_pfx=senha_certificado if senha_certificado is not None
            else ler_senha(CHAVE_SENHA_CERTIFICADO),
            caminho_certificado=None,
            caminho_chave=None,
            senha_chave=None,
            caminho_planilha=Path(self.ultima_planilha) if self.ultima_planilha else base / "faturamento.xlsx",
            diretorio_notas=Path(self.diretorio_notas) if self.diretorio_notas else base / "notas",
            diretorio_logs=Path(self.diretorio_logs) if self.diretorio_logs else base / "logs",
            serie_dps=self.serie_dps,
            numero_dps_inicial=self.numero_dps_inicial,
            timeout_segundos=self.http_timeout,
            max_tentativas=self.http_max_tentativas,
            diretorio_backup=Path(self.pasta_backup) if self.pasta_backup.strip() else None,
        )

    def para_configuracao_email(self, senha_smtp: str | None = None) -> ConfiguracaoEmail:
        return ConfiguracaoEmail(
            ativo=self.email_enviar,
            servidor=self.email_smtp_servidor,
            porta=self.email_smtp_porta,
            usuario=self.email_smtp_usuario,
            senha=senha_smtp if senha_smtp is not None else (ler_senha(CHAVE_SENHA_SMTP) or ""),
            remetente_email=self.email_remetente,
            remetente_nome=self.email_remetente_nome,
            usar_starttls=self.email_smtp_starttls,
            copia_oculta=list(self.email_bcc),
            destino_teste=self.email_teste_destino or None,
            permitir_homologacao=self.email_permitir_homologacao,
            assunto_modelo=self.email_assunto,
            corpo_modelo=self.email_corpo.strip() or CORPO_PADRAO,
        )

    # -- Validação ---------------------------------------------------------
    def pendencias(self) -> list[str]:
        """Lista o que falta para conseguir emitir. Vazio = pronto.

        A interface usa isto para mostrar o que ainda precisa ser preenchido,
        em vez de deixar o erro estourar no meio de um lote.
        """
        faltando: list[str] = []
        if not self.prestador_cnpj.isdigit() or len(self.prestador_cnpj) != 14:
            faltando.append("CNPJ do prestador (14 dígitos).")
        if not self.caminho_certificado_pfx:
            faltando.append("Arquivo do Certificado Digital A1 (.pfx).")
        elif not Path(self.caminho_certificado_pfx).exists():
            faltando.append(f"Certificado não encontrado em {self.caminho_certificado_pfx}.")
        if not self.servico_ctribnac.strip():
            faltando.append("Código de tributação nacional (cTribNac).")
        if len(self.prestador_cod_municipio) != 7:
            faltando.append("Código IBGE do município de emissão (7 dígitos).")
        if self.email_enviar and not self.email_remetente:
            faltando.append("E-mail remetente (o envio automático está ligado).")
        if self.iss_aliquota.strip():
            try:
                Decimal(self.iss_aliquota)
            except Exception:
                faltando.append(f"Alíquota de ISS inválida: {self.iss_aliquota!r}")
        return faltando


# ---------------------------------------------------------------------------
# Leitura e gravação
# ---------------------------------------------------------------------------
def carregar() -> ConfiguracaoApp:
    """Lê o `config.json`. Devolve os padrões se ainda não existir."""
    caminho = caminho_config()
    if not caminho.exists():
        return ConfiguracaoApp()

    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ErroConfiguracaoApp(
            f"Não foi possível ler {caminho}: {exc}. "
            "Corrija ou apague o arquivo para recomeçar da configuração padrão."
        ) from exc

    # Ignora chaves desconhecidas: assim uma versão antiga do app não quebra ao
    # abrir um config gravado por uma versão mais nova.
    conhecidos = {campo.name for campo in fields(ConfiguracaoApp)}
    return ConfiguracaoApp(**{k: v for k, v in dados.items() if k in conhecidos})


def salvar(config: ConfiguracaoApp) -> Path:
    """Grava o `config.json` de forma atômica (escreve .tmp e renomeia)."""
    config.versao_schema = VERSAO_SCHEMA
    caminho = caminho_config()
    temporario = caminho.with_suffix(".tmp")
    temporario.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporario.replace(caminho)
    return caminho


def carregar_do_app() -> tuple[Configuracao, ConfiguracaoEmail]:
    """Atalho usado pela interface: configuração pronta para o `Emissor`."""
    app = carregar()
    if pendencias := app.pendencias():
        raise ErroConfiguracaoApp(
            "Configuração incompleta: " + " ".join(pendencias)
        )
    return app.para_configuracao(), app.para_configuracao_email()

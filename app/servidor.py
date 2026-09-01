"""
Servidor da interface gráfica.

Escuta apenas em `127.0.0.1` (ver `lancador.py`) e serve as quatro telas do
aplicativo. Toda a emissão passa por `nfse.servico.Emissor` — o mesmo caminho da
linha de comando.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from nfse import armazenamento_config as ac
from nfse.certificado import ErroCertificado, carregar_certificado
from nfse.estado import ControleEmissao
from nfse.email_envio import ErroEmail, enviar_nfse
from nfse.planilha import ErroPlanilha
from nfse.backup import Espelho
from nfse.clientes import completar_com_cadastro
from nfse.estado import nome_da_maquina
from nfse.servico import OpcoesEmissao, montar_emissor

from .rotas_clientes import registrar as registrar_rotas_clientes, repositorio_clientes, repositorio_emissoes
from .seguranca import Guardiao, gravar_cookie, montar_middleware
from .sessao import ESTADO, LoteEmAndamento, importar_planilha

def _raiz_dos_recursos() -> Path:
    """Onde estão `templates/` e `static/`.

    Rodando do código-fonte é a pasta deste arquivo. Dentro do executável do
    PyInstaller, os módulos ficam num arquivo compactado e `__file__` pode não
    apontar para um caminho real — por isso o lançador exporta
    `EMISSOR_RAIZ_PACOTE`.
    """
    if pacote := os.getenv("EMISSOR_RAIZ_PACOTE"):
        candidato = Path(pacote) / "app"
        if (candidato / "templates").is_dir():
            return candidato
    return Path(__file__).resolve().parent


RAIZ = _raiz_dos_recursos()
CONFIRMACAO_PRODUCAO = "EMITIR EM PRODUCAO"


def criar_app(guardiao: Guardiao | None = None) -> FastAPI:
    guardiao = guardiao or Guardiao()
    app = FastAPI(title="Emissor de NFS-e", docs_url=None, redoc_url=None)
    app.state.guardiao = guardiao

    app.middleware("http")(montar_middleware(guardiao))
    app.mount("/static", StaticFiles(directory=RAIZ / "static"), name="static")
    modelos = Jinja2Templates(directory=str(RAIZ / "templates"))
    modelos.env.filters["moeda"] = _moeda_br

    def pagina(requisicao: Request, nome: str, **contexto) -> HTMLResponse:
        config = contexto.pop("config", None) or _carregar_config_tolerante()
        resposta = modelos.TemplateResponse(
            requisicao,
            nome,
            {
                "config": config,
                "ambiente": config.ambiente,
                "producao": config.ambiente == "producao",
                "pendencias": config.pendencias(),
                "token": guardiao.token,
                **contexto,
            },
        )
        gravar_cookie(resposta, guardiao.token)
        return resposta

    # ------------------------------------------------------------------
    # Painel
    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def painel(requisicao: Request):
        # O lançador abre a URL com ?t=<token>; redireciona para limpar a barra
        # de endereço depois de fixar o cookie.
        if requisicao.query_params.get("t"):
            resposta = RedirectResponse("/", status_code=303)
            gravar_cookie(resposta, guardiao.token)
            return resposta

        config = _carregar_config_tolerante()
        return pagina(
            requisicao, "painel.html",
            config=config,
            certificado=_situacao_certificado(config),
            cofre_ok=ac.cofre_disponivel(),
            importacao=ESTADO.importacao,
            trabalho=ESTADO.trabalho.resumo(),
            backup=_situacao_backup(config),
            maquina=nome_da_maquina(),
            conflito_maquina=_conflito_de_maquina(config),
        )

    # ------------------------------------------------------------------
    # Configuração
    # ------------------------------------------------------------------
    @app.get("/configuracao", response_class=HTMLResponse)
    def tela_configuracao(requisicao: Request, salvo: bool = False):
        return pagina(
            requisicao, "configuracao.html",
            salvo=salvo,
            cofre_ok=ac.cofre_disponivel(),
            tem_senha_certificado=bool(ac.ler_senha(ac.CHAVE_SENHA_CERTIFICADO)),
            tem_senha_smtp=bool(ac.ler_senha(ac.CHAVE_SENHA_SMTP)),
            maquina=nome_da_maquina(),
            conflito_maquina=_conflito_de_maquina(_carregar_config_tolerante()),
        )

    @app.post("/configuracao")
    async def salvar_configuracao(requisicao: Request):
        formulario = await requisicao.form()
        config = _carregar_config_tolerante()

        def texto(campo: str, atual: str = "") -> str:
            return str(formulario.get(campo, atual) or "").strip()

        def numero(campo: str, atual: int) -> int:
            try:
                return int(str(formulario.get(campo, atual)).strip())
            except (TypeError, ValueError):
                return atual

        def marcado(campo: str) -> bool:
            return formulario.get(campo) in ("on", "true", "1")

        config.ambiente = texto("ambiente", config.ambiente) or "homologacao"
        config.prestador_cnpj = "".join(c for c in texto("prestador_cnpj") if c.isdigit())
        config.prestador_im = texto("prestador_im")
        config.prestador_cod_municipio = texto("prestador_cod_municipio", config.prestador_cod_municipio)
        config.prestador_simples_nacional = numero("prestador_simples_nacional", config.prestador_simples_nacional)
        config.prestador_regime_especial = numero("prestador_regime_especial", config.prestador_regime_especial)

        config.servico_ctribnac = texto("servico_ctribnac", config.servico_ctribnac)
        config.servico_cod_municipio = texto("servico_cod_municipio", config.servico_cod_municipio)
        config.servico_trib_issqn = numero("servico_trib_issqn", config.servico_trib_issqn)
        config.servico_ret_issqn = numero("servico_ret_issqn", config.servico_ret_issqn)
        config.servico_ind_tot_trib = numero("servico_ind_tot_trib", config.servico_ind_tot_trib)
        config.iss_aliquota = texto("iss_aliquota").replace(",", ".")

        config.serie_dps = texto("serie_dps", config.serie_dps) or "1"
        config.numero_dps_inicial = numero("numero_dps_inicial", config.numero_dps_inicial)
        config.diretorio_notas = texto("diretorio_notas")
        config.diretorio_logs = texto("diretorio_logs")
        config.pasta_backup = texto("pasta_backup")

        config.email_enviar = marcado("email_enviar")
        config.email_smtp_servidor = texto("email_smtp_servidor", config.email_smtp_servidor)
        config.email_smtp_porta = numero("email_smtp_porta", config.email_smtp_porta)
        config.email_smtp_starttls = marcado("email_smtp_starttls")
        config.email_smtp_usuario = texto("email_smtp_usuario")
        config.email_remetente = texto("email_remetente")
        config.email_remetente_nome = texto("email_remetente_nome")
        config.email_bcc = [e.strip() for e in texto("email_bcc").split(",") if e.strip()]
        config.email_permitir_homologacao = marcado("email_permitir_homologacao")
        config.email_teste_destino = texto("email_teste_destino")
        config.email_assunto = texto("email_assunto", config.email_assunto)
        config.email_corpo = str(formulario.get("email_corpo", "") or "")

        # Certificado: o arquivo é copiado para a pasta do aplicativo, com
        # permissão restrita. O navegador não entrega o caminho real do arquivo
        # escolhido, então guardar uma cópia é o único caminho possível aqui.
        enviado = formulario.get("certificado_arquivo")
        if isinstance(enviado, UploadFile) and enviado.filename:
            config.caminho_certificado_pfx = str(await _guardar_certificado(enviado))

        # Senhas nunca entram no config.json — vão para o cofre do sistema.
        # Campo em branco significa "manter a senha atual", não "apagar".
        _atualizar_senha(ac.CHAVE_SENHA_CERTIFICADO, formulario.get("senha_certificado"))
        _atualizar_senha(ac.CHAVE_SENHA_SMTP, formulario.get("senha_smtp"))

        ac.salvar(config)
        return RedirectResponse("/configuracao?salvo=true", status_code=303)

    @app.post("/configuracao/assumir-maquina")
    def assumir_maquina():
        """Passa para este computador o controle de numeração criado em outro.

        Só faz sentido quando a outra máquina não emite mais nesta série —
        por isso é uma ação explícita, e não algo que aconteça sozinho.
        """
        config = _carregar_config_tolerante()
        controle = ControleEmissao.carregar(
            config.para_configuracao().diretorio_logs,
            config.ambiente, config.serie_dps, config.numero_dps_inicial,
        )
        anterior = controle.maquina
        controle.assumir_maquina()
        controle.salvar()
        return JSONResponse({
            "ok": True,
            "mensagem": f"Controle da série {config.serie_dps} transferido de "
                        f"{anterior or 'desconhecido'} para {nome_da_maquina()}.",
        })

    @app.post("/configuracao/testar-certificado")
    def testar_certificado():
        config = _carregar_config_tolerante()
        try:
            certificado = carregar_certificado(config.para_configuracao())
            certificado.validar_vigencia()
        except ErroCertificado as exc:
            return JSONResponse({"ok": False, "mensagem": str(exc)})
        return JSONResponse({
            "ok": True,
            "titular": certificado.titular,
            "validade": certificado.valido_ate.strftime("%d/%m/%Y"),
            "dias": certificado.dias_para_vencer,
            "mensagem": (
                f"Certificado válido até {certificado.valido_ate:%d/%m/%Y} "
                f"({certificado.dias_para_vencer} dias)."
            ),
        })

    @app.post("/configuracao/testar-email")
    def testar_email():
        config = _carregar_config_tolerante()
        config_email = config.para_configuracao_email()
        if not config_email.ativo:
            return JSONResponse({"ok": False, "mensagem": "O envio por e-mail está desligado."})

        destino = config_email.destino_teste or config_email.remetente_email
        try:
            # Envia para você mesmo, nunca para um cliente.
            resultado = enviar_nfse(
                config_email, "producao", destino,
                {
                    "tomador": "Teste de configuração", "prestador": config.prestador_cnpj,
                    "competencia": date.today().strftime("%m/%Y"),
                    "descricao": "Mensagem de teste do Emissor de NFS-e.",
                    "valor": "0,00", "chave": "TESTE", "chave_curta": "TESTE",
                },
                {},
            )
        except ErroEmail as exc:
            return JSONResponse({"ok": False, "mensagem": str(exc)})
        return JSONResponse({"ok": True, "mensagem": resultado})

    # ------------------------------------------------------------------
    # Importação da planilha
    # ------------------------------------------------------------------
    @app.get("/importar", response_class=HTMLResponse)
    def tela_importar(requisicao: Request):
        return pagina(requisicao, "importar.html", importacao=ESTADO.importacao, erro=None)

    @app.post("/importar", response_class=HTMLResponse)
    async def executar_importacao(requisicao: Request, planilha: UploadFile):
        if not planilha.filename:
            return pagina(requisicao, "importar.html", importacao=ESTADO.importacao,
                          erro="Nenhum arquivo selecionado.")

        destino = ac.diretorio_dados() / "planilhas"
        destino.mkdir(parents=True, exist_ok=True)
        caminho = destino / f"{datetime.now():%Y%m%d-%H%M%S}_{Path(planilha.filename).name}"
        with caminho.open("wb") as arquivo:
            shutil.copyfileobj(planilha.file, arquivo)

        try:
            ESTADO.importacao = importar_planilha(caminho, planilha.filename)
        except ErroPlanilha as exc:
            caminho.unlink(missing_ok=True)
            return pagina(requisicao, "importar.html", importacao=None, erro=str(exc))

        _completar_com_cadastro(ESTADO.importacao)

        config = _carregar_config_tolerante()
        config.ultima_planilha = str(caminho)
        ac.salvar(config)
        return RedirectResponse("/emitir", status_code=303)

    # ------------------------------------------------------------------
    # Emissão
    # ------------------------------------------------------------------
    @app.get("/emitir", response_class=HTMLResponse)
    def tela_emitir(requisicao: Request):
        return pagina(
            requisicao, "emitir.html",
            importacao=ESTADO.importacao,
            trabalho=ESTADO.trabalho.resumo(),
            competencia_padrao=date.today().strftime("%Y-%m"),
            confirmacao_exigida=CONFIRMACAO_PRODUCAO,
        )

    @app.post("/emitir/iniciar")
    async def iniciar_emissao(
        requisicao: Request,
        competencia: str = Form(...),
        modo: str = Form("simular"),
        linhas: str = Form(""),
        confirmacao: str = Form(""),
        reemitir: str = Form(""),
        sem_pdf: str = Form(""),
    ):
        if ESTADO.importacao is None:
            return JSONResponse({"ok": False, "mensagem": "Importe uma planilha primeiro."}, 400)

        config = _carregar_config_tolerante()
        if pendencias := config.pendencias():
            return JSONResponse(
                {"ok": False, "mensagem": "Configuração incompleta: " + " ".join(pendencias)}, 400
            )

        dry_run = modo != "emitir"

        # Trava de produção: exige a frase digitada por extenso. Sem isso, um
        # clique errado emite nota com valor fiscal real.
        if not dry_run and config.ambiente == "producao":
            if confirmacao.strip().upper() != CONFIRMACAO_PRODUCAO:
                return JSONResponse({
                    "ok": False,
                    "mensagem": f'Para emitir em produção, digite exatamente "{CONFIRMACAO_PRODUCAO}".',
                }, 400)

        try:
            competencia_data = datetime.strptime(competencia, "%Y-%m").date().replace(day=1)
        except ValueError:
            return JSONResponse({"ok": False, "mensagem": "Competência inválida."}, 400)

        if outra := _conflito_de_maquina(config):
            return JSONResponse({"ok": False, "mensagem": _mensagem_conflito(config, outra)}, 409)

        selecionadas = {int(n) for n in linhas.split(",") if n.strip().isdigit()} or None
        escolhidas = ESTADO.importacao.selecionadas(selecionadas)
        if not escolhidas:
            return JSONResponse({"ok": False, "mensagem": "Nenhuma linha válida selecionada."}, 400)

        opcoes = OpcoesEmissao(
            competencia=competencia_data,
            dry_run=dry_run,
            reemitir=reemitir in ("on", "true", "1"),
            baixar_pdf=sem_pdf not in ("on", "true", "1"),
        )

        try:
            ESTADO.trabalho.iniciar(
                montar=lambda: montar_emissor(
                    config.para_configuracao(), config.para_configuracao_email()
                ),
                linhas=[l.linha for l in escolhidas if l.linha],
                opcoes=opcoes,
                ambiente=config.ambiente,
                ao_autorizar=_registrar_no_historico(config.ambiente),
            )
        except LoteEmAndamento as exc:
            return JSONResponse({"ok": False, "mensagem": str(exc)}, 409)

        return JSONResponse({"ok": True, "total": len(escolhidas), "dry_run": dry_run})

    @app.get("/emitir/eventos")
    def eventos_emissao():
        """Fluxo SSE com o andamento do lote."""

        def gerar():
            for evento in ESTADO.trabalho.transmitir():
                yield f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            gerar(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/emitir/estado")
    def estado_emissao():
        return JSONResponse(ESTADO.trabalho.resumo())

    # ------------------------------------------------------------------
    # Histórico
    # ------------------------------------------------------------------
    @app.get("/historico", response_class=HTMLResponse)
    def tela_historico(requisicao: Request, busca: str = "", reconstruido: int = -1):
        return pagina(
            requisicao, "historico.html",
            notas=repositorio_emissoes().listar(busca=busca),
            busca=busca,
            reconstruido=reconstruido,
        )

    @app.post("/historico/reconstruir")
    def reconstruir_historico():
        """Refaz a tabela a partir dos arquivos em disco.

        O histórico é um modelo de leitura descartável; os arquivos da pasta de
        notas é que são a verdade.
        """
        config = _carregar_config_tolerante()
        total = repositorio_emissoes().reconstruir(config.para_configuracao().diretorio_notas)
        return RedirectResponse(f"/historico?reconstruido={total}", status_code=303)

    registrar_rotas_clientes(app, pagina, _carregar_config_tolerante)
    return app


# ---------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------
def _moeda_br(valor) -> str:
    """1234.5 -> '1.234,50' — formato brasileiro nas telas."""
    try:
        numero = float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        return str(valor or "")
    return f"{numero:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _carregar_config_tolerante() -> ac.ConfiguracaoApp:
    """Carrega a configuração; se o arquivo estiver corrompido, usa os padrões.

    A tela de configuração precisa abrir mesmo com o arquivo quebrado — senão o
    usuário fica sem como consertar pela interface.
    """
    try:
        return ac.carregar()
    except ac.ErroConfiguracaoApp:
        return ac.ConfiguracaoApp()


def _atualizar_senha(chave: str, valor) -> None:
    """Campo em branco mantém a senha atual; texto novo substitui."""
    if valor is None:
        return
    senha = str(valor)
    if senha.strip():
        ac.guardar_senha(chave, senha)


async def _guardar_certificado(enviado: UploadFile) -> Path:
    destino = ac.diretorio_dados() / "certificado.pfx"
    conteudo = await enviado.read()
    destino.write_bytes(conteudo)
    try:
        destino.chmod(0o600)   # sem efeito prático no Windows, essencial no Unix
    except OSError:
        pass
    return destino


def _situacao_certificado(config: ac.ConfiguracaoApp) -> dict:
    if not config.caminho_certificado_pfx:
        return {"ok": False, "mensagem": "Certificado A1 ainda não configurado."}
    try:
        certificado = carregar_certificado(config.para_configuracao())
        certificado.validar_vigencia()
    except ErroCertificado as exc:
        return {"ok": False, "mensagem": str(exc)}
    return {
        "ok": True,
        "titular": certificado.titular,
        "validade": certificado.valido_ate.strftime("%d/%m/%Y"),
        "dias": certificado.dias_para_vencer,
        "alerta": certificado.dias_para_vencer < 30,
    }


def _completar_com_cadastro(importacao) -> None:
    """Preenche endereço e chave de e-mail das linhas a partir do cadastro.

    Endereço incompleto do tomador é causa comum de rejeição, e a planilha
    raramente o traz. O que veio na planilha continua tendo precedência.
    """
    repo = repositorio_clientes()
    for previa in importacao.validas:
        if previa.linha is None:
            continue
        cliente = repo.buscar(previa.linha.documento_tomador)
        if cliente is None:
            continue
        _, completados = completar_com_cadastro(previa.linha, cliente)
        previa.email = previa.linha.email
        previa.do_cadastro = completados
        previa.recebe_email = cliente.receber_por_email
        previa.cliente_ativo = cliente.ativo


def _mensagem_conflito(config, outra: str) -> str:
    return (
        f"O controle da série {config.serie_dps} foi criado no computador {outra!r}, "
        f"e este é {nome_da_maquina()!r}. Duas máquinas na mesma série geram "
        "numeração repetida. Dê uma série própria a este computador na tela de "
        "Configuração, ou assuma o controle por lá se a outra máquina não emite mais."
    )


def _situacao_backup(config: ac.ConfiguracaoApp) -> dict:
    espelho = Espelho(config.para_configuracao().diretorio_backup)
    if not espelho.ativo:
        return {"ativo": False}
    ultimo = espelho.ultimo_backup()
    return {
        "ativo": True,
        "destino": str(espelho.destino),
        "ultimo": ultimo.strftime("%d/%m/%Y às %H:%M") if ultimo else None,
    }


def _conflito_de_maquina(config: ac.ConfiguracaoApp) -> str | None:
    """Nome do outro computador, se o controle de numeração veio de lá."""
    try:
        controle = ControleEmissao.carregar(
            config.para_configuracao().diretorio_logs,
            config.ambiente, config.serie_dps, config.numero_dps_inicial,
        )
    except (OSError, ValueError):
        return None
    return controle.conflito_de_maquina()


def _registrar_no_historico(ambiente: str):
    """Devolve o gancho que grava cada nota autorizada na tabela de histórico."""
    repo = repositorio_emissoes()

    def registrar(registro: dict) -> None:
        caminho = Path(registro.get("arquivo_xml") or "")
        repo.registrar({
            "chave_acesso": registro.get("chave_acesso"),
            "documento_tomador": registro.get("documento_tomador", ""),
            "tomador": registro.get("razao_social", ""),
            "valor_servico": registro.get("valor_servico", ""),
            "numero_dps": registro.get("numero_dps", ""),
            "emitida_em": datetime.now().isoformat(timespec="seconds"),
            "ambiente": ambiente,
            "pasta": str(caminho.parent) if caminho.name else "",
            "arquivos": {"xml": registro.get("arquivo_xml", "")},
        })

    return registrar

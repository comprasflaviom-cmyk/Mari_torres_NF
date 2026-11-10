"""
Servidor da interface gráfica.

Escuta apenas em `127.0.0.1` (ver `lancador.py`) e serve as quatro telas do
aplicativo. Toda a emissão passa por `nfse.servico.Emissor` — o mesmo caminho da
linha de comando.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.datastructures import UploadFile as ArquivoDeFormulario
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from lxml import etree

from nfse import armazenamento_config as ac
from nfse.certificado import ErroCertificado, carregar_certificado, criar_sessao_mtls
from nfse.config import ROTA_CONSULTA_CHAVE
from nfse.danfse import gerar_danfse_do_arquivo, resumo_para_email
from nfse.estado import ControleEmissao
from nfse.email_envio import ErroEmail, enviar_nfse
from nfse.planilha import ErroPlanilha
from nfse.recibo import formatar_documento
from nfse.armazenamento import (
    caminho_pdf_de, localizar_xml_da_nota, metadados_ao_lado, renomear_arquivos_antigos,
)
from nfse import sincronizacao
from nfse.backup import Espelho
from nfse.clientes import Cliente, ErroCadastro, completar_com_cadastro
from nfse.estado import nome_da_maquina
from nfse.servico import OpcoesEmissao, montar_emissor

from .recorrencias import registrar as registrar_rotas_recorrencias, repositorio_recorrencias
from .rotas_recibos import registrar as registrar_rotas_recibos
from .rotas_clientes import registrar as registrar_rotas_clientes, repositorio_clientes, repositorio_emissoes
from . import sincronia
from .ajuda import AJUDA
from .seguranca import NOME_HEADER, Guardiao, gravar_cookie, montar_middleware
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
    @asynccontextmanager
    async def ciclo_de_vida(_app):
        organizar_pasta_de_notas()
        try:
            sincronia.aplicar_configuracao(_carregar_config_tolerante())
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("Falha ao ligar a sincronização")
        yield

    app = FastAPI(title="Emissor de NFS-e", docs_url=None, redoc_url=None, lifespan=ciclo_de_vida)
    app.state.guardiao = guardiao

    app.middleware("http")(montar_middleware(guardiao))
    app.mount("/static", StaticFiles(directory=RAIZ / "static"), name="static")

    def tela_de_erro(requisicao: Request, titulo: str, mensagem: str, status: int, caminho_log=None):
        # Botões da interface (fetch) esperam JSON: devolvem a mesma mensagem
        # simpática no formato que a tela já sabe mostrar.
        if requisicao.headers.get(NOME_HEADER):
            return JSONResponse({"ok": False, "mensagem": f"{titulo}. {mensagem}"}, status_code=status)
        try:
            resposta = pagina(requisicao, "erro.html", titulo=titulo, mensagem=mensagem,
                              caminho_log=caminho_log)
            resposta.status_code = status
            return resposta
        except Exception:  # noqa: BLE001 — se até a tela de erro falhar, texto simples
            return HTMLResponse(f"<h2>{titulo}</h2><p>{mensagem}</p><p><a href='/'>Voltar</a></p>",
                                status_code=status)

    @app.exception_handler(Exception)
    async def erro_inesperado(requisicao: Request, exc: Exception):
        """No executável não há console: sem isto, um erro vira só "Internal
        Server Error" e o motivo se perde. Grava o traceback e mostra uma
        mensagem que a pessoa entende."""
        caminho = _registrar_erro(requisicao, exc)
        return tela_de_erro(
            requisicao, "Algo não saiu como esperado",
            "Nada foi transmitido por causa deste erro. Tente de novo; se continuar, "
            "envie ao suporte o arquivo erros.log da pasta do aplicativo.",
            500, caminho_log=caminho,
        )

    @app.exception_handler(StarletteHTTPException)
    async def erro_http(requisicao: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return tela_de_erro(requisicao, "Página não encontrada",
                                "Este endereço não existe no aplicativo. Use o menu acima.", 404)
        return await http_exception_handler(requisicao, exc)
    modelos = Jinja2Templates(directory=str(RAIZ / "templates"))
    modelos.env.filters["moeda"] = _moeda_br
    modelos.env.filters["documento"] = formatar_documento
    modelos.env.filters["data"] = _data_br
    modelos.env.filters["competencia"] = _competencia_br

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
                "ajuda": AJUDA,
                "pendentes_mensais": _pendentes_mensais(),
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
            pendentes_recorrencia=len(repositorio_recorrencias().listar_pendentes()),
            resumo=_resumo_do_mes(config),
            sincronia=sincronia.situacao(),
        )

    # ------------------------------------------------------------------
    # Configuração
    # ------------------------------------------------------------------
    @app.get("/configuracao", response_class=HTMLResponse)
    def tela_configuracao(requisicao: Request, salvo: bool = False, senha_volatil: str = ""):
        return pagina(
            requisicao, "configuracao.html",
            salvo=salvo,
            senhas_volateis=[s for s in senha_volatil.split(",") if s],
            cofre_ok=ac.cofre_disponivel(),
            tem_senha_certificado=bool(ac.ler_senha(ac.CHAVE_SENHA_CERTIFICADO)),
            tem_senha_smtp=bool(ac.ler_senha(ac.CHAVE_SENHA_SMTP)),
            maquina=nome_da_maquina(),
            conflito_maquina=_conflito_de_maquina(_carregar_config_tolerante()),
            sincronia=sincronia.situacao(),
            backups=sincronizacao.backups_disponiveis(),
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
        config.prestador_regime_apuracao_sn = numero("prestador_regime_apuracao_sn", config.prestador_regime_apuracao_sn)
        config.prestador_regime_especial = numero("prestador_regime_especial", config.prestador_regime_especial)
        config.prestador_razao_social = texto("prestador_razao_social", config.prestador_razao_social)
        config.prestador_endereco = texto("prestador_endereco", config.prestador_endereco)
        config.recibo_assinante = texto("recibo_assinante", config.recibo_assinante)

        # Aceita colado do portal ("17.03.03" / "17.03.03.001"): só os dígitos
        # valem, e do complementar municipal o XML leva só os 3 últimos.
        config.servico_ctribnac = "".join(
            c for c in texto("servico_ctribnac", config.servico_ctribnac) if c.isdigit()
        )
        config.servico_ctribmun = "".join(
            c for c in texto("servico_ctribmun", config.servico_ctribmun) if c.isdigit()
        )[-3:]
        config.servico_cod_municipio = texto("servico_cod_municipio", config.servico_cod_municipio)
        config.servico_trib_issqn = numero("servico_trib_issqn", config.servico_trib_issqn)
        config.servico_ret_issqn = numero("servico_ret_issqn", config.servico_ret_issqn)
        config.servico_ind_tot_trib = numero("servico_ind_tot_trib", config.servico_ind_tot_trib)
        config.iss_aliquota = texto("iss_aliquota").replace(",", ".")
        config.servico_ptottribsn = texto("servico_ptottribsn", config.servico_ptottribsn).replace(
            "%", ""
        ).replace(",", ".").strip()

        config.serie_dps = texto("serie_dps", config.serie_dps) or "1"
        config.numero_dps_inicial = numero("numero_dps_inicial", config.numero_dps_inicial)
        config.recorrencia_modo = texto("recorrencia_modo", config.recorrencia_modo) or "manual"
        config.diretorio_notas = texto("diretorio_notas")
        config.diretorio_logs = texto("diretorio_logs")
        config.pasta_backup = texto("pasta_backup")
        if "pasta_compartilhada" in formulario:
            config.pasta_compartilhada = texto("pasta_compartilhada").strip('"')
            config.mensais_neste_computador = marcado("mensais_neste_computador")

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
        # A checagem é contra o tipo do Starlette, não o do FastAPI: o
        # `fastapi.UploadFile` é subclasse dele, e o que sai de `form()` é a
        # classe-pai. Com o tipo errado o isinstance dá False e o upload some
        # sem erro nenhum.
        enviado = formulario.get("certificado_arquivo")
        if isinstance(enviado, ArquivoDeFormulario) and enviado.filename:
            config.caminho_certificado_pfx = str(await _guardar_certificado(enviado))

        # Senhas nunca entram no config.json — vão para o cofre do sistema.
        # Campo em branco significa "manter a senha atual", não "apagar".
        so_na_sessao = [
            rotulo
            for chave, campo, rotulo in (
                (ac.CHAVE_SENHA_CERTIFICADO, "senha_certificado", "do certificado"),
                (ac.CHAVE_SENHA_SMTP, "senha_smtp", "do e-mail"),
            )
            if _atualizar_senha(chave, formulario.get(campo)) is False
        ]

        ac.salvar(config)
        try:
            sincronia.aplicar_configuracao(config)
        except Exception:  # noqa: BLE001 — a configuração já está salva; a sincronia avisa na tela
            logging.getLogger(__name__).exception("Falha ao ligar a sincronização")
        destino = "/configuracao?salvo=true"
        if so_na_sessao:
            destino += "&senha_volatil=" + ",".join(so_na_sessao)
        return RedirectResponse(destino, status_code=303)

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
    async def testar_certificado(
        certificado_arquivo: UploadFile | None = File(None),
        senha_certificado: str | None = Form(None),
    ):
        """Testa o certificado do formulário, mesmo antes de salvar.

        A pessoa naturalmente quer conferir o certificado escolhido ANTES de
        decidir salvar — exigir "Salvar" primeiro só para poder testar é o
        tipo de furo que ela só descobre clicando. Se um arquivo novo veio no
        formulário, testa ele (numa cópia temporária, apagada ao final, nunca
        persistida); senão, cai para o que já está salvo.
        """
        config = _carregar_config_tolerante()
        config_teste = config.para_configuracao()

        arquivo_temporario: Path | None = None
        try:
            if certificado_arquivo is not None and certificado_arquivo.filename:
                conteudo = await certificado_arquivo.read()
                arquivo_temporario = ac.diretorio_dados() / f"teste-certificado-{secrets.token_hex(8)}.pfx"
                arquivo_temporario.write_bytes(conteudo)
                config_teste.caminho_pfx = arquivo_temporario

            if senha_certificado:
                config_teste.senha_pfx = senha_certificado

            if not config_teste.caminho_pfx:
                return JSONResponse({
                    "ok": False,
                    "mensagem": "Escolha o arquivo do certificado (.pfx) antes de testar.",
                })

            try:
                certificado = carregar_certificado(config_teste)
                certificado.validar_vigencia()
                # Certificado de outro CNPJ passa em tudo aqui e só é recusado
                # lá na Sefin, com uma mensagem que fala em assinatura. Melhor
                # dizer agora.
                certificado.validar_titular(config_teste.prestador.cnpj)
            except ErroCertificado as exc:
                return JSONResponse({"ok": False, "mensagem": str(exc)})
        finally:
            if arquivo_temporario is not None:
                arquivo_temporario.unlink(missing_ok=True)

        return JSONResponse({
            "ok": True,
            "titular": certificado.titular,
            "cnpj_titular": certificado.cnpj_titular,
            "validade": certificado.valido_ate.strftime("%d/%m/%Y"),
            "dias": certificado.dias_para_vencer,
            "mensagem": (
                f"Certificado de {certificado.cnpj_titular or 'CNPJ não identificado'}, "
                f"válido até {certificado.valido_ate:%d/%m/%Y} "
                f"({certificado.dias_para_vencer} dias)."
            ),
        })

    @app.post("/configuracao/testar-conexao")
    def testar_conexao():
        """Abre a conexão com a Sefin do ambiente escolhido, sem emitir nada.

        Serve para provar, antes de virar a chave para produção, as duas coisas
        que a homologação não prova: que o endereço de produção responde e que
        o certificado é aceito no handshake dele. A consulta é de leitura, por
        uma chave inexistente — a Sefin responde "não encontrada", e isso já
        basta: significa que passou pelo TLS e chegou na aplicação.
        """
        config = _carregar_config_tolerante()
        try:
            certificado = carregar_certificado(config.para_configuracao())
            certificado.validar_vigencia()
        except ErroCertificado as exc:
            return JSONResponse({"ok": False, "mensagem": str(exc)})

        alvo = config.para_configuracao()
        rota = ROTA_CONSULTA_CHAVE.format(chave="0" * 50)
        try:
            resposta = criar_sessao_mtls(certificado).get(
                alvo.url_base.rstrip("/") + rota, timeout=alvo.timeout_segundos
            )
        except Exception as exc:  # noqa: BLE001 — TLS/rede vira mensagem, não traceback
            return JSONResponse({
                "ok": False,
                "mensagem": (
                    f"Não foi possível falar com a Receita ({_modo(config.ambiente)}). "
                    f"Confira a internet e o certificado. Detalhe: {exc}"
                ),
            })

        return JSONResponse({
            "ok": True,
            "mensagem": (
                f"Conexão com a Receita ({_modo(config.ambiente)}) funcionando, e o "
                f"certificado foi aceito. (resposta de teste: HTTP {resposta.status_code})"
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

        novos = _cadastrar_clientes_novos(ESTADO.importacao)
        _completar_com_cadastro(ESTADO.importacao)

        config = _carregar_config_tolerante()
        config.ultima_planilha = str(caminho)
        ac.salvar(config)
        return RedirectResponse(f"/emitir?novos={novos}" if novos else "/emitir", status_code=303)

    # ------------------------------------------------------------------
    # Emissão
    # ------------------------------------------------------------------
    @app.get("/emitir", response_class=HTMLResponse)
    def tela_emitir(requisicao: Request, novos: int = 0):
        return pagina(
            requisicao, "emitir.html",
            clientes_novos=novos,
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
            gerar_pdf=sem_pdf not in ("on", "true", "1"),
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

    @app.get("/danfse/{chave}")
    def abrir_danfse(chave: str):
        """Abre o PDF da nota; se ainda não existir, gera a partir do XML arquivado.

        Cobre as notas autorizadas antes da geração local existir (a API de
        download da Sefin foi desligada em 03/08/2026) e qualquer PDF apagado.
        """
        try:
            _, pdf = _arquivos_da_nota(chave)
        except ErroNotaArquivada as exc:
            return HTMLResponse(str(exc), status_code=exc.status)
        return FileResponse(pdf, media_type="application/pdf",
                            headers={"Content-Disposition": f'inline; filename="{pdf.name}"'})

    @app.post("/historico/reenviar/{chave}")
    def reenviar_email(chave: str):
        """Manda de novo ao tomador o PDF e o XML de uma nota já emitida.

        O destinatário é o e-mail que está na própria nota; sem ele, o do
        cadastro. Valem as mesmas travas do envio automático (redirecionamento
        de teste, bloqueio em homologação).
        """
        try:
            xml, pdf = _arquivos_da_nota(chave)
        except ErroNotaArquivada as exc:
            return JSONResponse({"ok": False, "mensagem": str(exc)})
        config = _carregar_config_tolerante()
        destino, dados = resumo_para_email(xml.read_bytes())
        if not destino:
            # Sem e-mail na nota: o do cadastro, pelo CNPJ/CPF do tomador da própria nota.
            cliente = repositorio_clientes().buscar(_documento_do_tomador(xml.read_bytes()))
            destino = cliente.email if cliente else ""
        if not destino:
            return JSONResponse({"ok": False, "mensagem": "Nem a nota nem o cadastro têm e-mail do cliente."})
        try:
            situacao = enviar_nfse(
                config.para_configuracao_email(), config.ambiente, destino, dados,
                {"pdf": str(pdf), "xml_nfse": str(xml)},
            )
        except ErroEmail as exc:
            return JSONResponse({"ok": False, "mensagem": str(exc)})
        enviado = situacao.startswith(("e-mail enviado", "e-mail redirecionado"))
        return JSONResponse({"ok": enviado, "mensagem": situacao})

    @app.post("/sincronizacao/agora")
    def sincronizar_agora():
        if not sincronizacao.ativo():
            return JSONResponse({"ok": False, "mensagem": "Preencha a pasta compartilhada e salve primeiro."})
        if not sincronizacao.ativo().disponivel():
            return JSONResponse({"ok": False, "mensagem": (
                "A pasta compartilhada não foi encontrada. Confira se o Google Drive está aberto "
                "e sincronizado neste computador."
            )})
        resultado = sincronia.sincronizar_agora(forcar_backup=True)
        return JSONResponse({"ok": True, "mensagem": (
            f"Sincronizado: {resultado['enviados']} alteração(ões) enviada(s), "
            f"{resultado['recebidos']} recebida(s). Backup do banco atualizado."
        )})

    @app.post("/sincronizacao/restaurar")
    async def restaurar_backup(requisicao: Request):
        formulario = await requisicao.form()
        serie = str(formulario.get("serie", "")).strip()
        try:
            total = sincronia.restaurar(serie, _carregar_config_tolerante())
        except (ValueError, OSError, sqlite3.Error) as exc:
            return JSONResponse({"ok": False, "mensagem": f"Não foi possível restaurar: {exc}"})
        return JSONResponse({"ok": True, "mensagem": (
            f"Dados restaurados do computador da série {serie} ({total} alteração(ões) "
            "posteriores aplicadas). Confira Clientes e Notas emitidas."
        )})

    registrar_rotas_clientes(app, pagina, _carregar_config_tolerante)
    registrar_rotas_recorrencias(app, pagina, _carregar_config_tolerante)
    registrar_rotas_recibos(app, pagina, _carregar_config_tolerante)
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


def _resumo_do_mes(config: ac.ConfiguracaoApp) -> dict:
    """Notas e recibos do mês corrente, para o Início. Nunca derruba a página."""
    from .rotas_recibos import repositorio_recibos

    mes = date.today().strftime("%Y-%m")

    def total(itens, campo):
        soma = 0.0
        for item in itens:
            try:
                soma += float(str(item.get(campo) or 0))
            except ValueError:
                pass
        return soma

    try:
        notas = [n for n in repositorio_emissoes().listar(limite=5000)
                 if (n.get("emitida_em") or "").startswith(mes)
                 and (n.get("ambiente") or "producao") == config.ambiente]
        recibos = [r for r in repositorio_recibos().listar(limite=5000)
                   if (r.get("data") or "").startswith(mes)]
    except Exception:  # noqa: BLE001
        notas, recibos = [], []
    return {
        "mes": f"{_MESES[date.today().month - 1]} de {date.today().year}",
        "notas": len(notas), "total_notas": total(notas, "valor_servico"),
        "recibos": len(recibos), "total_recibos": total(recibos, "valor"),
        "ultimas": notas[:5],
    }


_MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
          "agosto", "setembro", "outubro", "novembro", "dezembro"]


def _modo(ambiente: str) -> str:
    return "modo real" if ambiente == "producao" else "modo teste"


def _pendentes_mensais() -> int:
    """Notas mensais esperando ação — vira o número no menu "Emitir nota"."""
    try:
        return len(repositorio_recorrencias().listar_pendentes())
    except Exception:  # noqa: BLE001 — contador do menu nunca derruba a página
        return 0


def _data_br(valor, com_hora: bool = False) -> str:
    """'2026-10-07T14:03:00' -> '07/10/2026' (ou '07/10/2026 14:03')."""
    texto = str(valor or "")
    if len(texto) < 10 or texto[4] != "-":
        return texto or "—"
    data = f"{texto[8:10]}/{texto[5:7]}/{texto[:4]}"
    return f"{data} {texto[11:16]}" if com_hora and len(texto) >= 16 else data


def _competencia_br(valor) -> str:
    """'2026-09' -> '09/2026'."""
    texto = str(valor or "")
    return f"{texto[5:7]}/{texto[:4]}" if len(texto) >= 7 and texto[4] == "-" else (texto or "—")


def _registrar_erro(requisicao: Request, exc: Exception) -> Path:
    """Acrescenta o traceback a `erros.log` na pasta de dados (até ~1 MB)."""
    import traceback

    caminho = ac.diretorio_dados() / "erros.log"
    try:
        if caminho.exists() and caminho.stat().st_size > 1_000_000:
            caminho.replace(caminho.with_suffix(".log.anterior"))
        with caminho.open("a", encoding="utf-8") as log:
            log.write(f"\n=== {datetime.now():%d/%m/%Y %H:%M:%S} {requisicao.method} {requisicao.url.path}\n")
            log.write("".join(traceback.format_exception(exc)))
    except OSError:
        pass
    return caminho


def _carregar_config_tolerante() -> ac.ConfiguracaoApp:
    """Carrega a configuração; se o arquivo estiver corrompido, usa os padrões.

    A tela de configuração precisa abrir mesmo com o arquivo quebrado — senão o
    usuário fica sem como consertar pela interface.
    """
    try:
        return ac.carregar()
    except ac.ErroConfiguracaoApp:
        return ac.ConfiguracaoApp()


def _atualizar_senha(chave: str, valor) -> bool | None:
    """Campo em branco mantém a senha atual; texto novo substitui.

    Devolve None se não havia nada a fazer, True se a senha foi para o cofre do
    sistema, e False se o cofre não estava disponível e ela vale só nesta
    sessão — caso em que a tela avisa, em vez de o salvamento inteiro falhar.
    """
    if valor is None:
        return None
    senha = str(valor)
    if not senha.strip():
        return None
    return ac.guardar_senha(chave, senha)


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


def _cadastrar_clientes_novos(importacao) -> int:
    """Cadastra os clientes da planilha que ainda não estão no cadastro.

    A planilha só vive na memória (some quando o app fecha); o cadastro é o
    que fica. Quem já está cadastrado não é tocado — o cadastro é a fonte da
    verdade, e a planilha de um mês não deve sobrescrever o que foi ajustado
    à mão. Devolve quantos foram cadastrados.
    """
    repo = repositorio_clientes()
    novos = 0
    for previa in importacao.validas:
        linha = previa.linha
        if linha is None or repo.buscar(linha.documento_tomador) is not None:
            continue
        extras = linha.extras
        cliente = Cliente(
            documento=linha.documento_tomador,
            razao_social=linha.razao_social,
            email=linha.email,
            logradouro=extras.get("Logradouro", ""),
            numero=extras.get("Numero", ""),
            complemento=extras.get("Complemento", ""),
            bairro=extras.get("Bairro", ""),
            cod_municipio=extras.get("Cod_Municipio", ""),
            uf=extras.get("UF", ""),
            cep=extras.get("CEP", ""),
            telefone=extras.get("Telefone", ""),
            receber_por_email=bool(linha.email),
        )
        try:
            repo.salvar(cliente)
            novos += 1
        except ErroCadastro:
            continue  # dado incompleto: a linha ainda é emitida, só não vira cadastro
    return novos


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
    espelho = Espelho(config.destino_backup(), serie=config.serie_dps)
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


class ErroNotaArquivada(Exception):
    def __init__(self, mensagem: str, status: int):
        super().__init__(mensagem)
        self.status = status


def _arquivos_da_nota(chave: str) -> tuple[Path, Path]:
    """(XML da NFS-e, PDF) de uma nota na pasta de notas — gera o PDF se faltar."""
    if not re.fullmatch(r"[A-Za-z0-9-]{1,60}", chave):
        raise ErroNotaArquivada("Chave inválida.", 400)
    notas = Path(_carregar_config_tolerante().para_configuracao().diretorio_notas)
    registro = next((n for n in repositorio_emissoes().listar(busca=chave, limite=5)
                     if n.get("chave_acesso") == chave), None)
    xml = localizar_xml_da_nota(notas, chave, registro.get("arquivos") if registro else None)
    if xml is None:
        # Nota emitida em outro computador: a cópia está na pasta compartilhada.
        espelho = _carregar_config_tolerante().destino_backup()
        if espelho is not None and espelho.is_dir():
            xml = localizar_xml_da_nota(espelho, chave)
    if xml is None:
        raise ErroNotaArquivada(f"O XML da NFS-e {chave} não está na pasta de notas ({notas}).", 404)
    pdf = caminho_pdf_de(xml)
    if not pdf.exists():
        try:
            gerar_danfse_do_arquivo(xml)
        except Exception as exc:  # noqa: BLE001 — mensagem na tela, não traceback
            raise ErroNotaArquivada(f"Não foi possível gerar o DANFSe: {exc}", 500) from exc
    return xml, pdf


def organizar_pasta_de_notas() -> int:
    """Na abertura: dá nome legível às notas antigas e refaz o índice delas.

    Idempotente e barato quando não há nada a renomear. Nunca impede o app de
    abrir — problema aqui vira linha no log.
    """
    try:
        notas = Path(_carregar_config_tolerante().para_configuracao().diretorio_notas)
        renomeadas = renomear_arquivos_antigos(notas)
        if renomeadas:
            repositorio_emissoes().reconstruir(notas)
        return renomeadas
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("Falha ao organizar a pasta de notas")
        return 0


def _documento_do_tomador(xml: bytes) -> str:
    try:
        raiz = etree.fromstring(xml)
    except etree.XMLSyntaxError:
        return ""
    achados = raiz.xpath("//*[local-name()='toma']/*[local-name()='CNPJ' or local-name()='CPF']")
    return (achados[0].text or "").strip() if achados else ""


def _registrar_no_historico(ambiente: str):
    """Devolve o gancho que grava cada nota autorizada na tabela de histórico."""
    repo = repositorio_emissoes()

    def registrar(registro: dict) -> None:
        caminho = Path(registro.get("arquivo_xml") or "")
        dados = {
            "chave_acesso": registro.get("chave_acesso"),
            "documento_tomador": registro.get("documento_tomador", ""),
            "tomador": registro.get("razao_social", ""),
            "valor_servico": registro.get("valor_servico", ""),
            "numero_dps": registro.get("numero_dps", ""),
            "emitida_em": datetime.now().isoformat(timespec="seconds"),
            "arquivos": {"xml_nfse": str(caminho)} if caminho.name else {},
        }
        # Os metadados gravados ao lado da nota têm número, competência e caminhos.
        meta = metadados_ao_lado(caminho) if caminho.name else None
        if meta and meta.is_file():
            try:
                dados.update(json.loads(meta.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        dados["ambiente"] = ambiente
        dados["pasta"] = str(caminho.parent) if caminho.name else ""
        repo.registrar(dados)

    return registrar

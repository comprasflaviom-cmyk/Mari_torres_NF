"""
Rotas do cadastro de clientes e da emissão avulsa.

Ficam separadas de `servidor.py` para o arquivo principal não virar um bloco
único. A função `registrar` recebe o app já criado e o auxiliar de renderização.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from nfse import armazenamento_config as ac
from nfse.clientes import (
    BancoLocal,
    Cliente,
    ErroCadastro,
    RepositorioClientes,
    RepositorioEmissoes,
)
from nfse.planilha import LinhaFaturamento, somente_digitos
from nfse.servico import OpcoesEmissao, montar_emissor

from .sessao import ESTADO, LoteEmAndamento

CAMPOS_TEXTO = [
    "razao_social", "email", "logradouro", "numero", "complemento",
    "bairro", "cod_municipio", "uf", "cep", "telefone", "observacao",
]


def caminho_banco():
    return ac.diretorio_dados() / "dados.db"


def repositorio_clientes() -> RepositorioClientes:
    return RepositorioClientes(BancoLocal(caminho_banco()))


def repositorio_emissoes() -> RepositorioEmissoes:
    return RepositorioEmissoes(BancoLocal(caminho_banco()))


def registrar(app: FastAPI, pagina, config_tolerante) -> None:
    # ------------------------------------------------------------------
    # Lista e edição
    # ------------------------------------------------------------------
    @app.get("/clientes", response_class=HTMLResponse)
    def listar_clientes(requisicao: Request, busca: str = "", apenas_ativos: str = ""):
        repo = repositorio_clientes()
        somente_ativos = apenas_ativos in ("1", "true", "on")
        return pagina(
            requisicao, "clientes.html",
            clientes=repo.listar(busca=busca, apenas_ativos=somente_ativos),
            busca=busca,
            apenas_ativos=somente_ativos,
        )

    @app.get("/clientes/novo", response_class=HTMLResponse)
    def novo_cliente(requisicao: Request):
        return pagina(requisicao, "cliente_form.html", cliente=None, erro=None)

    @app.get("/clientes/editar/{documento}", response_class=HTMLResponse)
    def editar_cliente(requisicao: Request, documento: str):
        cliente = repositorio_clientes().buscar(documento)
        if cliente is None:
            return RedirectResponse("/clientes", status_code=303)
        return pagina(requisicao, "cliente_form.html", cliente=cliente, erro=None)

    @app.post("/clientes", response_class=HTMLResponse)
    async def salvar_cliente(requisicao: Request):
        formulario = await requisicao.form()
        dados = {campo: str(formulario.get(campo, "") or "").strip() for campo in CAMPOS_TEXTO}
        cliente = Cliente(
            documento=somente_digitos(formulario.get("documento", "")),
            ativo=formulario.get("ativo") in ("on", "true", "1"),
            receber_por_email=formulario.get("receber_por_email") in ("on", "true", "1"),
            **dados,
        )
        try:
            repositorio_clientes().salvar(cliente)
        except ErroCadastro as exc:
            return pagina(requisicao, "cliente_form.html", cliente=cliente, erro=str(exc))
        return RedirectResponse("/clientes?salvo=true", status_code=303)

    @app.post("/clientes/chave/{documento}")
    async def alternar_chave(requisicao: Request, documento: str):
        """Liga/desliga `ativo` ou `receber_por_email` sem recarregar a página."""
        formulario = await requisicao.form()
        coluna = str(formulario.get("coluna", ""))
        valor = str(formulario.get("valor", "")) in ("1", "true", "on")

        repo = repositorio_clientes()
        cliente = repo.buscar(documento)
        if cliente is None:
            return JSONResponse({"ok": False, "mensagem": "Cliente não encontrado."}, 404)

        # Marcar para receber sem e-mail cadastrado não faria nada de útil.
        if coluna == "receber_por_email" and valor and not cliente.email:
            return JSONResponse(
                {"ok": False, "mensagem": "Cadastre um e-mail para este cliente primeiro."}, 400
            )
        try:
            repo.definir_chave(documento, coluna, valor)
        except ErroCadastro as exc:
            return JSONResponse({"ok": False, "mensagem": str(exc)}, 400)
        return JSONResponse({"ok": True, "coluna": coluna, "valor": valor})

    @app.post("/clientes/excluir/{documento}")
    def excluir_cliente(documento: str):
        repositorio_clientes().excluir(documento)
        return RedirectResponse("/clientes", status_code=303)

    # ------------------------------------------------------------------
    # Compartilhar entre máquinas
    # ------------------------------------------------------------------
    @app.get("/clientes/exportar")
    def exportar_clientes():
        conteudo = json.dumps(repositorio_clientes().exportar(), ensure_ascii=False, indent=2)
        nome = f"clientes-{datetime.now():%Y%m%d}.json"
        return Response(
            conteudo,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{nome}"'},
        )

    @app.post("/clientes/importar", response_class=HTMLResponse)
    async def importar_clientes(requisicao: Request, arquivo: UploadFile):
        try:
            registros = json.loads((await arquivo.read()).decode("utf-8"))
            if not isinstance(registros, list):
                raise ValueError("O arquivo deve conter uma lista de clientes.")
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
            return pagina(requisicao, "clientes.html",
                          clientes=repositorio_clientes().listar(), busca="", apenas_ativos=False,
                          erro=f"Arquivo inválido: {exc}")

        criados, atualizados, erros = repositorio_clientes().importar(registros)
        return pagina(
            requisicao, "clientes.html",
            clientes=repositorio_clientes().listar(), busca="", apenas_ativos=False,
            resultado_importacao={"criados": criados, "atualizados": atualizados, "erros": erros},
        )

    # ------------------------------------------------------------------
    # Nota avulsa
    # ------------------------------------------------------------------
    @app.get("/avulsa", response_class=HTMLResponse)
    def tela_avulsa(requisicao: Request):
        return pagina(
            requisicao, "avulsa.html",
            clientes=repositorio_clientes().listar(apenas_ativos=True),
            competencia_padrao=date.today().strftime("%Y-%m"),
            trabalho=ESTADO.trabalho.resumo(),
        )

    @app.post("/avulsa/emitir")
    async def emitir_avulsa(requisicao: Request):
        formulario = await requisicao.form()
        config = config_tolerante()
        if pendencias := config.pendencias():
            return JSONResponse(
                {"ok": False, "mensagem": "Configuração incompleta: " + " ".join(pendencias)}, 400
            )

        cliente = repositorio_clientes().buscar(str(formulario.get("documento", "")))
        if cliente is None:
            return JSONResponse({"ok": False, "mensagem": "Selecione um cliente do cadastro."}, 400)
        if not cliente.ativo:
            return JSONResponse(
                {"ok": False, "mensagem": f"{cliente.razao_social} está inativo no cadastro."}, 400
            )

        try:
            valor = Decimal(str(formulario.get("valor", "")).replace(".", "").replace(",", "."))
            if valor <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            return JSONResponse({"ok": False, "mensagem": "Valor inválido."}, 400)

        descricao = " ".join(str(formulario.get("descricao", "")).split())
        if not descricao:
            return JSONResponse({"ok": False, "mensagem": "Descreva o serviço prestado."}, 400)

        try:
            competencia = datetime.strptime(
                str(formulario.get("competencia", "")), "%Y-%m"
            ).date().replace(day=1)
        except ValueError:
            return JSONResponse({"ok": False, "mensagem": "Competência inválida."}, 400)

        dry_run = str(formulario.get("modo", "simular")) != "emitir"
        if not dry_run and config.ambiente == "producao":
            from .servidor import CONFIRMACAO_PRODUCAO
            if str(formulario.get("confirmacao", "")).strip().upper() != CONFIRMACAO_PRODUCAO:
                return JSONResponse({
                    "ok": False,
                    "mensagem": f'Para emitir em produção, digite exatamente "{CONFIRMACAO_PRODUCAO}".',
                }, 400)

        linha = LinhaFaturamento(
            numero_linha=1,                      # nota avulsa: não vem de planilha
            documento_tomador=cliente.documento,
            razao_social=cliente.razao_social,
            email=cliente.email,
            valor_servico=valor.quantize(Decimal("0.01")),
            descricao=descricao,
            extras=cliente.extras_para_dps(),
            enviar_email=cliente.receber_por_email,
        )

        try:
            ESTADO.trabalho.iniciar(
                montar=lambda: montar_emissor(
                    config.para_configuracao(), config.para_configuracao_email()
                ),
                linhas=[linha],
                opcoes=OpcoesEmissao(competencia=competencia, dry_run=dry_run),
                ambiente=config.ambiente,
            )
        except LoteEmAndamento as exc:
            return JSONResponse({"ok": False, "mensagem": str(exc)}, 409)

        return JSONResponse({"ok": True, "total": 1, "dry_run": dry_run})

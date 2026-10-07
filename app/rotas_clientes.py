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
    "valor_recorrente", "descricao_recorrente",
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
        try:
            dia_recorrente = int(str(formulario.get("dia_emissao_recorrente", "") or "0").strip())
        except ValueError:
            dia_recorrente = 0
        cliente = Cliente(
            documento=somente_digitos(formulario.get("documento", "")),
            ativo=formulario.get("ativo") in ("on", "true", "1"),
            receber_por_email=formulario.get("receber_por_email") in ("on", "true", "1"),
            dia_emissao_recorrente=dia_recorrente,
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

        from .servidor import _conflito_de_maquina, _mensagem_conflito, _registrar_no_historico
        if outra := _conflito_de_maquina(config):
            return JSONResponse({"ok": False, "mensagem": _mensagem_conflito(config, outra)}, 409)

        cliente, salvar_novo = _tomador_da_nota(formulario)
        if isinstance(cliente, str):
            return JSONResponse({"ok": False, "mensagem": cliente}, 400)

        codigos = _codigos_da_nota(formulario)
        if isinstance(codigos, str):
            return JSONResponse({"ok": False, "mensagem": codigos}, 400)

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
            codigo_tributacao_nacional=codigos[0] if codigos else None,
            codigo_tributacao_municipal=codigos[1] if codigos else None,
        )

        if salvar_novo and not dry_run:
            # Só cria (e só na emissão de verdade): nunca sobrescreve um cadastro.
            repo = repositorio_clientes()
            if repo.buscar(cliente.documento) is None:
                repo.salvar(cliente)

        try:
            ESTADO.trabalho.iniciar(
                montar=lambda: montar_emissor(
                    config.para_configuracao(), config.para_configuracao_email()
                ),
                linhas=[linha],
                opcoes=OpcoesEmissao(competencia=competencia, dry_run=dry_run),
                ambiente=config.ambiente,
                ao_autorizar=_registrar_no_historico(config.ambiente),
            )
        except LoteEmAndamento as exc:
            return JSONResponse({"ok": False, "mensagem": str(exc)}, 409)

        return JSONResponse({"ok": True, "total": 1, "dry_run": dry_run})


def _tomador_da_nota(formulario) -> tuple[Cliente | str, bool]:
    """Cliente do cadastro ou tomador digitado na hora.

    Devolve (cliente, salvar_no_cadastro) ou (mensagem de erro, False).
    """
    if str(formulario.get("tipo_tomador", "cadastro")) != "novo":
        cliente = repositorio_clientes().buscar(str(formulario.get("documento", "")))
        if cliente is None:
            return "Escolha um cliente da lista (ou marque \"Outro tomador\" para digitar um).", False
        if not cliente.ativo:
            return f"{cliente.razao_social} está inativo no cadastro. Reative-o em Clientes.", False
        return cliente, False

    def campo(nome: str) -> str:
        return " ".join(str(formulario.get(f"novo_{nome}", "")).split())

    email = campo("email")
    cliente = Cliente(
        documento=campo("documento"),
        razao_social=campo("razao_social"),
        email=email,
        logradouro=campo("logradouro"),
        numero=campo("numero"),
        complemento=campo("complemento"),
        bairro=campo("bairro"),
        cod_municipio=somente_digitos(campo("cod_municipio")),
        uf=campo("uf").upper(),
        cep=somente_digitos(campo("cep")),
        telefone=campo("telefone"),
        receber_por_email=bool(email),
    )
    try:
        cliente.validar()
    except ErroCadastro as exc:
        return f"Confira o tomador: {exc}", False
    salvar = str(formulario.get("novo_salvar", "")) in ("1", "true", "on")
    return cliente, salvar


def _codigos_da_nota(formulario) -> tuple[str, str] | None | str:
    """Códigos de serviço escolhidos para esta nota.

    None = o formulário não mandou os campos, vale o da Configuração.
    Texto = mensagem de erro.
    """
    if "servico_ctribnac" not in formulario:
        return None
    nacional = somente_digitos(str(formulario.get("servico_ctribnac", "")))
    if len(nacional) != 6:
        return ("O código de tributação nacional tem 6 dígitos (ex.: 17.03.03 → 170303). "
                "Copie do portal gov.br, na nota que você já emite para este serviço.")
    municipal = somente_digitos(str(formulario.get("servico_ctribmun", "")))[-3:]
    if municipal and len(municipal) != 3:
        return "O código complementar municipal tem 3 dígitos (ex.: 001) ou fica vazio."
    return nacional, municipal

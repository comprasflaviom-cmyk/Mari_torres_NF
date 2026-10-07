"""
Recibos de prestação de serviço: PDF para mandar ao cliente, sem Receita.

Nada aqui fala com a Sefin nem mexe na numeração da DPS. O recibo tem
numeração própria (tabela `recibos`) e fica em <pasta das notas>/recibos/AAAA.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from nfse import armazenamento_config as ac
from nfse.clientes import BancoLocal, RepositorioRecibos
from nfse.email_envio import ErroEmail, enviar_documento
from nfse.recibo import DadosRecibo, gerar_recibo, moeda

from .rotas_clientes import _tomador_da_nota, caminho_banco, repositorio_clientes


def repositorio_recibos() -> RepositorioRecibos:
    return RepositorioRecibos(BancoLocal(caminho_banco()))


def pasta_recibos(config: ac.ConfiguracaoApp, ano: int) -> Path:
    base = Path(config.diretorio_notas) if config.diretorio_notas else ac.diretorio_dados() / "notas"
    return base / "recibos" / str(ano)


def pendencias_recibo(config: ac.ConfiguracaoApp) -> list[str]:
    faltando = []
    if not config.prestador_razao_social.strip():
        faltando.append("o nome da empresa")
    if len(config.prestador_cnpj) != 14:
        faltando.append("o CNPJ")
    return faltando


def registrar(app: FastAPI, pagina, config_tolerante) -> None:
    @app.get("/recibos", response_class=HTMLResponse)
    def tela_recibos(requisicao: Request):
        config = config_tolerante()
        return pagina(
            requisicao, "recibos.html",
            config=config,
            clientes=repositorio_clientes().listar(apenas_ativos=True),
            recibos=repositorio_recibos().listar(),
            hoje=date.today().isoformat(),
            faltando=pendencias_recibo(config),
        )

    @app.post("/recibos/gerar")
    async def gerar(requisicao: Request):
        formulario = await requisicao.form()
        config = config_tolerante()
        if faltando := pendencias_recibo(config):
            return JSONResponse({"ok": False, "mensagem": (
                "Para o recibo sair com os dados da sua empresa, preencha em Configuração → "
                "Prestador: " + " e ".join(faltando) + "."
            )}, 400)

        cliente, salvar_novo = _tomador_da_nota(formulario)
        if isinstance(cliente, str):
            return JSONResponse({"ok": False, "mensagem": cliente}, 400)

        try:
            valor = Decimal(str(formulario.get("valor", "")).replace(".", "").replace(",", "."))
            if valor <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            return JSONResponse({"ok": False, "mensagem": "Informe o valor do recibo, ex.: 4.500,00."}, 400)
        valor = valor.quantize(Decimal("0.01"))

        descricao = " ".join(str(formulario.get("descricao", "")).split())
        if not descricao:
            return JSONResponse({"ok": False, "mensagem": "Preencha a que se refere o recibo."}, 400)

        try:
            dia = datetime.strptime(str(formulario.get("data", "")), "%Y-%m-%d").date()
        except ValueError:
            return JSONResponse({"ok": False, "mensagem": "Escolha a data do recibo."}, 400)

        repo = repositorio_recibos()
        numero = repo.criar(cliente.documento, cliente.razao_social, cliente.email,
                            f"{valor:f}", descricao, dia.isoformat())
        pdf = gerar_recibo(DadosRecibo(
            numero=numero, data=dia, valor=valor, descricao=descricao,
            tomador_nome=cliente.razao_social, tomador_documento=cliente.documento,
            prestador_nome=config.prestador_razao_social, prestador_documento=config.prestador_cnpj,
            prestador_endereco=config.prestador_endereco,
            prestador_cod_municipio=config.prestador_cod_municipio,
            assinante=config.recibo_assinante,
        ))
        pasta = pasta_recibos(config, dia.year)
        pasta.mkdir(parents=True, exist_ok=True)
        arquivo = pasta / f"Recibo_{numero:04d}_{cliente.documento}.pdf"
        arquivo.write_bytes(pdf)
        repo.definir_arquivo(numero, str(arquivo))

        if salvar_novo and repositorio_clientes().buscar(cliente.documento) is None:
            repositorio_clientes().salvar(cliente)

        return JSONResponse({
            "ok": True, "numero": numero, "pdf": f"/recibos/{numero}/pdf",
            "email": cliente.email,
            "mensagem": f"Recibo nº {numero:04d} gerado.",
        })

    @app.get("/recibos/{numero}/pdf")
    def abrir_pdf(numero: int):
        recibo = repositorio_recibos().buscar(numero)
        arquivo = Path(recibo["arquivo"]) if recibo and recibo.get("arquivo") else None
        if arquivo is None or not arquivo.exists():
            return HTMLResponse("Recibo não encontrado na pasta. Ele pode ter sido apagado ou movido.", 404)
        return FileResponse(arquivo, media_type="application/pdf",
                            headers={"Content-Disposition": f'inline; filename="{arquivo.name}"'})

    @app.post("/recibos/{numero}/enviar")
    def enviar(numero: int):
        recibo = repositorio_recibos().buscar(numero)
        if recibo is None:
            return JSONResponse({"ok": False, "mensagem": "Recibo não encontrado."}, 404)
        if not recibo.get("email"):
            return JSONResponse({"ok": False, "mensagem": (
                "Este recibo não tem e-mail de destino. Abra o PDF e envie você mesmo, "
                "ou gere de novo informando o e-mail do cliente."
            )}, 400)
        arquivo = Path(recibo.get("arquivo") or "")
        if not arquivo.is_file():
            return JSONResponse({"ok": False, "mensagem": "O PDF deste recibo não está mais na pasta."}, 404)

        config = config_tolerante()
        config_email = config.para_configuracao_email()
        if not all((config_email.servidor, config_email.usuario, config_email.senha,
                    config_email.remetente_email)):
            return JSONResponse({"ok": False, "mensagem": (
                "O e-mail ainda não está configurado. Preencha em Configuração → Envio automático "
                "ao cliente: servidor, usuário, senha e e-mail remetente."
            )}, 400)

        prestador = config.prestador_razao_social
        valor = moeda(Decimal(recibo["valor"]))
        corpo = (
            f"Olá, {recibo['tomador']}!\n\n"
            f"Segue em anexo o recibo nº {numero:04d}, no valor de R$ {valor}, "
            f"referente a {recibo['descricao'].rstrip('.')}.\n\n"
            "Qualquer dúvida, é só responder este e-mail.\n\n"
            f"Atenciosamente,\n{prestador}\n"
        )
        try:
            resultado = enviar_documento(
                config_email, recibo["email"], f"Recibo nº {numero:04d} - {prestador}", corpo, arquivo,
            )
        except ErroEmail as exc:
            return JSONResponse({"ok": False, "mensagem": f"Não foi possível enviar: {exc}"}, 502)
        repositorio_recibos().marcar_enviado(numero, recibo["email"])
        return JSONResponse({"ok": True, "mensagem": resultado[:1].upper() + resultado[1:] + "."})

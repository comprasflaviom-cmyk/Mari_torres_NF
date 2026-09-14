"""Emissão recorrente: nota mensal sem precisar de planilha.

Cada cliente pode ter um dia do mês, um valor e uma descrição fixos —
cadastrados uma vez em `Clientes`. Quando o dia chega, o app cria uma
pendência sozinho; o que acontece com ela depois depende do modo escolhido em
Configuração:

* **manual** (padrão) — fica esperando um clique em "Emitir agora" na tela
  Recorrências, mesmo que o valor já seja conhecido.
* **automático** — se o cliente tem valor fixo cadastrado, emite sozinho assim
  que a pendência nasce. Cliente sem valor fixo nunca é automático, em nenhum
  dos dois modos: alguém precisa digitar o valor daquele mês antes de qualquer
  emissão — não tem o que o app inventar sozinho.

`verificar_pendencias` roda ao abrir o app e, de tempos em tempos, enquanto
ele continuar aberto (ver `iniciar_verificacao_periodica` em `app.lancador`)
— é assim que uma pendência atrasada (app fechado no dia certo) aparece assim
que alguém volta a abrir, em vez de esperar o próximo ciclo mensal.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from nfse import armazenamento_config as ac
from nfse.clientes import BancoLocal, Recorrencia, RepositorioRecorrencias
from nfse.planilha import LinhaFaturamento
from nfse.recorrencia import clientes_vencidos, competencia_de
from nfse.servico import OpcoesEmissao, montar_emissor

from .rotas_clientes import caminho_banco, repositorio_clientes
from .sessao import ESTADO, LoteEmAndamento

INTERVALO_VERIFICACAO_SEGUNDOS = 3600  # de hora em hora, enquanto o app está aberto


def repositorio_recorrencias() -> RepositorioRecorrencias:
    return RepositorioRecorrencias(BancoLocal(caminho_banco()))


# ---------------------------------------------------------------------------
# O verificador: cria pendências vencidas e, no modo automático, dispara a
# emissão das que já têm valor.
# ---------------------------------------------------------------------------
def verificar_pendencias(config: ac.ConfiguracaoApp) -> list[Recorrencia]:
    """Roda uma passada: cria a pendência do mês para quem venceu.

    Idempotente — chamar de novo no mesmo mês para o mesmo cliente não
    duplica nada (ver `RepositorioRecorrencias.criar_pendencia`). Devolve só
    as pendências criadas nesta chamada, para quem quiser avisar sobre elas.
    """
    hoje = date.today()
    competencia = competencia_de(hoje)
    repo_clientes = repositorio_clientes()
    repo_rec = repositorio_recorrencias()

    criadas: list[Recorrencia] = []
    for cliente in clientes_vencidos(repo_clientes.listar(apenas_ativos=True), hoje):
        estado = "aguardando_aprovacao" if cliente.valor_recorrente.strip() else "aguardando_valor"
        recorrencia, nova = repo_rec.criar_pendencia(
            documento=cliente.documento, competencia=competencia,
            valor=cliente.valor_recorrente, descricao=cliente.descricao_recorrente,
            estado=estado,
        )
        if nova:
            criadas.append(recorrencia)

    if config.recorrencia_modo == "automatico":
        _tentar_emitir_automaticas(config, repo_clientes, repo_rec)
    return criadas


def _tentar_emitir_automaticas(config, repo_clientes, repo_rec: RepositorioRecorrencias) -> None:
    """Dispara a primeira pendência automática pronta.

    Só uma por passada: a emissão é sequencial por natureza (numeração da
    DPS), então não dá para começar duas ao mesmo tempo. As demais esperam a
    próxima verificação — o intervalo é curto o bastante (de hora em hora)
    para não incomodar.
    """
    for pendencia in repo_rec.listar_pendentes():
        if pendencia.estado != "aguardando_aprovacao":
            continue  # sem valor fixo, precisa de gente
        cliente = repo_clientes.buscar(pendencia.documento)
        if cliente is None or not cliente.ativo:
            continue
        try:
            _disparar_emissao(config, cliente, pendencia)
        except LoteEmAndamento:
            return  # já tem algo rodando; tenta de novo na próxima verificação
        return


def _linha_da_pendencia(cliente, pendencia: Recorrencia) -> LinhaFaturamento:
    return LinhaFaturamento(
        numero_linha=1,                      # recorrência: não vem de planilha
        documento_tomador=cliente.documento,
        razao_social=cliente.razao_social,
        email=cliente.email,
        valor_servico=Decimal(pendencia.valor),
        descricao=pendencia.descricao or cliente.descricao_recorrente,
        extras=cliente.extras_para_dps(),
        enviar_email=cliente.receber_por_email,
    )


def _disparar_emissao(config: ac.ConfiguracaoApp, cliente, pendencia: Recorrencia) -> None:
    """Dispara a emissão desta pendência na thread compartilhada de lote.

    Levanta `LoteEmAndamento` se já houver outra emissão rodando — o chamador
    decide o que fazer (tentar de novo depois).
    """
    from .servidor import _registrar_no_historico  # importe tardio: evita ciclo com servidor.py

    repo_rec = repositorio_recorrencias()
    registrar_historico = _registrar_no_historico(config.ambiente)

    def ao_autorizar(registro: dict) -> None:
        repo_rec.marcar_emitida(pendencia.id, registro.get("chave_acesso") or "")
        registrar_historico(registro)

    linha = _linha_da_pendencia(cliente, pendencia)
    competencia_data = date.fromisoformat(pendencia.competencia + "-01")
    ESTADO.trabalho.iniciar(
        montar=lambda: montar_emissor(config.para_configuracao(), config.para_configuracao_email()),
        linhas=[linha],
        opcoes=OpcoesEmissao(competencia=competencia_data, dry_run=False),
        ambiente=config.ambiente,
        ao_autorizar=ao_autorizar,
    )


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
def _para_tela(recorrencias: list[Recorrencia], clientes_por_documento: dict) -> list[dict]:
    resultado = []
    for r in recorrencias:
        cliente = clientes_por_documento.get(r.documento)
        resultado.append({
            "id": r.id,
            "razao_social": cliente.razao_social if cliente else r.documento,
            "competencia": r.competencia,
            "valor": r.valor,
            "descricao": r.descricao,
            "estado": r.estado,
        })
    return resultado


def registrar(app: FastAPI, pagina, config_tolerante) -> None:
    @app.get("/recorrencias", response_class=HTMLResponse)
    def tela_recorrencias(requisicao: Request):
        repo = repositorio_recorrencias()
        clientes_por_documento = {c.documento: c for c in repositorio_clientes().listar()}
        recentes = [r for r in repo.listar_recentes(50) if r.estado in ("emitida", "pulada")]
        return pagina(
            requisicao, "recorrencias.html",
            pendentes=_para_tela(repo.listar_pendentes(), clientes_por_documento),
            recentes=_para_tela(recentes, clientes_por_documento),
        )

    @app.post("/recorrencias/{id_}/emitir")
    async def emitir_recorrencia(requisicao: Request, id_: int):
        formulario = await requisicao.form()
        config = config_tolerante()
        if pendencias := config.pendencias():
            return JSONResponse(
                {"ok": False, "mensagem": "Configuração incompleta: " + " ".join(pendencias)}, 400
            )

        from .servidor import CONFIRMACAO_PRODUCAO, _conflito_de_maquina, _mensagem_conflito
        if outra := _conflito_de_maquina(config):
            return JSONResponse({"ok": False, "mensagem": _mensagem_conflito(config, outra)}, 409)

        repo_rec = repositorio_recorrencias()
        pendencia = repo_rec.buscar(id_)
        if pendencia is None or pendencia.estado not in ("aguardando_valor", "aguardando_aprovacao"):
            return JSONResponse({"ok": False, "mensagem": "Pendência não encontrada ou já resolvida."}, 404)

        cliente = repositorio_clientes().buscar(pendencia.documento)
        if cliente is None or not cliente.ativo:
            return JSONResponse({"ok": False, "mensagem": "Cliente inativo ou removido do cadastro."}, 400)

        bruto = str(formulario.get("valor", "") or pendencia.valor).strip()
        try:
            valor = Decimal(bruto.replace(".", "").replace(",", ".")) if "," in bruto else Decimal(bruto)
            if valor <= 0:
                raise InvalidOperation
        except InvalidOperation:
            return JSONResponse({"ok": False, "mensagem": "Valor inválido."}, 400)

        if config.ambiente == "producao":
            if str(formulario.get("confirmacao", "")).strip().upper() != CONFIRMACAO_PRODUCAO:
                return JSONResponse({
                    "ok": False,
                    "mensagem": f'Para emitir em produção, digite exatamente "{CONFIRMACAO_PRODUCAO}".',
                }, 400)

        valor_canonico = f"{valor.quantize(Decimal('0.01')):f}"
        repo_rec.definir_valor(id_, valor_canonico)
        pendencia.valor = valor_canonico

        try:
            _disparar_emissao(config, cliente, pendencia)
        except LoteEmAndamento as exc:
            return JSONResponse({"ok": False, "mensagem": str(exc)}, 409)

        return JSONResponse({"ok": True})

    @app.post("/recorrencias/{id_}/pular")
    def pular_recorrencia(id_: int):
        repositorio_recorrencias().marcar_pulada(id_)
        return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Verificação periódica enquanto o app está aberto (chamada por app.lancador)
# ---------------------------------------------------------------------------
def iniciar_verificacao_periodica(intervalo_segundos: int = INTERVALO_VERIFICACAO_SEGUNDOS) -> None:
    """Roda `verificar_pendencias` já ao ser chamada — cobre o app ter ficado
    fechado no dia certo, avisando com a pendência aparecendo assim que
    alguém reabre — e de novo a cada `intervalo_segundos`, enquanto o processo
    existir.

    Thread solta (`daemon=True`), sem sinal de parada: morre sozinha quando o
    app fecha, igual o resto do app não tem "desligamento gracioso" fora do
    uvicorn.
    """
    import threading
    import time
    import traceback

    def laco() -> None:
        while True:
            try:
                config = ac.carregar()
                if not config.pendencias():
                    verificar_pendencias(config)
            except Exception:  # noqa: BLE001 — nunca derruba o app por causa disso
                traceback.print_exc()
            time.sleep(intervalo_segundos)

    threading.Thread(target=laco, daemon=True).start()

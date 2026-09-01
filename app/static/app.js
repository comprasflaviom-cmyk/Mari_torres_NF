/* Interface do Emissor de NFS-e — sem framework, para o aplicativo funcionar
   offline e o empacotamento não depender de build. */

(function () {
  "use strict";

  /* Toda requisição que altera estado leva o token da sessão. Sem ele o
     servidor responde 403 — é o que impede outro site aberto no navegador de
     disparar uma emissão. */
  function enviar(url, corpo) {
    return fetch(url, {
      method: "POST",
      headers: { "X-Emissor-Token": window.EMISSOR_TOKEN },
      body: corpo
    }).then(function (r) { return r.json().then(function (j) { return { http: r.status, dados: j }; }); });
  }

  /* ---- Botões de teste da tela de configuração ---- */
  document.querySelectorAll("[data-testar]").forEach(function (botao) {
    botao.addEventListener("click", function () {
      var destino = botao.getAttribute("data-testar");
      var saida = document.getElementById(
        destino.indexOf("certificado") >= 0 ? "resultado-certificado" : "resultado-email"
      );
      botao.disabled = true;
      saida.textContent = "Testando...";
      saida.className = "discreto";

      enviar(destino, new FormData())
        .then(function (r) {
          saida.textContent = r.dados.mensagem || (r.dados.ok ? "OK" : "Falhou.");
          saida.className = r.dados.ok ? "etiqueta etiqueta-ok" : "etiqueta etiqueta-erro";
        })
        .catch(function (erro) {
          saida.textContent = "Falha na comunicação: " + erro;
          saida.className = "etiqueta etiqueta-erro";
        })
        .finally(function () { botao.disabled = false; });
    });
  });

  /* ---- Assumir o controle de numeração nesta máquina ---- */
  document.querySelectorAll("[data-assumir]").forEach(function (botao) {
    botao.addEventListener("click", function () {
      var saida = document.getElementById("resultado-maquina");
      botao.disabled = true;
      saida.textContent = "Transferindo...";
      enviar(botao.getAttribute("data-assumir"), new FormData())
        .then(function (r) {
          saida.textContent = r.dados.mensagem;
          saida.className = r.dados.ok ? "etiqueta etiqueta-ok" : "etiqueta etiqueta-erro";
          if (r.dados.ok) setTimeout(function () { location.reload(); }, 1200);
        })
        .catch(function (erro) { saida.textContent = "Falha: " + erro; })
        .finally(function () { botao.disabled = false; });
    });
  });

  /* ---- Interruptores do cadastro de clientes ---- */
  document.querySelectorAll(".interruptor").forEach(function (botao) {
    botao.addEventListener("click", function () {
      var linha = botao.closest("tr");
      var coluna = botao.getAttribute("data-coluna");
      var novoValor = botao.getAttribute("data-valor") === "1" ? "0" : "1";

      var dados = new FormData();
      dados.append("coluna", coluna);
      dados.append("valor", novoValor);

      botao.disabled = true;
      enviar("/clientes/chave/" + linha.getAttribute("data-documento"), dados)
        .then(function (r) {
          if (!r.dados.ok) { alert(r.dados.mensagem); return; }
          botao.setAttribute("data-valor", novoValor);
          botao.classList.toggle("ligado", novoValor === "1");
          if (coluna === "ativo") {
            botao.textContent = novoValor === "1" ? "Ativo" : "Inativo";
            linha.classList.toggle("inativa", novoValor !== "1");
          } else {
            botao.textContent = novoValor === "1" ? "Sim" : "Não";
          }
        })
        .catch(function (erro) { alert("Falha na comunicação: " + erro); })
        .finally(function () { botao.disabled = false; });
    });
  });

  /* ---- Tela de nota avulsa ---- */
  var btnSimularAvulsa = document.getElementById("btn-simular-avulsa");
  if (btnSimularAvulsa) { montarAvulsa(); return; }

  /* ---- Tela de emissão em lote ---- */
  var btnSimular = document.getElementById("btn-simular");
  if (!btnSimular) return;

  var config = window.EMISSOR_CONFIG || {};
  var cortina = document.getElementById("cortina");
  var confirmacao = document.getElementById("confirmacao");
  var btnConfirmar = document.getElementById("btn-confirmar");
  var consoleEl = document.getElementById("console");
  var painel = document.getElementById("painel-progresso");
  var barra = document.getElementById("barra-preenchida");
  var contador = document.getElementById("contador");
  var situacao = document.getElementById("situacao-lote");
  var fonte = null;

  function selecionadas() {
    return Array.prototype.slice
      .call(document.querySelectorAll(".marca-linha:checked"))
      .map(function (c) { return c.value; });
  }

  function valorSelecionado() {
    /* Soma pelo data-valor (bruto, com ponto decimal). O texto da célula está
       no formato brasileiro e parseFloat leria "4.500,00" como 4,5. */
    var total = 0;
    document.querySelectorAll(".marca-linha:checked").forEach(function (c) {
      total += parseFloat(c.closest("tr").getAttribute("data-valor")) || 0;
    });
    return total;
  }

  var marcarTodas = document.getElementById("marcar-todas");
  if (marcarTodas) {
    marcarTodas.addEventListener("change", function () {
      document.querySelectorAll(".marca-linha").forEach(function (c) {
        c.checked = marcarTodas.checked;
      });
    });
  }

  function registrar(texto, classe) {
    var linha = document.createElement("div");
    linha.className = classe || "";
    linha.textContent = texto;
    consoleEl.appendChild(linha);
    consoleEl.scrollTop = consoleEl.scrollHeight;
  }

  var CLASSE_POR_SITUACAO = {
    AUTORIZADA: "l-ok",
    REJEITADA: "l-erro",
    INVALIDA: "l-erro",
    ERRO_LOCAL: "l-erro",
    PULADA: "l-fraco"
  };

  function aplicarEvento(ev) {
    if (ev.tipo === "detalhe") return;   /* ruído de depuração fica fora da tela */

    if (ev.tipo === "inicio_lote") {
      registrar(ev.mensagem, "l-fraco");
      return;
    }
    if (ev.tipo === "aviso") { registrar("  " + ev.mensagem, "l-atencao"); return; }
    if (ev.tipo === "erro")  { registrar("  " + ev.mensagem, "l-erro"); return; }

    if (ev.tipo === "fim_linha") {
      registrar(ev.mensagem, CLASSE_POR_SITUACAO[ev.situacao] || "");
      if (ev.total) {
        barra.style.width = Math.round((ev.indice / ev.total) * 100) + "%";
        contador.textContent = ev.indice + " de " + ev.total;
      }
      marcarResultado(ev);
      return;
    }
    if (ev.tipo === "encerrado") {
      barra.style.width = "100%";
      registrar("");
      registrar(ev.mensagem, "l-ok");
      situacao.textContent = ev.mensagem;
      habilitar(true);
      if (fonte) { fonte.close(); fonte = null; }
    }
  }

  function marcarResultado(ev) {
    var linha = document.querySelector('tr[data-linha="' + ev.linha + '"]');
    if (!linha) return;
    var celula = linha.querySelector("td.resultado");
    if (!celula) return;

    var classe = ev.situacao === "AUTORIZADA" ? "etiqueta-ok"
      : (ev.situacao === "PULADA" ? "etiqueta-neutra" : "etiqueta-erro");
    var detalhe = ev.registro && (ev.registro.chave_acesso || ev.registro.detalhe) || "";
    celula.className = "resultado";
    celula.innerHTML = '<span class="etiqueta ' + classe + '"></span> <span class="discreto"></span>';
    celula.querySelector(".etiqueta").textContent = ev.situacao;
    celula.querySelector(".discreto").textContent = detalhe;
  }

  function habilitar(ligado) {
    btnSimular.disabled = !ligado;
    document.getElementById("btn-emitir").disabled = !ligado;
  }

  function acompanhar() {
    painel.hidden = false;
    consoleEl.innerHTML = "";
    barra.style.width = "0";
    if (fonte) fonte.close();
    /* O servidor reenvia os eventos já ocorridos ao conectar, então recarregar
       a página no meio do lote não perde o histórico. */
    fonte = new EventSource("/emitir/eventos");
    fonte.onmessage = function (e) { aplicarEvento(JSON.parse(e.data)); };
    fonte.onerror = function () { if (fonte) { fonte.close(); fonte = null; } };
  }

  function iniciar(modo, textoConfirmacao) {
    var escolhidas = selecionadas();
    if (!escolhidas.length) {
      situacao.textContent = "Selecione ao menos uma linha.";
      return;
    }
    var dados = new FormData();
    dados.append("competencia", document.getElementById("competencia").value);
    dados.append("modo", modo);
    dados.append("linhas", escolhidas.join(","));
    dados.append("confirmacao", textoConfirmacao || "");
    if (document.getElementById("reemitir").checked) dados.append("reemitir", "on");
    if (document.getElementById("sem_pdf").checked) dados.append("sem_pdf", "on");

    habilitar(false);
    situacao.textContent = "Iniciando...";

    enviar("/emitir/iniciar", dados)
      .then(function (r) {
        if (!r.dados.ok) {
          situacao.textContent = r.dados.mensagem || "Não foi possível iniciar.";
          habilitar(true);
          return;
        }
        situacao.textContent = r.dados.dry_run ? "Simulando..." : "Emitindo...";
        acompanhar();
      })
      .catch(function (erro) {
        situacao.textContent = "Falha na comunicação: " + erro;
        habilitar(true);
      });
  }

  btnSimular.addEventListener("click", function () { iniciar("simular"); });

  document.getElementById("btn-emitir").addEventListener("click", function () {
    if (!config.producao) { iniciar("emitir"); return; }

    /* Em produção, nada acontece sem a frase digitada por extenso. */
    document.getElementById("modal-quantidade").textContent = selecionadas().length;
    document.getElementById("modal-valor").textContent =
      "R$ " + valorSelecionado().toFixed(2).replace(".", ",");
    document.getElementById("modal-competencia").textContent =
      document.getElementById("competencia").value;
    confirmacao.value = "";
    btnConfirmar.disabled = true;
    cortina.hidden = false;
    confirmacao.focus();
  });

  confirmacao.addEventListener("input", function () {
    btnConfirmar.disabled =
      confirmacao.value.trim().toUpperCase() !== config.confirmacaoExigida;
  });

  btnConfirmar.addEventListener("click", function () {
    cortina.hidden = true;
    iniciar("emitir", confirmacao.value.trim());
  });

  document.getElementById("btn-cancelar").addEventListener("click", function () {
    cortina.hidden = true;
  });

  /* Se a página foi aberta com um lote em andamento, reconecta ao fluxo. */
  if (config.estadoInicial && config.estadoInicial.estado === "rodando") {
    habilitar(false);
    situacao.textContent = "Lote em andamento...";
    acompanhar();
  }

  function montarAvulsa() {
    var cfg = window.EMISSOR_CONFIG || {};
    var consoleEl = document.getElementById("console");
    var painel = document.getElementById("painel-progresso");
    var barra = document.getElementById("barra-preenchida");
    var situacao = document.getElementById("situacao-avulsa");
    var cortina = document.getElementById("cortina");
    var confirmacao = document.getElementById("confirmacao");
    var btnConfirmar = document.getElementById("btn-confirmar");
    var seletor = document.getElementById("documento");
    var fonte = null;

    /* Avisa quando o cliente escolhido está marcado para não receber e-mail:
       a nota sai normalmente, mas ninguém recebe nada. */
    seletor.addEventListener("change", function () {
      var opcao = seletor.selectedOptions[0];
      var dica = document.getElementById("dica-cliente");
      if (!opcao || !opcao.value) { dica.textContent = "Só clientes ativos aparecem aqui."; return; }
      dica.textContent = opcao.getAttribute("data-recebe") === "1"
        ? "A nota será enviada para " + opcao.getAttribute("data-email") + "."
        : "Este cliente está marcado para NÃO receber por e-mail. A nota será emitida e arquivada, sem envio.";
    });

    function escrever(texto, classe) {
      var linha = document.createElement("div");
      linha.className = classe || "";
      linha.textContent = texto;
      consoleEl.appendChild(linha);
      consoleEl.scrollTop = consoleEl.scrollHeight;
    }

    function acompanhar() {
      painel.hidden = false;
      consoleEl.innerHTML = "";
      barra.style.width = "0";
      if (fonte) fonte.close();
      fonte = new EventSource("/emitir/eventos");
      fonte.onmessage = function (e) {
        var ev = JSON.parse(e.data);
        if (ev.tipo === "detalhe") return;
        if (ev.tipo === "fim_linha") {
          barra.style.width = "100%";
          escrever(ev.mensagem, ev.situacao === "AUTORIZADA" ? "l-ok"
            : (ev.situacao === "PULADA" ? "l-fraco" : "l-erro"));
        } else if (ev.tipo === "encerrado") {
          situacao.textContent = ev.mensagem;
          habilitar(true);
          if (fonte) { fonte.close(); fonte = null; }
        } else if (ev.tipo === "aviso" || ev.tipo === "erro") {
          escrever("  " + ev.mensagem, ev.tipo === "erro" ? "l-erro" : "l-atencao");
        }
      };
      fonte.onerror = function () { if (fonte) { fonte.close(); fonte = null; } };
    }

    function habilitar(ligado) {
      btnSimularAvulsa.disabled = !ligado;
      document.getElementById("btn-emitir-avulsa").disabled = !ligado;
    }

    function disparar(modo, textoConfirmacao) {
      var dados = new FormData();
      dados.append("documento", seletor.value);
      dados.append("competencia", document.getElementById("competencia").value);
      dados.append("valor", document.getElementById("valor").value);
      dados.append("descricao", document.getElementById("descricao").value);
      dados.append("modo", modo);
      dados.append("confirmacao", textoConfirmacao || "");

      habilitar(false);
      situacao.textContent = "Iniciando...";
      enviar("/avulsa/emitir", dados)
        .then(function (r) {
          if (!r.dados.ok) {
            situacao.textContent = r.dados.mensagem || "Não foi possível emitir.";
            habilitar(true);
            return;
          }
          situacao.textContent = r.dados.dry_run ? "Simulando..." : "Emitindo...";
          acompanhar();
        })
        .catch(function (erro) {
          situacao.textContent = "Falha na comunicação: " + erro;
          habilitar(true);
        });
    }

    btnSimularAvulsa.addEventListener("click", function () { disparar("simular"); });

    document.getElementById("btn-emitir-avulsa").addEventListener("click", function () {
      if (!cfg.producao) { disparar("emitir"); return; }
      var opcao = seletor.selectedOptions[0];
      document.getElementById("modal-cliente").textContent = opcao ? opcao.textContent : "—";
      document.getElementById("modal-valor").textContent =
        "R$ " + (document.getElementById("valor").value || "0,00");
      document.getElementById("modal-competencia").textContent =
        document.getElementById("competencia").value;
      confirmacao.value = "";
      btnConfirmar.disabled = true;
      cortina.hidden = false;
      confirmacao.focus();
    });

    confirmacao.addEventListener("input", function () {
      btnConfirmar.disabled =
        confirmacao.value.trim().toUpperCase() !== cfg.confirmacaoExigida;
    });
    btnConfirmar.addEventListener("click", function () {
      cortina.hidden = true;
      disparar("emitir", confirmacao.value.trim());
    });
    document.getElementById("btn-cancelar").addEventListener("click", function () {
      cortina.hidden = true;
    });

    if (cfg.estadoInicial && cfg.estadoInicial.estado === "rodando") {
      habilitar(false);
      acompanhar();
    }
  }
})();

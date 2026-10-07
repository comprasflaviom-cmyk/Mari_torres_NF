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
    }).then(function (r) {
      return r.json().then(
        function (j) { return { http: r.status, dados: j }; },
        /* Resposta que não é JSON: nunca mostrar "SyntaxError" para quem usa. */
        function () { return { http: r.status, dados: { ok: false, mensagem: MSG_ERRO } }; }
      );
    });
  }

  /* Mensagens para quem usa — o detalhe técnico vai para o erros.log. */
  var MSG_ERRO = "Algo não saiu como esperado. Tente de novo; se continuar, " +
    "envie ao suporte o arquivo erros.log da pasta do aplicativo.";
  var MSG_SEM_CONEXAO = "Não foi possível falar com o aplicativo. Confira se ele " +
    "está aberto (ícone perto do relógio) e tente de novo.";

  /* ---- Ajuda "?" ao lado dos campos ----
     Os textos vêm de app/ajuda.py. Todo <label for="id"> com texto lá ganha um
     "?" que abre um balão; clicar fora ou Esc fecha. */
  (function montarAjuda() {
    var textos = window.EMISSOR_AJUDA || {};
    var tela = document.body.getAttribute("data-pagina") || "";
    var balao = null, aberto = null;

    function fechar() {
      if (balao) { balao.remove(); balao = null; }
      if (aberto) { aberto.setAttribute("aria-expanded", "false"); aberto = null; }
    }

    function abrir(botao, texto) {
      fechar();
      balao = document.createElement("div");
      balao.className = "balao-ajuda";
      balao.setAttribute("role", "tooltip");
      balao.textContent = texto;
      document.body.appendChild(balao);
      var r = botao.getBoundingClientRect();
      var esquerda = Math.min(r.left + window.scrollX,
        window.scrollX + document.documentElement.clientWidth - balao.offsetWidth - 12);
      balao.style.left = Math.max(8, esquerda) + "px";
      balao.style.top = (r.bottom + window.scrollY + 6) + "px";
      botao.setAttribute("aria-expanded", "true");
      aberto = botao;
    }

    document.querySelectorAll("label[for]").forEach(function (rotulo) {
      var id = rotulo.getAttribute("for");
      var texto = textos[tela + ":" + id] || textos[id];
      if (!texto) return;
      var botao = document.createElement("button");
      botao.type = "button";
      botao.className = "ajuda";
      botao.textContent = "?";
      botao.title = "O que é isto?";
      botao.setAttribute("aria-label", "Ajuda: o que é isto?");
      botao.setAttribute("aria-expanded", "false");
      /* O botão fica dentro do <label>: sem o preventDefault, clicar no "?"
         marcaria/desmarcaria a caixa do campo. */
      botao.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        if (aberto === botao) fechar(); else abrir(botao, texto);
      });
      var dica = rotulo.querySelector(".dica");
      if (dica) rotulo.insertBefore(botao, dica); else rotulo.appendChild(botao);
    });

    document.addEventListener("click", function (e) {
      if (balao && !balao.contains(e.target)) fechar();
    });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") fechar(); });
    window.addEventListener("resize", fechar);
  })();

  /* ---- Botões de teste da tela de configuração ---- */
  document.querySelectorAll("[data-testar]").forEach(function (botao) {
    botao.addEventListener("click", function () {
      var destino = botao.getAttribute("data-testar");
      var saida = document.getElementById(botao.getAttribute("data-saida"));
      botao.disabled = true;
      saida.textContent = botao.getAttribute("data-aguarde") || "Testando...";
      saida.className = "discreto";

      /* O teste do certificado usa o que está no formulário agora, mesmo sem
         ter salvo ainda — não faz sentido obrigar "Salvar" só para poder
         conferir o arquivo escolhido. */
      var dados = new FormData();
      if (destino.indexOf("certificado") >= 0) {
        var campoArquivo = document.getElementById("certificado_arquivo");
        var campoSenha = document.getElementById("senha_certificado");
        if (campoArquivo && campoArquivo.files[0]) {
          dados.append("certificado_arquivo", campoArquivo.files[0]);
        }
        if (campoSenha && campoSenha.value) {
          dados.append("senha_certificado", campoSenha.value);
        }
      }

      enviar(destino, dados)
        .then(function (r) {
          saida.textContent = r.dados.mensagem || (r.dados.ok ? "OK" : "Falhou.");
          saida.className = r.dados.ok ? "etiqueta etiqueta-ok" : "etiqueta etiqueta-erro";
        })
        .catch(function (erro) {
          saida.textContent = MSG_SEM_CONEXAO;
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
        .catch(function () { saida.textContent = MSG_SEM_CONEXAO; })
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
        .catch(function () { alert(MSG_SEM_CONEXAO); })
        .finally(function () { botao.disabled = false; });
    });
  });

  /* ---- Tela de recibos ---- */
  if (document.getElementById("btn-gerar-recibo")) { montarRecibos(); return; }

  /* ---- Tela de nota avulsa ---- */
  var btnSimularAvulsa = document.getElementById("btn-simular-avulsa");
  if (btnSimularAvulsa) { montarAvulsa(); return; }

  /* ---- Tela de recorrências ---- */
  if (window.EMISSOR_CONFIG && window.EMISSOR_CONFIG.recorrencias) { montarRecorrencias(); return; }

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
        situacao.textContent = MSG_SEM_CONEXAO;
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

  /* Máscara de moeda: digita só números, o R$ se forma da direita para a
     esquerda (como numa maquininha de cartão) — ninguém precisa pensar em
     onde vai o ponto ou a vírgula. */
  function mascaraMoeda(campo) {
    campo.addEventListener("input", function () {
      var digitos = campo.value.replace(/\D/g, "").replace(/^0+(?=\d)/, "");
      if (!digitos) { campo.value = ""; return; }
      var centavos = digitos.slice(-2).padStart(2, "0");
      var inteiro = (digitos.slice(0, -2) || "0").replace(/\B(?=(\d{3})+(?!\d))/g, ".");
      campo.value = inteiro + "," + centavos;
    });
  }

  /* Bloco do tomador (templates/_tomador.html): cliente do cadastro ou
     digitado na hora. `documento` é como a tela chama o que vai ao cliente
     ("A nota", "O recibo"). */
  function montarTomador(documento) {
    var seletor = document.getElementById("tomador_documento");
    var tipoNovo = document.getElementById("tipo-novo");
    var tipoCadastro = document.getElementById("tipo-cadastro");
    var CAMPOS_NOVO = ["documento", "razao_social", "email", "telefone", "logradouro", "numero",
      "complemento", "bairro", "cod_municipio", "uf", "cep"];

    function novo() { return tipoNovo && tipoNovo.checked; }

    function mostrar() {
      document.getElementById("bloco-cadastro").hidden = novo();
      document.getElementById("bloco-novo").hidden = !novo();
    }
    [tipoNovo, tipoCadastro].forEach(function (r) { if (r) r.addEventListener("change", mostrar); });

    /* Avisa quando o cliente escolhido está marcado para não receber e-mail. */
    seletor.addEventListener("change", function () {
      var opcao = seletor.selectedOptions[0];
      var dica = document.getElementById("dica-cliente");
      if (!opcao || !opcao.value) { dica.textContent = "Só clientes ativos aparecem aqui."; return; }
      dica.textContent = opcao.getAttribute("data-recebe") === "1"
        ? documento + " será enviado(a) para " + opcao.getAttribute("data-email") + "."
        : "Este cliente está marcado para NÃO receber por e-mail.";
    });

    return {
      novo: novo,
      email: function () {
        if (novo()) return document.getElementById("novo_email").value.trim();
        var opcao = seletor.selectedOptions[0];
        return opcao && opcao.value ? opcao.getAttribute("data-email") : "";
      },
      nome: function () {
        if (novo()) {
          return document.getElementById("novo_razao_social").value + " — " +
            document.getElementById("novo_documento").value;
        }
        var opcao = seletor.selectedOptions[0];
        return opcao && opcao.value ? opcao.textContent.trim() : "—";
      },
      anexar: function (dados) {
        dados.append("tipo_tomador", novo() ? "novo" : "cadastro");
        dados.append("documento", seletor.value);
        CAMPOS_NOVO.forEach(function (nome) {
          dados.append("novo_" + nome, document.getElementById("novo_" + nome).value);
        });
        dados.append("novo_salvar", document.getElementById("novo_salvar").checked ? "1" : "");
      }
    };
  }

  /* ---- Tela de recibos: gera o PDF, a pessoa confere e só então envia ---- */
  function montarRecibos() {
    var tomador = montarTomador("O recibo");
    var botao = document.getElementById("btn-gerar-recibo");
    var situacao = document.getElementById("situacao-recibo");
    var resultado = document.getElementById("resultado-recibo");
    var btnEnviar = document.getElementById("resultado-enviar");
    var saidaEnvio = document.getElementById("resultado-envio");
    var numeroAtual = null;
    mascaraMoeda(document.getElementById("valor"));

    botao.addEventListener("click", function () {
      var dados = new FormData();
      tomador.anexar(dados);
      dados.append("data", document.getElementById("data_recibo").value);
      dados.append("valor", document.getElementById("valor").value);
      dados.append("descricao", document.getElementById("descricao").value);

      botao.disabled = true;
      situacao.textContent = "Gerando...";
      resultado.hidden = true;
      enviar("/recibos/gerar", dados)
        .then(function (r) {
          if (!r.dados.ok) { situacao.textContent = r.dados.mensagem || MSG_ERRO; return; }
          situacao.textContent = "";
          numeroAtual = r.dados.numero;
          document.getElementById("resultado-titulo").textContent =
            r.dados.mensagem + " Confira o PDF antes de enviar.";
          document.getElementById("resultado-pdf").href = r.dados.pdf;
          btnEnviar.hidden = !r.dados.email;
          btnEnviar.textContent = "Enviar por e-mail para " + r.dados.email;
          btnEnviar.disabled = false;
          saidaEnvio.textContent = r.dados.email ? "" : "Sem e-mail do cliente: abra o PDF e envie você mesmo.";
          saidaEnvio.className = "discreto";
          resultado.hidden = false;
        })
        .catch(function () { situacao.textContent = MSG_SEM_CONEXAO; })
        .finally(function () { botao.disabled = false; });
    });

    btnEnviar.addEventListener("click", function () {
      if (!numeroAtual) return;
      btnEnviar.disabled = true;
      saidaEnvio.textContent = "Enviando...";
      saidaEnvio.className = "discreto";
      enviar("/recibos/" + numeroAtual + "/enviar", new FormData())
        .then(function (r) {
          saidaEnvio.textContent = r.dados.mensagem || (r.dados.ok ? "Enviado." : MSG_ERRO);
          saidaEnvio.className = r.dados.ok ? "etiqueta etiqueta-ok" : "etiqueta etiqueta-erro";
          if (!r.dados.ok) btnEnviar.disabled = false;
        })
        .catch(function () {
          saidaEnvio.textContent = MSG_SEM_CONEXAO;
          saidaEnvio.className = "etiqueta etiqueta-erro";
          btnEnviar.disabled = false;
        });
    });
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
    var tomador = montarTomador("A nota");
    var fonte = null;
    mascaraMoeda(document.getElementById("valor"));

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
          var chave = ev.registro && ev.registro.chave_acesso;
          if (ev.situacao === "AUTORIZADA" && chave) {
            /* Atalho para imprimir: o PDF também fica no Histórico. */
            var link = document.createElement("a");
            link.href = "/danfse/" + encodeURIComponent(chave);
            link.target = "_blank";
            link.className = "botao botao-pequeno";
            link.textContent = "Abrir PDF da nota (imprimir / salvar)";
            var linhaLink = document.createElement("div");
            linhaLink.style.marginTop = "10px";
            linhaLink.appendChild(link);
            consoleEl.appendChild(linhaLink);
          }
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
      tomador.anexar(dados);
      dados.append("servico_ctribnac", document.getElementById("servico_ctribnac").value);
      dados.append("servico_ctribmun", document.getElementById("servico_ctribmun").value);
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
          situacao.textContent = MSG_SEM_CONEXAO;
          habilitar(true);
        });
    }

    btnSimularAvulsa.addEventListener("click", function () { disparar("simular"); });

    document.getElementById("btn-emitir-avulsa").addEventListener("click", function () {
      if (!cfg.producao) { disparar("emitir"); return; }
      var nome = tomador.nome();
      document.getElementById("modal-cliente").textContent = nome;
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

  /* ---- Tela de recorrências ---- */
  function montarRecorrencias() {
    var cfg = window.EMISSOR_CONFIG || {};
    var cortina = document.getElementById("cortina-recorrencia");
    var confirmacao = document.getElementById("confirmacao-recorrencia");
    var btnConfirmar = document.getElementById("btn-confirmar-recorrencia");
    var linhaPendente = null;

    /* Mesma máscara de moeda da nota avulsa: digita só números, o R$ se forma
       da direita para a esquerda. */
    document.querySelectorAll(".campo-valor").forEach(function (campo) {
      campo.addEventListener("input", function () {
        var digitos = campo.value.replace(/\D/g, "").replace(/^0+(?=\d)/, "");
        if (!digitos) { campo.value = ""; return; }
        var centavos = digitos.slice(-2).padStart(2, "0");
        var inteiro = (digitos.slice(0, -2) || "0").replace(/\B(?=(\d{3})+(?!\d))/g, ".");
        campo.value = inteiro + "," + centavos;
      });
    });

    function travarBotoes(travado) {
      document.querySelectorAll(".botao-emitir-recorrencia, .botao-pular-recorrencia")
        .forEach(function (b) { b.disabled = travado; });
    }

    function acompanhar(situacao) {
      var fonte = new EventSource("/emitir/eventos");
      fonte.onmessage = function (e) {
        var ev = JSON.parse(e.data);
        if (ev.tipo === "encerrado") {
          situacao.textContent = ev.mensagem;
          fonte.close();
          setTimeout(function () { location.reload(); }, 1200);
        } else if (ev.tipo === "fim_linha") {
          situacao.textContent = ev.situacao === "AUTORIZADA" ? "Autorizada." : ev.mensagem;
        }
      };
      fonte.onerror = function () { fonte.close(); };
    }

    function disparar(linha, textoConfirmacao) {
      var id = linha.getAttribute("data-id");
      var situacao = linha.querySelector(".situacao-recorrencia");
      var dados = new FormData();
      dados.append("valor", linha.querySelector(".campo-valor").value);
      dados.append("confirmacao", textoConfirmacao || "");

      travarBotoes(true);
      situacao.textContent = "Iniciando...";
      enviar("/recorrencias/" + id + "/emitir", dados)
        .then(function (r) {
          if (!r.dados.ok) {
            situacao.textContent = r.dados.mensagem || "Não foi possível emitir.";
            travarBotoes(false);
            return;
          }
          situacao.textContent = "Emitindo...";
          acompanhar(situacao);
        })
        .catch(function (erro) {
          situacao.textContent = MSG_SEM_CONEXAO;
          travarBotoes(false);
        });
    }

    document.querySelectorAll(".botao-emitir-recorrencia").forEach(function (botao) {
      botao.addEventListener("click", function () {
        var linha = botao.closest("tr");
        if (!cfg.producao) { disparar(linha); return; }
        linhaPendente = linha;
        confirmacao.value = "";
        btnConfirmar.disabled = true;
        cortina.hidden = false;
        confirmacao.focus();
      });
    });

    confirmacao.addEventListener("input", function () {
      btnConfirmar.disabled = confirmacao.value.trim().toUpperCase() !== cfg.confirmacaoExigida;
    });
    btnConfirmar.addEventListener("click", function () {
      cortina.hidden = true;
      disparar(linhaPendente, confirmacao.value.trim());
    });
    document.getElementById("btn-cancelar-recorrencia").addEventListener("click", function () {
      cortina.hidden = true;
    });

    document.querySelectorAll(".botao-pular-recorrencia").forEach(function (botao) {
      botao.addEventListener("click", function () {
        var linha = botao.closest("tr");
        if (!confirm("Pular esta competência para este cliente? Ela não volta a aparecer sozinha.")) return;
        enviar("/recorrencias/" + linha.getAttribute("data-id") + "/pular", new FormData())
          .then(function () { location.reload(); });
      });
    });
  }
})();

# rpa_3003

Framework RPA interno, modular e extensível, em Python 3.12+, para automações web e desktop em
Windows. Versão 1.0.0.

## Visão geral

O `rpa_3003` padroniza a criação, a execução e a operação de robôs. Um único pacote instalável
oferece:

- **Projetos padronizados:** o comando `rpa3003 create` gera a estrutura completa a partir de um
  template, e `rpa3003 validate` confere a estrutura.
- **Orquestração em steps:** cada etapa é uma classe `Step`. O step de encerramento (wrapup) roda
  sempre, mesmo após falha, e cada resultado tem um código de saída padronizado.
- **Ambientes `dev` e `prd`** no mesmo projeto, escolhidos na hora de executar.
- **Logging centralizado** com `loguru`: formato único, rotação diária e mascaramento de
  segredos.
- **HashiCorp Vault** para credenciais, com retentativas, cache e provedor local para `dev`.
- **Automação web** com Selenium: Chrome, Edge e Edge em modo IE, com a mesma classe `Browser`.
- **Automação desktop:** busca de imagens na tela, mouse, teclado com acentos, janelas e
  processos (`DesktopBot`).
- **Utilitários:** arquivos e TXT, JSON, CSV, ZIP/TAR, datas e dias úteis, criptografia, bancos
  de dados, e-mail (SMTP, IMAP e Outlook), imagens e PDF.

Restrições de segurança:

- **Bancos de dados somente leitura.** O `database_utils` só faz consultas. Não existe API de
  inclusão, alteração, exclusão, DDL nem controle de transação, e a conexão é aberta em modo
  somente leitura. O usuário de banco de cada robô deve ter apenas privilégio de consulta. O que
  o robô precisar registrar vai para arquivos em `data/` do projeto (por exemplo, um CSV de itens
  processados).
- **Nenhum segredo em código.** Credenciais de produção vêm exclusivamente do Vault. O
  `global_vars.py` guarda só configuração não sensível, e o log mascara os segredos lidos.

Este README cobre o **uso** do framework. Para entender ou modificar o framework (arquitetura,
convenções, pontos de extensão, testes e fluxo de contribuição), leia o
[Guia do desenvolvedor](docs/GUIA_DO_DESENVOLVEDOR.md). A especificação normativa está no
[PRD](PRD.md), e os desvios registrados estão em [docs/DESVIOS.md](docs/DESVIOS.md).

## Requisitos

| Item | Versão |
|---|---|
| Sistema operacional | Windows 10 21H2+, Windows 11 ou Windows Server 2019+, x64 |
| Python | 3.12 ou superior, **64 bits** (`python --version` e `python -c "import struct; print(struct.calcsize('P') * 8)"` → `64`) |
| Google Chrome | Versão atual, para `Browser("chrome")` |
| Microsoft Edge | Versão atual, para `Browser("edge")` e `Browser("edge_ie")` |
| Modo IE do Edge | Somente para sistemas legados; configuração na seção [Navegadores](#navegadores) |
| Permissões | Usuário comum. Só a configuração inicial do modo IE exige administrador |

A automação desktop precisa de uma sessão Windows interativa (desbloqueada). Veja
[Automação desktop](#automação-desktop).

## Instalação

Abra o PowerShell na pasta do workspace (`rpa-framework`, obtida do repositório) e execute:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
```

Se houver mais de um Python na máquina, crie o ambiente com `py -3.12 -m venv .venv`. Para
instalar sem as ferramentas de teste, use `pip install -e .`.

Confira a instalação:

```powershell
rpa3003 --version     # rpa3003 1.0.0
rpa3003 templates     # lista o template "example"
```

Se o PowerShell recusar o `activate` por política de execução, veja
[Solução de problemas](#solução-de-problemas). Sem ativar o ambiente, use
`.venv\Scripts\rpa3003.exe` ou `.venv\Scripts\python.exe -m rpa_3003`.

## Início rápido

Com o ambiente ativo, no workspace, crie um projeto, gere um CSV de exemplo e execute em `dev`:

```powershell
rpa3003 create conciliacao_bancaria
rpa3003 validate conciliacao_bancaria
Set-Content projects\conciliacao_bancaria\data\input\extrato.csv "id;nome;valor`n1;José;10,50" -Encoding utf8
rpa3003 run conciliacao_bancaria
Get-Content projects\conciliacao_bancaria\data\output\status_*.json
```

Resultado esperado:

1. `create` imprime `Projeto 'conciliacao_bancaria' criado em ...` e os próximos passos.
2. `validate` imprime `Projeto 'conciliacao_bancaria' válido`.
3. `run` (ambiente `dev` por padrão) mostra o log no console e termina com
   `Execução finalizada | status=SUCCESS | exit_code=0`.
4. O status mostra `"sucesso": true`. O relatório está em `data\output\relatorio_<run_id>.json`
   e o log em `logs\conciliacao_bancaria_<AAAA-MM-DD>.log`.

O projeto de exemplo não usa Vault, navegador nem rede em `dev`. A partir daqui, edite os steps
em `workflows\dev\` (seção [Escrevendo steps](#escrevendo-steps)).

## Estrutura de um projeto

Os projetos ficam em `projects/<nome>/`. O nome segue `^[a-z][a-z0-9_]{2,49}$` (minúsculas,
dígitos e `_`, de 3 a 50 caracteres).

```text
projects/
└── conciliacao_bancaria/
    ├── main.py                    # Orquestrador: lê --env/--user/--log-level, chama bootstrap e WorkflowRunner
    ├── global_vars.py             # Constantes, ENV_CONFIG por ambiente e get_credentials() (Vault)
    ├── README.md                  # Instruções do projeto
    ├── .env.example               # Modelo de variáveis; copie para .env (não versionado)
    ├── workflows/
    │   ├── __init__.py
    │   ├── dev/                   # Steps em desenvolvimento
    │   │   ├── __init__.py
    │   │   ├── step_setup_env.py          # 1. Prepara pastas, configuração, credenciais e recursos
    │   │   ├── step_execute_task.py       # 2. Executa a tarefa do robô
    │   │   └── step_wrapup_execution.py   # 3. Encerra: grava status_<run_id>.json (sempre executa)
    │   └── prd/                   # Steps de produção (mesmos arquivos; nunca importa workflows.dev)
    ├── data/
    │   ├── input/                 # Arquivos de entrada
    │   ├── output/                # Relatórios, status e controles gravados pelo robô
    │   └── images/                # Imagens de referência PNG da automação desktop
    ├── logs/                      # Log diário <nome>_<AAAA-MM-DD>.log
    │   └── screenshots/           # Evidências de erro (criada na execução)
    └── .rpa3003.lock              # Lock de instância única (criado na execução)
```

`rpa3003 validate` exige `main.py`, `global_vars.py`, `logs/`, `data/` e os pacotes
`workflows/`, `workflows/dev/` e `workflows/prd/`, cada ambiente com os três steps padrão. Cada
arquivo `step_*.py` deve definir `class Step`, e nenhum módulo de `workflows/prd/` pode importar
`workflows.dev`.

## Escrevendo steps

Cada arquivo `workflows/<env>/step_<nome>.py` define `class Step(BaseStep)` com o método
`run()`. O framework registra início, fim e duração de cada step, aplica as retentativas e trata
as falhas.

```python
"""Step 2 - execução da tarefa (conciliacao_bancaria | dev)."""

from __future__ import annotations

from rpa_3003.core.exceptions import BusinessRuleError
from rpa_3003.core.workflow import BaseStep
from rpa_3003.utils import csv_utils, filesystem_utils

import global_vars as gv


class Step(BaseStep):
    """Concilia os extratos de data/input."""

    name = "execute_task"  # nome no log; vazio usa o nome do arquivo sem "step_"
    retries = 2  # até 2 novas tentativas em falha técnica
    retry_delay = 10.0  # segundos entre as tentativas

    def run(self) -> None:
        arquivos = filesystem_utils.list_files(gv.INPUT_DIR, "*.csv")
        if not arquivos:
            raise BusinessRuleError(
                "Nenhum extrato para conciliar", details={"pasta": gv.INPUT_DIR}
            )
        linhas = [linha for arquivo in arquivos for linha in csv_utils.read_csv(arquivo)]
        self.ctx.state["total_linhas"] = len(linhas)  # disponível para os próximos steps
        self.log.info("Extratos lidos | arquivos={} | linhas={}", len(arquivos), len(linhas))
```

| Recurso | Uso |
|---|---|
| `self.ctx.state` | Dicionário compartilhado entre os steps da execução |
| `self.ctx.register_cleanup(func)` | Registra uma função de limpeza (ex.: `browser.quit`). As funções rodam em ordem inversa (LIFO), após todos os steps, mesmo com falha |
| `self.log` | Logger da execução (`info`, `success`, `warning`, `error`...) |
| `self.ctx.env`, `self.ctx.run_id`, `self.ctx.paths`, `self.ctx.settings`, `self.ctx.extra_args` | Ambiente, identificador da execução, caminhos do projeto, configuração e argumentos após `--` |
| `self.ctx.error` | Primeira falha da execução, ou `None` (útil no wrapup) |
| `retries` e `retry_delay` | Novas tentativas de `run()` para falhas técnicas |
| `BusinessRuleError` | Sinaliza regra de negócio violada: **sem** retentativa, código de saída `4` |
| `always_run = True` | O step roda mesmo após falha anterior (usado no wrapup) |
| `before()`, `after()`, `on_error(error)` | Ganchos opcionais. `on_error` roda uma vez após a falha definitiva (ex.: screenshot) |

Regras de execução:

- Os steps rodam na ordem de `STEPS` em `main.py` (padrão: `step_setup_env`,
  `step_execute_task`, `step_wrapup_execution`). Para incluir um step, crie o arquivo e acrescente
  o nome em `STEPS`.
- Todos os steps são carregados antes do primeiro executar. Erro de importação encerra com
  código `3` sem executar nenhum.
- Depois de uma falha, os steps seguintes são ignorados, exceto os com `always_run = True`.
- Um recurso aberto em um step (navegador, conexão, aplicação) deve ser registrado com
  `register_cleanup` no mesmo step em que foi aberto.

Exemplo de recurso com limpeza garantida, no `step_setup_env`:

```python
from rpa_3003.bots.browser_selenium import Browser, BrowserOptions

options = BrowserOptions.from_settings(self.ctx.settings, "edge")
browser = Browser("edge", options, screenshots_dir=self.ctx.paths.screenshots_dir).start()
self.ctx.state["browser"] = browser
self.ctx.register_cleanup(browser.quit)
```

## Ambientes dev e prd

Cada projeto tem os steps de `dev` em `workflows/dev/` e os de produção em `workflows/prd/`. O
`global_vars.py` define em `ENV_CONFIG` os parâmetros não sensíveis de cada ambiente (URL,
navegador, caminho do segredo no Vault).

O ambiente é escolhido nesta ordem de precedência:

1. Opção `--env` de `rpa3003 run` ou de `python main.py`.
2. Variável `RPA3003_ENV` do ambiente.
3. `RPA3003_ENV` no `.env` do projeto.
4. `dev` (produção nunca roda por omissão).

Valor inválido encerra com código `2`.

Promoção de `dev` para `prd` (processo manual):

1. Valide a alteração executando o projeto em `dev`.
2. Submeta a alteração para revisão de código.
3. Depois de aprovada, copie os arquivos alterados de `workflows/dev/` para `workflows/prd/`.
   Ajuste só a docstring (`| dev)` → `| prd)`). Um step de `prd` nunca importa `workflows.dev`.
4. Confira `ENV_CONFIG[Environment.PRD]` em `global_vars.py` e o segredo no Vault.
5. Execute `rpa3003 validate <nome>`, que deve retornar `0`.

## Configuração

O framework lê variáveis de ambiente com prefixo `RPA3003_` e o arquivo `.env` do diretório
atual. Precedência: argumentos do CLI → variáveis de ambiente → `.env` → padrão.

- Na execução de um projeto, o diretório atual é a pasta do projeto, então vale o
  `projects/<nome>/.env`. Copie o `.env.example` gerado pelo template e ajuste.
- O próprio CLI lê o `.env` do workspace para `RPA3003_PROJECTS_DIR` e `RPA3003_RUN_TIMEOUT`.
- Valor inválido encerra com código `3`, e a mensagem cita o campo.

**Framework (`FrameworkSettings`):**

| Variável | Tipo | Padrão | Descrição |
|---|---|---|---|
| `RPA3003_ENV` | `dev` ou `prd` | `dev` | Ambiente quando `--env` não é informado |
| `RPA3003_LOG_LEVEL` | `TRACE` a `CRITICAL` | `INFO` | Nível mínimo de log |
| `RPA3003_LOG_USER` | texto | usuário Windows | Valor do campo `user` do log |
| `RPA3003_LOG_RETENTION_DAYS` | inteiro 1–3650 | `30` | Retenção dos arquivos de log |
| `RPA3003_LOG_COMPRESSION` | booleano | `false` | Compacta em ZIP o arquivo do dia anterior na rotação |
| `RPA3003_LOG_CONSOLE` | booleano | `true` | Habilita o log no console |
| `RPA3003_PROJECTS_DIR` | caminho | `projects` | Diretório de projetos do CLI |
| `RPA3003_RUN_TIMEOUT` | inteiro ≥ 1 | sem limite | Timeout padrão de `rpa3003 run`, em segundos |
| `RPA3003_DEFAULT_BROWSER` | texto | `chrome` | Navegador padrão do projeto (`ctx.settings.default_browser`) |
| `RPA3003_BROWSER_HEADLESS` | booleano | `false` | Headless padrão de `BrowserOptions.from_settings` |
| `RPA3003_SELENIUM_TIMEOUT` | inteiro ≥ 1 | `30` | Timeout padrão das esperas explícitas (s) |
| `RPA3003_PAGE_LOAD_TIMEOUT` | inteiro ≥ 1 | `60` | Timeout de carregamento de página (s) |
| `RPA3003_CHROMEDRIVER_PATH` | caminho | Selenium Manager | Caminho manual do chromedriver |
| `RPA3003_EDGEDRIVER_PATH` | caminho | Selenium Manager | Caminho manual do msedgedriver |
| `RPA3003_IEDRIVER_PATH` | caminho | Selenium Manager | Caminho manual do IEDriverServer |
| `RPA3003_EDGE_BINARY_PATH` | caminho | detecção automática | Caminho do `msedge.exe` para o modo IE |
| `RPA3003_SCREENSHOT_ON_ERROR` | booleano | `true` | Screenshot automático em falha (`ctx.settings.screenshot_on_error`) |
| `RPA3003_DESKTOP_FAILSAFE` | booleano | `true` | FAILSAFE do pyautogui: mouse no canto da tela aborta (`ctx.settings.desktop_failsafe`) |
| `RPA3003_DESKTOP_CONFIDENCE` | decimal 0,5–1,0 | `0.9` | Confiança padrão da busca de imagens (`ctx.settings.desktop_confidence`) |

`BrowserOptions.from_settings` aplica headless, timeouts e caminhos de driver.
`RPA3003_DEFAULT_BROWSER`, `RPA3003_SCREENSHOT_ON_ERROR`, `RPA3003_DESKTOP_FAILSAFE` e
`RPA3003_DESKTOP_CONFIDENCE` ficam em `ctx.settings`, e o step os repassa ao criar o bot:
`Browser(ctx.settings.default_browser, options, screenshot_on_error=ctx.settings.screenshot_on_error)`
e `DesktopBot(confidence=ctx.settings.desktop_confidence, failsafe=ctx.settings.desktop_failsafe)`.

**Vault (`VaultSettings`):**

| Variável (alternativa) | Padrão | Descrição |
|---|---|---|
| `RPA3003_VAULT_URL` (`VAULT_ADDR`) | nenhum | URL do Vault; obrigatória quando o provedor Vault é usado |
| `RPA3003_VAULT_AUTH_METHOD` | `approle` | `token`, `approle`, `ldap` ou `userpass` |
| `RPA3003_VAULT_TOKEN` (`VAULT_TOKEN`) | nenhum | Obrigatório para `token` |
| `RPA3003_VAULT_ROLE_ID` | nenhum | Obrigatório para `approle` |
| `RPA3003_VAULT_SECRET_ID` | nenhum | Obrigatório para `approle` |
| `RPA3003_VAULT_USERNAME` | nenhum | Obrigatório para `ldap` e `userpass` |
| `RPA3003_VAULT_PASSWORD` | nenhum | Obrigatório para `ldap` e `userpass` |
| `RPA3003_VAULT_NAMESPACE` (`VAULT_NAMESPACE`) | nenhum | Namespace (Vault Enterprise) |
| `RPA3003_VAULT_MOUNT_POINT` | `secret` | Ponto de montagem do KV |
| `RPA3003_VAULT_KV_VERSION` | `2` | Versão do KV (`1` ou `2`) |
| `RPA3003_VAULT_VERIFY` (`VAULT_CACERT`) | `true` | `true`, `false` ou caminho do bundle de CA |
| `RPA3003_VAULT_TIMEOUT` | `10` | Timeout por chamada (s) |
| `RPA3003_VAULT_MAX_RETRIES` | `3` | Tentativas totais |
| `RPA3003_VAULT_BACKOFF_BASE` | `1.0` | Base do backoff exponencial (s) |
| `RPA3003_VAULT_CACHE_TTL` | `300` | TTL do cache em memória (s); `0` desativa |
| `RPA3003_VAULT_OFFLINE_CACHE` | `false` | Habilita o cache offline DPAPI |
| `RPA3003_VAULT_OFFLINE_CACHE_TTL_HOURS` | `24` | Idade máxima do cache offline (h) |
| `RPA3003_VAULT_LOCAL_SECRETS_FILE` | nenhum | Provedor local, somente em `dev` |

## HashiCorp Vault

As credenciais ficam no Vault (KV versão 2, montado em `secret`). O framework só lê segredos;
criar e rotacionar é responsabilidade da equipe de segurança.

**Convenção de caminhos:** `rpa/<projeto>/<ambiente>/<sistema>`, com as chaves `username` e
`password`. Exemplo: `rpa/emissao_notas/prd/sistema_legado`. O caminho fica em
`ENV_CONFIG[...].vault_secret_path` do `global_vars.py`, e o step lê as credenciais com
`gv.get_credentials(self.ctx.env)`. A senha volta como `SecretStr`
(`credenciais.password.get_secret_value()`), e todos os valores lidos são mascarados no log.

**AppRole (padrão dos robôs):**

1. A equipe de segurança cria o segredo, a política de leitura e o AppRole da máquina. Exemplo de
   política:

   ```hcl
   path "secret/data/rpa/emissao_notas/prd/*" {
     capabilities = ["read"]
   }
   ```

2. Na máquina do robô, um administrador define as variáveis no nível da máquina:

   ```powershell
   [Environment]::SetEnvironmentVariable("RPA3003_VAULT_URL", "https://vault.empresa.local:8200", "Machine")
   [Environment]::SetEnvironmentVariable("RPA3003_VAULT_ROLE_ID", "<role_id>", "Machine")
   [Environment]::SetEnvironmentVariable("RPA3003_VAULT_SECRET_ID", "<secret_id>", "Machine")
   ```

3. Abra um novo terminal e execute em `prd`. Sem essas variáveis, a execução termina com código
   `3` e a mensagem lista as que faltam.

**Resiliência:** cada chamada tem timeout de 10 s. Falhas de rede e respostas 5xx têm 3
tentativas, com esperas de 1 s e 2 s. Token recusado (401/403) gera uma nova autenticação. Os
segredos lidos ficam 300 s em cache na memória.

**Provedor local (somente `dev`):** para desenvolver sem Vault, aponte
`RPA3003_VAULT_LOCAL_SECRETS_FILE` (no `.env` do projeto) para um JSON fora do repositório:

```json
{"rpa/conciliacao_bancaria/dev/sistema": {"username": "usuario.teste", "password": "senha-de-teste"}}
```

Defina `vault_secret_path="rpa/conciliacao_bancaria/dev/sistema"` em
`ENV_CONFIG[Environment.DEV]`. Em `prd`, essa variável é recusada (código `3`). Arquivos
`*.secrets.json` e `.env` já estão no `.gitignore`.

**Cache offline (opcional):** com `RPA3003_VAULT_OFFLINE_CACHE=true`, cada leitura bem-sucedida é
gravada cifrada com DPAPI em `%LOCALAPPDATA%\rpa_3003\vault_cache`, e só o mesmo usuário Windows
decifra. Se o Vault estiver indisponível, o framework usa o cache com até
`RPA3003_VAULT_OFFLINE_CACHE_TTL_HOURS` horas (padrão 24) e registra um `WARNING`. Deixe
desligado, salvo decisão da equipe de segurança.

## Navegadores

A classe `Browser` atende Chrome (`"chrome"`), Edge (`"edge"`) e Edge em modo IE (`"edge_ie"`):

```python
from pathlib import Path

from rpa_3003.bots.browser_selenium import Browser, BrowserOptions, By

opcoes = BrowserOptions(headless=False, download_dir=Path("data/output"), default_timeout=20)
with Browser("edge", opcoes, screenshots_dir=Path("logs/screenshots")) as browser:
    browser.open("https://sistema.interno/login")
    browser.type_text((By.ID, "usuario"), credenciais.username)
    browser.type_text((By.ID, "senha"), credenciais.password.get_secret_value(), sensitive=True)
    browser.click((By.ID, "entrar"))
    with browser.expect_download(timeout=90, pattern="*.pdf") as download:
        browser.click((By.LINK_TEXT, "Exportar PDF"))
    print(download.path)
```

Todas as interações usam espera explícita. Elemento não encontrado gera `ElementNotFoundError`.
Ao sair do `with` por exceção, o `Browser` salva um screenshot em `logs/screenshots/` e encerra o
navegador e o driver.

**Drivers:** o Selenium Manager (embutido no Selenium 4) encontra ou baixa o driver compatível com
o navegador instalado a cada inicialização. Em rede sem internet ou com proxy restritivo, há duas
saídas:

- Configure o proxy do Selenium Manager com a variável `SE_PROXY`.
- Distribua os drivers internamente e informe o caminho em `RPA3003_CHROMEDRIVER_PATH`,
  `RPA3003_EDGEDRIVER_PATH` ou `RPA3003_IEDRIVER_PATH`. O caminho manual tem precedência.

**Modo IE do Edge** (sistemas legados que exigem o Internet Explorer 11). Configuração única da
máquina, normalmente por GPO e com administrador:

1. Habilite a integração do Edge com o modo IE: política "Configure Internet Explorer
   integration" = "Internet Explorer mode".
2. Inclua os sites do sistema legado na lista de sites do modo IE (Enterprise Mode Site List).
3. Deixe o zoom em 100%. O framework usa `ie_ignore_zoom_level=True` por padrão, mas o zoom
   diferente ainda afeta cliques por coordenada.
4. Configure o Modo Protegido igual (todo ligado ou todo desligado) nas quatro zonas de
   segurança das Opções da Internet. A opção `ie_ignore_protected_mode_settings=True` contorna a
   divergência, mas é menos estável.
5. No Windows 64 bits, crie o valor DWORD `iexplore.exe = 0` em
   `HKLM\SOFTWARE\Wow6432Node\Microsoft\Internet Explorer\Main\FeatureControl\FEATURE_BFCACHE`.
6. Use o IEDriverServer **32 bits**, porque o de 64 bits digita muito devagar. O Selenium Manager
   baixa a versão 32 bits; para fixar uma versão, informe `RPA3003_IEDRIVER_PATH`.
7. Se o Edge não estiver no caminho padrão, informe `RPA3003_EDGE_BINARY_PATH`.

No modo IE, `headless` e `download_dir` não têm efeito (o framework registra um `WARNING`). Para
conferir a máquina, execute o teste do modo IE:

```powershell
$env:RPA3003_TEST_IE_MODE = "1"
pytest -m browser -k edge_ie
```

## Automação desktop

A classe `DesktopBot` automatiza aplicações Windows por imagens de referência, mouse, teclado,
janelas e processos:

```python
from rpa_3003.bots.desktop_bot import DesktopBot

bot = DesktopBot(confidence=0.92, timeout=15)
bot.start_application(r"C:\ERP\erp.exe", wait_title_re=".*ERP Financeiro.*", timeout=60)
bot.wait_image("tela_login")  # procura data/images/tela_login.png
bot.type_text(credenciais.username)
bot.press("tab")
bot.type_text(credenciais.password.get_secret_value(), sensitive=True)
bot.click_image("botao_salvar")
bot.kill_process("erp.exe")
```

**Imagens de referência:**

- Salve as imagens em PNG em `data/images/` do projeto. Nos métodos, o nome pode vir sem
  extensão (`"botao_salvar"`).
- Capture as imagens **na máquina de produção**, com a mesma resolução e escala de tela
  (100%, 125%...). Imagem capturada em outra escala não é encontrada.
- Recorte uma área com textura (texto, borda, ícone). Uma área de cor única é "encontrada" em
  qualquer lugar.
- Ajuste `confidence` (padrão 0,9) e use `region=(left, top, largura, altura)` para limitar a
  busca e ganhar velocidade.
- Imagem não encontrada no prazo gera `ImageNotFoundError` e salva a tela inteira em
  `logs/screenshots/`.

**DPI:** o `DesktopBot` ativa o DPI awareness por monitor, então as coordenadas são pixels
físicos, iguais às da captura de tela.

**Teclado:** `type_text` digita acentos e caracteres especiais (`ç`, `ã`, `€`). Se uma aplicação
perder caracteres, use `method="clipboard"`: o texto é colado pela área de transferência, e o
conteúdo anterior é restaurado. Com `sensitive=True`, o texto é mascarado no log.

**Sessão interativa:** a automação desktop precisa de uma sessão Windows ativa e desbloqueada.

- Use um usuário de serviço dedicado com logon automático (autologon).
- Não minimize a janela do cliente RDP. Se precisar, defina o DWORD
  `RemoteDesktop_SuppressWhenMinimized = 2` em `HKCU\Software\Microsoft\Terminal Server Client`
  na máquina de onde parte a conexão. Para desconectar sem bloquear a tela, transfira a sessão
  para o console com `tscon`.
- Com o FAILSAFE ligado (padrão), mover o mouse para um canto da tela interrompe o robô.

## CLI

```text
rpa3003 [--version] [-v] <comando> [opções]
```

| Comando | Finalidade |
|---|---|
| `rpa3003 create <nome> [--template NOME] [--projects-dir CAMINHO] [--author NOME] [--force]` | Cria o projeto a partir de um template (padrão `example`). `--force` renomeia o projeto existente para `<nome>.bak-<AAAAMMDDHHMMSS>` |
| `rpa3003 run <nome> [--env dev\|prd] [--user USUARIO] [--log-level NIVEL] [--timeout SEGUNDOS] [--projects-dir CAMINHO] [-- ARGS]` | Valida e executa o projeto em processo próprio. Argumentos após `--` chegam ao projeto em `ctx.extra_args` |
| `rpa3003 list [--projects-dir CAMINHO]` | Lista os projetos e a situação da estrutura (`OK` ou `INVÁLIDA`) |
| `rpa3003 templates` | Lista os templates disponíveis |
| `rpa3003 validate <nome> [--projects-dir CAMINHO]` | Valida a estrutura e lista os problemas encontrados |

Exemplos:

```powershell
rpa3003 run emissao_notas --env prd --user marcos.op --log-level DEBUG
rpa3003 run emissao_notas --env prd --timeout 3600 -- --data-ref 2026-10-31 --filial 12
python -m rpa_3003 run emissao_notas --env prd
```

O CLI usa `./projects` do diretório atual, ou `--projects-dir`/`RPA3003_PROJECTS_DIR`. Sem
`--timeout`, vale `RPA3003_RUN_TIMEOUT`. No tempo limite, o CLI encerra a árvore de processos
(Python, driver e navegador). O mesmo projeto pode ser executado sem o CLI, com
`python main.py --env prd` na pasta do projeto.

**Códigos de saída** (do CLI e de `python main.py`):

| Código | Significado | Origem típica |
|---|---|---|
| `0` | Execução concluída sem erros | — |
| `1` | Falha técnica ou exceção não tratada | `ElementNotFoundError`, `DatabaseError`, `RuntimeError` |
| `2` | Argumentos inválidos | Opção inválida, ambiente inválido, nome de projeto inválido |
| `3` | Configuração, Vault, projeto ou template inválido | `ConfigurationError`, `VaultError`, step que não carrega |
| `4` | Regra de negócio violada | `BusinessRuleError` |
| `5` | Tempo limite de `--timeout` excedido | CLI |
| `6` | Projeto já em execução na máquina | Lock `.rpa3003.lock` ocupado |
| `130` | Interrompido por Ctrl+C | `KeyboardInterrupt` |

## Logs

Cada linha segue o formato obrigatório, no arquivo e no console:

```text
[YYYY-MM-DD HH:MM:SS] [LEVEL] [nome_script] [linha] [user] mensagem
[2026-10-31 07:00:01] [INFO] [workflow.py] [455] [svc_rpa_fin] Iniciando execução | projeto=emissao_notas | ambiente=prd | run_id=3f9a1c2b7d4e | usuario=svc_rpa_fin | python=3.12.7 | rpa_3003=1.0.0
```

- **Local:** `projects/<nome>/logs/<nome>_<AAAA-MM-DD>.log`, em UTF-8. O CLI, fora da execução
  de um projeto, registra só no console.
- **Rotação:** à meia-noite, o framework abre o arquivo do novo dia, sem renomear o anterior.
- **Retenção:** arquivos com mais de `RPA3003_LOG_RETENTION_DAYS` dias (padrão 30) são removidos.
  Com `RPA3003_LOG_COMPRESSION=true`, o arquivo do dia anterior vira `.zip`.
- **Campo `user`:** `--user` do `run`, depois `RPA3003_LOG_USER`, depois o usuário do Windows.
- **Screenshots:** ficam em `logs/screenshots/` e não têm rotação automática. Limpe-as no wrapup,
  por exemplo com
  `filesystem_utils.clean_dir(gv.LOGS_DIR / "screenshots", "*.png", older_than_days=30)`.
- **Mascaramento:** os valores lidos do Vault, a senha de banco ao conectar, os textos digitados
  com `sensitive=True` e as senhas de SMTP/IMAP aparecem como `********`. Para mascarar outro
  valor, chame `register_secret(valor)` (mínimo de 4 caracteres) antes de registrá-lo. Variáveis
  locais nunca aparecem nos tracebacks.
- **Mensagens:** siga o padrão `Ação | chave=valor | chave=valor`, com chaves sem acento.

```python
from rpa_3003.core.logger import get_logger

log = get_logger()
log.info("Boleto processado | numero={} | valor={}", "123", "1.500,00")
```

**Diagnóstico de uma falha:** cada execução tem um `run_id` de 12 caracteres. Ele aparece na
linha `Iniciando execução`, na linha `Execução finalizada` e no nome do
`data/output/status_<run_id>.json`.

```powershell
Select-String -Path projects\emissao_notas\logs\*.log -Pattern "status=FAILED"
Select-String -Path projects\emissao_notas\logs\*.log -Pattern "\[ERROR\]" -Context 0,20
```

A linha `[ERROR]` mostra o arquivo e a linha do erro, seguida do traceback. A evidência está em
`logs/screenshots/<AAAAMMDD_HHMMSS_fff>_erro*.png`. Abra os logs em leitores que não bloqueiam o
arquivo, porque o robô pode estar gravando nele.

## Agendamento

Use o Agendador de Tarefas do Windows chamando `rpa3003 run <nome> --env prd --timeout <s>`. O
diretório inicial deve ser o workspace, porque o CLI procura `.\projects`. Exemplo para todo dia
às 07:00, no PowerShell como administrador:

```powershell
$acao = New-ScheduledTaskAction -Execute "C:\rpa-framework\.venv\Scripts\rpa3003.exe" `
    -Argument "run emissao_notas --env prd --timeout 3600" -WorkingDirectory "C:\rpa-framework"
$gatilho = New-ScheduledTaskTrigger -Daily -At 07:00
$config = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$usuario = New-ScheduledTaskPrincipal -UserId "EMPRESA\svc_rpa_fin" -LogonType Interactive
Register-ScheduledTask -TaskName "RPA emissao_notas" -Action $acao -Trigger $gatilho -Settings $config -Principal $usuario
```

- Defina `--timeout` menor que o limite da tarefa. No tempo limite, o CLI encerra o robô, o
  driver e o navegador e retorna `5`.
- O código de saída aparece na coluna "Último Resultado" do agendador (ex.: `0x3` = código `3`).
- `-LogonType Interactive` executa na sessão aberta do usuário, o que a automação desktop e o
  navegador visível exigem. Robôs só com navegador headless podem usar "Executar estando o
  usuário conectado ou não".
- As variáveis do Vault devem estar definidas no nível da máquina (seção
  [HashiCorp Vault](#hashicorp-vault)).
- Duas execuções simultâneas do mesmo projeto não acontecem: a segunda termina com código `6`.

## Testes

Comandos, no workspace com o ambiente ativo:

```powershell
pytest                                              # unit + e2e (padrão do pyproject)
pytest -m integration                               # requer variáveis RPA3003_TEST_*
pytest -m "browser or gui"                          # requer sessão Windows interativa e navegadores
pytest --cov=rpa_3003 --cov-report=term-missing --cov-report=html
coverage report --include="rpa_3003/core/*" --fail-under=90
ruff check .
ruff format --check .
mypy
detect-secrets scan --exclude-files "^tests/" > .secrets.baseline
```

| Marcador | Significado | Roda no `pytest` padrão |
|---|---|---|
| `unit` | Sem serviço externo | Sim |
| `e2e` | `rpa3003 create` → `validate` → `run` em processo real | Sim |
| `integration` | PostgreSQL, Denodo, Vault, SMTP/IMAP reais; pulado sem as variáveis | Não |
| `windows` | APIs do Windows (DPAPI, COM) | Sim, no Windows |
| `gui` | Sessão de desktop interativa (Bloco de Notas) | Não |
| `browser` | Chrome e Edge instalados | Não |

Variáveis dos testes de integração (ausentes → teste pulado):

| Serviço | Variáveis |
|---|---|
| PostgreSQL | `RPA3003_TEST_PG_HOST`, `RPA3003_TEST_PG_PORT`, `RPA3003_TEST_PG_DB`, `RPA3003_TEST_PG_USER`, `RPA3003_TEST_PG_PASSWORD` |
| Denodo | `RPA3003_TEST_DENODO_HOST`, `RPA3003_TEST_DENODO_PORT`, `RPA3003_TEST_DENODO_DB`, `RPA3003_TEST_DENODO_USER`, `RPA3003_TEST_DENODO_PASSWORD` |
| Vault | `RPA3003_TEST_VAULT_URL`, `RPA3003_TEST_VAULT_TOKEN`, `RPA3003_TEST_VAULT_PATH`; opcionais: `RPA3003_TEST_VAULT_ROLE_ID`, `RPA3003_TEST_VAULT_SECRET_ID`, `RPA3003_TEST_VAULT_MOUNT_POINT` |
| E-mail | `RPA3003_TEST_SMTP_HOST`, `RPA3003_TEST_IMAP_HOST`, `RPA3003_TEST_EMAIL_USER`, `RPA3003_TEST_EMAIL_PASSWORD`, `RPA3003_TEST_EMAIL_TO` |
| Modo IE | `RPA3003_TEST_IE_MODE=1` (máquina com o modo IE configurado) |

Use contas de teste com privilégio mínimo, e o usuário de banco só com consulta.

### Medições de desempenho

Medido em 02/11/2026: Windows 11 Pro, Intel Core i5-10400 (6 núcleos, 12 threads), 16 GB de
RAM, SSD, Python 3.12.16. Cada comando rodou 10 vezes, em processos novos, no `.venv` do
workspace, que tem um lançador `python.exe` (incluído nos tempos).

| Medição | Requisito | Limite | Mediana | Máximo |
|---|---|---|---|---|
| `python -c "import rpa_3003"` | RNF-004 | 1,0 s | 0,29 s | 0,31 s |
| `rpa3003 --help` | RNF-004 | 1,5 s | 0,34 s | 0,35 s |
| `rpa3003 create` | RNF-004, OBJ-02 | 5 s | 0,37 s | 0,39 s |
| Início de `main.py` até `>>> Iniciando step`, sem Vault | RNF-004, OBJ-10 | 2 s | 0,33 s | 0,35 s |

A máquina de referência do PRD tem 4 vCPU, 8 GB de RAM e SSD. Os mesmos limites são verificados
a cada `pytest` por `tests/e2e/test_create_and_run.py`.

## Solução de problemas

| Erro ou sintoma | Causa provável | Ação |
|---|---|---|
| `.venv\Scripts\activate` falha com "a execução de scripts foi desabilitada" | Política de execução do PowerShell | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, ou use `.venv\Scripts\rpa3003.exe` sem ativar |
| `pip install` falha com "requires a different Python" | Python anterior ao 3.12 | Crie o ambiente com `py -3.12 -m venv .venv` |
| `rpa3003` não é reconhecido | Ambiente virtual não ativo | Ative o `.venv` ou use `python -m rpa_3003` |
| Código `2` e "Ambiente inválido" ou "Nome de projeto inválido" | `--env` diferente de `dev`/`prd`, ou nome fora de `^[a-z][a-z0-9_]{2,49}$` | Corrija o argumento; nomes de dispositivo (`con`, `nul`, `com1`...) também são recusados |
| Código `3` com `ausente: ...` ou `classe 'Step' não encontrada` | Estrutura do projeto incompleta | Execute `rpa3003 validate <nome>` e corrija cada item listado |
| Código `3` com "Parâmetros obrigatórios do Vault ausentes" | Variáveis do Vault não definidas | Defina `RPA3003_VAULT_URL` e as do método de autenticação no nível da máquina; abra novo terminal |
| Código `3` com `VaultUnavailableError` e `Vault indisponível (tentativa 3/3)` | Vault fora do ar, rede ou proxy | Verifique a URL e a rede; o wrapup roda mesmo assim. Avalie o cache offline com a segurança |
| Código `3` com `VaultAuthenticationError` | `role_id`/`secret_id` ou token inválido ou expirado, ou política sem leitura no caminho | Peça novo `secret_id` ou revisão da política à equipe de segurança |
| Código `3` com `SecretNotFoundError` | Caminho ou chave inexistente no Vault | Confira `vault_secret_path` e as chaves `username`/`password` |
| Código `6` e "Projeto já está em execução" | Outra execução do mesmo projeto ativa (agendamentos sobrepostos) | Aguarde, ajuste o horário ou o `--timeout` da tarefa |
| Código `5` e "Tempo limite excedido" | Execução passou de `--timeout` | Investigue o log; aumente o limite se a duração for legítima |
| `DriverInitializationError` no modo IE, ou "Could not find an Internet Explorer window" | Modo IE não habilitado, zoom, Modo Protegido divergente ou IEDriverServer 64 bits | Siga o checklist de [Navegadores](#navegadores) e rode o teste do modo IE |
| `DriverInitializationError` com "This version of ChromeDriver only supports..." | Navegador atualizado e driver manual antigo | Remova o caminho manual (`RPA3003_*DRIVER_PATH`) ou atualize o driver distribuído |
| Falha ao baixar o driver pelo Selenium Manager | Proxy ou firewall | Defina `SE_PROXY` ou distribua os drivers e use os caminhos manuais |
| Driver ou automação de teclado bloqueados, processo encerrado sem erro do robô | Antivírus/EDR | Peça liberação do cache do Selenium Manager e do `.venv` à equipe de segurança |
| `ElementNotFoundError` intermitente | Página lenta ou elemento dinâmico | Aumente `timeout` na chamada ou `RPA3003_SELENIUM_TIMEOUT`; use `retries` no step |
| `ImageNotFoundError` com a imagem visível na tela | Escala/resolução diferente da captura ou imagem sem textura | Recapture em `data/images/` na máquina de produção; ajuste `confidence` e `region` |
| `ImageNotFoundError` com screenshot preto | Sessão bloqueada ou RDP minimizado | Use autologon, `RemoteDesktop_SuppressWhenMinimized = 2` ou `tscon` |
| Acentos digitados errado em aplicação desktop | A aplicação perde teclas Unicode | Use `type_text(..., method="clipboard")` |
| Processos `chromedriver.exe`/`msedgedriver.exe` ou aplicações remanescentes | Recurso aberto sem limpeza | Use `with Browser(...)` ou `ctx.register_cleanup(browser.quit)`; encerre aplicações com `DesktopBot.kill_process` no wrapup; defina `--timeout` |
| Senha aparece no log | Valor não veio do Vault nem de `sensitive=True` | Chame `register_secret(valor)` antes de registrar; nunca coloque segredos em mensagens de exceção |
| Log não atualiza ou `PermissionError` no arquivo de log | Arquivo aberto por editor que bloqueia, ou antivírus | Abra em leitor sem bloqueio; peça exclusão da pasta `logs/` da varredura em tempo real |
| Acentos trocados por `?` ou caracteres estranhos | Console ou arquivo legado em cp850/cp1252 | O framework já usa UTF-8 no console; para TXT/CSV legados, a leitura tenta cp1252 automaticamente ou informe `encoding` |
| `FileNotFoundError` em caminhos longos | Limite de 260 caracteres (MAX_PATH) | Use workspace em caminho curto (ex.: `C:\rpa`) ou habilite `LongPathsEnabled` por GPO |
| `DatabaseError` "Instrução SQL não permitida: acesso somente leitura" | `INSERT`, `UPDATE`, `DELETE`, DDL ou várias instruções no SQL | O framework só lê bancos. Grave o controle em `data/` (CSV/JSON) ou use a interface do sistema de destino |
| Usuário de banco do robô com privilégio de escrita | Concessão excessiva | Peça à equipe de banco um usuário dedicado só com consulta (`SELECT`) |
| Erro de conexão ou de consulta no Denodo | Porta (padrão 9996) ou recurso não suportado pela interface PostgreSQL do Denodo | Confira host e porta; alternativa ODBC por `register_engine` ([Guia do desenvolvedor](docs/GUIA_DO_DESENVOLVEDOR.md)) |
| `EmailError` de autenticação no SMTP/IMAP | Provedor desativou autenticação básica | Use o relay SMTP interno ou um canal próprio (`EmailSender`) |
| `EmailError` no Outlook | Sessão não interativa ou Outlook sem perfil | Use SMTP/IMAP em robôs não assistidos |
| Testes ou robô quebram após atualizar dependências | Mudança de API em biblioteca | Mantenha as faixas do `requirements.txt`; rode `pytest -m "browser or gui"` e `-m integration` antes de atualizar |
| Comportamento diferente do descrito no PRD | Desvio registrado | Consulte [docs/DESVIOS.md](docs/DESVIOS.md) |

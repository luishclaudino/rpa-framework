# Guia do desenvolvedor do rpa_3003

Este guia é para quem vai entender, manter ou estender o framework `rpa_3003` 1.0.0. Para usar
o framework em um robô, leia o [README](../README.md).

## Como esta documentação está organizada

Esta seção mostra onde fica cada tipo de informação. Leia este guia inteiro antes da primeira
mudança no framework.

| Documento | Papel | Quando consultar |
|---|---|---|
| [README.md](../README.md) | Usar o framework: instalação, projetos, steps, configuração, Vault, navegadores, CLI, logs, agendamento | Para saber como o framework se comporta para quem o usa |
| Este guia | Entender e modificar o framework: arquitetura, convenções, extensões, testes e contribuição | Antes de alterar o pacote `rpa_3003` |
| [PRD.md](../PRD.md) | Especificação normativa: requisitos (seções 5 e 6), contratos dos módulos (seção 8), CLI (9), logging (10), template (11) e testes (12) | Antes de mexer em um módulo, leia a subseção correspondente da seção 8 |
| [docs/DESVIOS.md](DESVIOS.md) | Desvios em relação ao PRD e decisões de interpretação, por sprint | Quando o código parecer divergir do PRD |
| [CLAUDE.md](../CLAUDE.md) | Resumo operacional para agentes de IA que trabalham no repositório | Ao usar um agente de programação |

Em caso de conflito, o PRD prevalece, exceto nos pontos registrados em `docs/DESVIOS.md`.

## Ambiente de desenvolvimento

Aqui você prepara o ambiente e confirma que o workspace está íntegro. O resultado é um `.venv`
com o framework em modo editável e os itens 1 a 3 do gate de qualidade
([PRD, seção 12.10](../PRD.md#1210-gate-de-qualidade)) aprovados.

1. Confira o Python 3.12 ou superior, 64 bits:

   ```powershell
   py -3.12 --version
   ```

   Resultado esperado: `Python 3.12.x`.

2. Na raiz do workspace, crie o ambiente virtual e instale o framework com as ferramentas de
   desenvolvimento:

   ```powershell
   py -3.12 -m venv .venv
   .venv\Scripts\python.exe -m pip install -e .[dev]
   ```

   Resultado esperado: `Successfully installed ... rpa-3003-1.0.0`. Nunca instale pacotes no
   Python global. Na máquina em que o framework foi desenvolvido, o `.venv` usa um CPython local
   em `.python/` ([D-01](DESVIOS.md#d-01--interpretador-python-312-local-ao-workspace-python)).

3. Item 1 do gate, testes padrão (unit + e2e):

   ```powershell
   .venv\Scripts\python.exe -m pytest
   ```

   Resultado esperado: nenhuma falha. Na versão 1.0.0, a linha final é
   `1389 passed, 39 deselected`, em cerca de 1 a 2 minutos. Os testes `integration`, `gui` e
   `browser` ficam de fora (`deselected`).

4. Item 2 do gate, limites de cobertura
   ([PRD, seção 12.8](../PRD.md#128-cobertura-mínima-exigida)):

   ```powershell
   .venv\Scripts\python.exe -m pytest --cov=rpa_3003 --cov-report=term-missing
   .venv\Scripts\coverage.exe report --include="rpa_3003/core/*" --fail-under=90
   .venv\Scripts\coverage.exe report --include="rpa_3003/utils/*" --fail-under=85
   .venv\Scripts\coverage.exe report --include="rpa_3003/cli.py" --fail-under=85
   .venv\Scripts\coverage.exe report --include="rpa_3003/templates/*" --fail-under=90
   .venv\Scripts\coverage.exe report --include="rpa_3003/bots/*" --fail-under=70
   ```

   Resultado esperado: a primeira linha termina com `Required test coverage of 85% reached`, e
   cada `coverage report` termina com `$LASTEXITCODE` igual a `0`.

5. Item 3 do gate, lint, formatação e tipos:

   ```powershell
   .venv\Scripts\ruff.exe check .
   .venv\Scripts\ruff.exe format --check .
   .venv\Scripts\mypy.exe
   ```

   Resultado esperado: `All checks passed!`, `... files already formatted` e
   `Success: no issues found in ... source files`. O `mypy` verifica só `rpa_3003/`, no modo
   estrito.

Os comandos acima funcionam sem ativar o ambiente. Se preferir, ative com
`.venv\Scripts\activate` e use `pytest`, `ruff` e `mypy` diretamente.

## Mapa do repositório

Esta seção diz o que cada arquivo faz. Os itens obrigatórios do enunciado e os adicionais estão
na [seção 7.1 do PRD](../PRD.md#71-estrutura-de-pastas-completa).

```text
rpa-framework/
├── rpa_3003/                          # Pacote do framework
│   ├── __init__.py                    # Exporta __version__, Environment, get_logger e setup_logger
│   ├── __main__.py                    # Permite `python -m rpa_3003` (chama cli.main)
│   ├── _version.py                    # Fonte única da versão (lida pelo setup.py sem importar o pacote)
│   ├── py.typed                       # Marcador PEP 561: projetos consumidores fazem checagem de tipos
│   ├── cli.py                         # Comando rpa3003: create, run, list, templates e validate
│   ├── core/
│   │   ├── __init__.py                # Reexporta a API estável de exceptions, logger e config
│   │   ├── exceptions.py              # Hierarquia RPAError, ExitCode e exit_code_for
│   │   ├── logger.py                  # LOG_FORMAT, setup_logger, get_logger e mascaramento de segredos
│   │   ├── config.py                  # FrameworkSettings, VaultSettings, Environment, ProjectPaths e validação de projetos
│   │   ├── vault.py                   # VaultClient, provedores de segredo, cache em memória e offline
│   │   └── workflow.py                # BaseStep, ExecutionContext, bootstrap, WorkflowRunner e ProjectLock
│   ├── utils/
│   │   ├── __init__.py                # Lista os módulos em __all__, sem importá-los
│   │   ├── filesystem_utils.py        # Arquivos, pastas, TXT com fallback cp1252 e gravação atômica
│   │   ├── json_utils.py              # JSON com encoder estendido, caminhos "a.b.0" e validação pydantic
│   │   ├── csv_utils.py               # CSV com ";" e utf-8-sig por padrão
│   │   ├── archive_utils.py           # ZIP e TAR com proteção contra path traversal
│   │   ├── calendar_utils.py          # Datas, fuso America/Sao_Paulo, feriados e dias úteis
│   │   ├── crypt_utils.py             # Fernet, PBKDF2, hash, HMAC e DPAPI
│   │   ├── database_utils.py          # PostgreSQL, SQLite, DuckDB e Denodo, somente leitura
│   │   ├── email_utils.py             # SMTP, IMAP e Outlook (COM)
│   │   ├── images_utils.py            # Redimensionar, converter, comparar e gerar PDF de imagens
│   │   └── pdf_utils.py               # Ler, criar, dividir, juntar, cifrar e preencher PDFs
│   ├── bots/
│   │   ├── __init__.py                # Importação preguiçosa de Browser, BrowserFactory, DesktopBot...
│   │   ├── browser_selenium.py        # Browser, BrowserOptions, BrowserFactory e builders de Chrome/Edge/IE
│   │   └── desktop_bot.py             # DesktopBot: imagens (OpenCV + mss), mouse, teclado, janelas e processos
│   └── templates/
│       ├── __init__.py                # Descoberta, renderização e criação atômica de projetos
│       └── example/                   # Template padrão (.tpl + template.toml), conteúdo da seção 11 do PRD
├── projects/                          # Projetos RPA (cada um em projects/<nome>/)
├── tests/
│   ├── conftest.py                    # Fixtures compartilhadas e pulo de testes fora do Windows
│   ├── fixtures/html/                 # Páginas servidas aos testes de navegador
│   ├── unit/                          # Testes unitários, por camada (core/, utils/, bots/, templates/)
│   ├── integration/                   # Serviços reais: PostgreSQL, Denodo, Vault, e-mail, navegador e desktop
│   └── e2e/                           # rpa3003 create → validate → run em processo real
├── docs/
│   ├── DESVIOS.md                     # Desvios e decisões de interpretação
│   └── GUIA_DO_DESENVOLVEDOR.md       # Este guia
├── .gitignore                         # Ignora .venv, .env, logs, saídas, locks e caches
├── MANIFEST.in                        # Inclui os templates no pacote fonte (sdist)
├── pyproject.toml                     # build-system e configuração de pytest, coverage, ruff e mypy
├── requirements.txt                   # Dependências de execução (lidas pelo setup.py)
├── requirements-dev.txt               # Dependências de teste e qualidade
├── setup.py                           # Metadados, console script rpa3003 e package_data dos templates
├── PRD.md                             # Especificação normativa
├── CLAUDE.md                          # Instruções para agentes de IA
└── README.md                          # Documentação de uso
```

## Arquitetura

Aqui você entende as quatro camadas e quem pode importar quem. Toda mudança de import passa por
estas regras, verificadas automaticamente.

O pacote tem quatro camadas. Cada uma só importa as de baixo:

```text
cli.py, templates/   Entrada: CLI e criação de projetos
       │
bots/                Automação web e desktop
       │
utils/               Utilitários de arquivos, dados e serviços
       │
core/                Base: exceptions ◄ logger ◄ config ◄ vault, workflow
```

O CLI não chama `bots` nem `utils`. Ele executa o `main.py` do projeto em outro processo, e os
steps do projeto usam qualquer módulo público. Dentro do `core`, `exceptions` e `logger` não
importam nada do framework, `config` importa os dois, e `vault` e `workflow` importam `config`.

| Módulo | Pode importar | Não pode importar |
|---|---|---|
| `core/exceptions.py` | biblioteca padrão | nenhum outro módulo do framework |
| `core/logger.py` | biblioteca padrão, `loguru` | nenhum outro módulo do framework |
| `core/config.py` | `exceptions`, `logger`, `pydantic`, `pydantic_settings` | `vault`, `workflow`, `utils`, `bots`, `cli`, `templates` |
| `core/vault.py` | `exceptions`, `logger`, `config`, `hvac`, `requests`, `pydantic`, `pywin32` | `workflow`, `utils`, `bots`, `cli`, `templates` |
| `core/workflow.py` | `exceptions`, `logger`, `config`, `_version`, `filelock`, `loguru` | `vault`, `utils`, `bots`, `cli`, `templates` |
| `utils/*` | `core` (exceto `workflow`), outros `utils`, bibliotecas externas | `bots`, `cli`, `templates` |
| `bots/*` | `core` (exceto `workflow`), `utils`, bibliotecas externas | `cli`, `templates` |
| `templates/__init__.py` | `core` (exceto `workflow` e `vault`), `_version` | `utils`, `bots`, `cli` |
| `cli.py` | `core`, `templates`, `_version`, `psutil` | `bots`, `utils` |

A tabela vem da [seção 7.2 do PRD](../PRD.md#72-camadas-e-regras-de-dependência), com as
exceções de [D-05](DESVIOS.md#d-05--dependências-de-vaultpy-e-workflowpy-em-test_architecturepy).

**Como as regras são verificadas.** O `tests/unit/test_architecture.py` lê cada módulo de
`rpa_3003/` com o módulo `ast` e analisa **todos** os imports, inclusive os feitos dentro de
funções e sob `TYPE_CHECKING`:

- `MODULE_RULES` tem uma regra por módulo do `core`, do `cli`, dos `templates` e da raiz.
  `GROUP_RULES` vale para todos os módulos de `utils/` e de `bots/`. Cada regra lista os módulos
  do framework permitidos, os proibidos e, quando restrito, as bibliotecas externas aceitas.
- `test_every_module_has_dependency_rule` falha se um módulo novo não tiver regra. Um módulo
  novo em `utils/` ou `bots/` já está coberto; em `core/`, exige uma regra nova.
- `import logging` só é aceito em `core/logger.py` (RF-014).
- Testes em processo novo confirmam que `import rpa_3003`, `rpa_3003.utils`, `rpa_3003.bots` e
  `rpa_3003.cli` não carregam selenium, cv2, numpy, duckdb, psycopg2, pywinauto e outras
  dependências pesadas (RNF-017).
- Literais com `\` como separador de caminho e `os.path.join` são recusados (RNF-016).

Só altere `MODULE_RULES` ou `GROUP_RULES` quando um contrato do PRD exigir a dependência, e
registre o motivo em `docs/DESVIOS.md`.

## Ciclo de uma execução

Esta seção mostra o caminho de `rpa3003 run` até o código de saída, com o arquivo e a função de
cada etapa. Use-a para localizar onde um comportamento acontece. A descrição normativa está na
[seção 7.5.3 do PRD](../PRD.md#753-fluxo-de-execução-completo).

| Etapa | Arquivo e função | O que acontece |
|---|---|---|
| 1 | `rpa_3003/cli.py` · `main` | Separa os argumentos após `--`, interpreta o comando, configura o log do CLI (só console) e converte `RPAError` em código de saída |
| 2 | `cli.py` · `cmd_run` | Valida `--timeout`, resolve a pasta de projetos e o ambiente, chama `find_project` e `validate_project_structure` (`core/config.py`) |
| 3 | `cli.py` · `build_run_command`, `build_child_env` | Monta `[python, main.py, --env, <env>, ...]` e o ambiente do filho com `RPA3003_ENV`, `PYTHONUTF8=1` e `PYTHONIOENCODING=utf-8` |
| 4 | `cli.py` · `run_child` | `subprocess.Popen` com `cwd` na pasta do projeto; no tempo limite, `kill_process_tree` e código `5`; no Ctrl+C, espera o filho e retorna `130` |
| 5 | `projects/<nome>/main.py` · `main` | `split_passthrough_args` e `parse_args` (`--env`, `--user`, `--log-level`); erro de inicialização vai para `stderr` com o código da exceção |
| 6 | `core/workflow.py` · `bootstrap` | `ProjectPaths.from_root`, `_activate_project` (`chdir`, `sys.path`, limpeza de `workflows*`/`global_vars` de outros projetos), `_reconfigure_std_streams` (UTF-8) |
| 7 | `workflow.py` · `bootstrap` | `load_settings` com o `.env` do projeto, `resolve_environment`, `ensure_runtime_dirs`, `setup_logger` (`core/logger.py`) e `run_id` de 12 hexadecimais |
| 8 | `workflow.py` · `WorkflowRunner.run` | Registra `Iniciando execução` e adquire o lock (`ProjectLock`, `.rpa3003.lock`); lock ocupado → código `6` |
| 9 | `workflow.py` · `WorkflowRunner._execute_steps`, `load_step` | Importa **todos** os steps de `workflows.<env>` antes de executar; falha → `StepLoadError`, código `3` |
| 10 | `workflow.py` · `WorkflowRunner._execute_step` | Ignora steps após falha, exceto os com `always_run`; guarda a primeira falha em `ctx.error` |
| 11 | `workflow.py` · `BaseStep.execute` | Template Method: log de início, `before`, `run` com `retries`, `after`, log de fim; na falha definitiva, log com traceback e `on_error` |
| 12 | `workflow.py` · `ExecutionContext.run_cleanups` | Rotinas de limpeza em ordem LIFO; exceções viram `WARNING` |
| 13 | `workflow.py` · `WorkflowRunner._finish` | Status, `exit_code_for` (`core/exceptions.py`), linha `Execução finalizada` e `logger.complete()`; o lock é liberado ao sair do `with` |
| 14 | `main.py` → `cli.py` · `run_child` | `sys.exit(main())` encerra o filho; o CLI retorna o mesmo código |

Com `python main.py --env prd`, as etapas 1 a 4 não acontecem: o agendador chama o projeto
direto, com o mesmo comportamento das etapas 5 a 14.

## Convenções de código

Estas são as regras que o código do framework segue. Cada uma tem um exemplo correto e um
incorreto. A fonte é a [seção 7.7 do PRD](../PRD.md#77-convenções-de-código) e os RNF-012,
RNF-016 e RNF-017.

1. **Cabeçalho do módulo.** Todo módulo começa com docstring e
   `from __future__ import annotations`.

   ```python
   # Correto
   """Normalização de textos para comparação."""

   from __future__ import annotations
   ```

   ```python
   # Incorreto: sem docstring e sem anotações adiadas
   import unicodedata
   ```

2. **Constantes e aliases.** Constantes públicas usam `Final`; aliases usam `TypeAlias` com
   `# noqa: UP040` (a convenção do PRD prevalece sobre a sugestão do ruff).

   ```python
   # Correto
   DEFAULT_TIMEOUT: Final[float] = 30.0
   PathLike: TypeAlias = str | os.PathLike[str]  # noqa: UP040 - convenção 7.7
   ```

   ```python
   # Incorreto
   DEFAULT_TIMEOUT = 30.0
   type PathLike = str | os.PathLike[str]
   ```

3. **Caminhos com `pathlib` (RNF-016).** Funções recebem `PathLike` e retornam `Path`. Caminhos
   do Windows partem de variáveis de ambiente.

   ```python
   # Correto
   cache_dir = Path(os.environ["LOCALAPPDATA"]) / "rpa_3003" / "vault_cache"
   ```

   ```python
   # Incorreto: separador fixo e os.path.join
   cache_dir = os.path.join("C:\\Users\\robo\\AppData\\Local", "rpa_3003")
   ```

4. **Dependências pesadas só no uso (RNF-017).** selenium, cv2, numpy, mss, pyautogui,
   pywinauto, win32com, duckdb, psycopg2, reportlab, pypdf e pdfplumber são importados dentro da
   função que os usa. As exceções são os módulos de `bots/`, que só são carregados no uso:
   `browser_selenium.py` importa o selenium e `desktop_bot.py` o numpy no topo.

   ```python
   # Correto
   def read_pdf_text(path: PathLike) -> str:
       import pdfplumber

       with pdfplumber.open(Path(path)) as pdf:
           return "\n".join(page.extract_text() or "" for page in pdf.pages)
   ```

   ```python
   # Incorreto: importar o módulo de utilitários já carrega o pdfplumber
   import pdfplumber
   ```

5. **Exceções do framework.** Toda exceção externa capturada é relançada como subclasse de
   `RPAError` com `raise ... from exc`. A classe define o código de saída; os `details` não levam
   segredos.

   ```python
   # Correto
   try:
       content = path.read_bytes()
   except OSError as exc:
       raise FileOperationError("Falha ao ler arquivo", details={"arquivo": str(path)}) from exc
   ```

   ```python
   # Incorreto: exceção genérica, sem a causa e com dado sensível na mensagem
   try:
       content = path.read_bytes()
   except OSError:
       raise Exception(f"Falha ao ler {path} do usuário {credentials.username}")
   ```

6. **Log.** Use `get_logger()` (nunca `import logging` fora de `core/logger.py`), mensagens em
   pt-BR no padrão `Ação | chave=valor`, chaves ASCII sem acento e argumentos com `{}`.

   ```python
   # Correto
   get_logger().info("Arquivo processado | arquivo={} | linhas={}", file.name, total)
   ```

   ```python
   # Incorreto: logging padrão, chave com acento e formatação antecipada
   logging.info(f"Processado o arquivo {file.name}, número de linhas: {total}")
   ```

7. **Segredos.** Senhas ficam em `pydantic.SecretStr` e são registradas com `register_secret`
   antes de qualquer log. Nunca entram em mensagens de exceção nem em `details`.

   ```python
   # Correto
   register_secret(config.password.get_secret_value())
   ```

   ```python
   # Incorreto
   password: str = "S3nh@Forte"
   ```

8. **Nomes, docstrings e tipos.** Identificadores em inglês; docstrings no estilo Google, em
   pt-BR; toda API pública com type hints completos (`mypy --strict`). Funções e classes que não
   fazem parte do contrato da seção 8 são privadas (prefixo `_`).

   ```python
   # Correto
   def business_days_between(start: date, end: date) -> int:
       """Conta os dias úteis entre duas datas.

       Args:
           start: Data inicial (excluída).
           end: Data final (incluída).

       Returns:
           Quantidade de dias úteis.
       """
       return _count(start, end)
   ```

   ```python
   # Incorreto: nome em português, sem tipos e sem docstring
   def dias_uteis(inicio, fim):
       return contar(inicio, fim)
   ```

9. **Banco somente leitura (P-24).** O `database_utils` não tem `execute`, `executemany` nem
   `transaction`, e o framework nunca chama `commit`. Não acrescente escrita, DDL nem controle de
   transação em nenhuma engine.

   ```python
   # Correto
   rows = db.fetch_all("SELECT id, nome FROM fornecedores WHERE ativo = %s", (True,))
   ```

   ```python
   # Incorreto
   db._active_connection().cursor().execute("UPDATE fornecedores SET ativo = false")
   ```

10. **Estilo.** Linhas de até 100 colunas e complexidade ciclomática de no máximo 10 por função,
    verificadas pelo `ruff`. Divida funções longas em auxiliares privadas.

O que nunca muda: `LOG_FORMAT`, a estrutura obrigatória de diretórios (`REQUIRED_PROJECT_ITEMS`
em `core/config.py`), os códigos de saída (`ExitCode`) e os nomes dos comandos do CLI.

## Como estender

Esta seção traz um roteiro por ponto de extensão da
[seção 7.5.4 do PRD](../PRD.md#754-pontos-de-extensibilidade). Nenhum roteiro altera as classes
existentes: você acrescenta um módulo e o registra.

| Extensão | Onde fica | Mecanismo |
|---|---|---|
| [Navegador](#novo-navegador) | `rpa_3003/bots/<nome>_driver.py` | `DriverBuilder` + `BrowserFactory.register` |
| [Banco de dados](#novo-banco-de-dados) | `rpa_3003/utils/<nome>_database.py` | Subclasse de `Database` + `register_engine` |
| [Provedor de segredos](#novo-provedor-de-segredos) | `rpa_3003/utils/<nome>_secret_provider.py` | Protocolo `SecretProvider` + `VaultClient(provider=...)` |
| [Canal de e-mail](#novo-canal-de-e-mail) | `rpa_3003/utils/<nome>_email.py` | Protocolos `EmailSender`/`EmailReader` |
| [Template](#novo-template) | `rpa_3003/templates/<nome>/` | Pasta com `template.toml` e arquivos `.tpl` |
| [Utilitário](#novo-utilitário) | `rpa_3003/utils/<nome>_utils.py` | Módulo novo + nome em `utils/__init__.__all__` |

As extensões ficam em `bots/` ou `utils/` porque essas camadas podem importar as classes base
(seção [Arquitetura](#arquitetura)), e as regras de `GROUP_RULES` já as cobrem. A importação dos
drivers fica dentro das funções, e o registro é feito por uma função `register_*`, nunca na
importação do módulo. Assim, importar o módulo não muda o estado global. Uma extensão usada por
um único projeto pode ficar na pasta do projeto, com o mesmo código, mas fora do gate de
qualidade.

Toda extensão acrescenta API pública: atualize a versão e o README
([Fluxo de contribuição](#fluxo-de-contribuição)).

### Novo navegador

Exemplo: Firefox, fora do escopo da versão 1.0 e disponível por extensão (RF-033).

1. Crie o builder e a função de registro.

   Arquivo `rpa_3003/bots/firefox_driver.py`:

   ```python
   """Suporte ao Firefox por extensão da BrowserFactory (RF-033)."""

   from __future__ import annotations

   from typing import Final

   from selenium import webdriver
   from selenium.webdriver.firefox.service import Service
   from selenium.webdriver.remote.webdriver import WebDriver

   from rpa_3003.bots.browser_selenium import BrowserFactory, BrowserOptions

   __all__: list[str] = ["FIREFOX", "FirefoxDriverBuilder", "register_firefox"]

   FIREFOX: Final[str] = "firefox"


   class FirefoxDriverBuilder:
       """Cria o driver do Firefox a partir das opções comuns do framework."""

       def build(self, options: BrowserOptions) -> WebDriver:
           """Cria o driver; sem `driver_path`, o Selenium Manager resolve o geckodriver.

           Args:
               options: Opções comuns de navegador.

           Returns:
               Driver do Firefox iniciado.
           """
           firefox_options = webdriver.FirefoxOptions()
           if options.headless:
               firefox_options.add_argument("-headless")
           for argument in options.arguments:
               firefox_options.add_argument(argument)
           if options.download_dir is not None:
               firefox_options.set_preference("browser.download.folderList", 2)
               firefox_options.set_preference(
                   "browser.download.dir", str(options.download_dir.resolve())
               )
           driver_path = str(options.driver_path) if options.driver_path else None
           service = Service(executable_path=driver_path)
           return webdriver.Firefox(options=firefox_options, service=service)


   def register_firefox(*, overwrite: bool = False) -> None:
       """Registra o Firefox na BrowserFactory; depois disso, `Browser("firefox")` funciona.

       Args:
           overwrite: Substitui um registro existente.

       Raises:
           ValueError: Se o Firefox já estiver registrado e `overwrite` for falso.
       """
       BrowserFactory.register(FIREFOX, FirefoxDriverBuilder(), overwrite=overwrite)
   ```

2. Escreva o teste. O registro da fábrica é global, então a fixture o restaura após cada teste.

   Arquivo `tests/unit/bots/test_firefox_driver.py`:

   ```python
   """Testes do Firefox registrado por extensão da BrowserFactory (RF-033)."""

   from __future__ import annotations

   from collections.abc import Iterator
   from pathlib import Path

   import pytest
   from pytest_mock import MockerFixture

   from rpa_3003.bots.browser_selenium import Browser, BrowserFactory, BrowserOptions
   from rpa_3003.bots.firefox_driver import register_firefox

   pytestmark = pytest.mark.unit


   @pytest.fixture(autouse=True)
   def _restore_registry() -> Iterator[None]:
       saved = dict(BrowserFactory._registry)
       yield
       BrowserFactory._registry.clear()
       BrowserFactory._registry.update(saved)


   def test_firefox_registrado_na_fabrica(mocker: MockerFixture, tmp_path: Path) -> None:
       """RF-033: navegador registrado é criado pela fábrica sem alterar a classe Browser."""
       firefox = mocker.patch("selenium.webdriver.Firefox")
       register_firefox()
       options = BrowserOptions(headless=True, download_dir=tmp_path, arguments=["-private"])
       assert BrowserFactory.create_driver("firefox", options) is firefox.return_value
       firefox_options = firefox.call_args.kwargs["options"]
       assert firefox_options.arguments == ["-headless", "-private"]
       assert firefox_options.preferences["browser.download.dir"] == str(tmp_path.resolve())


   def test_browser_firefox_usa_o_builder_registrado(mocker: MockerFixture) -> None:
       """RF-033: Browser("firefox") inicia e encerra o driver registrado."""
       firefox = mocker.patch("selenium.webdriver.Firefox")
       register_firefox()
       with Browser("firefox", BrowserOptions(headless=True)) as browser:
           assert browser.driver is firefox.return_value
       firefox.return_value.quit.assert_called_once()


   def test_registro_duplicado_exige_overwrite() -> None:
       """RF-033: registrar duas vezes sem overwrite gera ValueError."""
       register_firefox()
       with pytest.raises(ValueError, match="já registrado"):
           register_firefox()
       register_firefox(overwrite=True)
   ```

3. No projeto, registre o navegador antes de criar o `Browser`, por exemplo no
   `step_setup_env`:

   ```python
   from rpa_3003.bots.browser_selenium import Browser, BrowserOptions
   from rpa_3003.bots.firefox_driver import FIREFOX, register_firefox

   register_firefox(overwrite=True)
   options = BrowserOptions.from_settings(self.ctx.settings, FIREFOX)
   browser = Browser(FIREFOX, options, screenshots_dir=self.ctx.paths.screenshots_dir).start()
   self.ctx.register_cleanup(browser.quit)
   ```

4. Verifique:

   ```powershell
   .venv\Scripts\python.exe -m pytest tests/unit/bots/test_firefox_driver.py tests/unit/test_architecture.py
   ```

   Resultado esperado: todos os testes passam. Depois, rode os itens 1 a 3 do gate
   ([Ambiente de desenvolvimento](#ambiente-de-desenvolvimento)).

### Novo banco de dados

Exemplo: MySQL com o driver PyMySQL (UC-05). A classe base `Database` já valida que cada
instrução é somente leitura antes de chegar ao driver, converte linhas em `dict` e trata erros. A
subclasse abre a conexão em modo somente leitura e encerra a leitura após cada consulta (P-24).

1. Acrescente `PyMySQL` ao `requirements.txt` e à tabela de licenças da
   [seção 7.6 do PRD](../PRD.md#76-dependências-externas) (licença MIT), e instale.

2. Crie a engine.

   Arquivo `rpa_3003/utils/mysql_database.py`:

   ```python
   """Engine MySQL somente leitura para o database_utils (RF-043, P-24)."""

   from __future__ import annotations

   from typing import Any, ClassVar, Final

   from rpa_3003.utils.database_utils import Database, register_engine

   __all__: list[str] = ["MYSQL", "MySQLDatabase", "register_mysql"]

   MYSQL: Final[str] = "mysql"


   class MySQLDatabase(Database):
       """MySQL via PyMySQL, com a sessão em `READ ONLY` e `rollback` após cada consulta.

       Parâmetros no estilo `pyformat` (`%s`, `%(nome)s`).
       """

       engine_name: ClassVar[str] = MYSQL
       paramstyle: ClassVar[str] = "pyformat"
       default_port: ClassVar[int | None] = 3306
       _required_fields: ClassVar[tuple[str, ...]] = ("host", "database", "user")

       def _connect(self) -> Any:  # noqa: ANN401 - conexão nativa do driver
           import pymysql

           config = self.config
           password = config.password.get_secret_value() if config.password else ""
           connection = pymysql.connect(
               host=config.host,
               port=config.port or self.default_port,
               database=config.database,
               user=config.user,
               password=password,
               connect_timeout=config.connect_timeout,
               autocommit=False,
               **config.options,
           )
           try:
               cursor = connection.cursor()
               cursor.execute("SET SESSION TRANSACTION READ ONLY")
               cursor.close()
           except Exception:
               connection.close()
               raise
           return connection

       def _end_read(self, connection: Any) -> None:  # noqa: ANN401 - conexão do driver
           connection.rollback()

       def table_exists(self, table: str, schema: str | None = None) -> bool:
           """Consulta `information_schema.tables`; sem `schema`, usa o banco da conexão.

           Args:
               table: Nome da tabela ou view.
               schema: Banco (schema) do MySQL; `None` usa `database()`.

           Returns:
               `True` se a tabela existir.

           Raises:
               DatabaseError: Se a consulta falhar.
           """
           sql = (
               "SELECT count(*) FROM information_schema.tables "
               "WHERE lower(table_name) = lower(%s) AND table_schema = coalesce(%s, database())"
           )
           return bool(self.fetch_scalar(sql, (table, schema)))


   def register_mysql(*, overwrite: bool = False) -> None:
       """Registra a engine `mysql`; depois disso, `connect(DatabaseConfig(engine="mysql"))` funciona.

       Args:
           overwrite: Substitui um registro existente.

       Raises:
           ValueError: Se a engine já estiver registrada e `overwrite` for falso.
       """
       register_engine(MYSQL, MySQLDatabase, overwrite=overwrite)
   ```

   `_required_fields` lista os campos obrigatórios de `DatabaseConfig`, e `_end_read` encerra a
   leitura após cada consulta. Os dois são ganchos da classe base
   ([decisões da Sprint 3](DESVIOS.md#sprint-3--utils-ii)). O driver é importado dentro de
   `_connect` (RNF-017), e `autocommit` não pode vir em `options` (`RESERVED_OPTIONS`).

3. Escreva o teste. O driver é simulado em `sys.modules`, então o teste roda sem o PyMySQL
   instalado e sem servidor.

   Arquivo `tests/unit/utils/test_mysql_database.py`:

   ```python
   """Testes da engine MySQL registrada por extensão (RF-043, P-24)."""

   from __future__ import annotations

   import sys
   from collections.abc import Iterator
   from unittest.mock import MagicMock

   import pytest
   from pydantic import SecretStr
   from pytest_mock import MockerFixture

   from rpa_3003.core.exceptions import ConfigurationError, DatabaseError
   from rpa_3003.utils import database_utils
   from rpa_3003.utils.database_utils import DatabaseConfig, connect, create_database
   from rpa_3003.utils.mysql_database import MySQLDatabase, register_mysql

   pytestmark = pytest.mark.unit


   @pytest.fixture(autouse=True)
   def _restore_engines() -> Iterator[None]:
       saved = dict(database_utils._ENGINES)
       register_mysql(overwrite=True)
       yield
       database_utils._ENGINES.clear()
       database_utils._ENGINES.update(saved)


   @pytest.fixture
   def pymysql(mocker: MockerFixture) -> MagicMock:
       """Driver PyMySQL simulado; o cursor responde a uma consulta de uma coluna `n`."""
       driver = mocker.MagicMock()
       cursor = driver.connect.return_value.cursor.return_value
       cursor.description = (("n",),)
       cursor.fetchall.return_value = [(1,)]
       mocker.patch.dict(sys.modules, {"pymysql": driver})
       return driver


   def _config() -> DatabaseConfig:
       return DatabaseConfig(
           engine="mysql",
           host="db.local",
           database="erp",
           user="robo_leitura",
           password=SecretStr("senha-de-teste"),
       )


   def test_conexao_somente_leitura(pymysql: MagicMock) -> None:
       """RF-043/P-24: sessão READ ONLY, sem autocommit, rollback após a consulta e sem commit."""
       with connect(_config()) as db:
           assert isinstance(db, MySQLDatabase)
           assert db.fetch_all("SELECT 1 AS n") == [{"n": 1}]
       kwargs = pymysql.connect.call_args.kwargs
       assert (kwargs["port"], kwargs["autocommit"]) == (3306, False)
       connection = pymysql.connect.return_value
       connection.cursor.return_value.execute.assert_any_call("SET SESSION TRANSACTION READ ONLY")
       connection.rollback.assert_called()
       connection.commit.assert_not_called()


   def test_escrita_bloqueada_antes_do_driver(pymysql: MagicMock) -> None:
       """RF-043/RNF-010: instrução de escrita é recusada antes de conectar."""
       db = create_database(_config())
       with pytest.raises(DatabaseError, match="somente leitura"):
           db.fetch_all("DELETE FROM fornecedores")
       pymysql.connect.assert_not_called()


   def test_campos_obrigatorios() -> None:
       """RF-043: host, database e user são obrigatórios para o MySQL."""
       with pytest.raises(ConfigurationError, match="database, user"):
           create_database(DatabaseConfig(engine="mysql", host="db.local"))
   ```

4. No projeto, registre a engine antes de conectar:

   ```python
   from rpa_3003.utils.database_utils import DatabaseConfig, connect
   from rpa_3003.utils.mysql_database import register_mysql

   register_mysql(overwrite=True)
   config = DatabaseConfig.from_credentials("mysql", credenciais, host="mysql.interno", database="erp")
   with connect(config) as db:
       fornecedores = db.fetch_all("SELECT id, nome FROM fornecedores WHERE ativo = %s", (1,))
   ```

5. Verifique:

   ```powershell
   .venv\Scripts\python.exe -m pytest tests/unit/utils/test_mysql_database.py tests/unit/test_architecture.py
   ```

   Resultado esperado: todos os testes passam. Depois, rode os itens 1 a 3 do gate. Antes de usar
   em produção, confirme com a equipe de banco que o usuário do robô só tem `SELECT`.

### Novo provedor de segredos

Exemplo: segredos em variáveis de ambiente, úteis em pipelines e testes. Em `prd`, as
credenciais vêm **exclusivamente** do Vault; um provedor alternativo só é usado em `dev`.

1. Crie o provedor. Ele implementa o protocolo `SecretProvider` (`read_secret` e
   `is_available`); não precisa herdar de nenhuma classe.

   Arquivo `rpa_3003/utils/env_secret_provider.py`:

   ```python
   """Provedor de segredos em variáveis de ambiente, para dev e pipelines (SecretProvider)."""

   from __future__ import annotations

   import os
   import re
   from typing import Final

   from rpa_3003.core.exceptions import SecretNotFoundError

   __all__: list[str] = ["ENV_VARIABLE_PREFIX", "EnvironmentSecretProvider"]

   ENV_VARIABLE_PREFIX: Final[str] = "RPA3003_SECRET_"
   _NON_ALPHANUMERIC: Final[re.Pattern[str]] = re.compile(r"[^0-9A-Za-z]+")


   class EnvironmentSecretProvider:
       """Lê `<prefixo><CAMINHO>__<CHAVE>` do ambiente.

       O caminho `rpa/proj/dev/erp` e a chave `password` viram
       `RPA3003_SECRET_RPA_PROJ_DEV_ERP__PASSWORD`.
       """

       def __init__(self, prefix: str = ENV_VARIABLE_PREFIX) -> None:
           """Cria o provedor.

           Args:
               prefix: Prefixo das variáveis de ambiente.
           """
           self._prefix = prefix.upper()

       def read_secret(self, path: str) -> dict[str, str]:
           """Retorna as chaves do segredo `path`, em minúsculas.

           Args:
               path: Caminho do segredo (ex.: ``rpa/proj/dev/erp``).

           Returns:
               Pares chave/valor encontrados no ambiente.

           Raises:
               SecretNotFoundError: Se nenhuma variável corresponder ao caminho.
           """
           base = f"{self._prefix}{_NON_ALPHANUMERIC.sub('_', path).strip('_').upper()}__"
           data = {
               name[len(base) :].lower(): value
               for name, value in os.environ.items()
               if name.upper().startswith(base) and len(name) > len(base)
           }
           if not data:
               raise SecretNotFoundError(
                   "Segredo não encontrado nas variáveis de ambiente",
                   details={"caminho": path, "prefixo": base},
               )
           return data

       def is_available(self) -> bool:
           """O ambiente do processo sempre está disponível."""
           return True
   ```

2. Escreva o teste. O `VaultClient` registra os valores lidos para mascaramento, qualquer que
   seja o provedor.

   Arquivo `tests/unit/utils/test_env_secret_provider.py`:

   ```python
   """Testes do provedor de segredos em variáveis de ambiente (SecretProvider, RF-018)."""

   from __future__ import annotations

   from collections.abc import Callable
   from pathlib import Path

   import pytest

   from rpa_3003.core.config import Environment, VaultSettings
   from rpa_3003.core.exceptions import SecretNotFoundError
   from rpa_3003.core.logger import get_logger, setup_logger
   from rpa_3003.core.vault import SecretProvider, VaultClient
   from rpa_3003.utils.env_secret_provider import EnvironmentSecretProvider

   pytestmark = pytest.mark.unit

   PATH = "rpa/conciliacao/dev/erp"


   def test_provedor_le_credenciais_e_mascara(
       monkeypatch: pytest.MonkeyPatch,
       tmp_path: Path,
       read_log_lines: Callable[[Path], list[str]],
   ) -> None:
       """RF-018: credenciais do provedor chegam ao VaultClient e são mascaradas no log."""
       monkeypatch.setenv("RPA3003_SECRET_RPA_CONCILIACAO_DEV_ERP__USERNAME", "robo.teste")
       monkeypatch.setenv("RPA3003_SECRET_RPA_CONCILIACAO_DEV_ERP__PASSWORD", "S3nh@Teste")
       setup_logger(tmp_path, user="teste", console=False, file_prefix="segredos")
       client = VaultClient(
           VaultSettings(_env_file=None),
           env=Environment.DEV,
           provider=EnvironmentSecretProvider(),
       )
       credentials = client.get_credentials(PATH)
       assert credentials.username == "robo.teste"
       get_logger().info("Senha lida | senha={}", credentials.password.get_secret_value())
       assert read_log_lines(tmp_path)[-1].endswith("Senha lida | senha=********")


   def test_segredo_ausente() -> None:
       """RF-021: caminho sem variáveis gera SecretNotFoundError."""
       with pytest.raises(SecretNotFoundError):
           EnvironmentSecretProvider().read_secret("rpa/inexistente/dev/erp")


   def test_implementa_o_protocolo() -> None:
       """Seção 7.5.4: o provedor satisfaz o protocolo SecretProvider."""
       assert isinstance(EnvironmentSecretProvider(), SecretProvider)
   ```

3. No `global_vars.py` do projeto, use o provedor só em `dev`:

   ```python
   from rpa_3003.core.vault import VaultClient
   from rpa_3003.utils.env_secret_provider import EnvironmentSecretProvider


   def get_credentials(env: Environment) -> Credentials | None:
       """Lê as credenciais; em dev, das variáveis de ambiente, e em prd, do Vault."""
       path = get_env_config(env).vault_secret_path
       if path is None:
           return None
       if env is Environment.DEV:
           client = VaultClient(env=env, provider=EnvironmentSecretProvider())
           return client.get_credentials(path)
       return get_vault_client(env).get_credentials(path)
   ```

4. Verifique:

   ```powershell
   .venv\Scripts\python.exe -m pytest tests/unit/utils/test_env_secret_provider.py tests/unit/test_architecture.py
   ```

   Resultado esperado: todos os testes passam. Depois, rode os itens 1 a 3 do gate.

### Novo canal de e-mail

Exemplo: um canal que grava as mensagens como arquivos `.eml`, para validar e-mails em `dev`
sem enviá-los. O mesmo roteiro vale para um canal real, como o Microsoft Graph: implemente
`EmailSender.send` (envio) e, se precisar, `EmailReader` (leitura).

1. Crie o canal.

   Arquivo `rpa_3003/utils/folder_email.py`:

   ```python
   """Canal de e-mail que grava as mensagens em arquivos .eml (EmailSender), para dev e testes."""

   from __future__ import annotations

   import uuid
   from email.message import EmailMessage
   from email.utils import formatdate
   from pathlib import Path

   from rpa_3003.core.config import PathLike
   from rpa_3003.core.exceptions import EmailError
   from rpa_3003.core.logger import get_logger
   from rpa_3003.utils.email_utils import EmailMessageData

   __all__: list[str] = ["FolderEmailSender"]


   class FolderEmailSender:
       """Grava cada mensagem em `<pasta>/<id>.eml` em vez de enviá-la."""

       def __init__(self, folder: PathLike, *, from_addr: str = "robo@localhost") -> None:
           """Cria o canal.

           Args:
               folder: Pasta de destino dos arquivos `.eml` (criada no primeiro envio).
               from_addr: Remetente quando a mensagem não informa um.
           """
           self._folder = Path(folder)
           self._from_addr = from_addr

       def send(self, message: EmailMessageData) -> None:
           """Grava a mensagem com os anexos.

           Args:
               message: Mensagem a gravar.

           Raises:
               EmailError: Se não houver destinatário ou a gravação falhar.
           """
           if not message.to:
               raise EmailError("Mensagem sem destinatários", details={"assunto": message.subject})
           mail = EmailMessage()
           mail["Subject"] = message.subject
           mail["From"] = message.from_addr or self._from_addr
           mail["To"] = ", ".join(message.to)
           mail["Date"] = formatdate(localtime=True)
           mail.set_content(message.body, subtype="html" if message.html else "plain")
           target = self._folder / f"{uuid.uuid4().hex[:12]}.eml"
           try:
               for attachment in message.attachments:
                   path = Path(attachment)
                   mail.add_attachment(
                       path.read_bytes(),
                       maintype="application",
                       subtype="octet-stream",
                       filename=path.name,
                   )
               self._folder.mkdir(parents=True, exist_ok=True)
               target.write_bytes(mail.as_bytes())
           except OSError as exc:
               raise EmailError(
                   "Falha ao gravar a mensagem", details={"arquivo": str(target)}
               ) from exc
           get_logger().info(
               "E-mail gravado em arquivo | assunto={} | arquivo={}", message.subject, target.name
           )
   ```

2. Escreva o teste.

   Arquivo `tests/unit/utils/test_folder_email.py`:

   ```python
   """Testes do canal de e-mail em arquivos .eml (EmailSender, RF-044)."""

   from __future__ import annotations

   from email import message_from_bytes, policy
   from email.message import EmailMessage
   from pathlib import Path

   import pytest

   from rpa_3003.core.exceptions import EmailError
   from rpa_3003.utils.email_utils import EmailMessageData, EmailSender
   from rpa_3003.utils.folder_email import FolderEmailSender

   pytestmark = pytest.mark.unit


   def test_grava_mensagem_com_anexo(tmp_path: Path) -> None:
       """RF-044: a mensagem é gravada como .eml com assunto, destinatários e anexo."""
       anexo = tmp_path / "relatorio.csv"
       anexo.write_text("id;valor\n1;10,50\n", encoding="utf-8")
       sender: EmailSender = FolderEmailSender(tmp_path / "emails")
       sender.send(
           EmailMessageData(
               subject="Resumo da execução",
               body="Processamento concluído.",
               to=["financeiro@empresa.local"],
               attachments=[anexo],
           )
       )
       [eml] = (tmp_path / "emails").glob("*.eml")
       mail = message_from_bytes(eml.read_bytes(), policy=policy.default)
       assert isinstance(mail, EmailMessage)
       assert mail["Subject"] == "Resumo da execução"
       assert mail["To"] == "financeiro@empresa.local"
       assert [part.get_filename() for part in mail.iter_attachments()] == ["relatorio.csv"]


   def test_sem_destinatario(tmp_path: Path) -> None:
       """RF-044: mensagem sem destinatário gera EmailError."""
       with pytest.raises(EmailError, match="sem destinatários"):
           FolderEmailSender(tmp_path).send(EmailMessageData(subject="x", body="y", to=[]))
   ```

3. No projeto, escolha o canal pelo ambiente, por exemplo no wrapup:
   `FolderEmailSender(gv.OUTPUT_DIR / "emails")` em `dev` e `SMTPEmailClient(...)` em `prd`. Os
   dois atendem ao protocolo `EmailSender`.

4. Verifique:

   ```powershell
   .venv\Scripts\python.exe -m pytest tests/unit/utils/test_folder_email.py tests/unit/test_architecture.py
   ```

   Resultado esperado: todos os testes passam. Depois, rode os itens 1 a 3 do gate.

### Novo template

Um template é uma pasta em `rpa_3003/templates/` com `template.toml` e arquivos `.tpl`. O motor
o descobre sem alteração de código (RF-055), e o `setup.py` e o `MANIFEST.in` já incluem
qualquer pasta de template no pacote.

1. Copie o template `example` como ponto de partida:

   ```powershell
   Copy-Item -Recurse rpa_3003\templates\example rpa_3003\templates\web
   ```

2. Ajuste os metadados. `name` deve ser igual ao nome da pasta, e `ensure_dirs` lista pastas
   relativas criadas com `.gitkeep`.

   Arquivo `rpa_3003/templates/web/template.toml`:

   ```toml
   name = "web"
   description = "Projeto RPA web: navegador habilitado em dev e prd"
   version = "1.0.0"
   ensure_dirs = ["logs", "data", "data/input", "data/output", "data/images"]
   ```

3. Edite os `.tpl`. Por exemplo, em `global_vars.py.tpl`, use `browser_enabled=True` nos dois
   ambientes. Regras do motor:
   - Variáveis disponíveis: `${project_name}`, `${project_title}`, `${author}`, `${created_at}`
     e `${framework_version}`. Variável desconhecida gera `TemplateRenderError`.
   - `$$` produz um `$` literal.
   - O prefixo `dot_` gera arquivos e pastas ocultos (`dot_env.example.tpl` → `.env.example`).
   - Arquivos sem `.tpl` são copiados byte a byte.
   - O projeto gerado precisa passar em `rpa3003 validate` (estrutura do RF-001).

4. Escreva o teste.

   Arquivo `tests/unit/templates/test_template_web.py`:

   ```python
   """Testes do template web (RF-053 a RF-055)."""

   from __future__ import annotations

   from pathlib import Path

   import pytest

   from rpa_3003.core.config import validate_project_structure
   from rpa_3003.templates import create_project, get_template, list_templates

   pytestmark = pytest.mark.unit


   def test_template_web_e_descoberto() -> None:
       """RF-055: a pasta nova é listada com os metadados do template.toml."""
       assert "web" in [info.name for info in list_templates()]
       assert get_template("web").version == "1.0.0"


   def test_template_web_gera_projeto_valido(projects_dir: Path) -> None:
       """RF-053/RF-054: o projeto gerado é válido e não tem variáveis sem substituição."""
       project = create_project("portal_fornecedores", projects_dir, template="web")
       assert validate_project_structure(project) == []
       for path in project.rglob("*.py"):
           assert "${" not in path.read_text(encoding="utf-8")
   ```

5. Verifique:

   ```powershell
   .venv\Scripts\python.exe -m pytest tests/unit/templates
   .venv\Scripts\rpa3003.exe templates
   ```

   Resultado esperado: os testes passam, e a tabela lista `example` e `web`. Um nome de template
   com mais de 8 caracteres alarga a coluna `TEMPLATE` e muda a linha esperada em
   `test_templates_lists_packaged_example` (`tests/unit/test_cli.py`); ajuste o teste nesse caso.

### Novo utilitário

Exemplo: `text_utils`, para comparar textos sem diferenciar acentos, espaços e maiúsculas (útil
para conferir nomes vindos de sistemas diferentes).

1. Crie o módulo. Siga as regras de `utils/*` da seção [Arquitetura](#arquitetura): pode
   importar `core` (exceto `workflow`) e outros `utils`, e importa dependências pesadas só dentro
   das funções.

   Arquivo `rpa_3003/utils/text_utils.py`:

   ```python
   """Normalização de textos para comparação (acentos, espaços e maiúsculas)."""

   from __future__ import annotations

   import unicodedata

   __all__: list[str] = ["normalize_text", "same_text"]


   def normalize_text(text: str) -> str:
       """Remove acentos, junta espaços repetidos e converte para minúsculas.

       Args:
           text: Texto de entrada (ex.: ``"  São   Paulo "``).

       Returns:
           Texto normalizado (ex.: ``"sao paulo"``).
       """
       decomposed = unicodedata.normalize("NFKD", text)
       without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
       return " ".join(without_accents.split()).casefold()


   def same_text(first: str, second: str) -> bool:
       """Compara dois textos ignorando acentos, espaços repetidos e maiúsculas.

       Args:
           first: Primeiro texto.
           second: Segundo texto.

       Returns:
           `True` se os textos normalizados forem iguais.
       """
       return normalize_text(first) == normalize_text(second)
   ```

2. Acrescente `"text_utils"` em ordem alfabética em dois lugares: no `__all__` de
   `rpa_3003/utils/__init__.py` e na lista `expected` de
   `test_utils_package_declares_modules_without_importing_them` (`tests/unit/test_package.py`).

3. Escreva o teste.

   Arquivo `tests/unit/utils/test_text_utils.py`:

   ```python
   """Testes de text_utils."""

   from __future__ import annotations

   import pytest

   from rpa_3003.utils.text_utils import normalize_text, same_text

   pytestmark = pytest.mark.unit


   @pytest.mark.parametrize(
       ("raw", "expected"),
       [("  São   Paulo ", "sao paulo"), ("AÇAÍ LTDA", "acai ltda"), ("", "")],
   )
   def test_normalize_text(raw: str, expected: str) -> None:
       """Remove acentos, espaços repetidos e maiúsculas."""
       assert normalize_text(raw) == expected


   def test_same_text() -> None:
       """Compara nomes vindos de sistemas diferentes."""
       assert same_text("Conceição  Ltda", "CONCEICAO LTDA")
       assert not same_text("Conceição", "Concessão")
   ```

4. Verifique:

   ```powershell
   .venv\Scripts\python.exe -m pytest tests/unit/utils/test_text_utils.py tests/unit/test_package.py tests/unit/test_architecture.py
   ```

   Resultado esperado: todos os testes passam. Depois, rode os itens 1 a 3 do gate; a cobertura
   de `utils/*` precisa continuar em 85% ou mais.

## Como escrever testes

Esta seção mostra onde cada teste fica e como isolá-lo. A estratégia completa está na
[seção 12 do PRD](../PRD.md#12-estratégia-de-testes).

**Onde fica cada teste:**

| Pasta | Tipo | Execução |
|---|---|---|
| `tests/unit/<camada>/` | Unitário: sem rede, relógio real nem serviço externo; arquivos em `tmp_path` | `pytest` |
| `tests/unit/test_architecture.py`, `test_package.py`, `test_cli.py`, `test_docs.py` | Regras transversais, API exportada, CLI e documentação | `pytest` |
| `tests/e2e/` | `rpa3003 create` → `validate` → `run` em processo real | `pytest` |
| `tests/integration/` | Serviço real; pulado sem as variáveis `RPA3003_TEST_*` | `pytest -m integration` |
| `tests/integration/test_browser_live.py`, `test_desktop_live.py` | Navegador e Bloco de Notas reais | `pytest -m "browser or gui"` |

**Convenções:**

- Não há `__init__.py` nas pastas de teste: o nome de cada arquivo de teste é único em toda a
  árvore.
- Cada módulo declara o marcador: `pytestmark = pytest.mark.unit` (ou `e2e`, `integration`,
  `browser`, `gui`, `windows`). Fora do Windows, `windows`, `gui` e `browser` são pulados.
- O docstring de cada teste cita o requisito: `"""RF-017: arquivo do dia anterior não é
  renomeado."""`.
- Os testes não passam pelo `mypy`, e o ruff dispensa `S101` (assert) e anotações (`ANN`).

**Fixtures compartilhadas** (`tests/conftest.py`):

| Fixture | Uso |
|---|---|
| `_isolate_framework_state` (automática) | Remove `RPA3003_*` e `VAULT_*` do ambiente (preserva `RPA3003_TEST_*`) e, no fim, reseta logger, segredos, cache de settings e clientes Vault |
| `no_sleep` | Substitui `time.sleep` e devolve a lista de esperas pedidas |
| `projects_dir`, `sample_project` | Pasta `projects/` vazia em `tmp_path`; projeto `projeto_teste` criado do `example` |
| `read_log_lines` | Chama `logger.complete()` e lê todos os `*.log` da pasta |
| `fake_hvac` | Classe `hvac.Client` simulada; o cliente é `fake_hvac.return_value` |
| `sample_csv`, `sample_pdf` | CSV `;` em utf-8-sig e PDF de 3 páginas, com acentos |
| `http_server` | Serve `tests/fixtures/html` em porta livre e retorna a URL base |

**Isolamento de dependências externas** (resumo da
[seção 12.6 do PRD](../PRD.md#126-isolamento-de-dependências-externas)):

- Bibliotecas de rede e GUI são simuladas com `mocker.patch` no ponto de uso
  (`selenium.webdriver.Chrome`, `hvac.Client`, `psycopg2.connect`, `smtplib.SMTP`) ou com um
  módulo falso em `sys.modules` (`mocker.patch.dict(sys.modules, {"pymysql": driver})`).
- SQLite, DuckDB e OpenCV rodam de verdade, sobre dados criados pelo teste.
- Tempo simulado com `time_machine.travel(datetime(...).astimezone(), tick=False)`; esperas
  com a fixture `no_sleep`.
- Estado global (registro da `BrowserFactory`, engines de banco, `sys.modules`, diretório atual)
  é salvo e restaurado por fixture, como nos roteiros de [Como estender](#como-estender).
- O sink de arquivo do log usa `enqueue=True`: chame `logger.complete()` (ou `read_log_lines`)
  antes de ler o arquivo.

**Cobertura mínima** (medida no `pytest` padrão):

| Escopo | Mínimo |
|---|---|
| Global (`rpa_3003`) | 85% |
| `rpa_3003/core/*` | 90% |
| `rpa_3003/utils/*` | 85% |
| `rpa_3003/cli.py` | 85% |
| `rpa_3003/templates/*` | 90% |
| `rpa_3003/bots/*` | 70% |

## Fluxo de contribuição

Use este checklist antes de entregar qualquer mudança no framework. Ele junta o DoD global, o
gate de qualidade e o registro de decisões.

1. Leia a subseção da seção 8 do PRD do módulo que vai mudar e os desvios dele em
   `docs/DESVIOS.md`.
2. Implemente com o DoD global
   ([PRD, seção 13.2](../PRD.md#132-definition-of-done-global-vale-para-todas-as-sprints)):
   - type hints completos e docstrings Google em pt-BR em toda API pública;
   - testes do que mudou, com o requisito no docstring;
   - nenhuma regra de dependência violada;
   - nenhum segredo, caminho absoluto da sua máquina ou dado pessoal no código.
3. Rode o gate de qualidade ([PRD, seção 12.10](../PRD.md#1210-gate-de-qualidade)), no
   Windows:
   1. `pytest` passa;
   2. os limites de cobertura são atendidos;
   3. `ruff check .`, `ruff format --check .` e `mypy` sem erros;
   4. `detect-secrets scan --exclude-files "^tests/" .` sem achados fora de `tests/`;
   5. antes de um release, `pytest -m "browser or gui"` passa em máquina com Chrome, Edge e modo
      IE configurados.
4. Registre em `docs/DESVIOS.md` todo desvio do PRD e toda decisão de interpretação, com
   referência, desvio e justificativa (siga o formato `D-NN` existente).
5. Atualize o [README](../README.md) quando o uso mudar, e este guia quando mudar a estrutura, a
   arquitetura, um ponto de extensão ou o processo de testes.
6. Atualize a versão em `rpa_3003/_version.py`, a única fonte da versão (P-01): correção →
   `1.0.1`; funcionalidade ou extensão nova compatível → `1.1.0`; quebra de contrato → `2.0.0`.

**O que nunca muda:** `LOG_FORMAT`, a estrutura obrigatória de diretórios
(`REQUIRED_PROJECT_ITEMS`), os códigos de saída (`ExitCode`) e os nomes dos comandos do CLI
(`create`, `run`, `list`, `templates` e `validate`). Também não entra escrita em banco de dados
(P-24).

## Problemas comuns no desenvolvimento

Esta tabela cobre os erros mais frequentes ao mudar o framework, com a causa e a ação.

| Erro ou sintoma | Causa | Ação |
|---|---|---|
| `test_layer_dependency_rules[rpa_3003....]` falha listando um import | O módulo importa uma camada proibida (ex.: `utils` importando `bots` ou `core.workflow`) | Mova a lógica para a camada certa ou receba o objeto por parâmetro. Só mude `MODULE_RULES`/`GROUP_RULES` se o PRD exigir, com registro em `docs/DESVIOS.md` |
| `test_every_module_has_dependency_rule` falha | Módulo novo em `core/`, na raiz do pacote ou fora de `utils/`/`bots/` | Prefira `utils/` ou `bots/`; em `core/`, a regra nova exige justificativa no PRD |
| `test_package_imports_do_not_load_heavy_modules` ou `test_bots_load_heavy_modules_only_on_use` falham | Dependência pesada importada no nível do módulo, ou um `__init__.py` importando o módulo | Importe dentro da função que usa a biblioteca (RNF-017) |
| `test_std_logging_imported_only_by_logger` falha | `import logging` fora de `core/logger.py` | Use `get_logger()`; o logging das bibliotecas já é interceptado |
| `test_paths_use_pathlib_without_hardcoded_separators` falha | `os.path.join` ou `\` como separador num literal (inclusive em regex) | Use `pathlib`; em regex, prefira `(?!\d)\w` a `[^\W\d]` |
| Teste aparece como `SKIPPED` fora do Windows | Marcadores `windows`, `gui` ou `browser` | Esperado (P-22); rode no Windows antes de entregar |
| Teste de integração aparece como `SKIPPED` | Faltam as variáveis `RPA3003_TEST_*` | Defina as variáveis do serviço (README, seção [Testes](../README.md#testes)) |
| `ruff format --check` falha em `README.md` ou neste guia | O ruff 0.17 formata blocos Python dentro de Markdown | Rode `ruff format <arquivo>.md` e revise; só o `PRD.md` fica fora (D-02) |
| `ruff format` muda o recuo de uma docstring de módulo | O formatador remove o recuo comum das linhas de continuação e exige linha em branco após a docstring | Separe resumo e corpo da docstring, como em `utils/__init__.py` (D-03) |
| `mypy` acusa `missing library stubs` | Biblioteca sem tipos | Já coberto por `ignore_missing_imports = true`; para tipos parciais, anote com o tipo correto ou `Any` com `# noqa: ANN401` justificado |
| Cobertura de um pacote abaixo do limite | Módulo novo sem testes suficientes | Veja as linhas em `--cov-report=term-missing` e cubra os ramos de erro |
| Teste passa sozinho e falha na suíte | Estado global vazando (registro da fábrica, engines, `sys.modules`, `os.chdir`, logger) | Salve e restaure o estado em fixture; use `monkeypatch` para ambiente e diretório |
| Arquivo de log vazio no teste | O sink usa `enqueue=True` | Chame `logger.complete()` (ou use `read_log_lines`) antes de ler |
| `detect-secrets` acusa arquivos em `tests\` | No Windows, os caminhos saem com `\`, e `^tests/` não os exclui | Achados em `tests\` são aceitos; fora de `tests/`, corrija ou use `# pragma: allowlist secret` com justificativa (D-12) |
| `detect-secrets scan` não acusa nada num arquivo novo | O `scan` só considera arquivos rastreados pelo git | Rode depois do `git add`, ou passe os arquivos novos na linha de comando |
| Teste de processo encontra um processo Python a mais | No Windows, o `python.exe` do `.venv` é um lançador que cria o interpretador como filho | Considere a árvore inteira (`psutil.Process(pid).children(recursive=True)`) |
| `pip wheel . --no-deps -w <pasta>` deixa uma pasta `build/` | Comportamento do setuptools | Apague `build/` após conferir o wheel |
| `.venv` deixa de funcionar | O `.venv` aponta para um interpretador removido (ex.: `.python/`, D-01) | Recrie o `.venv` com um Python 3.12+ disponível |

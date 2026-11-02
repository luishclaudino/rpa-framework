# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Visão geral

Framework RPA `rpa_3003` (Python 3.12+, alvo Windows). O `PRD.md` é a especificação **normativa** e o código é implementado sprint a sprint (seção 13.3 do PRD). Antes de mexer em um módulo, leia a subseção correspondente da seção 8 (contratos), além das seções 5, 10 e 11 quando aplicável.

Estado atual: Sprints 0 a 6 concluídas (`core/*`, `utils/*`, `bots/*`, `cli.py`, `__main__.py`, o motor `templates/__init__.py` e o template `example` completos). Falta a Sprint 7 (`tests/e2e/test_create_and_run.py`, README, guia do desenvolvedor e gate final). Os testes de integração de banco e e-mail (`tests/integration/`) existem, mas ainda não rodaram contra servidores reais. Continuam pendentes o DoD do Denodo e o do modo IE, que não está habilitado na máquina de desenvolvimento (`docs/DESVIOS.md`, Sprint 3 e D-08).

## Ambiente e comandos

O `.venv` usa o CPython 3.12.16 local em `.python/` (`docs/DESVIOS.md`, D-01). Nunca instale pacotes no Python global; use sempre o `.venv`.

```powershell
.venv\Scripts\python.exe -m pip install -e .[dev]

.venv\Scripts\python.exe -m pytest                              # unit + e2e (addopts exclui integration, gui, browser)
.venv\Scripts\python.exe -m pytest tests/unit/core/test_logger.py::test_log_line_has_exact_mandatory_format
.venv\Scripts\python.exe -m pytest -k rotation
.venv\Scripts\python.exe -m pytest -m integration               # requer variáveis RPA3003_TEST_*
.venv\Scripts\python.exe -m pytest -m "browser or gui"          # sessão Windows interativa
$env:RPA3003_TEST_IE_MODE = "1"                                # habilita o teste do modo IE (D-08)

.venv\Scripts\python.exe -m pytest --cov=rpa_3003 --cov-report=term-missing
.venv\Scripts\coverage.exe report --include="rpa_3003/core/*" --fail-under=90

.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
.venv\Scripts\mypy.exe                                          # strict, somente rpa_3003/ (testes não são checados)
.venv\Scripts\detect-secrets.exe scan --exclude-files "^tests/" .
```

Limites de cobertura (seção 12.8): global 85% (`fail_under`), `core/*` 90%, `utils/*` 85%, `cli.py` 85%, `templates/*` 90%, `bots/*` 70%. No Windows, o `detect-secrets` informa caminhos com `\`, então `^tests/` não exclui nada; achados em `tests\` são aceitos.

## Arquitetura

Quatro camadas: `core` → `utils` → `bots` → `cli`/`templates`. As regras de dependência da seção 7.2 são verificadas por `tests/unit/test_architecture.py`, que analisa via `ast` **todos** os imports, inclusive os locais e os que estão sob `TYPE_CHECKING`. Ao adicionar uma dependência, atualize `MODULE_RULES`/`GROUP_RULES` nesse teste somente se o contrato do PRD a exigir, e registre o desvio em `docs/DESVIOS.md` (ver D-05).

Restrições que o teste também verifica:
- `import logging` só em `core/logger.py`, o único módulo que adiciona sinks ao loguru.
- `rpa_3003/__init__.py` e `core/__init__.py` não importam `vault` nem `workflow` (evita carregar `hvac`/`filelock`). Os `__init__.py` de `utils` e `bots` não importam módulos pesados (selenium, cv2, numpy, duckdb, psycopg2, pywinauto…); a importação é preguiçosa (RNF-017).
- `core/vault.py` importa `hvac`, `requests` e `win32crypt` dentro das funções.

Fluxo de execução (seção 7.5.3): `rpa3003 run <nome>` → subprocesso `python main.py --env <env>` com cwd na pasta do projeto → `core.workflow.bootstrap()` (chdir, `sys.path`, limpeza de `workflows*`/`global_vars` de outros projetos em `sys.modules`, settings, logger e `run_id`) → `WorkflowRunner.run()` (lock `.rpa3003.lock`, carga de **todos** os steps antes de executar, setup → execute → wrapup com `always_run`, cleanups LIFO, linha `Execução finalizada | …`) → o código de `ExitCode` é propagado.

Projetos ficam em `projects/<nome>/` e têm `main.py`, `global_vars.py` e
`workflows/{dev,prd}/step_*.py`. Cada step define `class Step(BaseStep)`. A estrutura obrigatória é
`REQUIRED_PROJECT_ITEMS` em `core/config.py` (fonte única). `workflows/prd` nunca importa
`workflows.dev`.

Pontos centrais do `core`:
- **Exceções:** toda exceção externa capturada é relançada como subclasse de `RPAError` com
  `raise … from exc`. A classe da exceção determina o código de saída (`exit_code_for`).
- **Configuração:** `FrameworkSettings`/`VaultSettings` (pydantic-settings) leem `RPA3003_*` e o
  `.env` **do diretório atual**; `ValidationError` vira `ConfigurationError(details={"campos": …})`.
  `get_settings()` usa cache (`reset_settings_cache()`).
- **Logger:** `LOG_FORMAT` é imutável. O mascaramento de segredos é um patcher do loguru, que
  roda depois da formatação de `{}`. O sink de arquivo usa `enqueue=True`, então chame
  `logger.complete()` antes de ler o arquivo de log.

## Regras do PRD para implementação

- Nomes, assinaturas, valores padrão e exceções da seção 8 são exatos. Funções e classes
  adicionais devem ser privadas (prefixo `_`).
- Identificadores em inglês; docstrings no estilo Google, mensagens de log e de CLI em pt-BR.
  Mensagens de log seguem `Ação | chave=valor | chave=valor`, com chaves ASCII sem acento.
- Todo módulo começa com docstring e `from __future__ import annotations`. Constantes usam
  `Final`; aliases usam `TypeAlias` (com `# noqa: UP040`, pois a convenção 7.7 prevalece sobre o
  ruff). Caminhos usam somente `pathlib`, e caminhos do Windows partem de variáveis de ambiente
  (`LOCALAPPDATA`, `ProgramFiles`).
- Nunca altere `LOG_FORMAT`, a estrutura obrigatória de diretórios, os códigos de saída nem os
  nomes de comandos do CLI.
- `database_utils` é somente leitura (P-24, seção 8.15, ADR-18): não existem `execute`,
  `executemany` nem `transaction`, e o framework nunca chama `commit`. Não adicione escrita, DDL
  ou controle de transação em nenhuma engine. O controle do robô vai para arquivos em `data/`.
- Ao concluir tarefas, marque os checkboxes da sprint em `PRD.md` (seção 13.3) e registre desvios
  ou decisões de interpretação em `docs/DESVIOS.md`.
- Templates usam `string.Template` em arquivos `.tpl`, `$$` para `$` literal e prefixo `dot_`
  para arquivos ocultos. `templates/example/` fica fora da cobertura e da verificação de
  arquitetura. O conteúdo do `example` é o da seção 11 do PRD, byte a byte, exceto a linha
  `# pragma: allowlist nextline secret` em `global_vars.py.tpl` (D-12). Os steps de `prd/` são
  cópias dos de `dev/`, mudando só `| dev)` → `| prd)` na primeira linha (seção 11.9).
- `setup.py` exclui `*.pyc` do `package_data` (D-11). Para conferir o wheel, use
  `pip wheel . --no-deps -w <pasta>` (o `.venv` não tem setuptools; o build usa isolamento) e
  apague a pasta `build/` que o pip deixa no repositório.

## Testes

- Não há `__init__.py` nas pastas de teste: os nomes de arquivo de teste devem ser únicos em toda
  a árvore.
- Cada teste cita no docstring o requisito que cobre (ex.: `"""RF-017: …"""`) e usa `pytestmark =
  pytest.mark.unit`. Marcadores `windows`, `gui` e `browser` são pulados fora do Windows.
- A fixture autouse `_isolate_framework_state` (`tests/conftest.py`) remove `RPA3003_*`/`VAULT_*`
  do ambiente (preserva `RPA3003_TEST_*`) e, ao final, chama `reset_logger`, `clear_secrets`,
  `reset_settings_cache` e `reset_vault_clients`. Testes que leem `.env` fazem `chdir` para
  `tmp_path`.
- Fixtures compartilhadas: `no_sleep` (substitui `time.sleep`), `read_log_lines`, `fake_hvac`
  (classe `hvac.Client` simulada; o cliente é `fake_hvac.return_value`), `projects_dir`,
  `sample_csv`, `sample_pdf` (3 páginas, gerado com reportlab) e `http_server` (serve
  `tests/fixtures/html` em porta livre e retorna a URL base terminada em `/`).
- Bots nos testes unitários: `test_browser_selenium.py` registra um `FakeBuilder` como `fake`
  (o registro da `BrowserFactory` é restaurado após cada teste) e simula `webdriver.Chrome/Edge/Ie`
  com `mocker.patch("selenium.webdriver.Chrome")`. `test_desktop_bot.py` instala módulos falsos de
  `mss`, `pyautogui`, `pyperclip` e `pywinauto` em `sys.modules` (fixture `gui`) e usa o OpenCV real
  sobre telas sintéticas de ruído. O `test_desktop_live.py` digita no Bloco de Notas: é pulado
  se já houver `notepad.exe` aberto e só digita em um arquivo próprio em `tmp_path`.
- Bancos nos testes: SQLite e DuckDB reais em `tmp_path`, populados pelo próprio teste com o
  driver. Para testar a conexão somente leitura, desative a validação com
  `mocker.patch.object(database_utils, "_read_only_violation", return_value=None)`. O
  PostgreSQL e o Denodo são simulados com `mocker.patch("psycopg2.connect")`.
- Tempo simulado com `time_machine.travel(datetime(...).astimezone(), tick=False)`; datetimes
  ingênuos são tratados como UTC. Na rotação de logs, alinhe a data de criação do arquivo ao
  relógio simulado (`_align_file_times` em `test_logger.py`).
- `test_workflow.py` monta projetos em `tmp_path` e restaura cwd, `sys.path` e `sys.modules`
  (`workflows*`, `global_vars`) após cada teste.
- CLI e templates nos testes: `test_cli.py` e `test_templates.py` trocam
  `rpa_3003.templates.templates_root` por uma pasta em `tmp_path` (fixture `template_root`).
  `test_cli.py` faz `chdir` para `tmp_path` (o `./projects` padrão é a fixture `projects_dir`) e
  simula `subprocess.Popen` nos testes de `run`. O `main()` chama `setup_logger(logs_dir=None)`,
  e o log sai em `capsys.readouterr().err`. `--help` e `--version` encerram com `SystemExit`.
  O teste do RF-052 roda um projeto real com `--timeout 2`. No Windows, o `python.exe` do `.venv`
  é um lançador que cria o interpretador de `.python/` como processo filho.
- Template `example` nos testes: a fixture `sample_project` cria `projeto_teste` com o template
  do pacote. Os testes da seção 11.12 em `test_templates.py` rodam `rpa3003 run` de verdade
  (subprocesso) num workspace com espaço e acentos no caminho. O `ruff check` dos arquivos gerados
  usa `--config pyproject.toml` com cwd na pasta que contém `projects/`, porque o ruff resolve o
  `per-file-ignores` `projects/**` a partir do diretório atual. O `global_vars.py` gerado é
  importado com `importlib.util.spec_from_file_location` e `monkeypatch.setitem(sys.modules, ...)`.

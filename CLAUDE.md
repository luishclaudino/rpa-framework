# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Visão geral

Framework RPA `rpa_3003` (Python 3.12+, alvo Windows). O `PRD.md` é a especificação **normativa** e o código é implementado sprint a sprint (seção 13.3 do PRD). Antes de mexer em um módulo, leia a subseção correspondente da seção 8 (contratos), além das seções 5, 10 e 11 quando aplicável.

Estado atual: Sprints 0 a 3 concluídas (`rpa_3003/core/*` e `rpa_3003/utils/*` completos). Os testes de integração de banco e e-mail (`tests/integration/`) existem, mas ainda não rodaram contra servidores reais, e o DoD do Denodo continua pendente (`docs/DESVIOS.md`, Sprint 3). `bots/`, `cli.py`, `__main__.py`, `templates/__init__.py` e os `.tpl` de `templates/example/` ainda contêm apenas docstrings (Sprints 4 a 6).

## Ambiente e comandos

O `.venv` usa o CPython 3.12.16 local em `.python/` (`docs/DESVIOS.md`, D-01). Nunca instale pacotes no Python global; use sempre o `.venv`.

```powershell
.venv\Scripts\python.exe -m pip install -e .[dev]

.venv\Scripts\python.exe -m pytest                              # unit + e2e (addopts exclui integration, gui, browser)
.venv\Scripts\python.exe -m pytest tests/unit/core/test_logger.py::test_log_line_has_exact_mandatory_format
.venv\Scripts\python.exe -m pytest -k rotation
.venv\Scripts\python.exe -m pytest -m integration               # requer variáveis RPA3003_TEST_*
.venv\Scripts\python.exe -m pytest -m "browser or gui"          # sessão Windows interativa

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
- Templates (Sprint 6) usam `string.Template` em arquivos `.tpl`, `$$` para `$` literal e
  prefixo `dot_` para arquivos ocultos. `templates/example/` fica fora da cobertura e da
  verificação de arquitetura.

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
  `sample_csv` e `sample_pdf` (3 páginas, gerado com reportlab).
- Bancos nos testes: SQLite e DuckDB reais em `tmp_path`, populados pelo próprio teste com o
  driver. Para testar a conexão somente leitura, desative a validação com
  `mocker.patch.object(database_utils, "_read_only_violation", return_value=None)`. O
  PostgreSQL e o Denodo são simulados com `mocker.patch("psycopg2.connect")`.
- Tempo simulado com `time_machine.travel(datetime(...).astimezone(), tick=False)`; datetimes
  ingênuos são tratados como UTC. Na rotação de logs, alinhe a data de criação do arquivo ao
  relógio simulado (`_align_file_times` em `test_logger.py`).
- `test_workflow.py` monta projetos em `tmp_path` e restaura cwd, `sys.path` e `sys.modules`
  (`workflows*`, `global_vars`) após cada teste.

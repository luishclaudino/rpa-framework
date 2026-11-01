# Desvios técnicos

Registro dos desvios inevitáveis em relação ao `PRD.md`, conforme a instrução 7 da seção 13.1.
Nenhum desvio altera contratos públicos, `LOG_FORMAT`, a estrutura obrigatória de diretórios, os
códigos de saída ou os nomes de comandos do CLI.

## Sprint 0 — Fundação do repositório

### D-01 — Interpretador Python 3.12 local ao workspace (`.python/`)

- **Referência:** DoD da Sprint 0 ("`pip install -e .[dev]` conclui em Python 3.12 no Windows") e
  seção 8.31 (`.gitignore`).
- **Desvio:** a máquina de desenvolvimento tinha apenas o Python 3.14 instalado globalmente. Para
  não alterar a instalação global, o CPython 3.12.16 foi baixado com
  `uv python install 3.12 --install-dir .python --no-bin --no-registry`, sem atalhos no `PATH`
  nem registro no Windows. O `.venv` do workspace foi criado a partir desse interpretador. A
  entrada `.python/` foi adicionada ao `.gitignore`.
- **Justificativa:** o PRD exige validação em Python 3.12, e não havia interpretador 3.12 na
  máquina.
- **Resultado:** `pip install -e .[dev]`, `ruff check .`, `ruff format --check .`, `mypy` e
  `pytest` (48 testes) passam em Python 3.12.16. A mesma validação também passou antes em
  Python 3.14.3.
- **Observação:** o `.venv` depende de `.python/`. Se a pasta for removida, recrie o ambiente com
  um Python 3.12+ disponível.

### D-02 — `PRD.md` excluído do ruff

- **Referência:** seção 8.31 (`[tool.ruff]`) e DoD global (`ruff format --check .` sem erros).
- **Desvio:** adicionado `extend-exclude = ["PRD.md"]` em `[tool.ruff]` no `pyproject.toml`.
- **Justificativa:** o ruff 0.17 (permitido por `ruff>=0.6`) passou a formatar blocos de código
  Python dentro de arquivos Markdown. Os blocos do PRD são contratos condensados (corpos `...`,
  várias definições por linha) e não devem ser reescritos pelo formatador. Sem a exclusão,
  `ruff format --check .` falha apenas por causa do `PRD.md`.

### D-03 — Ajustes de formatação em código transcrito do PRD

- **Referência:** seções 8.1 (`_version.py`), 8.9 (`utils/__init__.py`) e 8.30 (`setup.py`).
- **Desvio:**
  - `_version.py` e `setup.py`: linha em branco entre a docstring do módulo e o código seguinte.
  - Docstring de `utils/__init__.py` dividida em resumo (`Utilitários do rpa_3003.`) e corpo
    (`Importe cada módulo explicitamente:` seguido do exemplo recuado), com o mesmo texto.
- **Justificativa:** o `ruff format` exige a linha em branco após a docstring do módulo. Na
  docstring de `utils/__init__.py`, ele remove o recuo comum das linhas de continuação; com o
  texto original, o exemplo de import perderia o recuo. O `setup.py` continua lendo a versão com
  a mesma expressão regular.

### D-04 — `_isolate_framework_state` preserva `RPA3003_TEST_*`

- **Referência:** seção 12.5 ("Remove variáveis RPA3003_* e VAULT_* do ambiente").
- **Desvio:** a fixture remove `RPA3003_*` e `VAULT_*`, mas mantém as variáveis com prefixo
  `RPA3003_TEST_`.
- **Justificativa:** as variáveis `RPA3003_TEST_*` configuram os testes de integração
  (seção 12.4), não o framework. Se fossem removidas antes de cada teste, os testes de integração
  não conseguiriam ler os próprios parâmetros de conexão.

## Sprint 1 — Core

### D-05 — Dependências de `vault.py` e `workflow.py` em `test_architecture.py`

- **Referência:** tabela de regras de dependência da seção 7.2 e contratos das seções 8.7 e 8.8.
- **Desvio:** as regras de `tests/unit/test_architecture.py` passaram a aceitar `pydantic` em
  `core/vault.py` e `loguru` em `core/workflow.py`.
- **Justificativa:** a tabela da seção 7.2 não lista essas bibliotecas, mas os contratos
  normativos exigem as duas. Em `vault.py`, `Credentials.password` é `pydantic.SecretStr`
  (seção 8.7). Em `workflow.py`, o tipo `Logger` vem de `from loguru import Logger` sob
  `TYPE_CHECKING` (seção 8.8 e RNF-003). O verificador analisa também imports sob
  `TYPE_CHECKING`, por isso apontava os dois casos. As demais regras não mudaram, e dois casos
  novos em `test_checker_accepts_allowed_imports` documentam a exceção.

### Decisões de implementação (sem alteração de contrato)

Pontos em que o PRD é omisso ou ambíguo. Nenhum altera assinatura pública, `LOG_FORMAT`, códigos
de saída ou estrutura obrigatória.

- **`get_logger()` sem `setup_logger()` prévio:** a configuração implícita registra somente no
  console, em `INFO`, e não intercepta o módulo `logging`. Uma função de leitura não deve
  substituir os handlers do logger raiz. `setup_logger()` continua interceptando por padrão
  (RF-019).
- **Campo `[user]`:** `setup_logger` e `get_logger(user=...)` removem espaços nas pontas e trocam
  `[`, `]` e quebras de linha por `_` ou espaço. Usuário vazio é tratado como não informado.
  Assim toda linha continua casando com a regex da seção 10.4 (OBJ-04).
- **`setup_logger(retention_days < 1)`:** lança `ValueError`. O logger não pode importar
  `exceptions` (seção 7.2), e `FrameworkSettings` já valida o mínimo de 1 dia.
- **`RPA3003_ENV` inválido:** `load_settings()` propaga `InvalidEnvironmentError` (código `2`) em
  vez de convertê-lo em `ConfigurationError` (código `3`), conforme o RF-010. `env`, `log_level` e
  `auth_method` aceitam maiúsculas e espaços nas pontas (validadores privados).
- **`validate_project_structure`:** além dos três steps padrão, verifica outros `step_*.py` de
  cada ambiente (P-20) e procura importação de `workflows.dev` em todos os módulos de
  `workflows/prd/` (RF-011). A ordem dos problemas da seção 8.5 vale para os steps padrão.
- **`find_project`:** um projeto existe quando a pasta existe, mesmo sem `main.py`. Assim
  `rpa3003 validate` informa `ausente: main.py` em vez de "projeto não encontrado". Nomes com
  separador de caminho, `..`, pastas ocultas e backups `*.bak-*` nunca são aceitos.
- **`list_projects`:** pasta de projetos inexistente lança `ConfigurationError` (código `3`),
  comportamento que a seção 9.4.3 exige do comando `list`.
- **`bootstrap`:** pasta de projeto inexistente lança `ProjectNotFoundError` (código `3`) antes
  de `os.chdir`. `--user` em branco é tratado como não informado (RF-016).
- **`WorkflowRunner`:** em `StepLoadError`, `RunResult.steps` fica vazio. Erro no construtor de um
  step conta como falha do step, e o wrapup executa normalmente (RNF-006). `KeyboardInterrupt`
  sempre define `ctx.error`, mesmo após falha anterior, para que o código de saída seja `130`.
- **Vault:** além dos valores lidos, o `token`, o `secret_id`, a senha de autenticação e o token
  emitido pelo login também são registrados para mascaramento (RNF-008). `is_available()` aceita
  nós standby (`standby_ok`). Entradas do cache offline com timestamp no futuro são ignoradas.
  Falha ao gravar o cache offline gera `WARNING` e não interrompe a leitura.
- **Teste de rotação (RF-017):** a rotação do loguru usa a data de criação do arquivo, que o
  sistema de arquivos grava com o relógio real. O teste alinha essa data ao relógio simulado
  pelo `time-machine`, para não depender da data real da máquina.

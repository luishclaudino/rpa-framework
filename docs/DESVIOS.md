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

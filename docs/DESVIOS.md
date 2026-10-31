# Desvios técnicos

Registro dos desvios inevitáveis em relação ao `PRD.md`, conforme a instrução 7 da seção 13.1.
Nenhum desvio altera contratos públicos, `LOG_FORMAT`, a estrutura obrigatória de diretórios, os
códigos de saída ou os nomes de comandos do CLI.

## Sprint 0 — Fundação do repositório

### D-01 — Validação em Python 3.14 em vez de 3.12 (pendente)

- **Referência:** DoD da Sprint 0 ("`pip install -e .[dev]` conclui em Python 3.12 no Windows").
- **Desvio:** a máquina de desenvolvimento tem apenas o Python 3.14.3. A instalação
  `pip install -e .[dev]`, o `pytest`, o `ruff` e o `mypy` foram validados em um `.venv` com
  Python 3.14.3. A validação em 3.12 continua pendente.
- **Justificativa:** o pacote declara `python_requires=">=3.12"`, e todas as dependências
  instalaram a partir de wheels em 3.14. O `mypy` já verifica o código com
  `python_version = "3.12"`.
- **Ação:** repetir a instalação e a suíte em um `.venv` com Python 3.12 assim que houver um
  interpretador 3.12 disponível, e marcar o item do DoD.

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

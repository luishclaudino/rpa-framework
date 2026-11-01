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

## Sprint 2 — Utils I

Nenhum desvio de contrato nesta sprint. As regras de `tests/unit/test_architecture.py` não
mudaram: a seção 7.2 já permite que um `utils` importe outro.

### Decisões de implementação (sem alteração de contrato)

- **Política única de hash (RNF-009):** `filesystem_utils.file_hash` e `crypt_utils.hash_text`
  aceitam os mesmos algoritmos (`SECURE_HASH_ALGORITHMS` e, com `allow_insecure=True`,
  `INSECURE_HASH_ALGORITHMS`). A regra fica no helper privado `crypt_utils._new_hash`, importado
  dentro de `file_hash` para que importar `filesystem_utils` não carregue o `cryptography`.
  Nomes de algoritmo aceitam maiúsculas e hífen (`"SHA3-256"`). HMAC aceita somente algoritmos
  seguros, e chave HMAC vazia gera `CryptoError`.
- **`filesystem_utils`:**
  - `list_files` e `get_latest_file` lançam `FileOperationError` quando a pasta não existe.
    `clean_dir` retorna `0` nesse caso (não há o que limpar), como `delete_*` com `missing_ok`.
  - `wait_for_file` aceita pasta que ainda não existe (o navegador pode criá-la). Havendo mais
    de um arquivo estável, retorna o de maior `st_mtime`.
  - `is_file_locked` retorna `True` também para arquivo somente leitura, que não abre em `r+b`
    (limitação do método exigido), e `False` para pastas.
  - `copy_file`/`move_file` com `overwrite=True` removem o atributo somente leitura do destino.
    `move_file` só recorre a `shutil.move` quando `os.replace` falha por volumes diferentes
    (`EXDEV` / `ERROR_NOT_SAME_DEVICE`). Em outros erros não há cópia parcial.
  - `safe_filename` avalia nomes reservados pela parte antes do primeiro ponto, como o Windows
    (`"NUL .tar.gz"` vira `"_NUL .tar.gz"`). Nome vazio vira `"_"`, e `replacement` com
    caractere inválido gera `ValueError`.
  - `read_text` normaliza quebras de linha para `\n`, como o modo texto do Python. `read_lines`
    separa linhas apenas em `\n`, `\r\n` e `\r`, preservando o form feed de relatórios legados.
    A gravação usa modo texto, com a quebra de linha da plataforma.
  - Gravação atômica: 3 tentativas no total, com 2 esperas de 0,5 s entre elas (RNF-007).
- **`json_utils`:** o encoder mantém o `TypeError` padrão, mas `to_json`/`write_json` o
  convertem em `DataFormatError` (também para referência circular). `SecretBytes` também é
  mascarado. Conjuntos com tipos não comparáveis são ordenados por `repr`. `deep_merge` copia
  os valores em profundidade, para que o resultado não compartilhe listas com as entradas.
  `validate` registra os erros como `"campo: mensagem"`, sem os valores recebidos (RNF-008).
  `ModelT` continua `TypeVar` público (contrato 8.11), com `noqa: UP047` na função `validate`.
- **`csv_utils`:**
  - Sem cabeçalho e sem `fieldnames`, as chaves `col_N` seguem a largura da primeira linha de
    dados. Com `has_header=True`, `fieldnames` substitui o cabeçalho do arquivo.
  - Linhas vazias antes do cabeçalho são sempre ignoradas. Linha só com espaços conta como
    vazia em `skip_empty_rows`.
  - `detect_delimiter` descarta a última linha incompleta quando a amostra é truncada. No
    fallback, um empate fica com a primeira candidata.
  - O `quoting: int` do contrato é convertido (`cast`) para o tipo literal exigido pelo
    `csv.writer`.
- **`archive_utils`:**
  - Pastas (inclusive vazias) entram como entradas no ZIP/TAR, para que a ida e volta preserve
    a estrutura.
  - A criação grava em `<destino>.<uuid>.tmp` e renomeia com `os.replace`, então nunca fica
    arquivo parcial. O destino dentro da pasta de origem não entra no próprio arquivo.
  - Nomes internos duplicados geram `ArchiveError`, e origem inexistente gera
    `FileOperationError`.
  - `add_to_zip` cria o ZIP se ele não existir e recusa arquivo existente que não seja ZIP, pois
    o modo `"a"` do `zipfile` anexaria um ZIP novo ao final dele.
  - No TAR, `tarfile.data_filter` é aplicado a todos os membros antes de `extractall(...,
    filter="data")`. Assim, "nada é extraído" vale também para links para fora do destino e
    arquivos de dispositivo, e não só para nomes com `..`.
- **`calendar_utils`:**
  - A biblioteca `holidays` recebe `language` com o idioma padrão do país. Sem esse parâmetro,
    os nomes dos feriados mudam conforme `LANG`/`LANGUAGE`.
  - País, UF ou categoria sem suporte geram `ConfigurationError`.
  - `datetime` com fuso é convertido para `DEFAULT_TZ` antes de virar data (`to_date` e dias
    úteis).
  - `date_range` inclui o fim. `business_days_between(inclusive=False)` exclui só `start`, o que
    é coerente com `add_business_days`.
  - Datas de `extra_holidays` recebem o nome `"Feriado adicional"`.
  - O `holidays` só é importado no primeiro cálculo.
- **`crypt_utils`:** senha vazia em `derive_key` gera `CryptoError`, e parâmetros numéricos não
  positivos (`iterations`, `size`, `nbytes`) geram `ValueError`. `b64decode` ignora espaços e
  quebras de linha e valida o restante estritamente. `repr(FernetCipher)` não mostra a chave. No
  Windows sem `pywin32`, o DPAPI gera `CryptoError("DPAPI requer o pacote pywin32")`.
- **Testes:** a fixture `sample_csv` (seção 12.5) entrou no `conftest.py`. O RNF-016 (sem
  `os.path.join` nem `\` como separador em literais) passou a ser verificado em
  `test_architecture.py`.

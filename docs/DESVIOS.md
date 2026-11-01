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

## Sprint 3 — Utils II

As regras de `tests/unit/test_architecture.py` não mudaram. Um caso novo verifica que importar
`database_utils`, `email_utils` e `pdf_utils` não carrega `psycopg2`, `duckdb`, `win32com`,
`pypdf`, `pdfplumber` nem `reportlab` (RNF-004, RNF-017).

### D-06 — `sslmode` padrão do PostgreSQL e do Denodo (RNF-009 × seção 8.15)

- **Referência:** RNF-009 ("TLS com verificação de certificado ativa por padrão em [...]
  PostgreSQL e Denodo") e contrato de `DatabaseConfig` (`sslmode: str | None = None`, "sslmode
  repassado").
- **Desvio:** no PostgreSQL e no Denodo, a verificação de certificado não é ativada por padrão.
  Com `sslmode=None` (padrão do contrato), o parâmetro não é enviado ao `psycopg2`, e o libpq
  usa `prefer`: usa TLS quando o servidor oferece, sem verificar o certificado. A variável
  `PGSSLMODE`, quando definida no ambiente, substitui esse padrão. Quem quiser verificar o
  certificado informa `sslmode="verify-full"` (ou `verify-ca`) ao criar o `DatabaseConfig`.
- **Decisão:** do responsável pelo projeto, em 01/11/2026. O contrato da seção 8.15 não muda.
- **Justificativa:** exigir `verify-full` por padrão impediria a conexão com servidores sem TLS
  ou sem certificado confiável na máquina do robô. As variáveis `RPA3003_TEST_PG_*` e
  `RPA3003_TEST_DENODO_*` também não incluem certificado. O RNF-009 continua valendo para Vault,
  SMTP e IMAP, que verificam o certificado por padrão.

### Testes de integração ainda não executados

`tests/integration/test_postgres_live.py`, `test_denodo_live.py` e `test_email_live.py` foram
criados. Sem as variáveis `RPA3003_TEST_*`, eles são pulados, e nesta máquina não foram
executados contra servidores reais. O item do DoD sobre o Denodo corporativo (execução, registro
do resultado e confirmação da equipe de dados de que o usuário do robô só tem privilégio de
consulta) continua pendente. O resultado deve ser registrado aqui.

### Decisões de implementação (sem alteração de contrato)

- **`database_utils`:**
  - Validação de instruções (camada 4): além das regras da seção 8.15, comentário de bloco com
    `/*` dentro dele é rejeitado. O PostgreSQL aceita comentários aninhados e o SQLite não, então
    a análise seria ambígua. A regra do literal terminado em `\` passou a valer para qualquer `\`
    seguido de aspas dentro do literal, o que também cobre `'a\''b'`. Parâmetros `$nome` do
    DuckDB não são tratados como palavras: `$update` é aceito, mas `t.delete` é bloqueado.
  - `details["sql"]` traz os primeiros 200 caracteres do SQL em uma linha, com espaços
    consecutivos reduzidos a um. Assim, o log continua aderente à regex da seção 10.4.
  - O erro do driver vai em `details["erro"]` (`Classe: mensagem`) e, quando existe, o SQLSTATE
    (`pgcode`) vai em `details["sqlstate"]`. Valores de parâmetros nunca são incluídos.
  - Sem parâmetros, `execute(sql)` é chamado sem o segundo argumento, para que `%` literal não
    passe pela interpolação do `psycopg2`. `params` como texto ou bytes gera `TypeError`.
  - `iter_rows` valida a instrução na chamada, e a conexão e a consulta só acontecem na primeira
    iteração. O log `Consulta executada` é gravado quando a iteração chega ao fim.
  - `table_exists` não diferencia maiúsculas e inclui views. Sem `schema`, usa
    `current_schema()` no PostgreSQL e no DuckDB e `main` no SQLite. No SQLite, `schema` é o
    banco anexado (`main`, `temp`).
  - No DuckDB, `options` é repassado como `config` de `duckdb.connect`. O banco em memória não
    pode ser aberto com `read_only=True` (limitação do DuckDB), por isso conta só com a validação
    de instruções. Ele não tem dados próprios, e `COPY`/`ATTACH` são bloqueados.
  - A configuração fica em `Database.config`. Os campos obrigatórios de cada engine ficam no
    atributo privado `_required_fields`, ponto de extensão para novas engines junto com
    `validate_config`. `register_engine` rejeita nome vazio (`ValueError`) e classe que não seja
    subclasse de `Database` (`TypeError`).
  - A conexão aberta é registrada em `INFO`
    (`Conexão com banco de dados aberta | engine=<e> | destino=<host:porta/banco ou arquivo>`),
    sem segredos.
- **`email_utils`:**
  - `search` (IMAP) ordena pelo UID decrescente, que é a ordem de chegada na pasta, e baixa as
    mensagens uma a uma até atingir `limit`. `mark_as_read` e `move` selecionam a pasta em que o
    e-mail foi encontrado. Quando ela não é conhecida, usam a pasta selecionada e, sem nenhuma,
    lançam `EmailError`. O cliente IMAP conecta automaticamente no primeiro uso.
  - Os nomes de pasta IMAP são enviados em UTF-7 modificado (RFC 3501) e entre aspas quando
    necessário. `"INBOX"` continua sendo enviado como `select("INBOX", readonly=False)`.
  - São considerados anexos as partes com nome de arquivo ou `Content-Disposition: attachment`
    em qualquer nível. Isso inclui PDFs marcados como `inline` por alguns clientes. Partes sem
    nome recebem `anexo_<n><extensão>`. Falha ao gravar anexos gera `FileOperationError`.
  - No SMTP, `bcc` vai apenas no envelope. A mensagem recebe `Date` e `Message-ID`, este com o
    domínio do remetente para evitar a resolução lenta de `getfqdn()` no Windows. A validação
    também verifica `from_addr`, `reply_to` e o remetente resolvido. Um único endereço em texto
    é aceito como lista. Cabeçalho com quebra de linha gera `EmailError`.
  - No Outlook, a validação usa o mesmo limite de 25 MB do SMTP. `from_addr` vira
    `SentOnBehalfOfName`, `reply_to` vira `ReplyRecipients` e `account` (endereço SMTP ou nome
    de exibição) vira `SendUsingAccount` e define a Caixa de Entrada lida. Subpastas são
    relativas à Caixa de Entrada e aceitam níveis (`"Boletos/2026"`). A varredura para no
    primeiro item mais antigo que `since`. Os anexos são lidos com `SaveAsFile` em pasta
    temporária, para que `payload` venha preenchido como no IMAP. `body_html` só é preenchido
    quando `BodyFormat` é HTML. Endereços Exchange são resolvidos para SMTP.
  - `render_template` lê como arquivo um caminho (`os.PathLike`) ou um texto de uma linha
    terminado em `.html`/`.htm`/`.txt` que aponte para arquivo existente. O arquivo é lido com
    `filesystem_utils.read_text`, que tem fallback para cp1252.
- **`images_utils`:**
  - `resize` com largura e altura e `keep_aspect=True` encaixa a imagem na caixa sem distorção.
    Com `keep_aspect=False` e uma só dimensão, a outra é mantida.
  - `crop` exige área não vazia dentro da imagem (`ValueError`). `to_grayscale` preserva a
    transparência (`LA`). `add_text` converte para RGB ou, com transparência, RGBA.
  - `images_to_pdf` coloca a transparência sobre fundo branco e aceita uma única imagem.
    `to_base64` aceita `JPG`/`TIF` e rejeita formato sem gravação (`ValueError`). `from_base64`
    aceita prefixo `data:`. `save_image` valida `quality` entre 1 e 100, e extensão sem suporte
    gera `ImageProcessingError`.
- **`pdf_utils`:**
  - `pypdf`, `pdfplumber` e `reportlab` são importados dentro das funções (RNF-004).
  - PDF cifrado com senha de usuário vazia (só restrições de permissão) abre sem senha. Arquivo
    inexistente gera `PDFError`, como os demais erros do módulo. `search_text` não tem
    `password` no contrato: PDFs com senha de abertura devem passar antes por `decrypt_pdf`.
  - Os PDFs são gravados em `<destino>.<uuid>.tmp` e renomeados com `os.replace`, então nunca
    fica arquivo parcial. Os nomes gerados por `split_pdf` passam por `safe_filename`, e
    `ranges=[]` gera `ValueError`.
  - `get_metadata` remove a `/` inicial das chaves. Os PDFs criados recebem `Author=rpa_3003` e
    o título informado.
  - `add_watermark` gera um carimbo por tamanho de página, com o texto centralizado e a
    opacidade via `ExtGState`. `fill_form` só atualiza páginas com anotações (`/Annots`), para
    evitar avisos do pypdf.
  - `create_pdf_table` usa `Paragraph` em cada célula (quebra de texto) e colunas de mesma
    largura. `None` vira célula vazia.
- **Testes:** a fixture `sample_pdf` (seção 12.5) entrou no `conftest.py`. Os testes de fonte
  TrueType usam a `Vera.ttf` distribuída com o reportlab, para não depender das fontes do
  Windows. Expressões regulares de `database_utils` e `email_utils` foram escritas sem `\` entre
  caracteres de nome (`(?!\d)\w` em vez de `[^\W\d]`), para não gerar falso positivo no
  verificador do RNF-016.

## Sprint 4 — Bots

As regras de `tests/unit/test_architecture.py` não mudaram: a seção 7.2 já permite que `bots`
importe `core` (exceto `workflow`) e `utils`. Um caso novo verifica que importar
`bots.desktop_bot` carrega só o numpy (sem cv2, mss, pyautogui, pywinauto, pyperclip e psutil)
e que acessar `rpa_3003.bots.Browser` carrega o selenium sem as bibliotecas de desktop
(RNF-004, RNF-017).

### D-07 — `ignore_protected_mode_settings` no lugar de `introduce_flakiness_by_ignoring_security_domains`

- **Referência:** RF-029 e comportamento obrigatório de `EdgeIEDriverBuilder` (seção 8.21).
- **Desvio:** o builder do modo IE define `IeOptions.ignore_protected_mode_settings` com o valor
  de `BrowserOptions.ie_ignore_protected_mode_settings`.
- **Justificativa:** o `IeOptions` do Selenium 4 (4.52 no `.venv`) não tem o atributo
  `introduce_flakiness_by_ignoring_security_domains`. Como a classe não usa `__slots__`,
  atribuir esse nome só criaria um atributo Python comum, sem efeito no driver.
  `ignore_protected_mode_settings` é o nome atual da mesma opção e envia a capability
  `ignoreProtectedModeSettings` ao IEDriverServer, que ignora a divergência de Modo Protegido
  entre zonas. O campo de `BrowserOptions` e o contrato público não mudaram.

### D-08 — Teste do modo IE habilitado por `RPA3003_TEST_IE_MODE=1`

- **Referência:** RF-029 (teste de integração em Windows com modo IE), seção 12.4 e DoD da
  Sprint 4 ("teste do modo IE passa em máquina com modo IE configurado").
- **Desvio:** `test_edge_ie_mode_document_mode` (`tests/integration/test_browser_live.py`) só
  roda com a variável `RPA3003_TEST_IE_MODE=1`, que não consta na lista da seção 12.4. Sem ela,
  o teste é pulado com a orientação de configurar a máquina.
- **Justificativa:** o modo IE depende da configuração da máquina (zoom 100%, mesmo Modo
  Protegido em todas as zonas, IEDriverServer 32 bits; risco R-01). Sem essa configuração, o
  IEDriverServer não consegue se anexar ao Edge e o teste fica parado até o tempo limite, em vez
  de falhar com uma mensagem útil. Assim, `pytest -m browser` continua verificando Chrome e Edge
  em qualquer máquina de desenvolvimento.
- **Resultado na máquina de desenvolvimento (01/11/2026):** o teste não passou. O Selenium
  Manager baixou o IEDriverServer 4.14.0 de 32 bits, e o Edge abriu com `--ie-mode-force`. O log
  do driver (`--log-level=DEBUG`) mostra que as configurações de Modo Protegido foram aceitas,
  mas nenhuma janela do Internet Explorer (`IWebBrowser2`) apareceu no processo do Edge
  ("Could not find an Internet Explorer window belonging to the process"). Isso indica que a
  integração com o modo IE não está habilitada nesta máquina (configuração ou política do Edge).
  O item do DoD sobre o modo IE continua pendente até a execução em uma máquina configurada,
  cujo resultado deve ser registrado aqui.
- **Limitação conhecida:** depois de não encontrar a janela, o IEDriverServer 4.14 tenta outra
  estratégia (`IShellWindows`) sem prazo, e a criação da sessão fica bloqueada. O Selenium 4.52
  não tem forma pública de limitar o tempo dessa chamada. O framework define
  `browser_attach_timeout` (próxima seção), que limita só a primeira fase. Em tarefas agendadas,
  a proteção completa é o tempo limite de execução do CLI (`rpa3003 run --timeout`, RF-052), que
  encerra a árvore de processos.

### Decisões de implementação (sem alteração de contrato)

- **`Browser`:**
  - `Browser(options=None)` usa `BrowserOptions.from_settings(get_settings(), browser)`, para
    que `RPA3003_BROWSER_HEADLESS`, `RPA3003_SELENIUM_TIMEOUT`, `RPA3003_PAGE_LOAD_TIMEOUT` e os
    caminhos de driver valham também quando o projeto não monta as opções.
    `screenshot_on_error` continua sendo o parâmetro do construtor (padrão `True`).
  - `from_settings` com navegador registrado por extensão (ex.: `firefox`) deixa `driver_path`
    em `None`. `BrowserFactory.available()` retorna os nomes em ordem alfabética.
  - No modo IE, `headless` é ignorado também em `start()`: a janela é maximizada normalmente, e
    o log registra `headless=False`, que é o estado real.
  - Com `window_size` e `maximized=True` sem headless, o Chrome/Edge recebe `--start-maximized`
    e `--window-size` (leitura literal da seção 8.21). Em seguida, `start()` aplica
    `set_window_size`, então prevalece o tamanho informado.
  - `start()` converte `WebDriverException` e `OSError` em `DriverInitializationError`, com
    `details` `tipo`, `erro` (classe e primeira linha da mensagem, sem o stacktrace do driver) e,
    no modo IE, `dica`. Se a configuração do driver recém-criado falhar, o driver é encerrado
    antes de lançar o erro (RNF-006).
  - A regra "toda `TimeoutException` → `ElementNotFoundError`" foi aplicada às esperas por
    elemento (`find`, `wait_visible`, `wait_clickable`, `switch_to_frame` com localizador), com
    a mensagem `Elemento não encontrado: <locator> em <timeout>s`. `wait_invisible` e
    `wait_url_contains` usam a mesma classe com mensagens próprias (`Elemento continua
    visível...`, `URL não contém...`), porque a mensagem padrão seria enganosa nesses casos.
    `switch_to_window` lança `BrowserError`, como manda a seção 8.21. O tempo limite de
    carregamento em `open()` gera `BrowserError` com `url` e `timeout`, pois não há elemento
    envolvido.
  - Sem URL disponível no erro (ex.: alerta aberto), `details["url"]` recebe `desconhecida`.
  - `exists()` retorna `False` para qualquer erro do Selenium, inclusive seletor inválido. Com o
    navegador não iniciado, ainda lança `BrowserNotStartedError`, que indica erro de programação.
  - `get_text` e `select_option` esperam o elemento visível. `get_attribute` e
    `scroll_into_view` esperam a presença no DOM. Opção inexistente em `select_option` gera
    `ElementNotFoundError`, e elemento que não é `<select>` gera `BrowserError`. Clique
    interceptado com `js_fallback=False` gera `BrowserError`.
  - `type_text(sensitive=True)` registra o segredo antes de esperar o elemento, para que nenhuma
    mensagem posterior o exiba.
  - Rótulo das capturas: caracteres fora de letras, dígitos, `_` e `-` viram `_` (máximo de 80
    caracteres). Rótulo vazio vira `captura` (`Browser`) ou `tela` (`DesktopBot`).
  - `expect_download`: ao entrar no bloco, registra os nomes existentes. Ao sair, procura um
    arquivo novo que case com `pattern`, sem sufixo temporário e sem um arquivo
    `<nome><sufixo temporário>` correspondente. Esse caso cobre o arquivo vazio que o Firefox
    cria com o nome final durante o download. Em seguida, aguarda a estabilidade com
    `filesystem_utils.wait_for_file`, usando o nome exato escapado com `glob.escape`. Se o
    arquivo aparecer perto do fim do prazo, a verificação de estabilidade ainda recebe 1,5 s.
    Exceção dentro do bloco é propagada sem espera.
  - O builder do modo IE define `IeOptions.browser_attach_timeout` como
    `page_load_timeout * 1000` ms. O padrão do IEDriver (`0`) espera para sempre a janela do Edge
    em modo IE (RNF-007).
  - `find_edge_executable`: um caminho explícito inexistente gera `WARNING` e a busca continua
    nos caminhos padrão. A mensagem do erro lista todos os caminhos pesquisados (`nenhum` quando
    não há variável `ProgramFiles*` nem caminho explícito).
- **`DesktopBot`:**
  - `capture_screen` é anotado como `numpy.typing.NDArray[np.uint8]`. É o mesmo
    `numpy.ndarray` do contrato, com o parâmetro de tipo que o `mypy --strict` exige.
  - Sem `region`, as coordenadas somam o canto superior esquerdo da tela virtual
    (`mss.monitors[0]`), que é negativo quando há monitor à esquerda ou acima do principal. Assim
    o `Match` fica em coordenadas absolutas.
  - `find_all_images` aplica a regra de supressão de forma gulosa, por confiança decrescente,
    descartando as posições a menos de metade da largura e da altura de cada match aceito.
  - O OpenCV atribui confiança `1,0` a uma imagem de referência uniforme (uma cor só) em
    qualquer posição. Imagens de referência precisam de textura (texto, bordas, ícones).
  - O log `Imagem encontrada` informa em `posicao` o centro do `Match`, que é o ponto clicado
    por `click_image`.
  - `ImageNotFoundError` traz em `details["imagem"]` o caminho absoluto resolvido. A captura
    `imagem_nao_encontrada` cobre a tela inteira, para mostrar o contexto. Falha ao salvá-la
    vira `WARNING`.
  - O construtor valida `confidence` em (0, 1], `timeout` e `pause` não negativos e `backend`
    `uia`/`win32` (`ValueError`). `poll_interval` deve ser positivo, e `type_text` aceita apenas
    os métodos `keys` e `clipboard`.
  - No método `clipboard`, o conteúdo anterior é restaurado em `finally`, também quando a
    colagem falha. Falhas do pyperclip viram `DesktopAutomationError`.
  - `connect_window` lança `WindowNotFoundError` quando a janela não aparece no prazo e
    `DesktopAutomationError` quando mais de uma janela corresponde ao título: agir na janela
    errada é mais arriscado do que falhar. `wait_window` retorna `False` só no primeiro caso, e
    `window_exists` retorna `True` quando há mais de uma janela.
  - `start_application` registra o comando em `INFO` e converte `AppStartError` em
    `DesktopAutomationError`.
  - `kill_process` nunca encerra o próprio processo do robô (ex.: `kill_process("python.exe")`).
    Um processo que termina entre a espera de 5 s e o `kill()` conta como encerrado.
  - A captura usa `mss.MSS()` quando existe (mss >= 10.2, em que `mss.mss()` ficou obsoleto) e
    `mss.mss()` nas versões anteriores, que o `requirements.txt` ainda aceita (`mss>=9.0`).
- **Testes:** a fixture `http_server` (seção 12.5) entrou no `conftest.py`. Além das cinco
  páginas da seção 12.3, `tests/fixtures/html/relatorio.csv` é o arquivo baixado no teste de
  download. As páginas usam JavaScript ES5, compatível com o modo IE.
- **Teste do Bloco de Notas (`test_desktop_live.py`):**
  - O RF-036 cita `title_re=".*(Bloco de Notas|Notepad).*"`, mas no Windows 11 pt-BR o título
    é "Bloco de **n**otas", e o `title_re` do pywinauto diferencia maiúsculas. O teste usa
    `(?i).*(Bloco de Notas|Notepad).*` para conferir o título.
  - O Bloco de Notas do Windows 11 restaura as abas da sessão anterior, e a aba ativa pode ser
    um arquivo do usuário. Por isso o teste abre um arquivo próprio em `tmp_path`, conecta pela
    janela com esse arquivo ativo e digita só nessa aba. No fim, salva o arquivo (o conteúdo
    gravado confirma a digitação acentuada), fecha a aba com Ctrl+W e encerra o processo com
    `kill_process`. O teste é pulado quando já existe um `notepad.exe` aberto, e a área de
    transferência do usuário é restaurada ao final.
  - Resultado em 01/11/2026 (Windows 11 pt-BR): `pytest -m gui` com 3 testes passando.

## Sprint 5 — CLI

As regras de `tests/unit/test_architecture.py` não mudaram: a seção 7.2 já permite que `cli.py`
importe `core`, `templates`, `_version` e `psutil`, e que `templates/__init__.py` importe `core`
(exceto `workflow` e `vault`) e `_version`. Um caso novo verifica que importar `rpa_3003.cli` e
`rpa_3003.__main__` não carrega `psutil`, `vault`, `bots`, `utils` nem dependências pesadas
(RNF-004). O `psutil` só é importado em `kill_process_tree`. Medido em 01/11/2026:
`rpa3003 --help` em 0,47 s (limite de 1,5 s).

### D-09 — `rpa3003 run` sem `--env` resolve o ambiente e o repassa ao filho

- **Referência:** seção 9.4.2 (passo 2), seção 9.6 e contratos de `build_run_command` e
  `build_child_env` (seção 8.2).
- **Desvio:** sem `--env`, o CLI resolve o ambiente pela mesma precedência do filho (RF-010):
  `RPA3003_ENV` do ambiente, depois o `.env` **do projeto**, depois `dev`. Para isso usa
  `load_settings(env_file=<projeto>/.env)`, como faz `bootstrap()`. O valor resolvido vai ao filho
  em `--env` e em `RPA3003_ENV`. A seção 9.6 diz que `RPA3003_ENV` só é injetada quando `--env` é
  informado.
- **Justificativa:** as assinaturas normativas `build_run_command(paths, env: Environment, ...)`
  (que sempre inclui `--env env.value`) e `build_child_env(env: Environment)` exigem um ambiente.
  Resolver com a mesma precedência do filho cumpre o RF-010 sem mudar os contratos. Assim, o log
  `Executando projeto | ... | ambiente=<env>` mostra o ambiente real. O `.env` do diretório atual
  do CLI continua valendo para `RPA3003_PROJECTS_DIR` e `RPA3003_RUN_TIMEOUT`, mas não para o
  ambiente, porque o filho não o lê.

### D-10 — Nomes de dispositivo do Windows recusados como nome de projeto

- **Referência:** `validate_project_name` (seção 8.23) e RF-048.
- **Desvio:** além de `PROJECT_NAME_PATTERN`, `validate_project_name` recusa `con`, `prn`, `aux`,
  `nul`, `com1` a `com9` e `lpt1` a `lpt9` com `InvalidProjectNameError` (código 2).
- **Justificativa:** esses nomes casam com a expressão regular, mas são nomes reservados de
  dispositivo no Windows (RNF-002). Pastas com esses nomes não são criadas, ou ficam inacessíveis
  pelas ferramentas comuns. O erro sairia como `OSError` durante a renomeação, sem uma mensagem
  útil.

### Decisões de implementação (sem alteração de contrato)

- **Templates (`templates/__init__.py`):**
  - O nome do template é o nome da pasta. A chave `name` do `template.toml` precisa ser igual a
    ele; se for diferente, `TemplateRenderError`. Com isso, `--template web_basico` sempre aponta
    para `templates/web_basico/`.
  - As quatro chaves (`name`, `description`, `version`, `ensure_dirs`) são obrigatórias. Os textos
    não podem ser vazios. Cada entrada de `ensure_dirs` deve ser um caminho relativo no formato
    POSIX, sem `..`, drive nem `\`, para que um template não crie pastas fora do projeto.
  - `get_template` lê só o `template.toml` do template pedido. Um template quebrado não impede o
    uso dos outros, mas faz `list_templates` (e `rpa3003 templates`) falhar com o nome dele.
  - A renderização lê e grava bytes. Os `.tpl` são decodificados em UTF-8, e as quebras de linha
    do arquivo-fonte são preservadas (CRLF ou LF), sem a conversão do modo texto. Um `.tpl` fora
    de UTF-8 gera `TemplateRenderError`.
  - O prefixo `dot_` também vale para pastas (`dot_config/` vira `.config/`). Nome de saída vazio,
    `.` ou `..` (ex.: arquivo `dot_`) e dois arquivos com o mesmo destino (ex.: `a.txt` e
    `a.txt.tpl`) geram `TemplateRenderError`.
  - Marcador `$` inválido (`ValueError` do `substitute`) gera `TemplateRenderError` com `arquivo`,
    `variavel` (o trecho a partir do `$`) e `linha`.
  - Um `.gitkeep` que já vem do template não é sobrescrito.
  - Em `build_context`, o autor passa por uma limpeza: caracteres não imprimíveis viram espaço,
    `\` vira `/` e `"` vira `'`. Ele é inserido em docstrings dos `.py` gerados, e um autor como
    `DOMINIO\usuario` geraria `\u...`, um erro de sintaxe. Autor vazio usa `default_user()`.
  - Em `create_project`, com `--force` o projeto existente só é renomeado para backup **depois**
    que a renderização na pasta temporária termina. Se a renderização falhar, o projeto antigo
    fica intacto. Se a renomeação final falhar, o backup volta ao nome original; se isso também
    falhar, o framework registra um `WARNING`. A seção 8.23 lista o backup antes da renderização,
    mas essa ordem deixaria o projeto fora do lugar após uma falha.
  - Falhas de leitura, gravação ou renomeação viram `TemplateRenderError`, e a falha ao criar a
    pasta de projetos vira `ConfigurationError` (código 3 nos dois casos). `KeyboardInterrupt`
    também remove a pasta temporária antes de propagar.
  - Logs adicionais: `DEBUG` `Template renderizado | template=... | destino=... | arquivos=...` e
    `INFO` `Projeto existente renomeado | nome=... | backup=...`.
- **CLI (`cli.py`):**
  - A ajuda e os erros de uso estão em pt-BR. O framework usa uma subclasse privada de
    `ArgumentParser`, herdada pelos subcomandos, com `uso:`, `argumentos`, `opções`, `comandos`,
    `-h/--help` e `rpa3003: erro: ...` (código 2). Os metavars seguem a seção 9 (`CAMINHO`, `NOME`,
    `USUARIO`, `NIVEL`, `SEGUNDOS`). As frases internas do argparse (ex.: "invalid choice",
    "the following arguments are required") continuam em inglês, porque a biblioteca padrão não
    traz tradução.
  - `LOG_LEVELS` é público, como na estrutura de referência da seção 9.7.
  - `RPAError` capturado em `main` vira log `ERROR` com `str(exc)`, que inclui os `details` (ex.:
    `disponiveis=[...]` em projeto ou template inexistente).
  - Sem subcomando, a ajuda vai para `stderr` (é erro de uso, código 2). Os problemas de estrutura
    em `run` e `create` vão para `stderr`. Em `validate`, a lista de problemas é o resultado do
    comando e vai para `stdout`.
  - `--timeout` menor ou igual a zero registra `ERROR` e retorna 2, porque nenhuma exceção do
    framework tem código 2 com esse significado.
  - `create` com `--projects-dir` repete a opção no passo 3 do resumo (`Execute: rpa3003 run
    <nome> --env dev --projects-dir "<caminho>"`), para que o comando sugerido funcione.
  - Argumentos após `--` só são usados por `run`. Nos demais comandos, são ignorados.
  - No Ctrl+C, `kill_process_tree` só é chamado se o filho continuar ativo depois da espera de
    `CHILD_WAIT_ON_INTERRUPT`. Se o filho já terminou, os netos dele não podem mais ser
    localizados pela árvore. Além disso, o PID de um processo já coletado pode ser reutilizado por
    outro processo, que seria encerrado por engano. Um segundo Ctrl+C durante a espera não
    interrompe o encerramento.
  - `kill_process_tree` termina primeiro os descendentes e depois a raiz, espera
    `grace_seconds`, chama `kill()` nos remanescentes e espera de novo. Processos que continuam
    ativos ou sem permissão geram `WARNING`.
  - Falha ao iniciar o subprocesso (`OSError` do `Popen`) vira `WorkflowError` (código 1).
  - Log `Executando projeto | ... | timeout=sem limite` quando não há tempo limite (ou
    `timeout=<n>s`). O tempo limite excedido registra `Tempo limite excedido | timeout=<n>s |
    pid=<pid>`.
- **Template `example` antes da Sprint 6:** os `.tpl` ainda só têm docstrings, então
  `rpa3003 create` gera os arquivos e retorna 3, porque `validate` não encontra `class Step` nos
  steps (passo 4 da seção 9.4.1). Os testes da Sprint 5 usam templates mínimos em `tmp_path`.
- **Testes:**
  - `test_templates.py` e `test_cli.py` substituem `rpa_3003.templates.templates_root` por uma
    pasta em `tmp_path` (fixture `template_root`). Os testes de `run` simulam
    `subprocess.Popen`.
  - O critério do RF-052 roda de verdade em `test_run_timeout_kills_real_project_tree`. Um
    projeto com `bootstrap`/`WorkflowRunner` tem um step que inicia um processo neto e dorme 60 s.
    Com `--timeout 2`, o CLI retorna 5 em até 10 s, e o `psutil` confirma que o `main.py` e o neto
    foram encerrados. No `.venv` do Windows, `python.exe` é um lançador que cria o interpretador
    como processo filho, então a árvore real tem um nível a mais por processo Python, e
    `kill_process_tree` a encerra inteira.

"""Testes de `rpa_3003.core.config` (RF-001 a RF-003, RF-010, RF-013, RNF-008, seção 8.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rpa_3003.core.config import (
    PROJECT_LOCK_FILENAME,
    REQUIRED_PROJECT_ITEMS,
    STEP_MODULES,
    Environment,
    FrameworkSettings,
    ProjectPaths,
    VaultAuthMethod,
    VaultSettings,
    find_project,
    get_settings,
    list_projects,
    load_settings,
    load_vault_settings,
    reset_settings_cache,
    resolve_environment,
    resolve_projects_dir,
    validate_project_structure,
)
from rpa_3003.core.exceptions import (
    ConfigurationError,
    ExitCode,
    InvalidEnvironmentError,
    ProjectNotFoundError,
)

pytestmark = pytest.mark.unit

STEP_SOURCE = '''"""Step de teste."""

from rpa_3003.core.workflow import BaseStep


class Step(BaseStep):
    def run(self) -> None:
        self.log.info("ok")
'''


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Executa cada teste em uma pasta vazia, sem `.env` do workspace."""
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)


def make_project(root: Path) -> Path:
    """Cria em `root` a estrutura obrigatória mínima e válida do RF-001."""
    for item in REQUIRED_PROJECT_ITEMS:
        path = root / item
        if item.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        is_step = path.name.startswith("step_")
        path.write_text(STEP_SOURCE if is_step else '"""Módulo."""\n', encoding="utf-8")
    return root


# --- RF-001 ------------------------------------------------------------------------------------


def test_required_project_items() -> None:
    """RF-001: REQUIRED_PROJECT_ITEMS é tuple[str, ...] com 13 entradas na ordem do RF-001."""
    assert isinstance(REQUIRED_PROJECT_ITEMS, tuple)
    assert all(isinstance(item, str) for item in REQUIRED_PROJECT_ITEMS)
    assert REQUIRED_PROJECT_ITEMS == (
        "main.py",
        "global_vars.py",
        "logs/",
        "data/",
        "workflows/__init__.py",
        "workflows/dev/__init__.py",
        "workflows/dev/step_setup_env.py",
        "workflows/dev/step_execute_task.py",
        "workflows/dev/step_wrapup_execution.py",
        "workflows/prd/__init__.py",
        "workflows/prd/step_setup_env.py",
        "workflows/prd/step_execute_task.py",
        "workflows/prd/step_wrapup_execution.py",
    )
    assert STEP_MODULES == ("step_setup_env", "step_execute_task", "step_wrapup_execution")
    assert PROJECT_LOCK_FILENAME == ".rpa3003.lock"


# --- RF-010: ambiente --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("dev", Environment.DEV),
        ("prd", Environment.PRD),
        (" PRD ", Environment.PRD),
        ("Dev\n", Environment.DEV),
        (Environment.PRD, Environment.PRD),
    ],
)
def test_environment_from_value(value: str | Environment, expected: Environment) -> None:
    """RF-010: valores aceitos sem diferenciar maiúsculas e ignorando espaços."""
    assert Environment.from_value(value) is expected


@pytest.mark.parametrize("value", ["hml", "", "produção", "dev prd"])
def test_environment_from_invalid_value(value: str) -> None:
    """RF-010: valor inválido gera InvalidEnvironmentError (código 2) listando 'dev, prd'."""
    with pytest.raises(InvalidEnvironmentError) as info:
        Environment.from_value(value)
    assert info.value.exit_code is ExitCode.USAGE_ERROR
    assert "dev, prd" in str(info.value)


def test_environment_values() -> None:
    """RF-010: Environment é StrEnum com dev e prd."""
    assert [member.value for member in Environment] == ["dev", "prd"]
    assert f"{Environment.PRD}" == "prd"


def test_cli_env_overrides_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-010: RPA3003_ENV=prd e --env dev resultam em dev."""
    monkeypatch.setenv("RPA3003_ENV", "prd")
    assert resolve_environment("dev", load_settings()) is Environment.DEV


def test_environment_variable_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-010: apenas RPA3003_ENV=PRD resulta em prd."""
    monkeypatch.setenv("RPA3003_ENV", "PRD")
    assert resolve_environment(None, load_settings()) is Environment.PRD


def test_environment_defaults_to_dev() -> None:
    """RF-010 e P-05: sem valor informado, o ambiente é dev."""
    assert resolve_environment(None, load_settings()) is Environment.DEV


def test_environment_from_dotenv_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-010: .env do projeto vale quando não há --env nem RPA3003_ENV; a variável o supera."""
    env_file = tmp_path / ".env"
    env_file.write_text("RPA3003_ENV=prd\n", encoding="utf-8")
    assert resolve_environment(None, load_settings(env_file=env_file)) is Environment.PRD
    monkeypatch.setenv("RPA3003_ENV", "dev")
    assert resolve_environment(None, load_settings(env_file=env_file)) is Environment.DEV


def test_invalid_cli_env_is_usage_error() -> None:
    """RF-010: --env hml gera código 2 e a mensagem lista dev, prd."""
    with pytest.raises(InvalidEnvironmentError, match="dev, prd") as info:
        resolve_environment("hml", load_settings())
    assert int(info.value.exit_code) == 2


def test_invalid_env_variable_is_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-010: RPA3003_ENV inválido também gera InvalidEnvironmentError."""
    monkeypatch.setenv("RPA3003_ENV", "hml")
    with pytest.raises(InvalidEnvironmentError, match="dev, prd"):
        load_settings()


# --- RF-013: FrameworkSettings -----------------------------------------------------------------


def test_log_level_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-013: RPA3003_LOG_LEVEL=DEBUG resulta em get_settings().log_level == 'DEBUG'."""
    monkeypatch.setenv("RPA3003_LOG_LEVEL", "DEBUG")
    assert get_settings().log_level == "DEBUG"


def test_log_level_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-013: o nível de log aceita minúsculas e espaços."""
    monkeypatch.setenv("RPA3003_LOG_LEVEL", " warning ")
    assert load_settings().log_level == "WARNING"


def test_invalid_retention_raises_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-013: RPA3003_LOG_RETENTION_DAYS=0 lança ConfigurationError citando o campo."""
    monkeypatch.setenv("RPA3003_LOG_RETENTION_DAYS", "0")
    with pytest.raises(ConfigurationError, match="log_retention_days") as info:
        load_settings()
    assert info.value.details == {"campos": ["log_retention_days"]}
    assert info.value.exit_code is ExitCode.CONFIGURATION_ERROR
    assert isinstance(info.value.__cause__, Exception)


def test_multiple_invalid_fields_are_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-013: todos os campos inválidos aparecem em details['campos']."""
    monkeypatch.setenv("RPA3003_LOG_LEVEL", "VERBOSO")
    monkeypatch.setenv("RPA3003_DESKTOP_CONFIDENCE", "2")
    with pytest.raises(ConfigurationError) as info:
        load_settings()
    assert info.value.details["campos"] == ["log_level", "desktop_confidence"]


def test_get_settings_is_cached() -> None:
    """RF-013: duas chamadas a get_settings() retornam o mesmo objeto; após reset, um novo."""
    first = get_settings()
    assert get_settings() is first
    reset_settings_cache()
    assert get_settings() is not first


def test_settings_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-013: argumentos explícitos → variáveis de ambiente → .env → padrão."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "RPA3003_LOG_USER=do_arquivo\nRPA3003_SELENIUM_TIMEOUT=45\nRPA3003_LOG_LEVEL=ERROR\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RPA3003_LOG_USER", "da_variavel")
    monkeypatch.setenv("RPA3003_LOG_LEVEL", "WARNING")
    settings = load_settings(env_file=env_file, log_level="DEBUG")
    assert settings.log_level == "DEBUG"
    assert settings.log_user == "da_variavel"
    assert settings.selenium_timeout == 45
    assert settings.page_load_timeout == 60


def test_dotenv_in_current_directory_is_read() -> None:
    """RF-013: sem env_file, o `.env` do diretório atual é lido."""
    Path(".env").write_text("RPA3003_LOG_USER=robo_cwd\n", encoding="utf-8")
    assert load_settings().log_user == "robo_cwd"


def test_framework_settings_defaults() -> None:
    """Seção 8.5: valores padrão de FrameworkSettings."""
    settings = load_settings()
    assert isinstance(settings, FrameworkSettings)
    assert settings.model_dump() == {
        "env": Environment.DEV,
        "log_level": "INFO",
        "log_user": None,
        "log_retention_days": 30,
        "log_compression": False,
        "log_console": True,
        "projects_dir": Path("projects"),
        "run_timeout": None,
        "default_browser": "chrome",
        "browser_headless": False,
        "selenium_timeout": 30,
        "page_load_timeout": 60,
        "chromedriver_path": None,
        "edgedriver_path": None,
        "iedriver_path": None,
        "edge_binary_path": None,
        "screenshot_on_error": True,
        "desktop_failsafe": True,
        "desktop_confidence": 0.9,
    }


@pytest.mark.parametrize(
    ("variable", "raw", "field", "expected"),
    [
        ("RPA3003_ENV", "prd", "env", Environment.PRD),
        ("RPA3003_LOG_USER", "robo01", "log_user", "robo01"),
        ("RPA3003_LOG_RETENTION_DAYS", "90", "log_retention_days", 90),
        ("RPA3003_LOG_COMPRESSION", "true", "log_compression", True),
        ("RPA3003_LOG_CONSOLE", "false", "log_console", False),
        ("RPA3003_PROJECTS_DIR", "C:/rpa/projetos", "projects_dir", Path("C:/rpa/projetos")),
        ("RPA3003_RUN_TIMEOUT", "3600", "run_timeout", 3600),
        ("RPA3003_DEFAULT_BROWSER", "edge_ie", "default_browser", "edge_ie"),
        ("RPA3003_BROWSER_HEADLESS", "1", "browser_headless", True),
        ("RPA3003_SELENIUM_TIMEOUT", "15", "selenium_timeout", 15),
        ("RPA3003_PAGE_LOAD_TIMEOUT", "120", "page_load_timeout", 120),
        ("RPA3003_CHROMEDRIVER_PATH", "C:/d/chromedriver.exe", "chromedriver_path", None),
        ("RPA3003_EDGEDRIVER_PATH", "C:/d/msedgedriver.exe", "edgedriver_path", None),
        ("RPA3003_IEDRIVER_PATH", "C:/d/IEDriverServer.exe", "iedriver_path", None),
        ("RPA3003_EDGE_BINARY_PATH", "C:/e/msedge.exe", "edge_binary_path", None),
        ("RPA3003_SCREENSHOT_ON_ERROR", "false", "screenshot_on_error", False),
        ("RPA3003_DESKTOP_FAILSAFE", "false", "desktop_failsafe", False),
        ("RPA3003_DESKTOP_CONFIDENCE", "0.75", "desktop_confidence", 0.75),
    ],
)
def test_framework_settings_environment_variables(
    variable: str, raw: str, field: str, expected: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF-013: cada variável da tabela da seção 8.5 alimenta o campo correspondente."""
    monkeypatch.setenv(variable, raw)
    value = getattr(load_settings(), field)
    assert value == (Path(raw) if expected is None else expected)


@pytest.mark.parametrize(
    ("variable", "raw", "field"),
    [
        ("RPA3003_RUN_TIMEOUT", "0", "run_timeout"),
        ("RPA3003_LOG_RETENTION_DAYS", "3651", "log_retention_days"),
        ("RPA3003_DESKTOP_CONFIDENCE", "0.4", "desktop_confidence"),
        ("RPA3003_SELENIUM_TIMEOUT", "abc", "selenium_timeout"),
        ("RPA3003_LOG_COMPRESSION", "talvez", "log_compression"),
    ],
)
def test_framework_settings_invalid_values(
    variable: str, raw: str, field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF-013: valores fora dos limites geram ConfigurationError com o nome do campo."""
    monkeypatch.setenv(variable, raw)
    with pytest.raises(ConfigurationError, match=field):
        load_settings()


# --- VaultSettings -----------------------------------------------------------------------------


def test_vault_settings_defaults() -> None:
    """Seção 8.5: padrões de VaultSettings (TLS verificado, KV v2, AppRole)."""
    settings = load_vault_settings()
    assert isinstance(settings, VaultSettings)
    assert settings.url is None
    assert settings.auth_method is VaultAuthMethod.APPROLE
    assert settings.mount_point == "secret"
    assert settings.kv_version == 2
    assert settings.verify is True
    assert (settings.timeout, settings.max_retries, settings.backoff_base) == (10, 3, 1.0)
    assert settings.cache_ttl == 300
    assert settings.offline_cache is False
    assert settings.offline_cache_ttl_hours == 24
    assert settings.local_secrets_file is None


def test_vault_settings_prefixed_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-013: variáveis RPA3003_VAULT_* alimentam VaultSettings."""
    values = {
        "RPA3003_VAULT_URL": "https://vault.local:8200",
        "RPA3003_VAULT_AUTH_METHOD": "LDAP",
        "RPA3003_VAULT_TOKEN": "s.token-teste",
        "RPA3003_VAULT_ROLE_ID": "role-1",
        "RPA3003_VAULT_SECRET_ID": "secret-1",
        "RPA3003_VAULT_USERNAME": "svc",
        "RPA3003_VAULT_PASSWORD": "senha-ldap",
        "RPA3003_VAULT_NAMESPACE": "fin",
        "RPA3003_VAULT_MOUNT_POINT": "kv",
        "RPA3003_VAULT_KV_VERSION": "1",
        "RPA3003_VAULT_VERIFY": "false",
        "RPA3003_VAULT_TIMEOUT": "5",
        "RPA3003_VAULT_MAX_RETRIES": "4",
        "RPA3003_VAULT_BACKOFF_BASE": "0.5",
        "RPA3003_VAULT_CACHE_TTL": "0",
        "RPA3003_VAULT_OFFLINE_CACHE": "true",
        "RPA3003_VAULT_OFFLINE_CACHE_TTL_HOURS": "12",
        "RPA3003_VAULT_LOCAL_SECRETS_FILE": "C:/segredos/x.secrets.json",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = load_vault_settings()
    assert settings.url == "https://vault.local:8200"
    assert settings.auth_method is VaultAuthMethod.LDAP
    assert settings.token is not None and settings.token.get_secret_value() == "s.token-teste"
    assert settings.role_id == "role-1"
    assert settings.secret_id is not None and settings.secret_id.get_secret_value() == "secret-1"
    assert settings.username == "svc"
    assert settings.password is not None and settings.password.get_secret_value() == "senha-ldap"
    assert settings.namespace == "fin"
    assert settings.mount_point == "kv"
    assert settings.kv_version == 1
    assert settings.verify is False
    assert (settings.timeout, settings.max_retries, settings.backoff_base) == (5, 4, 0.5)
    assert settings.cache_ttl == 0
    assert settings.offline_cache is True
    assert settings.offline_cache_ttl_hours == 12
    assert settings.local_secrets_file == Path("C:/segredos/x.secrets.json")


def test_vault_settings_standard_aliases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Seção 8.5: VAULT_ADDR, VAULT_TOKEN, VAULT_NAMESPACE e VAULT_CACERT são aceitas."""
    ca_bundle = tmp_path / "ca.pem"
    monkeypatch.setenv("VAULT_ADDR", "https://vault.alias:8200")
    monkeypatch.setenv("VAULT_TOKEN", "s.alias")
    monkeypatch.setenv("VAULT_NAMESPACE", "ns-alias")
    monkeypatch.setenv("VAULT_CACERT", str(ca_bundle))
    settings = load_vault_settings()
    assert settings.url == "https://vault.alias:8200"
    assert settings.token is not None and settings.token.get_secret_value() == "s.alias"
    assert settings.namespace == "ns-alias"
    assert settings.verify == ca_bundle


def test_vault_prefixed_variable_wins_over_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seção 8.5: RPA3003_VAULT_URL tem precedência sobre VAULT_ADDR."""
    monkeypatch.setenv("VAULT_ADDR", "https://alias")
    monkeypatch.setenv("RPA3003_VAULT_URL", "https://prefixada")
    assert load_vault_settings().url == "https://prefixada"


def test_vault_settings_overrides_by_field_name() -> None:
    """Seção 8.5: populate_by_name permite sobrescrever pelo nome do campo."""
    settings = load_vault_settings(url="https://explicita", verify=False, auth_method="token")
    assert settings.url == "https://explicita"
    assert settings.verify is False
    assert settings.auth_method is VaultAuthMethod.TOKEN


def test_vault_settings_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-013: valor inválido em VaultSettings gera ConfigurationError com o campo."""
    monkeypatch.setenv("RPA3003_VAULT_KV_VERSION", "3")
    with pytest.raises(ConfigurationError, match="kv_version"):
        load_vault_settings()


def test_vault_settings_repr_hides_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """RNF-008: repr() de VaultSettings não exibe token, secret_id nem senha."""
    monkeypatch.setenv("RPA3003_VAULT_TOKEN", "s.token-muito-secreto")
    monkeypatch.setenv("RPA3003_VAULT_SECRET_ID", "secret-id-muito-secreto")
    monkeypatch.setenv("RPA3003_VAULT_PASSWORD", "senha-muito-secreta")
    text = repr(load_vault_settings()) + str(load_vault_settings())
    assert "muito-secret" not in text


# --- RF-002: ProjectPaths ----------------------------------------------------------------------


def test_project_paths_with_space_are_absolute() -> None:
    """RF-002: Path('projects/meu projeto') resolve atributos absolutos e screenshots em logs."""
    paths = ProjectPaths.from_root(Path("projects/meu projeto"))
    expected_root = (Path.cwd() / "projects" / "meu projeto").resolve()
    assert paths.name == "meu projeto"
    assert paths.root == expected_root
    for value in (
        paths.root,
        paths.workflows_dir,
        paths.logs_dir,
        paths.screenshots_dir,
        paths.data_dir,
        paths.main_file,
        paths.global_vars_file,
        paths.lock_file,
    ):
        assert isinstance(value, Path) and value.is_absolute()
    assert paths.screenshots_dir == paths.logs_dir / "screenshots"
    assert paths.workflows_dir == expected_root / "workflows"
    assert paths.data_dir == expected_root / "data"
    assert paths.main_file == expected_root / "main.py"
    assert paths.global_vars_file == expected_root / "global_vars.py"
    assert paths.lock_file == expected_root / ".rpa3003.lock"


def test_project_paths_accept_string_with_accents(tmp_path: Path) -> None:
    """RF-002 e RNF-002: caminho em texto com acentos é aceito."""
    paths = ProjectPaths.from_root(str(tmp_path / "Automação RPA" / "conciliação"))
    assert paths.name == "conciliação"
    assert paths.root == (tmp_path / "Automação RPA" / "conciliação").resolve()


def test_ensure_runtime_dirs_is_idempotent(tmp_path: Path) -> None:
    """RF-002: ensure_runtime_dirs() duas vezes cria logs/, screenshots/ e data/ sem erro."""
    paths = ProjectPaths.from_root(tmp_path / "projeto")
    paths.ensure_runtime_dirs()
    paths.ensure_runtime_dirs()
    assert paths.logs_dir.is_dir()
    assert paths.screenshots_dir.is_dir()
    assert paths.data_dir.is_dir()


def test_workflow_env_dir(tmp_path: Path) -> None:
    """RF-011: workflow_env_dir retorna workflows/<env>/."""
    paths = ProjectPaths.from_root(tmp_path)
    assert paths.workflow_env_dir(Environment.DEV) == paths.workflows_dir / "dev"
    assert paths.workflow_env_dir(Environment.PRD) == paths.workflows_dir / "prd"


def test_project_paths_is_frozen(tmp_path: Path) -> None:
    """Seção 8.5: ProjectPaths é um value object imutável."""
    paths = ProjectPaths.from_root(tmp_path)
    with pytest.raises(AttributeError):
        paths.name = "outro"  # type: ignore[misc]


# --- RF-003: validate_project_structure --------------------------------------------------------


def test_valid_project_has_no_problems(tmp_path: Path) -> None:
    """RF-003: projeto completo e válido retorna []."""
    assert validate_project_structure(make_project(tmp_path / "ok")) == []


def test_missing_item_is_reported(tmp_path: Path) -> None:
    """RF-003: sem workflows/prd/step_execute_task.py, o retorno contém 'ausente: ...'."""
    root = make_project(tmp_path / "p")
    (root / "workflows/prd/step_execute_task.py").unlink()
    assert validate_project_structure(root) == ["ausente: workflows/prd/step_execute_task.py"]


def test_missing_items_follow_required_order(tmp_path: Path) -> None:
    """RF-003: itens ausentes aparecem na ordem de REQUIRED_PROJECT_ITEMS."""
    root = make_project(tmp_path / "p")
    (root / "main.py").unlink()
    (root / "data").rmdir()
    (root / "logs").rmdir()
    (root / "logs").write_text("não é pasta", encoding="utf-8")
    assert validate_project_structure(root) == [
        "ausente: main.py",
        "ausente: logs/",
        "ausente: data/",
    ]


def test_empty_directory_reports_all_items(tmp_path: Path) -> None:
    """RF-003: pasta vazia reporta os 13 itens obrigatórios."""
    assert validate_project_structure(tmp_path) == [
        f"ausente: {item}" for item in REQUIRED_PROJECT_ITEMS
    ]


def test_syntax_error_is_reported_with_line(tmp_path: Path) -> None:
    """RF-003: step com erro de sintaxe na linha 7 gera '<arquivo>: erro de sintaxe na linha 7'."""
    root = make_project(tmp_path / "p")
    source = "\n".join(["# linha"] * 6) + "\ndef quebrado(:\n"
    (root / "workflows/dev/step_setup_env.py").write_text(source, encoding="utf-8")
    assert validate_project_structure(root) == [
        "workflows/dev/step_setup_env.py: erro de sintaxe na linha 7"
    ]


def test_invalid_encoding_is_reported_as_syntax_error(tmp_path: Path) -> None:
    """RF-003: bytes inválidos em UTF-8 são reportados como erro de sintaxe."""
    root = make_project(tmp_path / "p")
    (root / "workflows/dev/step_execute_task.py").write_bytes(b"x = '\xff\xfe'\n")
    problems = validate_project_structure(root)
    assert len(problems) == 1
    assert problems[0].startswith("workflows/dev/step_execute_task.py: erro de sintaxe na linha")


def test_missing_step_class_is_reported(tmp_path: Path) -> None:
    """RF-003: step sem class Step gera '<arquivo>: classe 'Step' não encontrada'."""
    root = make_project(tmp_path / "p")
    (root / "workflows/dev/step_execute_task.py").write_text(
        "class Passo:\n    pass\n\n\ndef f():\n    class Step:\n        pass\n", encoding="utf-8"
    )
    assert validate_project_structure(root) == [
        "workflows/dev/step_execute_task.py: classe 'Step' não encontrada"
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "from workflows.dev import x",
        "from workflows.dev.step_setup_env import Step as DevStep",
        "import workflows.dev",
        "import workflows.dev.step_setup_env as dev_step",
        "from workflows import dev",
        "from ..dev import step_setup_env",
        "from .. import dev",
        "def carregar():\n    import workflows.dev.step_setup_env",
    ],
)
def test_prd_importing_dev_is_reported(tmp_path: Path, statement: str) -> None:
    """RF-003 e RF-011: módulo de prd que importa workflows.dev é reportado."""
    root = make_project(tmp_path / "p")
    target = root / "workflows/prd/step_execute_task.py"
    target.write_text(f"{statement}\n\n{STEP_SOURCE}", encoding="utf-8")
    assert validate_project_structure(root) == [
        "workflows/prd/step_execute_task.py: importação proibida de workflows.dev em prd"
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "from workflows.prd import helpers",
        "import workflows.development",
        "from . import helpers",
        "from rpa_3003.core.config import Environment",
    ],
)
def test_allowed_imports_in_prd(tmp_path: Path, statement: str) -> None:
    """RF-003: imports que não apontam para workflows.dev são aceitos em prd."""
    root = make_project(tmp_path / "p")
    target = root / "workflows/prd/step_execute_task.py"
    target.write_text(f"{statement}\n\n{STEP_SOURCE}", encoding="utf-8")
    assert validate_project_structure(root) == []


def test_dev_importing_dev_is_allowed(tmp_path: Path) -> None:
    """RF-003: a proibição de importar workflows.dev vale apenas para prd."""
    root = make_project(tmp_path / "p")
    target = root / "workflows/dev/step_execute_task.py"
    target.write_text(f"from workflows.dev import helpers\n\n{STEP_SOURCE}", encoding="utf-8")
    assert validate_project_structure(root) == []


def test_prd_helper_module_importing_dev_is_reported(tmp_path: Path) -> None:
    """RF-011: qualquer módulo de prd (não só steps) é verificado contra workflows.dev."""
    root = make_project(tmp_path / "p")
    (root / "workflows/prd/helpers.py").write_text(
        "from workflows.dev import x\n", encoding="utf-8"
    )
    assert validate_project_structure(root) == [
        "workflows/prd/helpers.py: importação proibida de workflows.dev em prd"
    ]


def test_extra_step_modules_are_checked(tmp_path: Path) -> None:
    """RF-003 e P-20: steps adicionais (step_*.py) também precisam definir class Step."""
    root = make_project(tmp_path / "p")
    (root / "workflows/dev/step_notificar.py").write_text("x = 1\n", encoding="utf-8")
    (root / "workflows/dev/helpers.py").write_text("x = 1\n", encoding="utf-8")
    assert validate_project_structure(root) == [
        "workflows/dev/step_notificar.py: classe 'Step' não encontrada"
    ]


def test_problems_order_dev_before_prd(tmp_path: Path) -> None:
    """RF-003: ausentes primeiro; depois steps de dev e de prd na ordem de STEP_MODULES."""
    root = make_project(tmp_path / "p")
    (root / "global_vars.py").unlink()
    (root / "workflows/prd/step_wrapup_execution.py").write_text("x = 1\n", encoding="utf-8")
    (root / "workflows/prd/step_setup_env.py").write_text(
        "import workflows.dev\n", encoding="utf-8"
    )
    (root / "workflows/dev/step_wrapup_execution.py").write_text("def (:\n", encoding="utf-8")
    (root / "workflows/dev/step_setup_env.py").write_text("y = 2\n", encoding="utf-8")
    assert validate_project_structure(root) == [
        "ausente: global_vars.py",
        "workflows/dev/step_setup_env.py: classe 'Step' não encontrada",
        "workflows/dev/step_wrapup_execution.py: erro de sintaxe na linha 1",
        "workflows/prd/step_setup_env.py: classe 'Step' não encontrada",
        "workflows/prd/step_setup_env.py: importação proibida de workflows.dev em prd",
        "workflows/prd/step_wrapup_execution.py: classe 'Step' não encontrada",
    ]


def test_validation_does_not_import_project_code(tmp_path: Path) -> None:
    """RF-003: a verificação é estática; código do step não é executado."""
    root = make_project(tmp_path / "p")
    marker = tmp_path / "executado.txt"
    source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('x')\n\n{STEP_SOURCE}"
    (root / "workflows/dev/step_setup_env.py").write_text(source, encoding="utf-8")
    assert validate_project_structure(root) == []
    assert not marker.exists()


# --- list_projects, find_project e resolve_projects_dir ----------------------------------------


def test_list_projects_ignores_hidden_backup_and_non_projects(tmp_path: Path) -> None:
    """Seção 8.26: lista subpastas com main.py, por nome, ignorando .tmp e .bak."""
    base = tmp_path / "projects"
    for name in ("zeta", "alfa", ".alfa.tmp-1a2b3c4d", "alfa.bak-20261031120000", "sem_main"):
        (base / name).mkdir(parents=True)
    for name in ("zeta", "alfa", ".alfa.tmp-1a2b3c4d", "alfa.bak-20261031120000"):
        (base / name / "main.py").write_text("", encoding="utf-8")
    (base / "arquivo.txt").write_text("", encoding="utf-8")
    projects = list_projects(base)
    assert [paths.name for paths in projects] == ["alfa", "zeta"]
    assert all(isinstance(paths, ProjectPaths) for paths in projects)
    assert projects[0].root == (base / "alfa").resolve()


def test_list_projects_empty_directory(tmp_path: Path) -> None:
    """Seção 9.4.3: pasta sem projetos retorna lista vazia."""
    assert list_projects(tmp_path) == []


def test_list_projects_missing_directory(tmp_path: Path) -> None:
    """Seção 9.4.3: diretório de projetos inexistente gera ConfigurationError (código 3)."""
    with pytest.raises(ConfigurationError, match="Diretório de projetos não encontrado") as info:
        list_projects(tmp_path / "nao_existe")
    assert info.value.exit_code is ExitCode.CONFIGURATION_ERROR


def test_find_project(tmp_path: Path) -> None:
    """Seção 8.5: find_project retorna os caminhos do projeto existente."""
    root = make_project(tmp_path / "projects" / "emissao_notas")
    paths = find_project("emissao_notas", tmp_path / "projects")
    assert paths == ProjectPaths.from_root(root)


def test_find_project_not_found_lists_available(tmp_path: Path) -> None:
    """Seção 8.5: projeto inexistente gera ProjectNotFoundError com os disponíveis."""
    base = tmp_path / "projects"
    make_project(base / "beta")
    make_project(base / "alfa")
    with pytest.raises(ProjectNotFoundError) as info:
        find_project("gama", base)
    assert info.value.details["disponiveis"] == ["alfa", "beta"]
    assert info.value.exit_code is ExitCode.CONFIGURATION_ERROR
    assert "gama" in str(info.value)


@pytest.mark.parametrize(
    "name", ["..", ".", "", "../fora", "alfa/../alfa", ".alfa.tmp-1", "a.bak-1"]
)
def test_find_project_rejects_non_project_names(tmp_path: Path, name: str) -> None:
    """Seção 8.26: nomes com caminho, ocultos ou de backup não são projetos."""
    base = tmp_path / "projects"
    make_project(base / "alfa")
    (base / ".alfa.tmp-1").mkdir()
    (base / "a.bak-1").mkdir()
    with pytest.raises(ProjectNotFoundError):
        find_project(name, base)


def test_find_project_in_missing_directory(tmp_path: Path) -> None:
    """Seção 8.5: diretório de projetos inexistente resulta em lista de disponíveis vazia."""
    with pytest.raises(ProjectNotFoundError) as info:
        find_project("alfa", tmp_path / "nao_existe")
    assert info.value.details["disponiveis"] == []


def test_resolve_projects_dir_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P-02: --projects-dir → RPA3003_PROJECTS_DIR → ./projects, sempre absoluto."""
    assert resolve_projects_dir(None, load_settings()) == (Path.cwd() / "projects").resolve()
    monkeypatch.setenv("RPA3003_PROJECTS_DIR", str(tmp_path / "da_variavel"))
    settings = load_settings()
    assert resolve_projects_dir(None, settings) == (tmp_path / "da_variavel").resolve()
    assert resolve_projects_dir("relativo", settings) == (Path.cwd() / "relativo").resolve()
    assert resolve_projects_dir(tmp_path / "cli", settings) == (tmp_path / "cli").resolve()

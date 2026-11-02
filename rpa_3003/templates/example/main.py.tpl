"""Orquestrador do projeto ${project_name}.

Gerado por rpa_3003 ${framework_version} em ${created_at} (autor: ${author}).

Uso:
    python main.py --env dev
    rpa3003 run ${project_name} --env prd
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from rpa_3003.core.exceptions import RPAError
from rpa_3003.core.workflow import DEFAULT_STEPS, WorkflowRunner, bootstrap, split_passthrough_args

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent
STEPS: Final[tuple[str, ...]] = DEFAULT_STEPS
LOG_LEVELS: Final[tuple[str, ...]] = (
    "TRACE",
    "DEBUG",
    "INFO",
    "SUCCESS",
    "WARNING",
    "ERROR",
    "CRITICAL",
)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Interpreta os argumentos próprios do projeto (antes de '--')."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Executa o projeto ${project_name}.",
    )
    parser.add_argument(
        "--env", default=None, help="Ambiente: dev ou prd (padrão: RPA3003_ENV ou dev)."
    )
    parser.add_argument("--user", default=None, help="Usuário exibido no log.")
    parser.add_argument(
        "--log-level", default=None, choices=LOG_LEVELS, help="Nível mínimo de log."
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    """Executa os steps do workflow do ambiente selecionado e retorna o código de saída."""
    own_args, extra_args = split_passthrough_args(list(sys.argv[1:] if argv is None else argv))
    args = parse_args(own_args)
    try:
        ctx = bootstrap(
            PROJECT_ROOT,
            env=args.env,
            user=args.user,
            log_level=args.log_level,
            extra_args=extra_args,
        )
    except RPAError as exc:
        print(f"[ERRO DE INICIALIZAÇÃO] {exc}", file=sys.stderr)
        return int(exc.exit_code)
    result = WorkflowRunner(ctx, steps=STEPS).run()
    return int(result.exit_code)


if __name__ == "__main__":
    sys.exit(main())

"""Step 3 - encerramento da execução (${project_name} | prd).

Executa sempre, inclusive após falha em steps anteriores.
"""

from __future__ import annotations

from typing import Any

from rpa_3003.core.workflow import BaseStep
from rpa_3003.utils import calendar_utils, filesystem_utils, json_utils

import global_vars as gv


class Step(BaseStep):
    """Grava o status da execução em data/output/status_<run_id>.json."""

    name = "wrapup_execution"
    always_run = True

    def run(self) -> None:
        error = self.ctx.error
        status: dict[str, Any] = {
            "run_id": self.ctx.run_id,
            "projeto": self.ctx.project_name,
            "ambiente": self.ctx.env.value,
            "usuario": self.ctx.user,
            "inicio": self.ctx.started_at.isoformat(),
            "fim": calendar_utils.now().isoformat(),
            "sucesso": error is None,
            "erro": None if error is None else f"{type(error).__name__}: {error}",
            "relatorio": self.ctx.state.get("report"),
        }
        filesystem_utils.ensure_dir(gv.OUTPUT_DIR)
        output = json_utils.write_json(gv.OUTPUT_DIR / f"status_{self.ctx.run_id}.json", status)
        self.log.info("Status gravado | arquivo={} | sucesso={}", output.name, error is None)

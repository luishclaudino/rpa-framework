"""Step 2 - execução da tarefa (${project_name} | prd)."""

from __future__ import annotations

from typing import Any

from rpa_3003.core.workflow import BaseStep
from rpa_3003.utils import csv_utils, filesystem_utils, json_utils

import global_vars as gv


class Step(BaseStep):
    """Processa os CSV de data/input e grava o relatório em data/output."""

    name = "execute_task"

    def run(self) -> None:
        config: gv.EnvConfig = self.ctx.state["config"]
        files = filesystem_utils.list_files(gv.INPUT_DIR, "*.csv")
        total_rows = 0
        details: list[dict[str, Any]] = []
        for file in files:
            rows = csv_utils.read_csv(file)
            total_rows += len(rows)
            details.append({"arquivo": file.name, "linhas": len(rows)})
            self.log.info("Arquivo processado | arquivo={} | linhas={}", file.name, len(rows))

        browser = self.ctx.state.get("browser")
        if browser is not None:
            browser.open(config.base_url)
            self.log.info("Página aberta | titulo={}", browser.title)

        report: dict[str, Any] = {
            "run_id": self.ctx.run_id,
            "ambiente": self.ctx.env.value,
            "arquivos": len(files),
            "linhas": total_rows,
            "detalhes": details,
        }
        output = json_utils.write_json(gv.OUTPUT_DIR / f"relatorio_{self.ctx.run_id}.json", report)
        self.ctx.state["report"] = report
        self.log.success("Relatório gerado | arquivo={} | linhas={}", output.name, total_rows)

    def on_error(self, error: BaseException) -> None:
        browser = self.ctx.state.get("browser")
        if browser is not None and browser.is_running:
            browser.screenshot(label="erro_execute_task")

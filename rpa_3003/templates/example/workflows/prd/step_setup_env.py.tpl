"""Step 1 - preparação do ambiente (${project_name} | prd)."""

from __future__ import annotations

from rpa_3003.core.workflow import BaseStep
from rpa_3003.utils import filesystem_utils

import global_vars as gv


class Step(BaseStep):
    """Cria pastas, carrega configuração e credenciais e inicia recursos."""

    name = "setup_env"

    def run(self) -> None:
        config = gv.get_env_config(self.ctx.env)
        for directory in (gv.INPUT_DIR, gv.OUTPUT_DIR, gv.IMAGES_DIR):
            filesystem_utils.ensure_dir(directory)
        credentials = gv.get_credentials(self.ctx.env)
        self.ctx.state["config"] = config
        self.ctx.state["credentials"] = credentials
        self.log.info(
            "Ambiente preparado | url={} | navegador={} | credenciais={}",
            config.base_url,
            config.browser if config.browser_enabled else "desabilitado",
            "sim" if credentials is not None else "não",
        )
        if config.browser_enabled:
            self._start_browser(config)

    def _start_browser(self, config: gv.EnvConfig) -> None:
        from rpa_3003.bots.browser_selenium import Browser, BrowserOptions

        options = BrowserOptions.from_settings(self.ctx.settings, config.browser)
        options.headless = config.headless
        options.download_dir = gv.OUTPUT_DIR
        browser = Browser(
            config.browser, options, screenshots_dir=self.ctx.paths.screenshots_dir
        ).start()
        self.ctx.state["browser"] = browser
        self.ctx.register_cleanup(browser.quit)

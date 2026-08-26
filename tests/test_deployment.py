from __future__ import annotations

import unittest
from pathlib import Path


class DeploymentConfigurationTest(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    def test_container_binds_public_host_and_keeps_sqlite_on_durable_volume(self) -> None:
        dockerfile = (self.root / "Dockerfile").read_text(encoding="utf-8")
        compose = (self.root / "compose.yml").read_text(encoding="utf-8")
        self.assertIn('VOLUME ["/data"]', dockerfile)
        self.assertIn("--host 0.0.0.0", dockerfile)
        self.assertIn("${LOOPGRAPH_DATA_DIR}/loopgraph.db", dockerfile)
        self.assertIn("loopgraph-data:/data", compose)
        self.assertIn("USER loopgraph", dockerfile)

    def test_github_pages_deploys_the_replay_only_after_successful_ci(self) -> None:
        workflows = self.root / ".github" / "workflows"
        ci = (workflows / "ci.yml").read_text(encoding="utf-8")
        deployment = (workflows / "pages.yml").read_text(encoding="utf-8")
        self.assertIn("run: make test", ci)
        self.assertIn("docker build", ci)
        self.assertIn("workflow_run:", deployment)
        self.assertIn("conclusion == 'success'", deployment)
        self.assertIn("actions/configure-pages@v5", deployment)
        self.assertIn("actions/upload-pages-artifact@v4", deployment)
        self.assertIn("actions/deploy-pages@v4", deployment)
        self.assertIn("src/loopgraph/static/replay.js", deployment)
        self.assertIn("enablement: true", deployment)
        self.assertNotIn("DEPLOY_WEBHOOK_URL", deployment)


if __name__ == "__main__":
    unittest.main()

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

    def test_github_deployment_requires_successful_ci_and_secret_deploy_hook(self) -> None:
        workflows = self.root / ".github" / "workflows"
        ci = (workflows / "ci.yml").read_text(encoding="utf-8")
        deployment = (workflows / "deploy.yml").read_text(encoding="utf-8")
        self.assertIn("run: make test", ci)
        self.assertIn("docker build", ci)
        self.assertIn("workflow_run:", deployment)
        self.assertIn("conclusion == 'success'", deployment)
        self.assertIn("secrets.DEPLOY_WEBHOOK_URL", deployment)
        self.assertIn('if [ -z "$DEPLOY_WEBHOOK_URL" ]', deployment)


if __name__ == "__main__":
    unittest.main()

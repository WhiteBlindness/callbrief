from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from callbrief.settings import Settings, SettingsError


class SettingsTests(unittest.TestCase):
    def test_reads_dotenv_and_environment_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text(
                "CALLBRIEF_BASE_URL=https://provider.example/v1\n"
                "CALLBRIEF_MODEL=model-from-file\n"
                "CALLBRIEF_API_KEY='example-secret'\n",
                encoding="utf-8",
            )

            settings = Settings.from_sources(
                {"CALLBRIEF_MODEL": "model-from-environment"},
                env_file,
            )

        self.assertEqual(settings.model, "model-from-environment")
        self.assertEqual(settings.base_url, "https://provider.example/v1")
        self.assertEqual(settings.api_key, "example-secret")

    def test_rejects_missing_model_configuration(self) -> None:
        with self.assertRaises(SettingsError):
            Settings.from_sources({}, None)

    def test_rejects_plain_http_to_public_hosts(self) -> None:
        with self.assertRaises(SettingsError):
            Settings.from_sources(
                {
                    "CALLBRIEF_BASE_URL": "http://provider.example/v1",
                    "CALLBRIEF_MODEL": "model",
                },
                None,
            )

    def test_accepts_loopback_http_for_a_local_model(self) -> None:
        settings = Settings.from_sources(
            {
                "CALLBRIEF_BASE_URL": "http://127.0.0.1:8080/v1",
                "CALLBRIEF_MODEL": "local-model",
            },
            None,
        )
        self.assertEqual(settings.base_url, "http://127.0.0.1:8080/v1")

    def test_dotenv_rejects_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("OTHER_SECRET=value\n", encoding="utf-8")

            with self.assertRaises(SettingsError):
                Settings.from_sources({}, env_file)

    def test_rejects_symlinked_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.env"
            target.write_text(
                "CALLBRIEF_BASE_URL=https://provider.example/v1\n"
                "CALLBRIEF_MODEL=model\n",
                encoding="utf-8",
            )
            env_file = root / ".env"
            try:
                env_file.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("Symlink creation is unavailable on this host")

            with self.assertRaises(SettingsError):
                Settings.from_sources({}, env_file)


if __name__ == "__main__":
    unittest.main()

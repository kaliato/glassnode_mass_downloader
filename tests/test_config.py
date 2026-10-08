import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import config


class ConfigTests(TestCase):
    def test_reads_api_key_from_env_file(self) -> None:
        with TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text('GLASSNODE_API_KEY="file-key"\n', encoding="utf-8")

            with patch.dict(os.environ, {"GLASSNODE_API_KEY": ""}):
                self.assertEqual(config.get_glassnode_api_key(env_file), "file-key")

    def test_environment_variable_takes_precedence_over_env_file(self) -> None:
        with TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("GLASSNODE_API_KEY=file-key\n", encoding="utf-8")

            with patch.dict(os.environ, {"GLASSNODE_API_KEY": "environment-key"}):
                self.assertEqual(
                    config.get_glassnode_api_key(env_file),
                    "environment-key",
                )

    def test_missing_or_blank_api_key_returns_none(self) -> None:
        with TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("GLASSNODE_API_KEY=\n", encoding="utf-8")

            with patch.dict(os.environ, {"GLASSNODE_API_KEY": ""}):
                self.assertIsNone(config.get_glassnode_api_key(env_file))

    def test_quoted_key_allows_hash_and_trailing_comment(self) -> None:
        with TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                'GLASSNODE_API_KEY="secret#part" # local key\n',
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"GLASSNODE_API_KEY": ""}):
                self.assertEqual(
                    config.get_glassnode_api_key(env_file),
                    "secret#part",
                )

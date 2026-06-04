import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm.llama_supervisor import llama_server_bin, llama_server_candidates


def _exe_name() -> str:
    return "llama-server.exe" if os.name == "nt" else "llama-server"


class LlamaSupervisorPathTests(unittest.TestCase):
    def test_exact_executable_env_override_wins_without_existence_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / _exe_name()
            with patch.dict(
                os.environ,
                {
                    "VERITAS_LLAMA_SERVER_BIN": str(exe),
                    "VERITAS_LLAMA_INSTALL_DIR": "",
                },
            ):
                self.assertEqual(llama_server_bin(), exe)

    def test_install_dir_env_override_is_checked_before_bundled_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp) / "runtime"
            install_dir.mkdir()
            exe = install_dir / _exe_name()
            exe.touch()
            with patch.dict(
                os.environ,
                {
                    "VERITAS_LLAMA_SERVER_BIN": "",
                    "VERITAS_LLAMA_INSTALL_DIR": str(install_dir),
                },
            ):
                self.assertEqual(llama_server_bin(), exe)

    @unittest.skipUnless(os.name == "nt", "WinGet package layout is Windows-only")
    def test_winget_llamacpp_package_is_candidate_before_path_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_app_data = Path(tmp)
            package = (
                local_app_data
                / "Microsoft"
                / "WinGet"
                / "Packages"
                / "ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe"
            )
            package.mkdir(parents=True)
            exe = package / _exe_name()
            exe.touch()

            with patch.dict(
                os.environ,
                {
                    "LOCALAPPDATA": str(local_app_data),
                    "VERITAS_LLAMA_SERVER_BIN": "",
                    "VERITAS_LLAMA_INSTALL_DIR": "",
                },
            ):
                candidates = llama_server_candidates(repo_root=Path(tmp) / "repo")

            self.assertIn(exe, candidates)
            self.assertLess(candidates.index(exe), len(candidates) - 1)


if __name__ == "__main__":
    unittest.main()

"""Configuration loading for patch shepherd."""

import os
from dataclasses import dataclass
from pathlib import Path

from llm_tool_common.config import load_env_files

load_env_files("patch-shepherd")


@dataclass
class PatchShepherdConfig:
    """Patch watcher configuration.

    Attributes:
        patches_file: Path to the patches JSON file.
        report_file: Path where the report JSON is written.
        watcher_tool: Path to watcher_tool.sh.
    """

    patches_file: str = ""
    report_file: str = ""
    watcher_tool: str = ""

    def __post_init__(self) -> None:
        watcher_dir = str(Path(__file__).resolve().parent)

        # patches_file: PATCH_SHEPHERD_PATCHES_FILE > PATCHES_FILE > default
        if not self.patches_file:
            self.patches_file = os.environ.get(
                "PATCH_SHEPHERD_PATCHES_FILE",
                os.environ.get(
                    "PATCHES_FILE",
                    "/shared/support_files/patches_to_watch.json",
                ),
            )

        # report_file: PATCH_SHEPHERD_REPORT_FILE > REPORT_FILE > default
        if not self.report_file:
            self.report_file = os.environ.get(
                "PATCH_SHEPHERD_REPORT_FILE",
                os.environ.get(
                    "REPORT_FILE",
                    "/tmp/patch_shepherd_report.json",
                ),
            )

        # watcher_tool: PATCH_SHEPHERD_TOOL_PATH > derived from __file__
        if not self.watcher_tool:
            self.watcher_tool = os.environ.get(
                "PATCH_SHEPHERD_TOOL_PATH",
                os.path.join(watcher_dir, "watcher_tool.sh"),
            )

        # --- Validation ---
        watcher_path = Path(self.watcher_tool)
        if not watcher_path.exists():
            raise FileNotFoundError(
                f"watcher_tool.sh not found at {self.watcher_tool}\n"
                f"Set PATCH_SHEPHERD_TOOL_PATH or ensure watcher_tool.sh "
                f"is in {watcher_dir}/"
            )
        if not os.access(self.watcher_tool, os.X_OK):
            raise PermissionError(
                f"watcher_tool.sh is not executable: {self.watcher_tool}"
            )

        patches_path = Path(self.patches_file)
        if not patches_path.exists():
            raise FileNotFoundError(
                f"Patches file not found: {self.patches_file}\n"
                f"Set PATCH_SHEPHERD_PATCHES_FILE or PATCHES_FILE, "
                f"or create the file at the default location."
            )


def load_config(
    patches_file: str | None = None,
    report_file: str | None = None,
    watcher_tool: str | None = None,
) -> PatchShepherdConfig:
    """Load patch shepherd configuration from environment.

    Explicit arguments override environment variables.
    """
    return PatchShepherdConfig(
        patches_file=patches_file or "",
        report_file=report_file or "",
        watcher_tool=watcher_tool or "",
    )

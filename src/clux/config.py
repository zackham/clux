"""Configuration management."""

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomli
except ImportError:
    import tomllib as tomli  # type: ignore

import tomli_w


def get_config_path() -> Path:
    """Get config file path following XDG spec."""
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    config_dir = xdg_config / "clux"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "config.toml"


@dataclass
class Config:
    """clux configuration."""

    yolo_mode: bool = True  # --dangerously-skip-permissions by default
    claude_command: str = "claude"

    @classmethod
    def load(cls) -> "Config":
        """Load config from file, creating default if needed."""
        config_path = get_config_path()
        if config_path.exists():
            with open(config_path, "rb") as f:
                data = tomli.load(f)
            return cls(
                yolo_mode=data.get("yolo_mode", True),
                claude_command=data.get("claude_command", "claude"),
            )
        # Create default config
        config = cls()
        config.save()
        return config

    def save(self) -> None:
        """Save config to file."""
        config_path = get_config_path()
        data = {
            "yolo_mode": self.yolo_mode,
            "claude_command": self.claude_command,
        }
        with open(config_path, "wb") as f:
            tomli_w.dump(data, f)

    def get_claude_command(
        self, safe: bool = False, session_id: str | None = None, resume: bool = False,
    ) -> list[str]:
        """Get the claude command with appropriate flags."""
        cmd = [self.claude_command]
        if self.yolo_mode and not safe:
            cmd.append("--dangerously-skip-permissions")
        if session_id:
            if resume:
                cmd.extend(["--resume", session_id])
            else:
                cmd.extend(["--session-id", session_id])
        return cmd

"""Windows Task Scheduler launcher with a provider-free preflight mode."""

from __future__ import annotations

import argparse
import importlib.metadata
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PINNED_DISTRIBUTIONS = {
    "langchain": "1.4.1",
    "langchain-groq": "1.1.3",
    "langchain-tavily": "0.2.18",
    "python-dotenv": "1.2.4",
    "tzdata": "2026.5",
}


def _path_from_project(path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def preflight() -> int:
    """Check local runtime/configuration without importing or running the agent."""
    errors: list[str] = []
    print(f"project_root={PROJECT_ROOT}")
    print(f"python_executable={Path(sys.executable).resolve()}")
    print(f"python_version={sys.version.split()[0]}")

    if sys.version_info < (3, 11):
        errors.append("python_version_unsupported")

    for distribution, expected in PINNED_DISTRIBUTIONS.items():
        try:
            installed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            print(f"dependency_{distribution}=missing")
            errors.append(f"dependency_missing:{distribution}")
            continue
        print(f"dependency_{distribution}={installed}")
        if installed != expected:
            errors.append(f"dependency_version_mismatch:{distribution}")

    try:
        import pydantic

        pydantic_parts = tuple(int(part) for part in pydantic.__version__.split(".")[:2])
        print(f"dependency_pydantic={pydantic.__version__}")
        if not ((2, 7) <= pydantic_parts < (3, 0)):
            errors.append("dependency_version_mismatch:pydantic")
    except (ImportError, ValueError, AttributeError):
        print("dependency_pydantic=missing_or_invalid")
        errors.append("dependency_missing:pydantic")

    try:
        from dotenv import dotenv_values

        dotenv_path = PROJECT_ROOT / ".env"
        dotenv_data = dotenv_values(dotenv_path) if dotenv_path.is_file() else {}
        print(f"dotenv_file_found={str(dotenv_path.is_file()).lower()}")
        print(f"dotenv_groq_api_key_configured={str(bool(dotenv_data.get('GROQ_API_KEY'))).lower()}")
        print(f"dotenv_tavily_api_key_configured={str(bool(dotenv_data.get('TAVILY_API_KEY'))).lower()}")
        if not dotenv_path.is_file():
            errors.append("dotenv_file_missing")
        if not dotenv_data.get("GROQ_API_KEY") or not dotenv_data.get("TAVILY_API_KEY"):
            errors.append("dotenv_required_api_key_missing")
    except ImportError:
        print("dotenv_file_status=unavailable_python_dotenv_missing")
        errors.append("dependency_missing:python-dotenv")

    try:
        from config import load_settings

        settings = load_settings()
    except Exception as exc:
        # Configuration exceptions can mention setting names, but never print
        # values or exception text because environment data must stay private.
        print(f"configuration_status=failed error_type={type(exc).__name__}")
        errors.append("configuration_invalid")
    else:
        print("configuration_status=loaded")
        print(f"groq_api_key_configured={str(bool(settings.groq_api_key)).lower()}")
        print(f"tavily_api_key_configured={str(bool(settings.tavily_api_key)).lower()}")
        print(f"groq_model={settings.groq_model}")
        print(f"report_dir={_path_from_project(settings.report_dir)}")
        print(f"log_dir={_path_from_project(settings.log_dir)}")
        if not settings.groq_api_key or not settings.tavily_api_key:
            errors.append("required_api_key_missing")

    if errors:
        print("preflight_status=failed")
        for error in errors:
            print(f"preflight_issue={error}")
        return 1

    print("preflight_status=passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="check interpreter, dependencies, .env, and output paths; never run the agent",
    )
    args = parser.parse_args()

    status = preflight()
    if status or args.preflight_only:
        return status

    return subprocess.run([sys.executable, "-m", "main"], cwd=PROJECT_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())

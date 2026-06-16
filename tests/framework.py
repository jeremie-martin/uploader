# Copyright 2026 Ability Neurotech
# Author: Jérémie Martin <jeremie.martin@abilityneuro.com>

"""Testing framework for observable, artifact-producing tests.

Tests are executable experiments that should:
- Observe and record behavior rather than crash on first failure
- Leave artifacts (logs, reports) that tell a clear story
- Make debugging easier than reading a traceback
- Only abort when continuing would make results meaningless

Usage:
    @recorded_test("my_test_name")
    def test_something(tf, api_client):
        tf.log("Starting test...")
        tf.expect(condition, "Expected condition to be true")
        tf.expect(other_condition, "Critical check", abort=True)
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest
from loguru import logger

REPORTS_DIR = Path("reports")


class TestAbort(Exception):
    """Raised to gracefully abort a test when continuing would be meaningless."""

    pass


@dataclass
class TestResult:
    """Accumulates test results."""

    name: str
    failures: list[str] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str | None = None

    @property
    def passed(self) -> bool:
        return not self.failures and not self.aborted


class Framework:
    """Test helper providing logging, assertions, and report generation.

    Uses loguru for all logging. Test logs are filtered by a unique test_id
    to ensure only this test's logs appear in the report.

    Parameters
    ----------
    name
        Test identifier for reports and logs.
    reports_dir
        Override default reports directory.
    logs_dir
        Directory to search for backend logs. Defaults to "logs".
    log_pattern
        Glob pattern for finding backend log files. Defaults to "bolt_*.log*".
    """

    def __init__(
        self,
        name: str,
        reports_dir: Path | None = None,
        logs_dir: Path | str | None = None,
        log_pattern: str = "bolt_*.log*",
    ) -> None:
        self.name = name
        self.result = TestResult(name)
        self._test_id = f"test_{id(self)}"  # Unique filter key

        # Configure backend log discovery
        self._logs_dir = Path(logs_dir) if logs_dir else Path("logs")
        self._log_pattern = log_pattern

        # Setup output directory
        base = reports_dir or REPORTS_DIR
        self.output_dir = base / name
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Setup loguru sink for test-only logs (overwrite each run)
        self._log_path = self.output_dir / f"{name}.log"
        self._handler_id = logger.add(
            self._log_path,
            format="{time:HH:mm:ss.SSS} | {level: <7} | {message}",
            level="DEBUG",
            mode="w",
            filter=lambda record: record["extra"].get("test_id") == self._test_id,
        )

        # Create bound logger that includes our test_id
        self._log = logger.bind(test_id=self._test_id)
        self._log.info(f"Test started: {name}")

    def log(self, message: str) -> None:
        """Log an informational message."""
        self._log.info(message)

    def debug(self, message: str) -> None:
        """Log a debug message."""
        self._log.debug(message)

    def warning(self, message: str) -> None:
        """Log a warning message."""
        self._log.warning(message)

    def error(self, message: str) -> None:
        """Log an error message."""
        self._log.error(message)

    def expect(self, condition: bool, message: str, *, abort: bool = False) -> bool:
        """Assert a condition, recording failure if false.

        Unlike assert, this records failures without raising by default,
        allowing observation of multiple issues in a single run.

        Args:
            condition: The condition to check (True = pass, False = fail)
            message: Description of what was expected (shown on failure)
            abort: If True and condition is False, abort the test immediately

        Returns:
            The condition value (for use in conditional logic)
        """
        if condition:
            return True

        self.result.failures.append(message)
        self._log.error(f"FAILED: {message}")

        if abort:
            self.result.aborted = True
            self.result.abort_reason = message
            raise TestAbort(message)

        return False

    def finalize(self) -> None:
        """Write summary, copy backend logs, and signal result to pytest."""
        # Log summary
        if self.result.passed:
            self._log.info(f"Test PASSED: {self.name}")
        else:
            if self.result.aborted:
                self._log.error(f"Test ABORTED: {self.result.abort_reason}")
            self._log.error(f"Test FAILED: {len(self.result.failures)} failure(s)")
            for i, msg in enumerate(self.result.failures, 1):
                self._log.error(f"  {i}. {msg}")

        # Remove our handler (may already be removed if backend reconfigured logging)
        with contextlib.suppress(ValueError):
            logger.remove(self._handler_id)

        # Copy backend log if it exists
        self._copy_backend_log()

        # Generate markdown report from our log
        self._generate_report()

        # Signal to pytest
        if not self.result.passed:
            lines = []
            if self.result.aborted:
                lines.append(f"ABORTED: {self.result.abort_reason}")
            lines.append(f"{len(self.result.failures)} failure(s):")
            for i, msg in enumerate(self.result.failures, 1):
                lines.append(f"  {i}. {msg}")
            pytest.fail("\n".join(lines), pytrace=False)

    def _copy_backend_log(self) -> None:
        """Copy the latest backend log to the report directory."""
        if not self._logs_dir.exists():
            return

        # Find the most recent log file matching the configured pattern
        log_files = sorted(self._logs_dir.glob(self._log_pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if not log_files:
            # Fallback to any log file
            log_files = sorted(self._logs_dir.glob("*.log*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not log_files:
            return

        latest = log_files[0]
        dest = self.output_dir / f"backend_{latest.name}"
        try:
            shutil.copy2(latest, dest)
            self._log.debug(f"Copied backend log: {latest.name}")
        except Exception as e:
            self._log.warning(f"Could not copy backend log: {e}")

    def _generate_report(self) -> None:
        """Generate a markdown report from the log file."""
        report_path = self.output_dir / f"{self.name}.md"

        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"# Test Report: {self.name}\n\n")
            f.write(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            # Result summary at the top
            if self.result.passed:
                f.write("**Result:** PASSED\n\n")
            else:
                f.write("**Result:** FAILED\n\n")
                if self.result.failures:
                    f.write("## Failures\n\n")
                    for i, msg in enumerate(self.result.failures, 1):
                        f.write(f"{i}. {msg}\n")
                    f.write("\n")

            # Include log content
            f.write("## Execution Log\n\n")
            f.write("```\n")
            if self._log_path.exists():
                f.write(self._log_path.read_text())
            f.write("```\n")


def recorded_test(
    name: str,
    reports_dir: Path | str | None = None,
    logs_dir: Path | str | None = None,
    log_pattern: str = "bolt_*.log*",
):
    """Decorator to wrap a pytest test with the Framework.

    The decorated function receives a Framework instance as its first
    argument (named `tf` by convention).

    Args:
        name: Test identifier for reports and logs
        reports_dir: Override default reports directory
        logs_dir: Directory to search for backend logs. Defaults to "logs".
        log_pattern: Glob pattern for finding backend log files. Defaults to "bolt_*.log*".

    Example:
        @recorded_test("gate_connection")
        def test_gate_connects(tf, api_client):
            tf.log("Testing gate connection...")
            tf.expect(status.connected, "Gate should be connected")
    """
    _reports_dir = Path(reports_dir) if reports_dir else None
    _logs_dir = Path(logs_dir) if logs_dir else None

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            tf = Framework(
                name=name,
                reports_dir=_reports_dir,
                logs_dir=_logs_dir,
                log_pattern=log_pattern,
            )
            try:
                return func(tf, *args, **kwargs)
            except TestAbort:
                pass  # Graceful abort - finalize will report
            except Exception as e:
                tf.error(f"Unhandled exception: {type(e).__name__}: {e}")
                tf.result.failures.append(f"Unhandled exception: {e}")
                tf.result.aborted = True
                tf.result.abort_reason = str(e)
            finally:
                tf.finalize()

        # Hide the `tf` parameter from pytest's fixture injection
        sig = inspect.signature(func)
        params = list(sig.parameters.values())[1:]
        wrapper.__signature__ = sig.replace(parameters=params)
        return wrapper

    return decorator


__all__ = ["REPORTS_DIR", "Framework", "TestAbort", "TestResult", "recorded_test"]

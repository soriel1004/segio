"""Run the real suite and expose failures in the GitHub job summary.

Detailed Actions logs require signing in even for this public repository.
The summary keeps failed test names and tracebacks visible to reviewers.
"""
from pathlib import Path
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main():
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        failures = result.failures + result.errors
        passed = result.testsRun - len(failures) - len(result.skipped)
        lines = ["## Test results", "",
                 f"Run: {result.testsRun} · Passed: {passed} · "
                 f"Failed/errors: {len(failures)} · Skipped: {len(result.skipped)}", ""]
        for test, traceback in failures:
            lines.extend([f"### {test.id()}", "", "```text",
                          traceback[-12000:].replace("```", "` ` `"), "```", ""])
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

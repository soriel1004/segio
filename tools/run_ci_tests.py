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
    failures = result.failures + result.errors
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for test, traceback in failures:
            # Annotations are visible even when detailed logs and job summaries
            # require signing in. Encode workflow-command data, not raw lines.
            detail = f"{test.id()}\n{traceback[-5000:]}"
            detail = detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error::{detail}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
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

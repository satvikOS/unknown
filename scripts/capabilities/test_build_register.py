"""Tests for the capability register checker (build_register.py --check / --run-tests).

  python3 -m unittest discover -s scripts/capabilities

Each test builds a throwaway repository root with the files a record cites, so the checks run against
known content and never against the real register.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_register as br  # noqa: E402

UNIT = "terminal/src/ui/sample.test.ts"
E2E = "terminal/e2e/01-sample.spec.ts"


def record(tests: list[str], status: str = "tested") -> dict:
    return {"capabilities": [{"id": "CAP-UX-99", "implementation": {
        "status": status, "files": ["README.md"], "tests": tests, "gaps": ["fixture"],
        "verified_against": "fixture@0000000", "updated": "2026-10-22"}}]}


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="register-check-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.write("README.md", "fixture root\n")

    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")

    def problems(self, tests: list[str]) -> list[str]:
        return br.validate(record(tests), self.root)


class TerminalTitleTests(_Root):
    def setUp(self) -> None:
        super().setUp()
        self.write(UNIT, """\
            import { describe, expect, it } from 'vitest';

            // The old test 'drops stale quotes' was removed when the quote panel moved.
            describe('palette filtering', () => {
              it('ranks prefix matches first', () => {
                expect(1).toBe(1);
              });
              it.skip('ignores accents', () => {});
              it.todo('remembers recent commands');
            });

            describe('table sorting', () => {
              it("keeps empty values last", () => {
                const label = 'ranks prefix matches first and more';
                expect(label).toBeTruthy();
              });
            });
            """)
        self.write(E2E, """\
            import { test } from '@playwright/test';

            for (const scheme of ['dark', 'light'] as const) {
              test(`every workspace passes (${scheme} theme)`, async () => {});
            }
            test('login: a wrong passphrase is refused', async () => {});
            """)

    def test_real_describe_and_it_titles_pass(self):
        self.assertEqual(self.problems([
            f"{UNIT}::palette filtering > ranks prefix matches first",
            f"{UNIT}::table sorting > keeps empty values last",
            f"{E2E}::every workspace passes (${{scheme}} theme)",
            f"{E2E}::login: a wrong passphrase is refused",
        ]), [])

    def test_a_title_found_only_in_a_comment_is_not_a_test(self):
        """Before the fix any substring of the file passed: a deleted test whose title survives in a comment was
        still reported as existing."""
        (p,) = self.problems([f"{UNIT}::palette filtering > drops stale quotes"])
        self.assertIn("no it/test titled 'drops stale quotes'", p)

    def test_a_title_found_only_inside_a_string_is_not_a_test(self):
        (p,) = self.problems([f"{UNIT}::table sorting > ranks prefix matches first and more"])
        self.assertIn("no it/test titled", p)

    def test_skipped_and_todo_tests_do_not_count(self):
        problems = self.problems([f"{UNIT}::palette filtering > ignores accents",
                                  f"{UNIT}::palette filtering > remembers recent commands"])
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(all("skipped, todo or focused" in p for p in problems), problems)

    def test_the_it_must_follow_its_describe(self):
        """'ranks prefix matches first' is a test, but not under 'table sorting'."""
        (p,) = self.problems([f"{UNIT}::table sorting > ranks prefix matches first"])
        self.assertIn("after describe 'table sorting'", p)

    def test_a_describe_title_must_be_a_describe_call(self):
        (p,) = self.problems([f"{UNIT}::ranks prefix matches first > keeps empty values last"])
        self.assertIn("no describe titled 'ranks prefix matches first'", p)

    def test_reference_format_and_missing_file(self):
        problems = self.problems(["terminal/src/ui/sample.ts::x > y", "terminal/src/ui/absent.test.ts::x > y"])
        self.assertIn("terminal test reference must be", problems[0])
        self.assertIn("missing test file terminal/src/ui/absent.test.ts", problems[1])


class PythonReferenceTests(_Root):
    def setUp(self) -> None:
        super().setUp()
        self.write("backend/tests/test_sample.py", """\
            import unittest


            class SampleTests(unittest.TestCase):
                def test_passes(self):
                    self.assertTrue(True)


            class OtherTests(unittest.TestCase):
                def test_other(self):
                    pass
            """)

    def test_class_and_method_must_exist_in_that_class(self):
        problems = self.problems(["backend/tests/test_sample.py::SampleTests.test_passes",
                                  "backend/tests/test_sample.py::SampleTests.test_other",
                                  "backend/tests/test_sample.py::Missing.test_passes",
                                  "backend/tests/test_sample.py::SampleTests.test_passes [IBKR venv]"])
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("test method not found in its class", problems[0])
        self.assertIn("class not found", problems[1])


class RunCitedTestsTests(_Root):
    def setUp(self) -> None:
        super().setUp()
        self.write("backend/tests/sub/test_nested.py", """\
            import unittest


            class NestedTests(unittest.TestCase):
                def test_fails(self):
                    self.fail("the cited behaviour is broken")

                def test_passes(self):
                    pass
            """)

    def run_cited(self, tests: list[str]) -> list[str]:
        with contextlib.redirect_stdout(io.StringIO()):
            return br.run_cited_tests(record(tests), self.root)

    def test_a_test_the_checker_accepts_in_a_subdirectory_is_run(self):
        """Before the fix --check accepted backend/tests/<dir>/<file>.py references while --run-tests silently left
        them out, so a failing cited test there was reported as clean."""
        ref = "backend/tests/sub/test_nested.py::NestedTests.test_fails"
        self.assertEqual(self.problems([ref]), [])
        self.assertEqual(self.run_cited([ref]), ["cited test sub.test_nested.NestedTests.test_fails: FAIL"])
        self.assertEqual(self.run_cited(["backend/tests/sub/test_nested.py::NestedTests.test_passes"]), [])

    def test_a_malformed_backend_reference_is_a_problem_not_skipped(self):
        """--run-tests without --check used to skip a reference it could not parse."""
        (p,) = self.run_cited(["backend/tests/sub/test_nested.py::NestedTests"])
        self.assertIn("cannot run", p)

    def test_no_cited_backend_tests_runs_nothing(self):
        self.assertEqual(self.run_cited([f"{UNIT}::palette filtering > ranks prefix matches first"]), [])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""业务 Eval 纯函数单测：表名语义匹配与用例字段解析。

运行::

    python3 -m unittest scripts.test_business_eval_utils -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import business_eval  # noqa: E402


class TableMatchTest(unittest.TestCase):
    def test_plural_and_business_suffix(self) -> None:
        self.assertTrue(business_eval.table_matches("resumes", "resumes"))
        self.assertTrue(business_eval.table_matches("resume_submissions", "resumes"))
        self.assertTrue(business_eval.table_matches("customers", "customer"))
        self.assertTrue(business_eval.table_matches("user_profiles", "user"))

    def test_unrelated_tables_do_not_match(self) -> None:
        self.assertFalse(business_eval.table_matches("orders", "resumes"))
        self.assertFalse(business_eval.table_matches("customer_notes", "orders"))


class CaseParsingTest(unittest.TestCase):
    def test_upload_case_enables_runtime_acceptance(self) -> None:
        cases = {
            case.case_id: case
            for case in business_eval.load_cases(business_eval.DEFAULT_SUITE)
        }
        self.assertTrue(cases["biz_003"].runtime)
        self.assertTrue(cases["biz_003"].requires_upload)
        self.assertFalse(cases["biz_002"].runtime)


if __name__ == "__main__":
    unittest.main()

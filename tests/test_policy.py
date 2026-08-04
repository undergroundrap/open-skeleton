# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from pathlib import Path
from unittest import TestCase

from open_skeleton.policy import ScanPolicy, classify_language, classify_role


class ScanPolicyTests(TestCase):
    def test_sensitive_and_generated_paths_are_excluded(self) -> None:
        policy = ScanPolicy(max_file_bytes=100)
        self.assertEqual(policy.directory_exclusion("node_modules"), "excluded-directory")
        self.assertEqual(policy.file_exclusion(Path(".env"), 10), "sensitive-file-name")
        self.assertEqual(policy.file_exclusion(Path(".env.production"), 10), "sensitive-file-name")
        self.assertIsNone(policy.file_exclusion(Path(".env.example"), 10))
        self.assertEqual(policy.file_exclusion(Path("secret.pem"), 10), "sensitive-file-type")
        self.assertEqual(policy.file_exclusion(Path("large.py"), 101), "oversized-file:101>100")

    def test_language_and_role_classification(self) -> None:
        self.assertEqual(classify_language(Path("thing.hum")), "Hum")
        self.assertEqual(classify_language(Path("component.tsx")), "TypeScript JSX")
        self.assertEqual(classify_role(Path("tests/test_parser.py")), "test")
        self.assertEqual(classify_role(Path("docs/design.md")), "documentation")
        self.assertEqual(classify_role(Path(".github/workflows/ci.yml")), "workflow")

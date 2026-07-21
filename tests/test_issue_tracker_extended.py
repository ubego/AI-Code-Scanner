"""Additional tests for issue tracker functionality."""

import pytest
from datetime import datetime

from code_scanner.issue_tracker import IssueTracker
from code_scanner.models import Issue, IssueStatus


class TestIssueTrackerResolveNonMatching:
    """Tests for _resolve_non_matching method."""

    def test_resolves_old_issues_not_in_current(self):
        """Old issues not in current scan are resolved."""
        tracker = IssueTracker()
        now = datetime.now()
        
        old_issue = Issue(
            file_path="test.py",
            line_number=10,
            description="Old issue",
            suggested_fix="old fix",
            code_snippet="old code",
            check_query="check",
            timestamp=now,
        )
        tracker.add_issue(old_issue)
        
        # New scan finds different issue in same file
        new_issue = Issue(
            file_path="test.py",
            line_number=20,
            description="New issue",
            suggested_fix="new fix",
            code_snippet="new code",
            check_query="check",
            timestamp=now,
        )
        
        resolved = tracker._resolve_non_matching("test.py", [new_issue])
        
        assert resolved == 1
        assert old_issue.status == IssueStatus.RESOLVED


class TestIssueTrackerUpdateFromScan:
    """Tests for update_from_scan method."""

    def test_resolves_all_issues_for_scanned_file_with_no_new_issues(self):
        """All issues resolved for scanned file with no new issues."""
        tracker = IssueTracker()
        now = datetime.now()
        
        issue = Issue(
            file_path="test.py",
            line_number=10,
            description="Issue",
            suggested_fix="fix",
            code_snippet="code",
            check_query="check",
            timestamp=now,
        )
        tracker.add_issue(issue)
        
        # Scan same file but find no issues
        new_count, resolved_count = tracker.update_from_scan([], ["test.py"])
        
        assert new_count == 0
        assert resolved_count == 1
        assert issue.status == IssueStatus.RESOLVED

    def test_keeps_issues_for_non_scanned_files(self):
        """Issues in non-scanned files remain open."""
        tracker = IssueTracker()
        now = datetime.now()
        
        issue = Issue(
            file_path="other.py",
            line_number=10,
            description="Issue",
            suggested_fix="fix",
            code_snippet="code",
            check_query="check",
            timestamp=now,
        )
        tracker.add_issue(issue)
        
        # Scan different file
        new_count, resolved_count = tracker.update_from_scan([], ["test.py"])
        
        assert issue.status == IssueStatus.OPEN


class TestIssueTrackerAddIssues:
    """Tests for add_issues method."""

    def test_add_multiple_issues_returns_new_count(self):
        """add_issues returns count of truly new issues."""
        tracker = IssueTracker()
        now = datetime.now()
        
        issue1 = Issue(
            file_path="a.py",
            line_number=1,
            description="Issue 1",
            suggested_fix="Fix",
            code_snippet="code 1",
            check_query="check",
            timestamp=now,
        )
        issue2 = Issue(
            file_path="b.py",
            line_number=1,
            description="Issue 2",
            suggested_fix="Fix",
            code_snippet="code 2",
            check_query="check",
            timestamp=now,
        )
        
        # Add first issue
        tracker.add_issue(issue1)
        
        # Add both (first is duplicate)
        duplicate = Issue(
            file_path="a.py",
            line_number=1,
            description="Issue 1",
            suggested_fix="Fix",
            code_snippet="code 1",
            check_query="check",
            timestamp=now,
        )
        
        count = tracker.add_issues([duplicate, issue2])
        
        assert count == 1  # Only issue2 is new


class TestIssueTrackerProperties:
    """Tests for IssueTracker property methods."""

    def test_open_issues_returns_only_open(self):
        """open_issues returns only OPEN status issues."""
        tracker = IssueTracker()
        now = datetime.now()
        
        open_issue = Issue(
            file_path="open.py",
            line_number=1,
            description="Open",
            suggested_fix="Fix",
            code_snippet="code",
            check_query="check",
            timestamp=now,
        )
        tracker.add_issue(open_issue)
        
        # Add and resolve another
        resolved_issue = Issue(
            file_path="resolved.py",
            line_number=1,
            description="Resolved",
            suggested_fix="Fix",
            code_snippet="code",
            check_query="check",
            timestamp=now,
        )
        tracker.add_issue(resolved_issue)
        tracker.resolve_issues_for_file("resolved.py")
        
        open_issues = tracker.open_issues
        
        assert len(open_issues) == 1
        assert open_issues[0].file_path == "open.py"

    def test_resolved_issues_returns_only_resolved(self):
        """resolved_issues returns only RESOLVED status issues."""
        tracker = IssueTracker()
        now = datetime.now()
        
        issue = Issue(
            file_path="test.py",
            line_number=1,
            description="Test",
            suggested_fix="Fix",
            code_snippet="code",
            check_query="check",
            timestamp=now,
        )
        tracker.add_issue(issue)
        tracker.resolve_issues_for_file("test.py")
        
        resolved = tracker.resolved_issues
        
        assert len(resolved) == 1
        assert resolved[0].status == IssueStatus.RESOLVED


class TestIssueMatches:
    """Tests for Issue.matches method edge cases."""

    def test_matches_different_check_query_same_description(self):
        """Issues match even with different check queries if description same."""
        now = datetime.now()
        issue1 = Issue(
            file_path="test.py",
            line_number=10,
            description="Same issue",
            suggested_fix="Fix",
            code_snippet="same code",
            check_query="check1",
            timestamp=now,
        )
        issue2 = Issue(
            file_path="test.py",
            line_number=10,
            description="Same issue",
            suggested_fix="Fix",
            code_snippet="same code",
            check_query="check2",
            timestamp=now,
        )
        
        assert issue1.matches(issue2) is True

    def test_matches_different_descriptions_same_code(self):
        """Issues with different descriptions but same code still match."""
        now = datetime.now()
        issue1 = Issue(
            file_path="test.py",
            line_number=10,
            description="Desc 1",
            suggested_fix="Fix",
            code_snippet="identical code snippet",
            check_query="check",
            timestamp=now,
        )
        issue2 = Issue(
            file_path="test.py",
            line_number=10,
            description="Desc 2",
            suggested_fix="Fix",
            code_snippet="identical code snippet",
            check_query="check",
            timestamp=now,
        )

        # They match because code_snippet is the same
        assert issue1.matches(issue2) is True


class TestSteadyResolution:
    """Tests for content-based (steady) issue resolution.

    Reproduces the userFunds/userFundsForDisplay bug: an issue must not be
    auto-resolved by LLM non-detection when its code snippet is still present
    in the file.
    """

    SNIPPET = (
        "const userFunds = Math.floor(bonus + balance)\n"
        "const userFundsForDisplay = Math.floor(bonus + balance)"
    )

    def _file_content_with_snippet(self) -> str:
        return (
            "import QtQuick\n"
            "Item {\n"
            f"    {self.SNIPPET.replace(chr(10), chr(10) + '    ')}\n"
            "    Text { text: 'hello' }\n"
            "}\n"
        )

    def _make_issue(self) -> Issue:
        return Issue(
            file_path="SimpleQuestDetails.qml",
            line_number=387,
            description="Redundant calculation of user funds.",
            suggested_fix="Use a single variable.",
            code_snippet=self.SNIPPET,
            check_query="Check that JavaScript in QML is moved to C++.",
            timestamp=datetime.now(),
        )

    def test_resolve_non_matching_keeps_open_when_code_present(self):
        """Issue kept OPEN when its snippet is still in the file."""
        tracker = IssueTracker()
        issue = self._make_issue()
        tracker.add_issue(issue)

        # New scan reports a *different* issue in the same file; the
        # userFunds issue is not re-reported but its code is still present.
        new_issue = Issue(
            file_path="SimpleQuestDetails.qml",
            line_number=10,
            description="Some unrelated issue.",
            suggested_fix="Fix it.",
            code_snippet="Text { text: 'typo' }",
            check_query="check",
            timestamp=datetime.now(),
        )
        content = self._file_content_with_snippet()

        resolved = tracker._resolve_non_matching(
            "SimpleQuestDetails.qml", [new_issue], file_content=content
        )

        assert resolved == 0
        assert issue.status == IssueStatus.OPEN

    def test_resolve_non_matching_resolves_when_code_gone(self):
        """Issue resolved when its snippet is actually removed from the file."""
        tracker = IssueTracker()
        issue = self._make_issue()
        tracker.add_issue(issue)

        new_issue = Issue(
            file_path="SimpleQuestDetails.qml",
            line_number=10,
            description="Unrelated issue.",
            suggested_fix="Fix it.",
            code_snippet="Text { text: 'typo' }",
            check_query="check",
            timestamp=datetime.now(),
        )
        # File no longer contains the redundant calculation.
        content = "import QtQuick\nItem {\n    const other = 1\n}\n"

        resolved = tracker._resolve_non_matching(
            "SimpleQuestDetails.qml", [new_issue], file_content=content
        )

        assert resolved == 1
        assert issue.status == IssueStatus.RESOLVED

    def test_resolve_non_matching_legacy_when_no_content(self):
        """Without file_content, legacy behavior resolves on non-detection."""
        tracker = IssueTracker()
        issue = self._make_issue()
        tracker.add_issue(issue)

        new_issue = Issue(
            file_path="SimpleQuestDetails.qml",
            line_number=10,
            description="Unrelated issue.",
            suggested_fix="Fix it.",
            code_snippet="Text { text: 'typo' }",
            check_query="check",
            timestamp=datetime.now(),
        )

        resolved = tracker._resolve_non_matching(
            "SimpleQuestDetails.qml", [new_issue]  # no file_content
        )

        assert resolved == 1
        assert issue.status == IssueStatus.RESOLVED

    def test_resolve_issues_for_file_keeps_open_when_code_present(self):
        """Zero-new-issues path keeps OPEN when snippet still present."""
        tracker = IssueTracker()
        issue = self._make_issue()
        tracker.add_issue(issue)

        content = self._file_content_with_snippet()
        resolved = tracker.resolve_issues_for_file(
            "SimpleQuestDetails.qml", file_content=content
        )

        assert resolved == 0
        assert issue.status == IssueStatus.OPEN

    def test_update_from_scan_blocks_resolution_when_code_present(self):
        """End-to-end: update_from_scan keeps OPEN when snippet present in files_content."""
        tracker = IssueTracker()
        issue = self._make_issue()
        tracker.add_issue(issue)

        # Scan the file but the LLM reports no issues (non-detection).
        files_content = {"SimpleQuestDetails.qml": self._file_content_with_snippet()}
        new_count, resolved_count = tracker.update_from_scan(
            [], ["SimpleQuestDetails.qml"], files_content=files_content
        )

        assert new_count == 0
        assert resolved_count == 0
        assert issue.status == IssueStatus.OPEN

    def test_update_from_scan_resolves_when_code_absent(self):
        """End-to-end: update_from_scan resolves when snippet removed."""
        tracker = IssueTracker()
        issue = self._make_issue()
        tracker.add_issue(issue)

        files_content = {"SimpleQuestDetails.qml": "import QtQuick\nItem {\n}\n"}
        new_count, resolved_count = tracker.update_from_scan(
            [], ["SimpleQuestDetails.qml"], files_content=files_content
        )

        assert new_count == 0
        assert resolved_count == 1
        assert issue.status == IssueStatus.RESOLVED

    def test_empty_snippet_uses_legacy_behavior(self):
        """Issue with empty snippet resolves via legacy non-detection path."""
        tracker = IssueTracker()
        issue = Issue(
            file_path="test.py",
            line_number=1,
            description="Issue without code.",
            suggested_fix="Fix it.",
            code_snippet="",  # no snippet -> cannot verify presence
            check_query="check",
            timestamp=datetime.now(),
        )
        tracker.add_issue(issue)

        files_content = {"test.py": "some content"}
        new_count, resolved_count = tracker.update_from_scan(
            [], ["test.py"], files_content=files_content
        )

        assert resolved_count == 1
        assert issue.status == IssueStatus.RESOLVED



"""Dependency audit coverage must fail closed independently of pip-audit's exit code."""

import runpy
from pathlib import Path

import pytest

audit = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "audit_dependencies.py"))
requirements = audit["active_requirements"]
verify = audit["verify_report"]


def test_exact_dependency_closure_and_platform_markers():
    """All active direct and transitive pins must be represented."""
    expected = requirements(
        'First_Dep==1.2\ntransitive==3.0 ; python_version >= "3.12"\n'
        'foreign==4.0 ; python_version < "1.0"\n'
    )
    assert expected == {"first-dep": "1.2", "transitive": "3.0"}
    verify(
        {
            "dependencies": [
                {"name": name, "version": version, "vulns": []}
                for name, version in expected.items()
            ]
        },
        expected,
    )


@pytest.mark.parametrize(
    "text", ["", "dep>=1", "dep==1.*", "dep @ https://example.com/dep.whl", "dep==1\ndep==2"]
)
def test_unresolved_or_ambiguous_dependency_pins_fail(text):
    """Unresolved registry and direct-URL dependencies cannot pass."""
    with pytest.raises(ValueError):
        requirements(text)


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"name": "private-dep", "skip_reason": "Dependency not found on PyPI"}],
        [{"name": "dep", "version": "2", "vulns": []}],
        [{"name": "other", "version": "1", "vulns": []}],
        [{"name": "dep", "version": "1", "vulns": []}] * 2,
        [{"name": "dep", "version": "1", "vulns": [{"id": "CVE-example"}]}],
        [{"name": "dep", "version": "1"}],
        [None],
    ],
)
def test_skipped_missing_unexpected_duplicate_vulnerable_or_incomplete_results_fail(entries):
    """No partial or unauditable dependency report is accepted."""
    with pytest.raises(ValueError):
        verify({"dependencies": entries}, {"dep": "1"})


@pytest.mark.parametrize("report", [None, [], {}, {"dependencies": "invalid"}])
def test_malformed_audit_reports_fail(report):
    """Malformed audit evidence fails closed."""
    with pytest.raises(ValueError):
        verify(report, {"dep": "1"})


def test_hashed_lock_export_preserves_pins_and_validates_hashes():
    """Hash continuations remain bound to their pinned distribution identity."""
    text = "dep==1 " + chr(92) + "\n    --hash=sha256:" + "a" * 64 + "\n"
    assert requirements(text) == {"dep": "1"}
    with pytest.raises(ValueError, match="distribution hash"):
        requirements(text.replace("a" * 64, "invalid"))

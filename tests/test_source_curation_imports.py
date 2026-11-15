"""Source review transport must start without loading selected curation replay."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    ["html_to_markdown.curation_model_evidence", "html_to_markdown.baseline_source_review"],
)
def test_source_transport_import_does_not_load_curation_orchestration(module):
    code = (
        f"import {module}\n"
        "import sys\n"
        "assert 'html_to_markdown.curation_renewal_jobs' not in sys.modules\n"
        "assert 'html_to_markdown.curation_existing_validation' not in sys.modules\n"
        "import html_to_markdown.curation_existing_validation\n"
        "import html_to_markdown.curation_renewal_jobs\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr

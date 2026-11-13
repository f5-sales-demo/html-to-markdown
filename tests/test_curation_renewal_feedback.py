"""Bounded editor corrections receive the actual unresolved source findings."""

import json

from test_curation_renewal import context, proposal
from test_curation_renewal_publication import Client, evidence

from html_to_markdown.curation_renewal_jobs import run_renewal


def test_unresolved_proposal_reasons_survive_into_exact_corrected_request(tmp_path):
    source = context()
    decision = proposal(source)
    explanation = "Keep the author's blank mapping unchanged while preserving port 8443."

    class UnresolvedClient(Client):
        emitted = False

        def execute(self, body):
            if body["metadata"]["content_review_role"] == "editor" and not self.emitted:
                self.emitted = True
                self.calls.append(body)
                return evidence(body, {**decision, "unresolved": [explanation]})
            return super().execute(body)

    client = UnresolvedClient(source, decision)
    result = run_renewal(source, [], tmp_path, client)
    corrected = json.loads(client.calls[1]["input"][0]["content"][0]["text"])
    assert explanation in corrected["validation_feedback"]
    assert len(client.calls) == 3
    assert result["independently_validated"] is True
    assert "8443" in result["candidate"]["body"]
    assert "| Optional mapping | |" in result["candidate"]["body"]

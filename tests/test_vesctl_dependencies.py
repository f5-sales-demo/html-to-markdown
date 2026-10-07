"""Fresh authored examples and dependencies that survive absent targets and later filters."""

import copy
import json
import re
from pathlib import Path
from urllib.parse import quote

import pytest
from test_vesctl_curation import BASE, document

from html_to_markdown.curation import (
    CurationPolicy,
    curate_topics,
    digest,
    json_bytes,
    load_curation_policy,
)
from html_to_markdown.models import DiscoveredPage, PageMetadata
from html_to_markdown.render import serialize_document, split_document
from html_to_markdown.state import StateStore
from html_to_markdown.urls import stable_path

WINGMAN = "https://docs.cloud.f5.com/docs-v2/multi-cloud-network-connect/how-tos/secret-mgmt/pp-secrets-using-wingman"


@pytest.mark.parametrize(
    "target",
    [
        WINGMAN,
        "https://docs.cloud.f5.com/docs-v2/docs/how-to/secret-mgmt/app-secrets-using-wingman",
        "https://docs.cloud.f5.com/docs-v2/multi-cloud-network-connect/how-to/adv-security/blindfold-tls-certs",
        "https://docs.cloud.f5.com/docs-v2/platform/how-to/volt-automation/vesctl",
    ],
)
@pytest.mark.parametrize(
    "syntax",
    [
        "inline",
        "reference",
        "html",
        "encoded",
        "twice_encoded",
        "local",
        "local_html",
        "local_reference",
    ],
)
def test_missing_targets_remain_detectable(tmp_path: Path, target: str, syntax: str) -> None:

    local = (
        "content/docs-cloud-f5-com/" + str(stable_path("docs-cloud-f5-com", target)) + "/index.md"
    )
    values = {
        "inline": f"[setup]({target}#workflow)",
        "reference": f"[setup][required]\n\n[required]: <{target}>",
        "html": f'<a href="{target}">setup</a>',
        "encoded": f"[setup]({quote(target, safe='')})",
        "twice_encoded": f"[setup]({quote(quote(target, safe=''), safe='')})",
        "local": f"[setup]({local})",
        "local_html": f'<a href="{local}">setup</a>',
        "local_reference": f"[setup][required]\n\n[required]: {local}",
    }
    path = document(tmp_path, "new", "# Guide\n\nRequired " + values[syntax] + "\n")
    result = curate_topics(tmp_path)
    assert not path.exists()
    assert result["counts"]["keep"] == 0


@pytest.mark.parametrize("syntax", ["inline", "html", "reference", "encoded"])
def test_transitive_original_graph_precedes_all_filters(tmp_path: Path, syntax: str) -> None:
    last = document(tmp_path, "z-last", "# Required\n\nUse vesctl.\n")
    middle = document(
        tmp_path,
        "y-middle",
        f"# Network\n\nKeep independent routes.\n\n- AppStack requires [setup]({BASE}z-last).\n",
    )
    url = BASE + "y-middle"
    value = {
        "inline": f"[setup]({url})",
        "html": '<a href="../y-middle/index.md">setup</a>',
        "reference": "[setup][required]\n\n[required]: ../y-middle/index.md",
        "encoded": f"[setup]({quote(quote(url, safe=''), safe='')})",
    }[syntax]
    first = document(
        tmp_path,
        "a-first",
        "# Network\n\nKeep independent routes.\n\n- AppStack needs " + value + "\n",
    )
    independent = document(
        tmp_path,
        "independent",
        "# Console\n\nSelect Blindfold New Secret, enter the webhook URL in Secret to Blindfold and Apply.\n\nEncode with `echo -n <URL> | base64`. Wingman supports GET /status. Manage ordinary TLS certificates.\n",
    )
    result = curate_topics(tmp_path)
    assert not first.exists() and not middle.exists() and not last.exists()
    assert independent.exists()
    assert result["counts"] == {"keep": 1, "remove": 1, "omit": 2}


@pytest.mark.parametrize("name", ["alerts-slack", "alerts-pagerduty", "alerts-opsgenie"])
def test_restored_reviewed_incomplete_guides_cannot_reenter(tmp_path: Path, name: str) -> None:
    url = "https://docs.cloud.f5.com/docs-v2/shared-configuration/how-tos/alerting/" + name
    path = document(
        tmp_path,
        name,
        "# Alerts\n\nEnter externally prepared ciphertext.\n",
        url=url,
        canonical_url=url,
    )
    assert curate_topics(tmp_path)["counts"]["remove"] == 1
    assert not path.exists()


@pytest.mark.parametrize(
    "name,body",
    [
        (
            "blindfold-tls-certs",
            "# Blindfold TLS Certificates\n\nDownload vesctl and run `vesctl request secrets encrypt key.pem`.\n",
        ),
        ("vesctl", "# Vesctl download and usage\n\nInstall the downloaded vesctl binary.\n"),
        (
            "pp-secrets-using-wingman",
            "# Encrypt and Decrypt Application Secrets using Wingman\n\nRun `vesctl request secrets get-public-key`.\n",
        ),
    ],
)
def test_fresh_authored_three_examples(tmp_path: Path, name: str, body: str) -> None:
    path = document(tmp_path, name, body)
    assert curate_topics(tmp_path)["counts"]["remove"] == 1
    assert not path.exists()


def test_digest_reviewed_wingman_reference_and_changed_inputs(tmp_path: Path) -> None:
    engine = load_curation_policy().vesctl
    assert engine is not None
    url = "https://docs.cloud.f5.com/docs-v2/platform/reference/wingman-api-reference"
    decision = engine.decisions[url]
    assert decision["source_sha256"] and decision["output_sha256"] != decision["input_sha256"]
    assert engine.transform("# API\n\nChanged reference.\n", url).omit


def test_local_target_removed_after_restoration(tmp_path: Path) -> None:
    target = document(tmp_path, "required", "# Network\n\nUse vesctl.\n")
    first = document(
        tmp_path, "first", "# Network\n\n- AppStack needs [setup](../required/index.md).\n"
    )
    assert curate_topics(tmp_path)["counts"] == {"keep": 0, "remove": 1, "omit": 1}
    assert not first.exists() and not target.exists()


@pytest.mark.parametrize("changed", ["none", "body", "metadata"])
def test_authored_incoming_fixtures(tmp_path: Path, changed: str) -> None:

    fixtures = json.loads((Path(__file__).parent / "fixtures/vesctl-dependencies.json").read_text())
    paths = {}
    for fixture in fixtures:
        metadata = dict(fixture["metadata"])
        body = fixture["body"]
        if fixture["name"] == "wingman-api-reference":
            if changed == "body":
                body += "\nChanged required action.\n"
            if changed == "metadata":
                metadata["tags"] = ["changed-authored-classification"]
        path = tmp_path / "content/docs-cloud-f5-com" / fixture["name"] / "index.md"
        path.parent.mkdir(parents=True)
        path.write_text(serialize_document(PageMetadata.model_validate(metadata), body))
        paths[fixture["name"]] = path
    audit = curate_topics(tmp_path)
    for name in ("alerts-slack", "alerts-pagerduty", "alerts-opsgenie"):
        assert not paths[name].exists()
    wingman = paths["wingman-api-reference"]
    if changed != "none":
        assert not wingman.exists()
    else:
        assert wingman.exists()
        body = split_document(wingman.read_text())[1]
        assert "pp-secrets-using-wingman" not in body
        assert "GET /status" in body and "POST /secret/unseal" in body
        assert "Base64-encoded" in body
        assert curate_topics(tmp_path)["counts"]["keep"] == 1
    assert audit["counts"]["keep"] == int(changed == "none")


def test_observability_procedures_survive_reviewed_optional_alert_removal(tmp_path: Path) -> None:

    fixtures = json.loads(
        (Path(__file__).parent / "fixtures/observability-dependencies.json").read_text()
    )
    raw = copy.deepcopy(load_curation_policy().raw)
    topic = next(t for t in raw["topics"] if t["id"] == "vesctl-retired")
    decision = next(d for d in topic["documents"] if d["url"].endswith("/adv-http-syn-mon"))
    # Synthetic image bytes exercise pixel guards without redistributing upstream screenshots.
    for item in decision["reviewed_media"]:
        item["sha256"] = digest(item["reference"].encode())
    paths = {}
    for fixture in fixtures:
        path = tmp_path / fixture["path"]
        path.parent.mkdir(parents=True)
        path.write_text(
            serialize_document(PageMetadata.model_validate(fixture["metadata"]), fixture["body"])
        )
        for reference in re.findall(r"assets/[a-zA-Z0-9_.-]+", fixture["body"]):
            asset = path.parent / reference
            asset.parent.mkdir(exist_ok=True)
            asset.write_bytes(reference.encode())
        paths[fixture["metadata"]["slug"]] = path
    policy = CurationPolicy(raw, digest(json_bytes(raw)))
    audit = curate_topics(tmp_path, policy=policy)
    assert audit["counts"] == {"keep": 4, "remove": 0, "omit": 0}
    advanced = split_document(paths["adv-http-syn-mon"].read_text())[1]
    assert "Step 6" not in advanced and "alerts-slack" not in advanced
    assert "Select **Add HTTP Monitor** to save" in advanced
    assert "Figure: Setting health policy" in advanced
    assert not (
        paths["adv-http-syn-mon"].parent
        / "assets/68dba405c10ae5c2f39603909d5d31197fe27d2abe8c1fd8d2e9f3f639343cbd.png"
    ).exists()
    for fixture in fixtures:
        if fixture["metadata"]["slug"] != "adv-http-syn-mon":
            assert (
                split_document(paths[fixture["metadata"]["slug"]].read_text())[1] == fixture["body"]
            )
    assert curate_topics(tmp_path, policy=policy)["counts"]["keep"] == 4
    store = StateStore(tmp_path / "state.sqlite")
    monitor_url = decision["url"]
    store.discover([DiscoveredPage(source_id="docs-cloud-f5-com", url=monitor_url)])
    store.replace_candidate_links(
        monitor_url,
        ["https://docs.cloud.f5.com/docs-v2/shared-configuration/how-tos/alerting/alerts-slack"],
    )
    assert curate_topics(tmp_path, store, policy=policy)["counts"]["keep"] == 4
    store.close()
    (paths["adv-http-syn-mon"].parent / decision["reviewed_media"][0]["reference"]).write_bytes(
        b"changed image pixels"
    )
    assert curate_topics(tmp_path, policy=policy)["counts"]["omit"] >= 1

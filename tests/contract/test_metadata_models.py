from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from backend.metadata.connectors import MetadataConnector, StaticMetadataConnector
from backend.metadata.models import DocumentType, MetadataDocument, MetadataQuery, SourceType
from backend.metadata.service import MetadataService
from tests.fakes.metadata_trees import seed_tree, settings_for

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.spec("S04-AC7")
def test_source_type_supports_future_connectors() -> None:
    assert {s.value for s in SourceType} == {"STATIC", "JIRA", "CONFLUENCE"}
    for source_type in ("STATIC", "JIRA", "CONFLUENCE"):
        doc = MetadataDocument.model_validate(
            {
                "document_id": "PAGE-1",
                "document_type": "RUNBOOK",
                "title": "Imported page",
                "source_type": source_type,
                "content": {"steps": ["look"]},
            }
        )
        assert doc.source_type == source_type
    assert (
        MetadataDocument(
            document_id="X", document_type=DocumentType.SOP, title="t", content={}
        ).source_type
        is SourceType.STATIC
    )


@pytest.mark.spec("S04-AC7")
async def test_only_a_static_connector_exists_and_it_satisfies_the_protocol(
    tmp_path: Path,
) -> None:
    import backend.metadata.connectors as connectors

    service = MetadataService(settings_for(seed_tree(tmp_path)))
    service.load_initial()
    connector = StaticMetadataConnector(service)
    assert isinstance(connector, MetadataConnector)
    implementations = [
        name for name, obj in vars(connectors).items()
        if isinstance(obj, type) and name.endswith("Connector") and obj is not MetadataConnector
    ]  # fmt: skip
    assert implementations == ["StaticMetadataConnector"]

    found = await connector.search(MetadataQuery(document_types=(DocumentType.SOP,)))
    assert [d.document_id for d in found] == ["SOP-PG-001", "SOP-PG-002"]
    assert all(d.source_type is SourceType.STATIC for d in found)
    assert found[0].source_reference == "sops/missing_partition.yaml"
    one = await connector.get("KI-DQ-001")
    assert one is not None and one.document_type is DocumentType.KNOWN_ISSUE
    assert await connector.get("JIRA-123") is None


@pytest.mark.spec("S04-AC7")
def test_no_jira_or_confluence_dependencies() -> None:
    forbidden = re.compile(r"jira|confluence|atlassian", re.IGNORECASE)
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    declared = [*project["project"]["dependencies"], *project["dependency-groups"]["dev"]]
    assert [d for d in declared if forbidden.search(d)] == []
    lock_names = re.findall(r'^name = "([^"]+)"', (REPO / "uv.lock").read_text("utf-8"), re.M)
    assert [n for n in lock_names if forbidden.search(n)] == []

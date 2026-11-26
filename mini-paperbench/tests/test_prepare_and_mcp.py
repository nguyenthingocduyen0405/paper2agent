from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import prepare
from hermes import paper_mcp


def test_prepare_first_heading_and_fit_paper():
    assert prepare.first_heading("# **Adam: Stochastic Optimization**\nBody") == "Adam: Stochastic Optimization"
    assert prepare.first_heading("No heading here") is None

    short = "# Title\nContent\n"
    assert prepare.fit_paper(short) == short

    body = "A" * (prepare.MAX_PAPER_CHARS - 50)
    with_refs = body + "\n## References\n" + ("R" * 5000)
    trimmed = prepare.fit_paper(with_refs)
    assert len(trimmed) <= prepare.MAX_PAPER_CHARS
    assert "References" not in trimmed

    with pytest.raises(SystemExit):
        prepare.fit_paper("X" * (prepare.MAX_PAPER_CHARS + 100))


def test_prepare_evidence_and_build_rubric():
    paper = "We train the **ResNet-18** model using Adam with learning rate 1e-3 for 50 epochs."
    normed = [prepare._norm(paper)]

    assert prepare.evidence_found("train the ResNet-18 model using Adam", normed)
    assert prepare.evidence_found("train the ResNet-18 ... for 50 epochs", normed)
    assert not prepare.evidence_found("too short", normed)
    assert not prepare.evidence_found("completely fabricated sentence from nowhere", normed)

    draft = prepare.RubricDraft(
        title="Test Paper",
        sections=[
            prepare.Section(
                name="Model",
                items=[
                    prepare.Item(
                        requirement="Implement ResNet-18 with Adam lr=1e-3",
                        category="Code Development",
                        evidence="train the ResNet-18 model using Adam with learning rate 1e-3",
                    )
                ],
            )
        ],
    )
    tree = prepare.build_rubric(draft, [paper])
    leaf = tree["sub_tasks"][0]["sub_tasks"][0]
    assert leaf["evidence_found"] is True
    assert leaf["task_category"] == "Code Development"

    no_code_draft = prepare.RubricDraft(
        title="No Code",
        sections=[
            prepare.Section(
                name="Results",
                items=[prepare.Item(requirement="Check accuracy", category="Result Analysis", evidence="train the ResNet-18 model")],
            )
        ],
    )
    with pytest.raises(ValueError):
        prepare.build_rubric(no_code_draft, [paper])


def test_mcp_protocol_handlers():
    assert paper_mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    init_resp = paper_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init_resp["result"]["serverInfo"]["name"] == "paperagent"

    ping_resp = paper_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert ping_resp["result"] == {}

    tools_resp = paper_mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
    assert tool_names == ["submit_paper", "job_status", "job_report", "list_code_files", "read_code_file"]

    bad_method = paper_mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "unknown/method"})
    assert bad_method["error"]["code"] == -32601

    bad_tool = paper_mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "shell"}})
    assert bad_tool["error"]["code"] == -32602


def test_mcp_security_boundaries(tmp_path: Path, monkeypatch):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4")
    bad_ext = uploads / "script.sh"
    bad_ext.write_text("echo pwn", encoding="utf-8")

    monkeypatch.setattr(paper_mcp, "UPLOAD_ROOT", str(uploads.resolve()))

    # Prompt-injected path outside UPLOAD_ROOT must be refused
    resp = paper_mcp.handle({
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {"name": "submit_paper", "arguments": {"file_path": str(outside)}},
    })
    assert resp["result"]["isError"] is True

    # Non-paper extension inside UPLOAD_ROOT must be refused
    resp = paper_mcp.handle({
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {"name": "submit_paper", "arguments": {"file_path": str(bad_ext)}},
    })
    assert resp["result"]["isError"] is True

    # Path traversal in read_code_file must be refused
    resp = paper_mcp.handle({
        "jsonrpc": "2.0",
        "id": 12,
        "method": "tools/call",
        "params": {"name": "read_code_file", "arguments": {"job_id": "abc123", "path": "../.env"}},
    })
    assert resp["result"]["isError"] is True

    # Invalid job_id must be refused
    with pytest.raises(paper_mcp.ToolError):
        paper_mcp._job_id({"job_id": "../admin"})

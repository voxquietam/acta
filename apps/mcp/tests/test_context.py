"""Tests for the per-request MCP context flag."""

from apps.mcp.context import IS_MCP_REQUEST, mcp_request_scope


def test_default_state_is_false():
    """Outside any MCP request the flag must read as False."""
    assert IS_MCP_REQUEST.get() is False


def test_scope_sets_flag_then_resets():
    """``mcp_request_scope`` toggles the flag for the inner block only."""
    assert IS_MCP_REQUEST.get() is False
    with mcp_request_scope():
        assert IS_MCP_REQUEST.get() is True
    assert IS_MCP_REQUEST.get() is False


def test_scope_resets_on_exception():
    """The flag must reset even if the inner code raises."""
    try:
        with mcp_request_scope():
            assert IS_MCP_REQUEST.get() is True
            raise RuntimeError("simulated tool failure")
    except RuntimeError:
        pass
    assert IS_MCP_REQUEST.get() is False

"""
@file test_flows.py
@brief Unit tests for deterministic flow-key and role inference helpers.
"""

import networkx as nx

from codebrain.ingestion.flows import _member_role, _stable_flow_key


def test_stable_flow_key_is_order_independent() -> None:
    """@brief Verify flow-key hashing is deterministic regardless of locator ordering."""
    repo_name = "codebrain"
    locators_a = [
        "src/a.ts::A.run@10-20",
        "src/b.ts::B.handle@3-8",
        "src/c.ts::C.save@1-7",
    ]
    locators_b = list(reversed(locators_a))

    assert _stable_flow_key(repo_name, locators_a) == _stable_flow_key(repo_name, locators_b)


def test_member_role_distinguishes_entrypoint_orchestrator_and_terminal() -> None:
    """@brief Verify flow-role inference uses in/out balance inside a component."""
    graph = nx.DiGraph()
    graph.add_edge(1, 2, weight=1.0)
    graph.add_edge(2, 3, weight=1.0)
    component = {1, 2, 3}

    assert _member_role(graph, 1, component) == "entrypoint"
    assert _member_role(graph, 2, component) == "orchestrator"
    assert _member_role(graph, 3, component) == "terminal"

from app.services.semi_kb import SemiKbAdapter


def test_semantic_counts_are_real():
    counts = SemiKbAdapter().semantic_counts()
    assert counts["classes"] > 0
    assert counts["properties"] > 0
    assert counts["semantic_triples"] > 0


def test_artifact_counts_include_vfab_state():
    counts = SemiKbAdapter().artifact_counts()
    assert counts["vfab_state"] in {"awaiting_source", "ready", "validated"}
    assert counts["simulation_scenarios"] >= 1


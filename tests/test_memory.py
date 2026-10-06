import pytest
import yaml

from steward.memory import CompanyPack, Memory
from steward.store import Store


@pytest.fixture
def pack(tmp_path):
    (tmp_path / "sops").mkdir()
    (tmp_path / "sops" / "refund.md").write_text(
        "---\nid: refund\ntitle: Customer refund\ntriggers: [refund, money back, charged twice]\n"
        "systems: [helpdesk, billing]\ndone_when: [refund recorded in billing, customer notified]\n---\n"
        "1. Open the ticket in helpdesk.\n2. Check the order in billing.\n3. Issue the refund per policy.\n")
    (tmp_path / "sops" / "onboard.md").write_text(
        "---\nid: onboard\ntitle: New employee onboarding\ntriggers: [new hire, onboarding, joiner]\n"
        "systems: [admin_console]\n---\nCreate the account, add to groups, raise a laptop request.\n")
    (tmp_path / "systems.yaml").write_text(yaml.dump({"systems": {"billing": {"url": "http://localhost:8001"},
                                                                  "helpdesk": {"url": "http://localhost:8002"}}}))
    (tmp_path / "directory.yaml").write_text(yaml.dump({"people": [{"name": "Priya Nair", "email": "priya@northwind.example", "role": "Finance Manager"}]}))
    return CompanyPack(tmp_path)


def test_pack_loads_sops_systems_people(pack):
    assert set(pack.sops) == {"refund", "onboard"}
    assert pack.sops["refund"].done_when[0].startswith("refund recorded")
    assert pack.lookup_person("priya")[0]["role"] == "Finance Manager"


def test_sop_retrieval_picks_the_right_procedure(pack):
    assert pack.search_sops("customer says they were charged twice, give money back")[0][0].id == "refund"
    assert pack.search_sops("we have a new hire joining Monday")[0][0].id == "onboard"
    assert pack.search_sops("zzzz qqqq") == []


def test_lessons_scoped_and_retrieved(tmp_path, pack):
    mem = Memory(Store(tmp_path / "m.db"), pack)
    mem.add_lesson("Enterprise customer refunds must go to the account manager, not auto-approved",
                   source="approval:apr_1", scope="sop:refund")
    mem.add_lesson("Onboarding laptops are ordered via IT desk", source="user", scope="sop:onboard")
    ctx = mem.context_for("customer wants a refund for enterprise plan")
    assert [s["id"] for s in ctx["candidate_sops"]][0] == "refund"
    assert any("account manager" in l["text"] for l in ctx["lessons"])
    assert not any("laptops" in l["text"] for l in ctx["lessons"])      # other SOP's lesson stays out
    assert set(ctx["systems"]) == {"billing", "helpdesk"}


def test_working_notes_upsert(tmp_path, pack):
    mem = Memory(Store(tmp_path / "m.db"), pack)
    mem.note("t1", "order", "A-77")
    mem.note("t1", "order", "A-78")
    assert mem.notes("t1") == {"order": "A-78"}

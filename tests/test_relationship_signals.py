from __future__ import annotations

from agent.models import GroupThread, MessageSample, PersonRecord, RolodexStore
from agent.relationship_signals import infer_relationship


def test_infer_relationship_marks_role_name_as_family() -> None:
    person = PersonRecord(person_id="p1", display_name="Dad", first_name="Dad")
    inference = infer_relationship(person, RolodexStore(people=[person]), "Pilkington")
    assert inference.label == "family"
    assert inference.rule_id == "role_name_first_name"
    assert inference.should_skip_llm is True


def test_infer_relationship_marks_family_prefix_display_name_as_family() -> None:
    person = PersonRecord(person_id="p1", display_name="Aunt Cheri")
    inference = infer_relationship(person, RolodexStore(people=[person]), "Pilkington")
    assert inference.label == "family"
    assert inference.rule_id == "family_prefix_display_name"


def test_infer_relationship_marks_cousin_prefix_as_family() -> None:
    person = PersonRecord(person_id="p1", display_name="Cousin Mike")
    inference = infer_relationship(person, RolodexStore(people=[person]), "Pilkington")
    assert inference.label == "family"
    assert inference.rule_id == "family_prefix_display_name"


def test_infer_relationship_uses_user_last_name_match() -> None:
    person = PersonRecord(person_id="p1", display_name="Aaron Pilkington", last_name="Pilkington")
    inference = infer_relationship(person, RolodexStore(people=[person]), "Pilkington")
    assert inference.label == "family"
    assert inference.rule_id == "surname_match_with_user"
    assert inference.should_skip_llm is False


def test_infer_relationship_marks_landlord_as_service_provider() -> None:
    person = PersonRecord(person_id="p1", display_name="Landlord Tom")
    inference = infer_relationship(person, RolodexStore(people=[person]), "Pilkington")
    assert inference.label == "service_provider"
    assert inference.rule_id == "service_provider_keyword"
    assert inference.should_skip_llm is True


def test_infer_relationship_marks_company_metadata_as_professional() -> None:
    person = PersonRecord(
        person_id="p1",
        display_name="Jamie",
        company="Acme Property Management LLC",
    )
    inference = infer_relationship(person, RolodexStore(people=[person]), "Pilkington")
    assert inference.label == "professional"
    assert inference.rule_id == "business_keywords"


def test_infer_relationship_marks_operator_note_mom_as_family() -> None:
    person = PersonRecord(person_id="p1", display_name="Stephanie Tocado", user_note="Stephanie Tocado is my mom")
    inference = infer_relationship(person, RolodexStore(people=[person]), "Pilkington")
    assert inference.label == "family"
    assert inference.rule_id == "operator_note_role"
    assert inference.confidence == 0.99


def test_infer_relationship_marks_operator_note_mother_contraction_as_family() -> None:
    person = PersonRecord(person_id="p1", display_name="Stephanie Tocado", user_note="she's my mother")
    inference = infer_relationship(person, RolodexStore(people=[person]), "Pilkington")
    assert inference.label == "family"
    assert inference.rule_id == "operator_note_role"


def test_infer_relationship_marks_operator_note_old_college_buddy() -> None:
    person = PersonRecord(person_id="p1", display_name="UNC Friend", user_note="old college buddy from UNC")
    inference = infer_relationship(person, RolodexStore(people=[person]), "Pilkington")
    assert inference.label == "old_friend"
    assert inference.rule_id == "operator_note_role"


def test_infer_relationship_marks_operator_note_business_partner() -> None:
    person = PersonRecord(person_id="p1", display_name="Alex", user_note="my business partner")
    inference = infer_relationship(person, RolodexStore(people=[person]), "Pilkington")
    assert inference.label == "business"
    assert inference.rule_id == "operator_note_business_partner"


def test_infer_relationship_marks_operator_note_realtor_as_service_provider() -> None:
    person = PersonRecord(person_id="p1", display_name="Pat", user_note="my realtor")
    inference = infer_relationship(person, RolodexStore(people=[person]), "Pilkington")
    assert inference.label == "service_provider"
    assert inference.rule_id == "operator_note_role"


def test_infer_relationship_uses_family_group_coparticipation_hint() -> None:
    group = GroupThread(chat_id=9, title="Family", handles=["+15550001", "+15550002", "+15550003"])
    mom = PersonRecord(person_id="mom", display_name="Mom", first_name="Mom", handles=["+15550001"], relationship_class="family", group_threads=[group])
    dad = PersonRecord(person_id="dad", display_name="Dad", first_name="Dad", handles=["+15550002"], relationship_class="family", group_threads=[group])
    unknown = PersonRecord(
        person_id="u1",
        display_name="+15550003",
        handles=["+15550003"],
        group_threads=[group],
        recent_messages=[
            MessageSample(direction="inbound", text="hi", handle="+15550003"),
        ],
    )
    store = RolodexStore(people=[mom, dad, unknown])

    inference = infer_relationship(unknown, store, "Pilkington")
    assert inference.label == "family"
    assert inference.rule_id == "family_group_coparticipation"
    assert inference.confidence == 0.6
    assert inference.should_skip_llm is False

from __future__ import annotations

from proximic_ring.interaction_associations import (
    AssociationActionRouter,
    AssociationMember,
    RecentFailureCoordinator,
)


def _member(
    number: int,
    *,
    mode: str = "dictation",
    target: str = "target-a",
    request_id: int = 0,
    result: str = "",
    timestamp: float | None = None,
) -> AssociationMember:
    return AssociationMember(
        interaction_id=f"interaction-{number}",
        session_id=number,
        request_id=request_id,
        mode=mode,
        target_key=target,
        asr_text=f"text-{number}",
        result_text=result,
        status="failed",
        occurred_monotonic=float(number if timestamp is None else timestamp),
    )


def test_recent_failures_stop_at_each_success_and_keep_only_five():
    coordinator = RecentFailureCoordinator(limit=5, max_age_s=60)
    for number in range(1, 8):
        coordinator.record_failure(_member(number))

    first = coordinator.record_success(_member(8))
    assert [item.interaction_id for item in first[0].rejected] == [
        "interaction-3",
        "interaction-4",
        "interaction-5",
        "interaction-6",
        "interaction-7",
    ]
    assert coordinator.record_success(_member(9)) == []


def test_target_or_mode_change_cuts_the_failure_chain():
    coordinator = RecentFailureCoordinator()
    coordinator.record_failure(_member(1, target="target-a"))
    coordinator.record_failure(_member(2, target="target-b"))
    assert coordinator.record_success(_member(3, target="target-a")) == []

    coordinator.record_failure(_member(4, mode="dictation"))
    coordinator.record_failure(
        _member(5, mode="edit", request_id=50, result="bad")
    )
    assert coordinator.record_success(_member(6, mode="dictation")) == []


def test_empty_asr_failure_can_follow_successfully_routed_mode():
    coordinator = RecentFailureCoordinator()
    empty = _member(1, mode="dictation")
    empty = AssociationMember(
        **{
            **empty.__dict__,
            "asr_text": "",
        }
    )
    coordinator.record_failure(empty)

    recommendations = coordinator.record_success(
        _member(2, mode="edit", request_id=20, result="编辑成功")
    )

    assert recommendations[0].kind == "asr"
    assert recommendations[0].subtype == "instruction_retry"
    assert [item.interaction_id for item in recommendations[0].rejected] == [
        "interaction-1"
    ]


def test_edit_success_can_recommend_asr_and_llm_groups():
    coordinator = RecentFailureCoordinator()
    coordinator.record_failure(
        _member(1, mode="edit", request_id=10, result="bad-1")
    )
    coordinator.record_failure(
        _member(2, mode="edit", request_id=20, result="bad-2")
    )
    recommendations = coordinator.record_success(
        _member(3, mode="edit", request_id=30, result="good")
    )
    assert [item.kind for item in recommendations] == ["asr", "llm"]
    assert all(len(item.rejected) == 2 for item in recommendations)


def test_action_router_keeps_input_surfaces_decoupled():
    received = []
    router = AssociationActionRouter()
    router.register("recommendation.accept", received.append)

    assert router.dispatch("recommendation.accept", "gesture") is True
    assert router.dispatch("missing", "button") is False
    assert received == ["gesture"]


def test_provisional_success_can_restore_failures_after_undo():
    coordinator = RecentFailureCoordinator(limit=5, max_age_s=60)
    failed = _member(1)
    first_success = _member(2)
    coordinator.record_failure(failed)
    recommendations = coordinator.record_success(first_success)
    assert [item.interaction_id for item in recommendations[0].rejected] == [
        "interaction-1"
    ]

    coordinator.restore_failures(recommendations)
    coordinator.record_failure(first_success)
    restored = coordinator.record_success(_member(3))
    assert [item.interaction_id for item in restored[0].rejected] == [
        "interaction-1",
        "interaction-2",
    ]

import pytest

from app.identity.challenge import (
    PASS_THRESHOLD,
    TASK_COUNT,
    ChallengeTask,
    generate_tasks,
    number_to_words,
    score,
)


def test_deterministic() -> None:
    assert generate_tasks(42) == generate_tasks(42)
    assert generate_tasks(42) != generate_tasks(43)


def test_count_and_shape() -> None:
    tasks = generate_tasks(7)
    assert len(tasks) == TASK_COUNT
    assert all(isinstance(t, ChallengeTask) and t.prompt and t.answer for t in tasks)


def test_all_kinds_present() -> None:
    prompts = " ".join(t.prompt for t in generate_tasks(1))
    for marker in ("Compute", "Reverse", "Count", "Sort", "shortest"):
        assert marker in prompts


def test_score_normalizes() -> None:
    tasks = [ChallengeTask("q1", "abc"), ChallengeTask("q2", "42")]
    assert score(tasks, [" ABC ", "42"]) == 2
    assert score(tasks, ["abc"]) == 1
    assert score(tasks, ["x", "y", "z"]) == 0


def test_perfect_answers_pass() -> None:
    tasks = generate_tasks(99)
    assert score(tasks, [t.answer for t in tasks]) == TASK_COUNT >= PASS_THRESHOLD


def test_arith_answers_are_digits_only() -> None:
    arith = [t for s in range(200) for t in generate_tasks(s) if t.prompt.startswith("Compute")]
    assert any("minus" in t.prompt for t in arith)
    assert all(t.answer.isdigit() for t in arith)


def test_every_prompt_states_answer_format() -> None:
    tasks = [t for s in range(50) for t in generate_tasks(s)]
    assert all(t.prompt.endswith(" only.") for t in tasks)


def test_number_to_words() -> None:
    assert number_to_words(0) == "zero"
    assert number_to_words(17) == "seventeen"
    assert number_to_words(42) == "forty-two"
    assert number_to_words(305) == "three hundred five"


def test_number_to_words_rejects_out_of_range() -> None:
    for n in (-1, 1000):
        with pytest.raises(ValueError):
            number_to_words(n)

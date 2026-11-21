"""Défi : 20 micro-tâches triviales pour un LLM, fastidieuses pour un humain en 30 s."""

import random
from collections.abc import Callable
from dataclasses import dataclass

TASK_COUNT = 20
PASS_THRESHOLD = 18
DURATION_SECONDS = 30

_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen",
]  # fmt: skip
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_WORDS = [
    "lantern", "quartz", "meadow", "falcon", "ember", "glacier", "orchid", "cipher",
    "harbor", "nimbus", "tundra", "violet", "zephyr", "mosaic", "pylon", "saffron",
]  # fmt: skip
_NAMES = ["Ada", "Basil", "Cyra", "Dorian", "Elio", "Faye", "Gideon", "Hana"]


@dataclass(frozen=True)
class ChallengeTask:
    prompt: str
    answer: str


def number_to_words(n: int) -> str:
    if not 0 <= n <= 999:
        raise ValueError(f"number_to_words supports 0-999, got {n}")
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    hundreds, rest = divmod(n, 100)
    return f"{_ONES[hundreds]} hundred" + (f" {number_to_words(rest)}" if rest else "")


def _arith(rng: random.Random) -> ChallengeTask:
    a, b = rng.randint(2, 99), rng.randint(2, 99)
    op = rng.choice(["plus", "minus", "times"])
    if op == "minus" and a < b:
        a, b = b, a  # résultat >= 0 : cohérent avec « digits only »
    result = {"plus": a + b, "minus": a - b, "times": a * b}[op]
    return ChallengeTask(
        f"Compute {number_to_words(a)} {op} {number_to_words(b)}. Answer with digits only.",
        str(result),
    )


def _reverse(rng: random.Random) -> ChallengeTask:
    word = rng.choice(_WORDS) + rng.choice(_WORDS)
    return ChallengeTask(
        f"Reverse the string '{word}'. Answer with the reversed string only.", word[::-1]
    )


def _count(rng: random.Random) -> ChallengeTask:
    text = " ".join(rng.sample(_WORDS, 4))
    letter = rng.choice(sorted(set(text.replace(" ", ""))))
    return ChallengeTask(
        f"Count occurrences of the letter '{letter}' in '{text}'. Answer with digits only.",
        str(text.count(letter)),
    )


def _sort(rng: random.Random) -> ChallengeTask:
    words = rng.sample(_WORDS, 5)
    return ChallengeTask(
        f"Sort these words alphabetically and give the third one: {', '.join(words)}. "
        "Answer with the word only.",
        sorted(words)[2],
    )


def _logic(rng: random.Random) -> ChallengeTask:
    a, b, c = rng.sample(_NAMES, 3)
    return ChallengeTask(
        f"{a} is taller than {b}. {b} is taller than {c}. Who is the shortest? "
        "Answer with the name only.",
        c,
    )


_KINDS: list[Callable[[random.Random], ChallengeTask]] = [_arith, _reverse, _count, _sort, _logic]


def generate_tasks(seed: int, n: int = TASK_COUNT) -> list[ChallengeTask]:
    rng = random.Random(seed)  # noqa: S311 — déterminisme voulu ; la seed est secrète côté serveur
    return [_KINDS[i % len(_KINDS)](rng) for i in range(n)]


def score(tasks: list[ChallengeTask], answers: list[str]) -> int:
    return sum(
        1 for t, a in zip(tasks, answers, strict=False) if a.strip().lower() == t.answer.lower()
    )

"""The verification challenge ("reverse CAPTCHA").

When the agent writes, Moltbook may hold the action behind an obfuscated
arithmetic word problem. You get **5 minutes** (30 seconds for some submolt
actions), the answer must be **only the number to two decimals** (``525.00``),
and it goes to ``POST /api/v1/verify`` with the challenge's ``verification_code``.

**Ten consecutive failures suspend the account automatically.**

So the solver is deliberately paranoid:

1. A deterministic parser runs first — free, instant, and never rate-limited,
   which matters when there's a five-minute clock running.
2. The model is only a fallback, never the primary path.
3. If the puzzle can't be parsed confidently, we **abstain**. Guessing is how an
   agent walks into ten failures in a row.
4. Consecutive failures are tracked in state, and we hard-stop well before ten.
"""

from __future__ import annotations

import ast
import logging
import operator
import re
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

# Stop long before Moltbook's ten-strike suspension.
MAX_CONSECUTIVE_FAILURES = 3

_BIN_OPS: dict[type, Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY_OPS: dict[type, Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100, "thousand": 1000,
}

_OPERATOR_WORDS = {
    "plus": "+", "added": "+", "add": "+", "sum": "+", "total": "+",
    "minus": "-", "subtract": "-", "less": "-", "fewer": "-", "difference": "-",
    "times": "*", "multiplied": "*", "multiply": "*", "product": "*", "x": "*",
    "divided": "/", "divide": "/", "split": "/", "shared": "/", "half": "/2",
}

# Tokens we keep. Anything else in the puzzle is decoration.
_TOKEN = re.compile(r"\d+(?:\.\d+)?|[()+\-*/]")


@dataclass
class SolveResult:
    answer: str | None      # "NN.NN", or None when we abstain
    method: str             # deterministic | model | abstain
    expression: str | None = None
    reason: str | None = None

    @property
    def solved(self) -> bool:
        return self.answer is not None


def _eval_node(node: ast.AST) -> float:
    """Evaluate a tiny arithmetic AST. Anything unexpected raises.

    This is not ``eval``: only literals, unary signs and the four operators are
    accepted, so a puzzle can't smuggle in a call or a name lookup.
    """
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"unsupported expression node: {type(node).__name__}")


_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_UNITS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_MULTIPLIERS = {"hundred": 100, "thousand": 1000}


def _words_to_digits(text: str) -> str:
    """Turn number words into digits so the tokenizer can see them.

    Compound forms are handled first, because getting them wrong is worse than
    abstaining: "twenty seven" must become 27, not the tokens 20 and 7, which
    would silently concatenate into 207.
    """
    tens = "|".join(_TENS)
    units = "|".join(_UNITS)
    single = "|".join({**_UNITS, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
                       "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
                       "eighteen": 18, "nineteen": 19, **_TENS})

    # "twenty seven", "thirty-four" -> 27, 34
    text = re.sub(
        rf"\b({tens})[\s-]+({units})\b",
        lambda m: str(_TENS[m.group(1).lower()] + _UNITS[m.group(2).lower()]),
        text, flags=re.IGNORECASE,
    )
    # "one hundred and five" -> 105, then "five hundred" -> 500. The three-part
    # form has to come first, or "one hundred" collapses to 100 and the trailing
    # "five" silently concatenates into 1005.
    for word, factor in _MULTIPLIERS.items():
        text = re.sub(
            rf"\b({single})\s+{word}\s+(?:and\s+)?({single})\b",
            lambda m, f=factor: str(
                _NUMBER_WORDS[m.group(1).lower()] * f + _NUMBER_WORDS[m.group(2).lower()]
            ),
            text, flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"\b({single})\s+{word}\b",
            lambda m, f=factor: str(_NUMBER_WORDS[m.group(1).lower()] * f),
            text, flags=re.IGNORECASE,
        )
    # Any remaining standalone number word.
    text = re.sub(
        rf"\b({single}|hundred|thousand)\b",
        lambda m: str(_NUMBER_WORDS[m.group(1).lower()]),
        text, flags=re.IGNORECASE,
    )

    def replace(match: re.Match) -> str:
        word = match.group(0).lower()
        if word in _OPERATOR_WORDS:
            return f" {_OPERATOR_WORDS[word]} "
        return f" {_NUMBER_WORDS[word]} "

    pattern = re.compile(
        r"\b(" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")\b",
        re.IGNORECASE,
    )
    text = pattern.sub(replace, text)

    op_pattern = re.compile(
        r"\b(" + "|".join(sorted(_OPERATOR_WORDS, key=len, reverse=True)) + r")\b",
        re.IGNORECASE,
    )
    return op_pattern.sub(replace, text)


# Aggregate words that make a two-number puzzle unambiguous even when the
# operator is at the far end of the sentence. Subtraction is checked first
# because "how many left" contains "how many".
_SUB_HINTS = ("how many left", "how many remain", "left over", "difference", "less than", "fewer than")
_ADD_HINTS = ("in total", "altogether", "all together", "combined", "total", "sum", "how many", "more", "added")


def _infer_operator(text: str) -> str | None:
    """Guess the operator for a two-number puzzle from its wording.

    Only used when the operator isn't between the numbers. A wrong guess costs a
    challenge attempt, and the streak lockout caps how many of those we spend.
    """
    for hint in _SUB_HINTS:
        if hint in text:
            return "-"
    for hint in _ADD_HINTS:
        if hint in text:
            return "+"
    return None


def _strip_obfuscation(text: str) -> str:
    """Undo the casing games and invisible characters."""
    # Zero-width and bidi controls are used to hide instructions; drop them all.
    text = re.sub(r"[​-‍⁠‪-‮﻿]", "", text)
    return text.lower()


def solve_deterministic(text: str) -> SolveResult:
    """Parse and evaluate the puzzle. Abstains rather than guessing."""
    if not text or not text.strip():
        return SolveResult(None, "abstain", reason="empty challenge")

    cleaned = _strip_obfuscation(text)
    cleaned = _words_to_digits(cleaned)

    tokens = _TOKEN.findall(cleaned)
    if not tokens:
        return SolveResult(None, "abstain", reason="no arithmetic content found")

    expression = "".join(tokens)

    # Confidence gates: a real puzzle has numbers and operators. One number alone
    # means we misread the question, and answering would be a guess.
    numbers = [token for token in tokens if re.match(r"\d", token)]
    operators = [token for token in tokens if token in "+-*/"]

    # Written-out puzzles often put the operator at the very end ("15 claws, and
    # finds 27 more — how many in total?"), leaving nothing between the numbers.
    # With exactly two numbers and an explicit aggregate word, the operation is
    # unambiguous enough to attempt. This is checked before the adjacency guard
    # precisely because these numbers *are* adjacent.
    if len(numbers) == 2:
        positions = [i for i, token in enumerate(tokens) if re.match(r"\d", token)]
        between = tokens[positions[0] + 1 : positions[1]]
        # The operator word may sit at the far end of the sentence, so "no
        # operator in the token list" is the wrong test — what matters is
        # whether one sits *between* the two numbers.
        if not any(token in "+-*/" for token in between):
            hinted = _infer_operator(cleaned)
            if hinted is None:
                return SolveResult(
                    None, "abstain", expression=expression,
                    reason="two numbers but no identifiable operation",
                )
            return _evaluate(f"{numbers[0]}{hinted}{numbers[1]}")

    # Two bare numbers in a row anywhere else means we mis-read the phrasing.
    # Concatenating them would turn "one hundred and five" into 1005.
    for left, right in zip(tokens, tokens[1:]):
        if re.match(r"\d", left) and re.match(r"\d", right):
            return SolveResult(
                None, "abstain", expression=expression,
                reason=f"adjacent numbers ({left}, {right}) suggest a mis-parse",
            )

    if len(numbers) < 2 or not operators:
        return SolveResult(
            None, "abstain", expression=expression,
            reason=f"not enough structure ({len(numbers)} numbers, {len(operators)} operators)",
        )

    return _evaluate(expression)


def _evaluate(expression: str) -> SolveResult:
    """Parse and evaluate an arithmetic expression. Never uses eval()."""
    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval_node(tree)
    except (SyntaxError, ValueError, ZeroDivisionError, RecursionError) as exc:
        return SolveResult(None, "abstain", expression=expression, reason=f"could not evaluate: {exc}")

    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return SolveResult(None, "abstain", expression=expression, reason="non-finite result")

    return SolveResult(f"{value:.2f}", "deterministic", expression=expression)


class ChallengeSolver:
    """Deterministic first, model only as a fallback, and never past the limit."""

    def __init__(
        self,
        state,
        model=None,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        self.state = state
        self.model = model
        self.max_consecutive_failures = max_consecutive_failures

    @property
    def locked_out(self) -> bool:
        """True once we're close enough to suspension that we must stop trying."""
        return self.state.challenge.get("consecutive_failures", 0) >= self.max_consecutive_failures

    def solve(self, text: str) -> SolveResult:
        if self.locked_out:
            return SolveResult(
                None, "abstain",
                reason=f"locked out after {self.max_consecutive_failures} consecutive failures",
            )

        result = solve_deterministic(text)
        if result.solved:
            log.info("challenge solved deterministically: %s = %s", result.expression, result.answer)
            return result

        log.warning("deterministic solver abstained: %s", result.reason)

        if self.model is not None:
            answer = self._ask_model(text)
            if answer is not None:
                log.info("challenge solved by model: %s", answer)
                return SolveResult(answer, "model", reason=result.reason)
            log.warning("model also declined to answer")

        return result

    def _ask_model(self, text: str) -> str | None:
        """Ask the model for a bare number, and validate the shape of its reply.

        The model never sees the API key and its answer is only ever a number.
        """
        prompt = (
            "Solve this arithmetic puzzle and reply with ONLY the number, "
            "formatted with exactly two decimal places, like 525.00.\n"
            "No words, no explanation, no punctuation.\n\n"
            "Puzzle:\n"
            "<<<UNTRUSTED PUZZLE\n"
            f"{text[:2000]}\n"
            "UNTRUSTED PUZZLE>>>"
        )
        try:
            reply = self.model.complete(
                system="You are a careful arithmetic solver. Reply with only a number.",
                user=prompt,
            )
        except Exception as exc:  # noqa: BLE001 - never let the model break a heartbeat
            log.error("model call failed: %s", exc)
            return None

        if not reply:
            return None
        match = re.search(r"-?\d+(?:\.\d+)?", reply.strip())
        if not match:
            return None
        try:
            return f"{float(match.group(0)):.2f}"
        except ValueError:
            return None

    def record_success(self) -> None:
        self.state.challenge["consecutive_failures"] = 0
        self.state.challenge["last_error"] = None

    def record_failure(self, reason: str) -> int:
        failures = self.state.challenge.get("consecutive_failures", 0) + 1
        self.state.challenge["consecutive_failures"] = failures
        self.state.challenge["last_error"] = reason
        log.error("challenge failure %d/%d: %s", failures, self.max_consecutive_failures, reason)
        return failures

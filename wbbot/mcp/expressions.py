r"""Safe evaluation of user-supplied mathematical expressions.

``eval`` is not an option: the expressions come from an AI acting on somebody's
prompt, and the process running them can drive a motor.

The defence is two independent layers:

1. **Lexical whitelist.**  The raw string is tokenised with Python's own
   ``tokenize`` module and every name is checked against an allow-list.  Any
   attribute access, any dunder, any keyword such as ``import`` or ``lambda``,
   and any name that is not a known function or variable is rejected *before*
   a parser ever sees it.
2. **Restricted parse.**  Only then is the expression handed to SymPy's
   ``parse_expr`` with an explicit ``global_dict`` containing nothing but the
   whitelisted symbols.  SymPy's own documentation warns that ``sympify`` is
   not safe on hostile input, so it never sees anything that has not already
   passed layer 1.

The result is a SymPy expression, which is then compiled with ``lambdify`` for
fast numpy evaluation.
"""

from __future__ import annotations

import io
import math
import tokenize
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

import numpy as np

__all__ = [
    "ExpressionError",
    "ALLOWED_FUNCTIONS",
    "ALLOWED_CONSTANTS",
    "validate_expression",
    "compile_expression",
    "CompiledExpression",
]


class ExpressionError(ValueError):
    """The expression is malformed, unsupported, or not safe to evaluate."""


#: Functions an expression may call.
ALLOWED_FUNCTIONS: frozenset[str] = frozenset({
    "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
    "sinh", "cosh", "tanh", "asinh", "acosh", "atanh",
    "exp", "log", "ln", "log10", "log2", "sqrt", "cbrt",
    "abs", "Abs", "sign", "floor", "ceiling", "ceil", "round",
    "min", "Min", "max", "Max", "Mod", "factorial",
    "gamma", "erf", "erfc", "Piecewise", "Heaviside",
    "re", "im", "conjugate", "arg",
})

#: Constants an expression may reference.
ALLOWED_CONSTANTS: frozenset[str] = frozenset({
    "pi", "PI", "Pi", "e", "E", "tau", "oo", "inf", "I",
    "GoldenRatio", "EulerGamma",
})

#: Tokens that are never acceptable, whatever the context.
_FORBIDDEN_SUBSTRINGS = ("__", "import", "lambda", "exec", "eval", "open",
                         "os.", "sys.", "subprocess", "globals", "locals",
                         "getattr", "setattr", "compile", "input", "breakpoint")

_ALLOWED_OPERATORS = frozenset({
    "+", "-", "*", "/", "**", "%", "(", ")", ",", "^",
    "<", ">", "<=", ">=", "==", "!=",
})


@dataclass
class CompiledExpression:
    """A validated expression plus its fast numeric evaluator."""

    source: str
    variables: tuple[str, ...]
    sympy_expr: object
    _func: Callable

    def __call__(self, *args: np.ndarray) -> np.ndarray:
        if len(args) != len(self.variables):
            raise ExpressionError(
                f"expression {self.source!r} needs {len(self.variables)} "
                f"argument(s) {self.variables}, got {len(args)}")
        with np.errstate(all="ignore"):
            result = self._func(*args)
        # A constant expression lambdifies to a scalar; broadcast it back.
        arr = np.asarray(result, dtype=float)
        if arr.ndim == 0 and args:
            arr = np.full_like(np.asarray(args[0], dtype=float), float(arr))
        return arr

    def __str__(self) -> str:      # pragma: no cover
        return f"{self.source} of {self.variables}"


def validate_expression(source: str, variables: Sequence[str]) -> None:
    """Lexical safety check. Raises :class:`ExpressionError` if suspicious."""
    if not isinstance(source, str) or not source.strip():
        raise ExpressionError("the expression is empty")
    if len(source) > 2000:
        raise ExpressionError(
            f"the expression is {len(source)} characters long; the limit is 2000")

    lowered = source.lower()
    for bad in _FORBIDDEN_SUBSTRINGS:
        if bad in lowered:
            raise ExpressionError(
                f"the expression contains {bad!r}, which is not allowed")

    allowed_names = set(ALLOWED_FUNCTIONS) | set(ALLOWED_CONSTANTS) | set(variables)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        raise ExpressionError(f"the expression is not parseable: {exc}") from exc

    for tok in tokens:
        if tok.type in (tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER,
                        tokenize.INDENT, tokenize.DEDENT, tokenize.COMMENT):
            continue
        if tok.type == tokenize.NUMBER:
            continue
        if tok.type == tokenize.NAME:
            if tok.string not in allowed_names:
                raise ExpressionError(
                    f"unknown name {tok.string!r} in the expression. "
                    f"Variables here are {', '.join(variables) or 'none'}; "
                    f"allowed functions are "
                    f"{', '.join(sorted(ALLOWED_FUNCTIONS))}")
            continue
        if tok.type == tokenize.OP:
            if tok.string == ".":
                raise ExpressionError(
                    "attribute access ('.') is not allowed in expressions; "
                    "write decimals as 0.5 rather than .5 if that was the intent"
                    if source.count(".") else "attribute access is not allowed")
            if tok.string not in _ALLOWED_OPERATORS:
                raise ExpressionError(
                    f"the operator {tok.string!r} is not allowed in expressions")
            continue
        if tok.type == tokenize.STRING:
            raise ExpressionError("string literals are not allowed in expressions")
        raise ExpressionError(
            f"unexpected token {tok.string!r} in the expression")

    # A lone '.' inside a NUMBER token is fine; the check above only sees OP.
    if source.count("(") != source.count(")"):
        raise ExpressionError("unbalanced parentheses in the expression")


def compile_expression(source: str,
                       variables: Sequence[str] = ("x",)) -> CompiledExpression:
    """Validate, parse and compile an expression for numeric evaluation."""
    variables = tuple(variables)
    validate_expression(source, variables)

    try:
        import sympy                                        # noqa: PLC0415
        from sympy.parsing.sympy_parser import (            # noqa: PLC0415
            convert_xor, implicit_multiplication_application,
            parse_expr, standard_transformations)
    except ImportError as exc:  # pragma: no cover
        raise ExpressionError("SymPy is required to evaluate expressions") from exc

    symbols = {name: sympy.Symbol(name, real=True) for name in variables}
    namespace: dict[str, object] = dict(symbols)
    for name in ALLOWED_FUNCTIONS:
        obj = getattr(sympy, name, None)
        if obj is not None:
            namespace[name] = obj

    # Names people actually type that SymPy spells differently. Without these
    # the implicit-multiplication transformation silently reinterprets, say,
    # `abs` as `a*b*s` and the error surfaces as "undefined variable s".
    aliases: dict[str, object] = {
        "abs": sympy.Abs,
        "ln": sympy.log,
        "log10": lambda a: sympy.log(a, 10),
        "log2": lambda a: sympy.log(a, 2),
        "ceil": sympy.ceiling,
        "round": lambda a: sympy.floor(a + sympy.Rational(1, 2)),
        "min": sympy.Min,
        "max": sympy.Max,
        "cbrt": sympy.cbrt,
    }
    for name, obj in aliases.items():
        namespace.setdefault(name, obj)
    namespace["pi"] = namespace["PI"] = namespace["Pi"] = sympy.pi
    namespace["e"] = namespace["E"] = sympy.E
    namespace["tau"] = 2 * sympy.pi
    namespace["oo"] = namespace["inf"] = sympy.oo
    namespace["I"] = sympy.I
    namespace["GoldenRatio"] = sympy.GoldenRatio
    namespace["EulerGamma"] = sympy.EulerGamma

    # SymPy's `auto_number` transformation rewrites literals as `Integer(2)`
    # and `Float('1.5')`, so those constructors must resolve at eval time.
    # This does not widen the attack surface: the lexical whitelist above runs
    # on the *raw* string and rejects these names outright, so they can only
    # ever appear because SymPy itself just inserted them.
    for helper in ("Integer", "Float", "Rational", "Symbol"):
        namespace[helper] = getattr(sympy, helper)

    transformations = (standard_transformations
                       + (implicit_multiplication_application, convert_xor))
    try:
        expr = parse_expr(source, local_dict=namespace, global_dict={},
                          transformations=transformations, evaluate=True)
    except Exception as exc:
        raise ExpressionError(
            f"cannot parse {source!r}: {exc}") from exc

    free = {str(s) for s in getattr(expr, "free_symbols", set())}
    unknown = free - set(variables)
    if unknown:
        raise ExpressionError(
            f"the expression uses undefined variable(s) "
            f"{', '.join(sorted(unknown))}; only {', '.join(variables)} "
            f"are available here")

    try:
        func = sympy.lambdify([symbols[v] for v in variables], expr, "numpy")
    except Exception as exc:
        raise ExpressionError(
            f"cannot compile {source!r} for evaluation: {exc}") from exc

    return CompiledExpression(source=source, variables=variables,
                              sympy_expr=expr, _func=func)

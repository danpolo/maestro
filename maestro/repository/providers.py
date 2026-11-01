"""Structural providers: what one file *says*, extracted without an index (`[P9]`).

`06_REPOSITORY_INTELLIGENCE.md` §1-§2. Two layers live here, behind one `ParseResult`:

* **Layer 0 — `PythonAstProvider`.** The standard library `ast`. Always available. Exact
  on well-formed code and useless on broken code: one syntax error and the whole file
  yields nothing, which is reported (`parse_error`, `completeness="failed"`) rather than
  quietly returned as "no definitions".
* **Layer 1 — `TreeSitterProvider`.** Parse-error tolerant, which is the only reason it
  is here: an agent worktree mid-edit is routinely unparseable, and a definition above
  the broken line is still a fact. Results from a tree with errors are `partial`.

Neither provider is trusted to have the last word. A file whose parse failed is answered
by the lexical fallback (`lexical_definitions`, and `git grep`/`rg` in `queries.py`),
because search and read are permanent (`SEARCH_READ_FALLBACK_PERMANENT`).

**The grammar gate (`[INV-A09]`, `[INV-12]`).** Tree-sitter is not loaded because it
imports. `grammar_gate` requires the parser row *and* the row of every grammar in
`GRAMMARS` to be `verified` against the installed version in `maestro/thirdparty.json`
— the same comparison `maestro doctor --thirdparty` makes — and a failed gate selects
Layer 0 and says why. A grammar added to `GRAMMARS` without its own row is refused.

Explicitly absent: `tree-sitter-graph`, SCIP, Joern, embeddings (§2.2). SCIP and Joern
are gated behind experiments this phase does not run.
"""
from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

__all__ = [
    "SymbolDefinition",
    "ImportRelation",
    "ParseResult",
    "Provider",
    "PythonAstProvider",
    "TreeSitterProvider",
    "GateResult",
    "GRAMMARS",
    "PARSER_SUBJECT",
    "grammar_gate",
    "select_provider",
    "lexical_definitions",
    "content_hash",
    "module_name_for",
]

#: Completeness of one extraction, in worsening order.
COMPLETENESS = ("complete", "partial", "failed")

#: The parser distribution's register subject.
PARSER_SUBJECT = "tree-sitter"

#: Enabled grammars: language -> (register subject, import name, file suffixes). Every
#: entry needs its own verified row; that is the whole of `[INV-A09]`.
GRAMMARS: Mapping[str, tuple[str, str, tuple[str, ...]]] = {
    "python": ("tree-sitter-python", "tree_sitter_python", (".py",)),
}


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def module_name_for(path: str) -> Optional[str]:
    """`pkg/sub/mod.py` -> `pkg.sub.mod`; `pkg/__init__.py` -> `pkg`. None for non-Python."""
    if not path.endswith(".py"):
        return None
    parts = path[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


# ── extracted facts ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SymbolDefinition:
    name: str
    kind: str                 # 'function' | 'class' | 'method'
    path: str
    start_line: int
    end_line: int
    provider: str             # 'ast' | 'tree_sitter' | 'search'
    qualname: str = ""
    signature_end_line: int = 0  # last line of the header, so a signature slice has no body
    partial: bool = False     # extracted from a file that did not parse cleanly

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class ImportRelation:
    importer: str             # path of the importing file
    module: str               # absolute dotted module the statement names
    names: tuple[str, ...]    # names imported from it (empty for `import x`)
    line: int
    provider: str
    resolved_path: Optional[str] = None  # filled by the query layer against a snapshot

    def to_dict(self) -> dict:
        out = dict(self.__dict__)
        out["names"] = list(self.names)
        return out


@dataclass(frozen=True)
class ParseResult:
    path: str
    content_hash: str
    provider: str
    completeness: str
    definitions: tuple[SymbolDefinition, ...] = ()
    imports: tuple[ImportRelation, ...] = ()
    parse_error: bool = False
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.completeness != "failed"


class Provider(Protocol):
    name: str

    def handles(self, path: str) -> bool: ...

    def parse(self, path: str, data: bytes) -> ParseResult: ...


# ── Layer 0: Python ast ─────────────────────────────────────────────────────────────


def _absolute_module(importer: str, module: Optional[str], level: int) -> str:
    """Resolve a relative import against the importing file's package."""
    if not level:
        return module or ""
    package = (module_name_for(importer) or "").split(".")
    if not importer.endswith("__init__.py"):
        package = package[:-1]
    base = package[: len(package) - (level - 1)] if level > 1 else package
    return ".".join([*base, *( [module] if module else [] )])


class PythonAstProvider:
    name = "ast"

    def handles(self, path: str) -> bool:
        return path.endswith(".py")

    def parse(self, path: str, data: bytes) -> ParseResult:
        digest = content_hash(data)
        try:
            tree = ast.parse(data, filename=path)
        except (SyntaxError, ValueError) as exc:
            return ParseResult(
                path=path, content_hash=digest, provider=self.name, completeness="failed",
                parse_error=True, detail=f"{type(exc).__name__}: {exc}",
            )
        definitions: list[SymbolDefinition] = []
        imports: list[ImportRelation] = []

        def visit(node: ast.AST, scope: list[str], in_class: bool) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    is_class = isinstance(child, ast.ClassDef)
                    body_start = child.body[0].lineno if child.body else child.lineno
                    definitions.append(SymbolDefinition(
                        name=child.name,
                        kind="class" if is_class else ("method" if in_class else "function"),
                        path=path, start_line=child.lineno,
                        end_line=getattr(child, "end_lineno", child.lineno) or child.lineno,
                        provider=self.name, qualname=".".join([*scope, child.name]),
                        signature_end_line=max(child.lineno, body_start - 1),
                    ))
                    visit(child, [*scope, child.name], is_class)
                elif isinstance(child, ast.Import):
                    for alias in child.names:
                        imports.append(ImportRelation(
                            importer=path, module=alias.name, names=(), line=child.lineno,
                            provider=self.name,
                        ))
                elif isinstance(child, ast.ImportFrom):
                    imports.append(ImportRelation(
                        importer=path,
                        module=_absolute_module(path, child.module, child.level),
                        names=tuple(a.name for a in child.names), line=child.lineno,
                        provider=self.name,
                    ))
                else:
                    visit(child, scope, in_class)

        visit(tree, [], False)
        return ParseResult(
            path=path, content_hash=digest, provider=self.name, completeness="complete",
            definitions=tuple(definitions), imports=tuple(imports),
        )


# ── lexical fallback ────────────────────────────────────────────────────────────────

_LEXICAL_DEF = re.compile(r"^(?P<indent>[ \t]*)(?:async[ \t]+)?(?P<kw>def|class)[ \t]+(?P<name>[A-Za-z_]\w*)")


def lexical_definitions(path: str, data: bytes, name: Optional[str] = None) -> tuple[SymbolDefinition, ...]:
    """Line-regex definitions for a file no parser could read. Always `partial`.

    It cannot know where a body ends, so `end_line` is the header line: a consumer that
    wants the body must read the file, which is the fallback working as intended.
    """
    out = []
    for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), start=1):
        match = _LEXICAL_DEF.match(line)
        if not match or (name is not None and match.group("name") != name):
            continue
        kind = "class" if match.group("kw") == "class" else (
            "method" if match.group("indent") else "function"
        )
        out.append(SymbolDefinition(
            name=match.group("name"), kind=kind, path=path, start_line=number,
            end_line=number, provider="search", qualname=match.group("name"),
            signature_end_line=number, partial=True,
        ))
    return tuple(out)


# ── Layer 1: tree-sitter ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateResult:
    enabled: bool
    reasons: tuple[str, ...] = ()
    grammars: tuple[str, ...] = ()


def grammar_gate(
    *,
    register: Optional[Mapping[str, Any]] = None,
    distribution_version: Optional[Callable[[str], Optional[str]]] = None,
    grammars: Optional[Mapping[str, tuple[str, str, tuple[str, ...]]]] = None,
) -> GateResult:
    """May tree-sitter be loaded on this host? Parser and every grammar must be `verified`."""
    from maestro import thirdparty

    grammars = GRAMMARS if grammars is None else grammars
    try:
        register = thirdparty.load_register() if register is None else register
    except (OSError, ValueError) as exc:
        return GateResult(False, (f"thirdparty register unreadable: {exc}",))
    probe = distribution_version or thirdparty.installed_distribution_version

    reasons = []
    for subject in (PARSER_SUBJECT, *(entry[0] for entry in grammars.values())):
        record = register.get(subject)
        if record is None:
            reasons.append(f"{subject}: no row in maestro/thirdparty.json")
            continue
        status = thirdparty.status_for(record, probe(subject))
        if status.status != "verified":
            reasons.append(f"{subject}: {status.status} ({status.detail})")
    if reasons:
        return GateResult(False, tuple(reasons))
    return GateResult(True, grammars=tuple(sorted(grammars)))


_TS_KINDS = {"function_definition": "function", "class_definition": "class"}


class TreeSitterProvider:
    """Tolerant extraction. Construct only through `select_provider` / after the gate."""

    name = "tree_sitter"

    def __init__(self, grammars: Optional[Mapping[str, tuple[str, str, tuple[str, ...]]]] = None) -> None:
        import importlib

        import tree_sitter

        self._ts = tree_sitter
        self._parsers: dict[str, Any] = {}
        for language, (_subject, module, suffixes) in (GRAMMARS if grammars is None else grammars).items():
            grammar = importlib.import_module(module)
            parser = tree_sitter.Parser(tree_sitter.Language(grammar.language()))
            for suffix in suffixes:
                self._parsers[suffix] = parser

    def handles(self, path: str) -> bool:
        return any(path.endswith(suffix) for suffix in self._parsers)

    def parse(self, path: str, data: bytes) -> ParseResult:
        digest = content_hash(data)
        parser = next(p for s, p in self._parsers.items() if path.endswith(s))
        try:
            tree = parser.parse(data)
        except Exception as exc:  # a native parser failure must not take the query down
            return ParseResult(
                path=path, content_hash=digest, provider=self.name, completeness="failed",
                parse_error=True, detail=f"tree-sitter raised {type(exc).__name__}: {exc}",
            )
        root = tree.root_node
        definitions: list[SymbolDefinition] = []
        imports: list[ImportRelation] = []

        def text(node) -> str:
            return data[node.start_byte:node.end_byte].decode("utf-8", "replace")

        def walk(node, scope: list[str], in_class: bool) -> None:
            for child in node.children:
                kind = _TS_KINDS.get(child.type)
                if kind:
                    name_node = child.child_by_field_name("name")
                    body = child.child_by_field_name("body")
                    if name_node is None or name_node.is_missing:
                        walk(child, scope, in_class)
                        continue
                    name = text(name_node)
                    start = child.start_point[0] + 1
                    definitions.append(SymbolDefinition(
                        name=name,
                        kind="method" if kind == "function" and in_class else kind,
                        path=path, start_line=start, end_line=child.end_point[0] + 1,
                        provider=self.name, qualname=".".join([*scope, name]),
                        signature_end_line=(
                            max(start, body.start_point[0]) if body is not None else start
                        ),
                        partial=child.has_error,
                    ))
                    walk(child, [*scope, name], kind == "class")
                elif child.type == "import_statement":
                    for name_node in child.children_by_field_name("name"):
                        target = name_node.child_by_field_name("name") or name_node
                        imports.append(ImportRelation(
                            importer=path, module=text(target), names=(),
                            line=child.start_point[0] + 1, provider=self.name,
                        ))
                elif child.type == "import_from_statement":
                    module_node = child.child_by_field_name("module_name")
                    raw = text(module_node) if module_node is not None else ""
                    level = len(raw) - len(raw.lstrip("."))
                    names = []
                    for name_node in child.children_by_field_name("name"):
                        target = name_node.child_by_field_name("name") or name_node
                        names.append(text(target))
                    if child.children and any(c.type == "wildcard_import" for c in child.children):
                        names.append("*")
                    imports.append(ImportRelation(
                        importer=path,
                        module=_absolute_module(path, raw.lstrip(".") or None, level),
                        names=tuple(names), line=child.start_point[0] + 1, provider=self.name,
                    ))
                else:
                    walk(child, scope, in_class)

        walk(root, [], False)
        return ParseResult(
            path=path, content_hash=digest, provider=self.name,
            completeness="partial" if root.has_error else "complete",
            definitions=tuple(definitions), imports=tuple(imports),
            parse_error=root.has_error,
            detail="syntax errors present; definitions outside them are reported" if root.has_error else "",
        )


@dataclass
class ProviderSelection:
    provider: Provider
    fallback: Provider
    gate: GateResult
    detail: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Identifies the extractor, so an index built by another one is not reused as fresh."""
        return f"{self.provider.name}+{self.fallback.name}:{','.join(self.gate.grammars)}"


def select_provider(*, prefer_tree_sitter: bool = True, gate: Optional[GateResult] = None) -> ProviderSelection:
    """Layer 1 when the gate passes and the parser loads; Layer 0 otherwise, with the reason."""
    fallback = PythonAstProvider()
    if not prefer_tree_sitter:
        return ProviderSelection(fallback, fallback, GateResult(False, ("tree-sitter not requested",)))
    gate = grammar_gate() if gate is None else gate
    if not gate.enabled:
        return ProviderSelection(fallback, fallback, gate, detail="; ".join(gate.reasons))
    try:
        provider = TreeSitterProvider()
    except Exception as exc:  # absent or broken install: Layer 0 still answers
        failed = GateResult(False, (f"tree-sitter failed to load: {type(exc).__name__}: {exc}",))
        return ProviderSelection(fallback, fallback, failed, detail=failed.reasons[0])
    return ProviderSelection(provider, fallback, gate)

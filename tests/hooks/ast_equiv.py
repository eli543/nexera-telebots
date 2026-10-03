#!/usr/bin/env python3
"""Check the streamer against a private source or its public AST fingerprints.

Comments and docstrings do not affect this check. The only permitted differences
are the public configuration, local timezone and optional AI extras adaptations.
The fingerprint file contains hashes, never source text or private paths.
Regenerate it from the pinned source with --write-baseline <live-path>.
"""

import argparse
import ast
import copy
import hashlib
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
PORT = HERE.parent.parent / "plugin/hooks/tg-stream.py"
BASELINE = HERE / "streamer-v30.22-ast.json"
ALLOWED_FUNCTIONS = {
    "bot_identity": "Display identity comes from fleet.json state_dir matching.",
    "_et_day": "Day boundaries use the computer's local timezone.",
    "_models_keychain_key": "Use the optional public key service without an account pin.",
    "_models_cache_maybe_refresh": "Refresh only when optional AI extras are enabled.",
    "_models_refresh_main": "Require a bot session and optional AI extras before refreshing.",
    "_apply_rich_result": "HTTP 404 from a rich endpoint falls back to plain text.",
}
ALLOWED_CONSTANTS = {
    "BOT_IDENTITIES": "The private identity table is empty; configuration supplies names.",
    "ET_ZONE": "None selects the local timezone.",
    "MODELS_KEYCHAIN_ACCOUNT": "No private account is selected.",
    "MODELS_KEYCHAIN_SERVICE": "Use nexera-telebots-judge.",
}


class StripDocstrings(ast.NodeTransformer):
    def visit(self, node):
        node = super().visit(node)
        # Python 3.12 adds an empty type_params field to existing declarations.
        # Ignore only that empty metadata so the pinned proof also runs on 3.9.
        if hasattr(node, "type_params") and not node.type_params:
            node._fields = tuple(field for field in node._fields if field != "type_params")
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:]
        return node


def digest(node):
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()


def fingerprints(source):
    tree = StripDocstrings().visit(ast.parse(source))
    functions = {}
    constants = {}

    def collect(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions[prefix + node.name] = digest(node)
            elif isinstance(node, ast.ClassDef):
                collect(node.body, prefix + node.name + ".")

    collect(tree.body)
    normalized = copy.deepcopy(tree)
    for node in normalized.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ALLOWED_FUNCTIONS:
            node.body = [ast.Pass()]
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in ALLOWED_CONSTANTS:
                constants[target.id] = digest(node)
                node.value = ast.Constant(value=None)
    return {"functions": functions, "constants": constants, "normalized_module": digest(normalized)}


def compare(reference, port):
    changed_functions = sorted(name for name in reference["functions"].keys() | port["functions"].keys()
                               if reference["functions"].get(name) != port["functions"].get(name))
    changed_constants = sorted(name for name in reference["constants"].keys() | port["constants"].keys()
                               if reference["constants"].get(name) != port["constants"].get(name))
    errors = [name for name in changed_functions if name not in ALLOWED_FUNCTIONS]
    errors += [name for name in changed_constants if name not in ALLOWED_CONSTANTS]
    if reference["normalized_module"] != port["normalized_module"]:
        errors.append("module structure or executable code outside the allowed adaptations")
    return changed_functions, changed_constants, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--live", type=Path, help="Optional read-only original v30.22 source")
    source_group.add_argument("--write-baseline", type=Path,
                              help="Regenerate the fixture from the pinned read-only v30.22 source")
    parser.add_argument("--port", type=Path, default=PORT)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    baseline = json.loads(BASELINE.read_text())
    live_source = args.live or args.write_baseline
    if live_source:
        raw = live_source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != baseline["source_sha256"]:
            raise SystemExit("FAIL: reference source is not the pinned v30.22 input")
        reference = fingerprints(raw.decode())
        if args.write_baseline:
            baseline["ast"] = reference
            BASELINE.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
            print("Wrote pinned v30.22 AST fixture: " + str(BASELINE))
    else:
        reference = baseline["ast"]
    source = args.port.read_text()
    port = fingerprints(source)
    functions, constants, errors = compare(reference, port)
    print("Changed functions: " + (", ".join(functions) or "none"))
    print("Changed constants: " + (", ".join(constants) or "none"))
    if errors:
        print("FAIL: " + "; ".join(errors))
        return 1
    print("PASS: all remaining executable AST nodes match v30.22")
    if args.self_test:
        mutated = source.replace('MISSION_DEFAULT = "on it"', 'MISSION_DEFAULT = "unexpected change"', 1)
        assert mutated != source
        assert compare(reference, fingerprints(mutated))[2]
        mutated = source.replace('def short(v, n):', 'def short(v, n):\n    return "unexpected change"', 1)
        assert mutated != source
        assert compare(reference, fingerprints(mutated))[2]
        print("PASS: forbidden constant and function changes are rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

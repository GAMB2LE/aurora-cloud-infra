#!/usr/bin/env python3
"""Render a reader-only patch with the exact deployed configuration preserved."""
import ast
import base64
import hashlib
import json
import sys


PATCH_FUNCTIONS = {"normalize_entries", "list_gws_entries", "main"}


def comparable(source):
    tree = ast.parse(source)
    found = set()
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.FunctionDef) and node.name in PATCH_FUNCTIONS:
            if node.name in found:
                raise ValueError("Duplicate patch function")
            found.add(node.name)
            tree.body[index] = ast.parse("def " + node.name + "(): pass").body[0]
    if found != PATCH_FUNCTIONS:
        raise ValueError("Expected all three verifier patch functions")
    return ast.dump(tree, include_attributes=False)


def render(deployed, template):
    assignments = [n for n in ast.parse(deployed).body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "CONFIG" for t in n.targets)]
    if len(assignments) != 1:
        raise ValueError("Expected one deployed CONFIG assignment")
    node = assignments[0]
    call = node.value
    if (not isinstance(call, ast.Call) or ast.dump(call.func) != ast.dump(ast.parse("json.loads", mode="eval").body)
            or len(call.args) != 1 or call.keywords):
        raise ValueError("Expected literal JSON configuration, never executable bindings")
    config = json.loads(ast.literal_eval(call.args[0]))
    if not isinstance(config, dict):
        raise ValueError("Expected a configuration object")
    prefix, remainder = template.split("CONFIG = json.loads(", 1)
    _, suffix = remainder.split("\n\n\ndef json_default", 1)
    candidate = prefix + ast.get_source_segment(deployed, node) + "\n\n\ndef json_default" + suffix
    compile(candidate, "aurora-mirror-verify", "exec")
    if comparable(candidate) != comparable(deployed):
        raise ValueError("Refusing changes outside the three audited reader functions")
    return candidate


if __name__ == "__main__":
    request = json.load(sys.stdin)
    deployed = base64.b64decode(request["deployed_base64"], validate=True)
    candidate = render(deployed.decode(), request["template"]).encode()
    print(json.dumps({"before_sha256": hashlib.sha256(deployed).hexdigest(),
                      "after_sha256": hashlib.sha256(candidate).hexdigest(),
                      "candidate_base64": base64.b64encode(candidate).decode()}))

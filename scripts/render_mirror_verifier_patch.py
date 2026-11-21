#!/usr/bin/env python3
"""Render a reader-only patch with the exact deployed configuration preserved."""
import ast
import base64
import hashlib
import json
import sys


PATCH_FUNCTIONS = {"normalize_entries", "list_gws_entries", "main"}
OPTIONAL_PATCH_FUNCTIONS = {"native_radar_alert_grace"}


def comparable(source):
    tree = ast.parse(source)
    found = set()
    extra_nodes = []
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.FunctionDef) and node.name in PATCH_FUNCTIONS | OPTIONAL_PATCH_FUNCTIONS:
            if node.name in found:
                raise ValueError("Duplicate patch function")
            found.add(node.name)
            if node.name in OPTIONAL_PATCH_FUNCTIONS:
                extra_nodes.append(node)
            else:
                tree.body[index] = ast.parse("def " + node.name + "(): pass").body[0]
    if found & PATCH_FUNCTIONS != PATCH_FUNCTIONS:
        raise ValueError("Expected the audited verifier patch functions")
    tree.body = [node for node in tree.body if node not in extra_nodes]
    return ast.dump(tree, include_attributes=False)


def render(deployed, template):
    if "run_python_original" in deployed:
        raise ValueError("Storage-guarded entrypoint: use a pinned-payload release; do not replace its wrapper")
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
        raise ValueError("Refusing changes outside the audited reader functions")
    return candidate


if __name__ == "__main__":
    request = json.load(sys.stdin)
    deployed = base64.b64decode(request["deployed_base64"], validate=True)
    candidate = render(deployed.decode(), request["template"]).encode()
    print(json.dumps({"before_sha256": hashlib.sha256(deployed).hexdigest(),
                      "after_sha256": hashlib.sha256(candidate).hexdigest(),
                      "candidate_base64": base64.b64encode(candidate).decode()}))

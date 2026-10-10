"""Executable ownership rules for the replacement; deliberately broken fixtures prove them."""
import ast
import importlib.util
from pathlib import Path
import unittest


ROOT=Path(__file__).resolve().parents[1]
OWNERS={"applications","correspondence","external_actions","commands"}
COMPOSITION={"job_search/applications/workflows.py","job_search/applications/queries.py"}


def owner_for(path):
    parts=Path(path).parts
    if "understanding" in parts:
        return "understanding"
    return next((part for part in parts if part in OWNERS),"transport")


def violations(path,source):
    owner=owner_for(path)
    composing=path in COMPOSITION
    package=".".join(Path(path).with_suffix("").parts[:-1])
    issues=[]
    tree=ast.parse(source)
    for node in ast.walk(tree):
        modules=[]
        if isinstance(node,ast.Import):
            modules=[alias.name for alias in node.names]
        if isinstance(node,ast.ImportFrom):
            base=importlib.util.resolve_name("."*node.level+(node.module or ""),package) if node.level else node.module or ""
            modules=[base]+[base+"."+alias.name for alias in node.names]
        for module in modules:
            parts=module.split(".")
            if not parts or parts[0]!="job_search":
                continue
            target="understanding" if "understanding" in parts else next((p for p in parts if p in OWNERS),None)
            if module in {"job_search.store","job_search.reducer","job_search.lifecycle","job_search.career_actions"}:
                issues.append("Use the replacement public owner, not a legacy writer")
            if owner=="commands" and target and target!="commands":
                issues.append("Shared command mechanics cannot import business owners")
            if owner in {"correspondence","external_actions"} and target in {"applications","understanding"}:
                issues.append("External owners cannot import application internals; inject public callbacks")
            if owner=="understanding" and target in {"applications","external_actions"}:
                issues.append("Understanding cannot import accepted-state or execution implementations")
            if composing and target and any(p.startswith("_") and p!="__init__" for p in parts):
                issues.append("Workflow/query composition must use public interfaces")
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute):
            if composing and node.func.attr in {"execute","executemany","executescript"}:
                issues.append("Cross-owner workflows cannot issue business SQL")
            if node.func.attr=="scope" and node.args and isinstance(node.args[0],ast.Constant):
                if owner in OWNERS|{"understanding"} and not composing and node.args[0].value!=owner:
                    issues.append("A module can only enter its own transaction scope")
    return sorted(set(issues))


class BoundariesTest(unittest.TestCase):
    def test_new_owners_and_composition_obey_rules(self):
        paths=[]
        for owner in OWNERS:
            paths.extend((ROOT/"job_search"/owner).rglob("*.py"))
        errors={str(p.relative_to(ROOT)):violations(str(p.relative_to(ROOT)),p.read_text()) for p in paths}
        self.assertEqual({p:v for p,v in errors.items() if v},{})

    def test_bad_fixtures_are_rejected(self):
        fixtures={
            "job_search/commands/bad.py":"from job_search.applications.api import ApplicationOperations",
            "job_search/applications/understanding/bad.py":"from job_search.external_actions.api import ExternalActionOperations",
            "job_search/correspondence/bad.py":"from job_search.applications import tasks",
            "job_search/external_actions/bad.py":"def mutate(tx):\n with tx.scope('applications'): pass",
            "job_search/applications/workflows.py":"def mutate(tx): tx.connection.execute('UPDATE app_tasks SET status=1')",
            "job_search/applications/queries.py":"from job_search.applications import _store",
        }
        for path,source in fixtures.items():
            with self.subTest(path=path):
                self.assertTrue(violations(path,source))

    def test_owner_imports_are_permitted(self):
        self.assertEqual(violations("job_search/applications/tasks.py","from . import _store\ndef mutate(tx):\n with tx.scope('applications'): pass"),[])


if __name__=="__main__":
    unittest.main()

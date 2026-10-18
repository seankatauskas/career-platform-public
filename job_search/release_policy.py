"""Reviewed release edges; equal compatibility labels alone never permit rollback."""
from __future__ import annotations

import re

from .operation_journal import OpsError


def validate_policy(value: dict) -> dict:
    required = {"version", "schema_compatibility", "test_baseline_sha", "predecessor"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"deployment_mode"}:
        raise ValueError("invalid release policy fields")
    if value["version"] != 1 or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", str(value["schema_compatibility"])):
        raise ValueError("invalid release compatibility identifier")
    if not re.fullmatch(r"[a-f0-9]{40}", str(value["test_baseline_sha"])):
        raise ValueError("test baseline must be a full commit SHA")
    if value.get("deployment_mode", "migration") not in ("migration", "compatible"):
        raise ValueError("invalid deployment mode")
    previous = value["predecessor"]
    if previous is not None:
        if not isinstance(previous, dict) or set(previous) != {"release_id", "source_sha"}:
            raise ValueError("invalid predecessor identity")
        if previous["source_sha"] != value["test_baseline_sha"]:
            raise ValueError("predecessor must match tested baseline")
        if not re.fullmatch(re.escape(previous["source_sha"]) + r"-[0-9]+", previous["release_id"]):
            raise ValueError("predecessor release must identify the tested source")
    return value


def validate_evidence(policy: dict, source: str, evidence: dict) -> None:
    if (not isinstance(evidence, dict) or evidence.get("schema_version") != 1 or
            evidence.get("passed") is not True or evidence.get("runtime") != "docker" or
            evidence.get("working_tree_dirty", False) is not False or
            evidence.get("source_sha") != source or evidence.get("baseline_sha") != policy["test_baseline_sha"] or
            not isinstance(evidence.get("rollback_passed"), bool)):
        raise ValueError("release requires passing Docker transition evidence for this source and predecessor")


def check_transition(target: dict, current: dict | None, *, rollback: bool = False) -> None:
    try:
        if target.get("operations_protocol") != 1:
            raise ValueError("release predates durable recovery")
        policy = validate_policy(target.get("release_policy"))
        validate_evidence(policy, target.get("source_sha"), target.get("transition_validation"))
        if rollback:
            if current is None or current.get("operations_protocol") != 1:
                raise ValueError("no hardened current release")
            current_policy = validate_policy(current.get("release_policy"))
            validate_evidence(current_policy, current.get("source_sha"), current.get("transition_validation"))
            if (current_policy["predecessor"] != {"release_id": target["release_id"], "source_sha": target["source_sha"]} or
                    current_policy["schema_compatibility"] != policy["schema_compatibility"] or
                    not current["transition_validation"]["rollback_passed"]):
                raise ValueError("no tested compatible rollback edge")
        elif current is None:
            if policy["predecessor"] is not None:
                raise ValueError("expected predecessor is not installed")
        elif (target.get("hermes_base_image") != current.get("hermes_base_image") and
              policy["schema_compatibility"] == current.get("schema_compatibility")):
            raise ValueError("Hermes base changed; review a new compatibility contract and its live migration")
        elif policy["predecessor"] != {"release_id": current["release_id"], "source_sha": current.get("source_sha")}:
            raise ValueError("installed release differs from tested predecessor")
    except (ValueError, KeyError, TypeError) as error:
        raise OpsError(str(error)) from None


def deployment_mode(target: dict, current: dict | None, *, rollback: bool = False) -> tuple[str, str]:
    """Select the cheaper procedure only for a reviewed, proven release edge.

    check_transition remains mandatory. Missing proof never grants permission to
    omit a checkpoint; the original migration procedure is the fallback.
    """
    newer, older = (current, target) if rollback else (target, current)
    if newer is None or older is None:
        return "migration", "first_install"
    policy = validate_policy(newer.get("release_policy"))
    if policy.get("deployment_mode", "migration") != "compatible":
        return "migration", "policy_requires_migration"
    if any(type(m.get("startup_schema_protocol")) is not int or m["startup_schema_protocol"] != 2
           for m in (newer, older)):
        return "migration", "startup_boundary_unavailable"
    contract = newer.get("runtime_contract_sha256")
    if not isinstance(contract, str) or not re.fullmatch(r"[a-f0-9]{64}", contract) or older.get("runtime_contract_sha256") != contract:
        return "migration", "runtime_contract_changed"
    if (newer.get("hermes_base_image") != older.get("hermes_base_image") or
            newer.get("tectonic_version") != older.get("tectonic_version") or
            newer.get("schema_compatibility") != older.get("schema_compatibility")):
        return "migration", "persistent_runtime_changed"
    evidence = newer.get("transition_validation", {})
    proof = evidence.get("compatible_deployment")
    if (not evidence.get("rollback_passed") or not isinstance(proof, dict) or
            type(proof.get("protocol")) is not int or proof["protocol"] != 2 or proof.get("passed") is not True or
            proof.get("baseline_startup_read_only") is not True or
            proof.get("candidate_startup_read_only") is not True or
            proof.get("baseline_diagnostics_read_only") is not True or
            proof.get("candidate_diagnostics_read_only") is not True or
            proof.get("predecessor_after_candidate_writes") is not True or
            proof.get("runtime_contract_sha256") != contract):
        return "migration", "compatible_transition_unproven"
    return "compatible", "tested_compatible_edge"

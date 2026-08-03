from __future__ import annotations

import json
import os
from dataclasses import dataclass

import httpx

from .schemas import MeshReport, ReconstructionPlan


@dataclass(slots=True)
class AIResult:
    plan: ReconstructionPlan | None
    warning: str | None = None


def is_configured() -> bool:
    return bool(os.getenv("MESHMIND_LLM_URL") and os.getenv("MESHMIND_LLM_MODEL"))


def _extract_json(content: str) -> dict:
    content = content.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        content = "\n".join(lines[1:-1]).strip()
        if content.lower().startswith("json"):
            content = content[4:].lstrip()
    return json.loads(content)


def revise_plan(
    mesh: MeshReport,
    current: ReconstructionPlan,
    user_prompt: str,
) -> AIResult:
    endpoint = os.getenv("MESHMIND_LLM_URL", "").rstrip("/")
    model = os.getenv("MESHMIND_LLM_MODEL", "")
    if not endpoint or not model:
        return AIResult(
            plan=None,
            warning=(
                "AI refinement was requested but MESHMIND_LLM_URL and "
                "MESHMIND_LLM_MODEL are not configured."
            ),
        )

    schema = ReconstructionPlan.model_json_schema()
    system = (
        "You revise a mechanical reverse-engineering feature plan. Return exactly one JSON "
        "object matching the supplied schema. Never return code or prose. Preserve millimetres. "
        "Use only feature kinds present in the supplied schema, including cardinal or arbitrary-"
        "plane extrusions, analytic cylinders/cones, revolves, tapered lofts, holes, and edge "
        "finishes when the measured plan supports them. Prefer few clean, manufacturing-intent "
        "dimensions. Do not invent unsupported details."
    )
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "request": user_prompt,
                        "mesh_report": mesh.model_dump(mode="json"),
                        "current_plan": current.model_dump(mode="json"),
                        "required_schema": schema,
                    }
                ),
            },
        ],
    }
    api_key = os.getenv("MESHMIND_LLM_KEY")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=90) as client:
            response = client.post(
                f"{endpoint}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        plan = ReconstructionPlan.model_validate(_extract_json(content))
        plan.source = "hybrid"
        plan.assumptions.append("A configured language model revised the typed feature plan.")
        return AIResult(plan=plan)
    except (
        httpx.HTTPError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return AIResult(plan=None, warning=f"AI refinement was rejected: {exc}")

"""Maintainer-authorized absorption service; no model work occurs on submission."""
from dataclasses import asdict

from research_brain.jobs import AbsorptionJobs, SpendingLimits
from research_brain.spaces import ContextScope
from .registry import Unavailable


def budget_summary(plan):
    """Compact uncached baseline, not a price quote or completion guarantee."""
    previews = plan.get("previews")
    records = plan.get("source_embedding_records")
    if previews is None or records is None:
        return {"available": False}
    extraction = [{"task": item["task"], "calls": item["expected_calls"]} for item in previews]
    source_calls = (records + 63) // 64 if "embeddings" in plan["tasks"] else 0
    minimum = sum(item["calls"] for item in extraction) + source_calls
    return {"available": True, "extraction": extraction,
        "source_embedding_records": records, "source_embedding_calls": source_calls,
        "uncached_minimum_calls": minimum, "max_calls": plan["limits"]["max_calls"],
        "below_uncached_baseline": plan["limits"]["max_calls"] < minimum,
        "notice": "Uncached baseline only. Generated-card embeddings and retries add calls; successful cached work may reduce calls. Token feasibility is not estimated. Limits are not increased automatically."}


class ScopedAbsorption:
    def __init__(self, registry, actor, space_id, *, create=False):
        self.registry, self.space_id = registry, space_id
        with registry.connect(readonly=True) as db:
            self.principal = registry._principal(db, actor)["principal"]
        self.actor = "discord:" + actor
        self.scope = registry.spaces.scope(self.principal, conversation_id="paper-absorption",
                                          writable_space=space_id)
        registry.spaces.validate(self.scope, space_id=space_id, maintainer=True)
        self.jobs = AbsorptionJobs(registry.spaces.open(space_id), space_id, create=create,
            authorization_context=asdict(self.scope), authorization_check=self._check)

    def _check(self, context):
        self.registry.spaces.validate(self.scope, space_id=self.space_id, maintainer=True)
        data = dict(context)
        data["read_spaces"] = tuple(data["read_spaces"])
        original = ContextScope(**data)
        if original.writable_space != self.space_id:
            raise Unavailable()
        self.registry.spaces.validate(original, space_id=self.space_id, maintainer=True)

    def submit(self, url, *, limits: SpendingLimits):
        # No implicit/unlimited budget, and no remote filesystem input.
        if not isinstance(limits, SpendingLimits): raise ValueError("Explicit limits required")
        self._check(asdict(self.scope))
        result = self.jobs.absorb(url, limits=limits)
        self._check(asdict(self.scope))
        return self.preview(result["id"])

    def preview(self, job_id):
        self._check(asdict(self.scope))
        result = self.jobs.show(job_id)
        plan = result["plan"]
        return {"space_id": self.space_id, "job_id": result["id"], "status": result["status"],
            "plan_digest": result["plan_digest"], "document_id": plan["document_id"],
            "compilation_id": plan["compilation_id"], "tasks": plan["tasks"],
            "limits": plan["limits"], "model": plan["extract_model"],
            "budget_summary": budget_summary(plan),
            "reasoning_effort": plan["reasoning_effort"], "source_ready": result["source_ready"],
            "cards_ready_for_review": result["cards_ready_for_review"],
            "calls_reserved": result["calls_reserved"], "tokens_reserved": result["tokens_reserved"],
            "reviewed_memory": result["reviewed_memory"]}

    def approve(self, job_id, plan_digest, *, live=False):
        self._check(asdict(self.scope))
        context = self.jobs.show(job_id)["plan"].get("authorization_context")
        if context is None:
            raise ValueError("Create a scoped plan before remote approval")
        self._check(context)
        self.jobs.approve(job_id, plan_digest, live=live, actor=self.actor)
        return self.preview(job_id)

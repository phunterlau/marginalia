"""Human-only, evidence-backed review operations. No provider or Pi writes."""
import json
import re

from .registry import Unavailable


NOTICE = ("Acceptance means the card faithfully represents its cited source and qualifications, "
          "not universal correctness or suitability. Acceptance makes it eligible for reliable recall. "
          "Inspect the card and evidence before deciding; agent origin is preserved.")


class CardReviews:
    def __init__(self, registry, actor, space):
        self.registry, self.space, self.actor = registry, space, actor
        with registry.connect(readonly=True) as db:
            principal = registry._principal(db, actor)["principal"]
        self.scope = registry.spaces.scope(principal, conversation_id="card-review", writable_space=space)

    def show(self, object_id):
        if not isinstance(object_id, str) or not re.fullmatch(r"obj_[A-Za-z0-9_-]{1,90}", object_id): raise Unavailable()
        result = self.registry.spaces.read(self.scope, self.space, "get_research_object", object_id)
        record = result["result"]
        if record is None or record["kind"] not in {"method_card", "math_card"}: raise Unavailable()
        result["review_notice"] = NOTICE
        if len(json.dumps(result, ensure_ascii=False).encode()) > 16000:
            raise ValueError("Card exceeds Discord review bound; inspect it in the local review workbench")
        return result

    def decide(self, object_id, decision, expected_version, note=""):
        if decision not in {"ACCEPTED", "DISPUTED", "REJECTED"}:
            raise ValueError("Choose ACCEPTED, DISPUTED or REJECTED")
        if not isinstance(expected_version, str) or not 1 <= len(expected_version) <= 100:
            raise ValueError("A current card version is required")
        if not isinstance(note, str) or len(note) > 4000 or "\x00" in note:
            raise ValueError("Review note must be at most 4000 characters")
        if decision != "ACCEPTED" and not note.strip():
            raise ValueError("Dispute and rejection require a note")
        self.show(object_id)
        return self.registry.spaces.review_card(self.scope, object_id, review_state=decision,
            expected_version=expected_version, note=note.strip() or None, actor="discord:" + self.actor)

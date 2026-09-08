"""Immutable consent and explicit retry-safe publication into a shared Brain."""
import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit

from .registry import Unavailable, encode, now


def safe_text(value):
    if not isinstance(value, str) or len(value) > 20000 or "\x00" in value:
        raise ValueError("Publication text exceeds bounds")
    if re.search(r"\b(?:obj|block|comp|conv|asset|submission|job|doc|version|run|turn|publication|session)_[A-Za-z0-9_-]+|file://|/(?:Users|home|private)/", value):
        raise ValueError("Remove unresolved private references or paths before publication")
    return value


class Publications:
    def __init__(self, registry): self.registry = registry

    def _principal(self, actor):
        with self.registry.connect(readonly=True) as db:
            return self.registry._principal(db, actor)["principal"]

    def prepare(self, actor, source_space, destination_space, *, note_ids=(), paper_block_ids=()):
        if not note_ids and not paper_block_ids or len(note_ids) > 10 or len(paper_block_ids) > 10:
            raise ValueError("Select 1..10 notes and/or 1..10 paper evidence blocks")
        principal = self._principal(actor)
        spaces = self.registry.spaces
        source = spaces.scope(principal, conversation_id="publication-preview", writable_space=source_space)
        destination = spaces.scope(principal, conversation_id="publication-preview", writable_space=destination_space)
        if spaces.get(source_space)["kind"] != "personal" or spaces.get(source_space)["owner"] != principal:
            raise Unavailable()
        if spaces.get(destination_space)["kind"] != "shared": raise Unavailable()
        brain = spaces.open(source_space)
        bundle = {"version": 1, "audience": "shared:" + destination_space, "notes": [], "papers": [], "evidence": [],
                  "review_state": "UNREVIEWED", "notice": "Publication is not scientific acceptance. No conversation history is included."}
        refs = {"notes": [], "blocks": {}}
        evidence_map, paper_map = {}, {}

        def evidence(block_id):
            if not isinstance(block_id, str) or not re.fullmatch(r"block_[A-Za-z0-9_-]{1,90}", block_id): raise Unavailable()
            if block_id in evidence_map: return evidence_map[block_id]
            if len(evidence_map) >= 50: raise ValueError("Too many evidence dependencies")
            item = brain.get_evidence(block_id)
            if item is None: raise Unavailable()
            url = item["source_uri"]
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname not in {"arxiv.org", "export.arxiv.org", "www.arxiv.org"}
                    or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None, 443)
                    or not re.search(r"v[1-9][0-9]*(?:\.pdf)?$", parsed.path)
                    or not re.fullmatch(r"v[1-9][0-9]*", item["version_label"] or "")):
                raise ValueError("Publication requires a pinned arXiv source; unresolved/local dependencies must be removed")
            key = (url, item["version_label"], item["source_sha256"])
            if key not in paper_map:
                alias = "paper_" + str(len(paper_map) + 1)
                paper_map[key] = alias
                bundle["papers"].append({"ref": alias, "title": safe_text(item["document_title"]), "source_url": url,
                    "revision": item["version_label"], "sha256": item["source_sha256"],
                    "license": safe_text(item["license_uri"]) if item["license_uri"] else None})
            alias = "evidence_" + str(len(evidence_map) + 1)
            evidence_map[block_id] = alias
            refs["blocks"][alias] = {"block_id": block_id, "compilation_id": item["compilation_id"]}
            bundle["evidence"].append({"ref": alias, "paper": paper_map[key],
                "text": safe_text(item["raw_text"]), "latex": safe_text(item["raw_latex"]) if item["raw_latex"] else None,
                "member": safe_text(item["source_member"] or ""), "line_start": item["line_start"],
                "line_end": item["line_end"], "page": item["page"], "char_start": item["char_start"],
                "char_end": item["char_end"], "raw_text_sha256": item["raw_sha256"]})
            return alias

        for ident in sorted(set(note_ids)):
            if not isinstance(ident, str) or not re.fullmatch(r"obj_[A-Za-z0-9_-]{1,90}", ident): raise Unavailable()
            note = brain.get_research_object(ident)
            if note is None or note["kind"] != "note": raise Unavailable()
            if note["structured"]: raise ValueError("Curate a plain note before publishing structured content")
            links = [{"ref": evidence(link["block_id"]), "relation": safe_text(link["relation"])} for link in note["evidence"]]
            bundle["notes"].append({"title": safe_text(note["title"] or ""), "body": safe_text(note["body"]),
                "origin": note["origin"], "review_state": "UNREVIEWED", "evidence": links})
            refs["notes"].append({"id": ident, "updated_at": note["updated_at"]})
        for ident in sorted(set(paper_block_ids)): evidence(ident)
        encoded = encode(bundle)
        if len(encoded.encode()) > 100000: raise ValueError("Publication preview exceeds 100000 bytes")
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        ident = "publication_" + uuid.uuid4().hex
        with spaces.connect() as policy:
            policy.execute("BEGIN IMMEDIATE")
            spaces.validate(source)
            spaces.validate(destination)
            with self.registry.connect() as db:
                db.execute("INSERT INTO publications VALUES (?,?,?,?,?,?,?,?,'PREVIEW',NULL,NULL,?)",
                    (ident, principal, source_space, destination_space, source.policy_version, encoded, encode(refs), digest, now()))
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('publication_prepared',?,?)", (ident, now()))
        return self.show(actor, ident)

    def _access(self, actor, row, *, owner=False):
        principal = self._principal(actor)
        if owner and principal != row["owner"]: raise Unavailable()
        space = row["source_space"] if principal == row["owner"] else row["destination_space"]
        if principal != row["owner"] and row["state"] not in {"CONSENTED", "APPROVED", "RUNNING", "NEEDS_ATTENTION", "COMPLETE"}: raise Unavailable()
        scope = self.registry.spaces.scope(principal, conversation_id="publication-access", writable_space=space)
        self.registry.spaces.validate(scope, maintainer=True)
        if scope.policy_version != row["policy_version"]: raise Unavailable()
        return principal

    def show(self, actor, ident):
        with self.registry.connect(readonly=True) as db:
            row = db.execute("SELECT * FROM publications WHERE id=?", (ident,)).fetchone()
        if row is None: raise Unavailable()
        self._access(actor, row)
        return {"publication_id": ident, "digest": row["digest"], "state": row["state"], "bundle": json.loads(row["bundle_json"])}

    def decide(self, actor, ident, digest, action):
        if action not in {"consent", "approve", "cancel"}: raise ValueError("Invalid publication action")
        with self.registry.spaces.connect() as policy:
            policy.execute("BEGIN IMMEDIATE")
            with self.registry.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM publications WHERE id=?", (ident,)).fetchone()
                if row is None: raise Unavailable()
                principal = self._access(actor, row, owner=action in {"consent", "cancel"})
                if row["digest"] != digest: raise ValueError("Publication digest changed")
                states = {"consent": ("PREVIEW", "CONSENTED"), "approve": ("CONSENTED", "APPROVED"), "cancel": ("PREVIEW", "CANCELLED")}
                previous, target = states[action]
                if action == "approve":
                    scope = self.registry.spaces.scope(principal, conversation_id="publication-approval", writable_space=row["destination_space"])
                    self.registry.spaces.validate(scope, maintainer=True)
                if row["state"] == target: return {"publication_id": ident, "state": target, "digest": digest}
                if action == "cancel" and row["state"] in {"CONSENTED", "APPROVED"}: previous = row["state"]
                if row["state"] != previous: raise ValueError("Publication is not ready for this decision")
                field = ",consent_actor=?" if action == "consent" else ",approval_actor=?" if action == "approve" else ""
                args = (target, principal, ident) if field else (target, ident)
                db.execute("UPDATE publications SET state=?" + field + " WHERE id=?", args)
                db.execute("INSERT INTO events(kind,subject,at) VALUES (?,?,?)", ("publication_" + action, ident, now()))
        return {"publication_id": ident, "state": target, "digest": digest}

    def _execution_access(self, actor, row):
        principal = self._access(actor, row)
        if row["consent_actor"] != row["owner"] or not row["approval_actor"]: raise Unavailable()
        for person, space, maintainer in ((principal, row["destination_space"], True),
                (row["owner"], row["source_space"], True),
                (row["approval_actor"], row["destination_space"], True)):
            scope = self.registry.spaces.scope(person, conversation_id="publication-execute", writable_space=space)
            self.registry.spaces.validate(scope, maintainer=maintainer)
            if scope.policy_version != row["policy_version"]: raise Unavailable()

    def execute(self, actor, ident, digest, *, retry=False):
        """Trusted backend action: no network/model calls, no session copying.

        Approved sources may become searchable before the whole publication
        completes. Notes and a destination receipt commit atomically; the receipt
        reconciles a crash between the separate Brain and Arms commits.
        """
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM publications WHERE id=?", (ident,)).fetchone()
            if row is None: raise Unavailable()
            row = dict(row)
            self._execution_access(actor, row)
            if row["digest"] != digest: raise ValueError("Publication digest changed")
            bundle, private = json.loads(row["bundle_json"]), json.loads(row["private_refs_json"])
            if hashlib.sha256(encode(bundle).encode()).hexdigest() != digest: raise ValueError("Publication snapshot corrupted")
            if row["state"] != "COMPLETE":
                if row["state"] != "APPROVED" and not (row["state"] == "NEEDS_ATTENTION" and retry is True):
                    raise ValueError("Publication requires approval or explicit inspected retry")
                db.execute("UPDATE publications SET state='RUNNING' WHERE id=?", (ident,))
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('publication_dispatched',?,?)", (ident, now()))
        try:
            source = self.registry.spaces.open(row["source_space"])
            destination = self.registry.spaces.open(row["destination_space"])
            mappings, papers = {}, {}
            cached = destination.publication_receipt(digest)
            for paper in ([] if cached else bundle["papers"]):
                self._execution_access(actor, row)
                selected = next(e for e in bundle["evidence"] if e["paper"] == paper["ref"])
                block_id = private["blocks"][selected["ref"]]["block_id"]
                current = source.get_evidence(block_id)
                if current is None or current["source_uri"] != paper["source_url"] or current["version_label"] != paper["revision"]:
                    raise ValueError("Approved source identity changed")
                imported = source.copy_source_revision_to(destination, block_id, expected_sha256=paper["sha256"])
                papers[paper["ref"]] = destination.compilation_blocks(imported.document_id, imported.compilation_id)
            for evidence in ([] if cached else bundle["evidence"]):
                matches = [block for block in papers[evidence["paper"]]
                    if (block["raw_text"], block["raw_latex"], block["source_member"] or "", block["line_start"], block["line_end"],
                        block["char_start"], block["char_end"], block["page"], block["raw_sha256"]) ==
                       (evidence["text"], evidence["latex"], evidence["member"], evidence["line_start"], evidence["line_end"],
                        evidence["char_start"], evidence["char_end"], evidence["page"], evidence["raw_text_sha256"])]
                if len(matches) != 1: raise ValueError("Destination evidence could not be resolved exactly")
                mappings[evidence["ref"]] = matches[0]["id"]
            notes = [{"title": note["title"], "body": note["body"], "origin": note["origin"],
                "evidence": [{"block_id": mappings[link["ref"]], "relation": link["relation"]} for link in note["evidence"]]}
                for note in ([] if cached else bundle["notes"])]
            with self.registry.spaces.connect() as policy:
                policy.execute("BEGIN IMMEDIATE")
                self._execution_access(actor, row)
                receipt = cached or destination.publish_notes(digest, notes)
                with self.registry.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    changed = db.execute("UPDATE publications SET state='COMPLETE' WHERE id=? AND state='RUNNING'", (ident,)).rowcount
                    if changed:
                        db.execute("INSERT INTO events(kind,subject,at) VALUES ('publication_completed',?,?)", (ident, now()))
            return {"destination_space": row["destination_space"], "state": "COMPLETE", **receipt}
        except BaseException:
            with self.registry.connect() as db:
                changed = db.execute("UPDATE publications SET state='NEEDS_ATTENTION' WHERE id=? AND state='RUNNING'", (ident,)).rowcount
                if changed: db.execute("INSERT INTO events(kind,subject,at) VALUES ('publication_uncertain',?,?)", (ident, now()))
            raise

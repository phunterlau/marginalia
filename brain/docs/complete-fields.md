# Compact previews and complete field reads

ResearchPackets and Pi object/recall previews report omitted list items, mapping
entries and characters. They retain generic research-object fields, including
experiment controls and measurements, instead of a Method/Math-only field list.
An omission is not evidence that the omitted information is absent.

Read the complete value through bounded CLI pages:

```sh
research --space personal object field OBJECT_ID structured.controls --limit 20
research --space personal object field OBJECT_ID structured.controls \
  --offset 20 --expected-version VERSION

# Enumerate all structured field names/values:
research --space personal object field OBJECT_ID structured

# Oversized list item: reconstruct text/JSON from successive fragments:
research --space personal object field OBJECT_ID structured.controls \
  --item-index 0 --char-offset 0 --char-limit 8000 --expected-version VERSION

# Exact long source/equation text:
research --space personal evidence BLOCK_ID --field raw_latex --char-limit 8000
```

Lists use `next_offset`; a list item too large for a page returns
`requires_item_index`. Read that item using character fragments until
`next_char_offset` is null, then continue at the next list index. Mapping pages
contain sorted `{key, value}` entries. Fragments indicate `text` or `json`
encoding. No source text is rewritten or silently truncated in this interface.

Every page carries a content-derived `version`. Supply it as `expected_version`
on subsequent reads; changed content or review state invalidates the read rather
than mixing versions. The page retains epistemic labels; source pages retain
their compilation/revision/member/span locator. Each call is bounded to 100 list
items and 16,000 fragment characters, with a 24,000-character list payload cap.

Pi's existing `research_object` and `research_evidence` tools expose these same
options through fixed Python argument arrays. They introduce no mutations,
provider calls, shell access, or caller-selected filesystem paths. Space-scoped
Python reads enforce the same authorization checks as other retrieval methods.

Tests cover full multi-page controls, oversized nested item reconstruction,
mapping enumeration, review conflicts, exact evidence fragments, bounded Node
projections, and loading the actual Pi extension in credential-free RPC mode.
The RPC load test sends no model prompt; it is not an end-to-end synthesis test.

import json
from pathlib import Path

from interp_pipeline.development import aggregate, validate_development


def test_published_measurements_match_frozen_protocol():
    root = Path(__file__).parents[1]
    def read(name):
        return json.loads((root / name).read_text())
    protocol = read("configs/baseline-development.json")
    pilot = read("configs/sentiment.json")
    validate_development(protocol, pilot)
    rows = read("results/baseline-measurements.json")
    assert len(rows) == protocol["budget"]["forward_passes"] == 36
    assert aggregate(rows, protocol) == read("results/baseline-summary.json")
    assert [c["correct"] for c in aggregate(rows, protocol)["conditions"]] == [6, 11, 12]
    older = read("results/pilot-summary.json")
    assert older["baseline_label_accuracy"] == .5
    assert len({c["method"] for c in older["conditions"] if c["method"].startswith("random_")}) == 5

import test from "node:test";
import assert from "node:assert/strict";
import { projectStructured } from "./compact.mjs";

test("experiment controls survive and excess items are explicit", () => {
  const controls = Array.from({length: 11}, (_, i) => `control-${i}`);
  const result = projectStructured({schema: "ExperimentResultV1", controls, measurements: {accuracy: 0.5}});
  assert.deepEqual(result.value.controls, controls.slice(0, 8));
  assert.equal(result.value.measurements.accuracy, 0.5);
  assert.deepEqual(result.omissions["/controls"], {total: 11, returned: 8, omitted: 3});
});

test("nested strings and dictionaries are bounded with metadata", () => {
  const result = projectStructured({symbols: [{meaning: "x".repeat(4000)}]});
  assert.equal(result.omissions["/symbols/0/meaning"].omitted_characters, 3400);
  assert.equal(projectStructured(null).value.constructor, Object);
});

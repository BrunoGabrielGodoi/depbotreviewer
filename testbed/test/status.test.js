const test = require("node:test");
const assert = require("node:assert");
const { statusBadge } = require("../src/status");

test("statusBadge labels success and failure", () => {
  assert.match(statusBadge(true), /PASS/);
  assert.match(statusBadge(false), /FAIL/);
});

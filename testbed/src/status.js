// Uses chalk. Covered by test/status.test.js.
const chalk = require("chalk");

function statusBadge(ok) {
  return ok ? chalk.green("PASS") : chalk.red("FAIL");
}

module.exports = { statusBadge };

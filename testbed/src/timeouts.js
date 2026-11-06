// Uses ms. Deliberately has NO tests, so an ms update should need a human.
const ms = require("ms");

function parseTimeout(value) {
  const millis = ms(value);
  if (millis === undefined) {
    throw new Error(`Invalid timeout: ${value}`);
  }
  return millis;
}

function describeTimeout(millis) {
  return ms(millis, { long: true });
}

module.exports = { parseTimeout, describeTimeout };

// Uses escape-html. Covered by test/render.test.js.
const escapeHtml = require("escape-html");

function renderComment(author, text) {
  return `<p><strong>${escapeHtml(author)}</strong>: ${escapeHtml(text)}</p>`;
}

module.exports = { renderComment };

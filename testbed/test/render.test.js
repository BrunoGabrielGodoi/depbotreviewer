const test = require("node:test");
const assert = require("node:assert");
const { renderComment } = require("../src/render");

test("renderComment escapes HTML in author and text", () => {
  assert.strictEqual(
    renderComment("<b>eve</b>", `5 > 3 & "quotes" 'single'`),
    "<p><strong>&lt;b&gt;eve&lt;/b&gt;</strong>: 5 &gt; 3 &amp; &quot;quotes&quot; &#39;single&#39;</p>"
  );
});

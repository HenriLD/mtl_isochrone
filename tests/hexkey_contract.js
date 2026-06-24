// CI contract: the browser hexKey (web/hex.js) must produce the same hex as the
// engine's _hex_key. hexkey_cases.json is generated from the Python reference
// (see tests/test_walk.py). Run: node tests/hexkey_contract.js
const path = require("path");
const fs = require("fs");

const { hexKey } = require(path.join(__dirname, "..", "web", "hex.js"));
const cases = JSON.parse(fs.readFileSync(path.join(__dirname, "hexkey_cases.json"), "utf8"));

let fail = 0;
for (const c of cases) {
  const got = hexKey(c.lon, c.lat).split(",").map(Number);
  if (got[0] !== c.key[0] || got[1] !== c.key[1]) {
    console.error(`MISMATCH (${c.lon}, ${c.lat}): JS ${got} vs Python ${c.key}`);
    fail++;
  }
}
if (fail) {
  console.error(`hexKey contract FAILED: ${fail}/${cases.length} mismatches`);
  process.exit(1);
}
console.log(`hexKey contract OK (${cases.length} cases match engine/walk.py)`);

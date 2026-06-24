// Shared hex projection — MUST stay identical to engine/walk.py `_hex_key`
// (the side-quests panel maps a POI's lat/lon to a fog hex this way). Loaded as a
// plain <script> (exposes globals for app.js) AND importable by Node, so CI can
// assert it matches the Python reference (tests/hexkey_contract.js).
(function (root) {
  var HEX_M = 140;
  var HEX_SX = 111320 * Math.cos(45.5 * Math.PI / 180);   // metres per degree lon @ 45.5
  var HEX_SY = 110540;                                     // metres per degree lat
  var HEX_SQ3 = Math.sqrt(3);

  function hexKey(lon, lat) {                              // pointy-top axial + cube rounding
    var s = HEX_M, mx = lon * HEX_SX, my = lat * HEX_SY;
    var x = (HEX_SQ3 / 3 * mx - 1 / 3 * my) / s, z = (2 / 3 * my) / s, y = -x - z;
    var rx = Math.round(x), ry = Math.round(y), rz = Math.round(z);
    var dx = Math.abs(rx - x), dy = Math.abs(ry - y), dz = Math.abs(rz - z);
    if (dx > dy && dx > dz) rx = -ry - rz; else if (dy > dz) ry = -rx - rz; else rz = -rx - ry;
    return rx + "," + rz;
  }

  var api = { HEX_M: HEX_M, HEX_SX: HEX_SX, HEX_SY: HEX_SY, HEX_SQ3: HEX_SQ3, hexKey: hexKey };
  if (typeof module !== "undefined" && module.exports) module.exports = api;   // Node
  for (var k in api) root[k] = api[k];                                          // browser globals
})(typeof window !== "undefined" ? window : globalThis);

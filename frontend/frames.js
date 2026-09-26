// host serial frames: AA 55 | type | len_lo len_hi | payload | xor
// type 1 = sender MAC (6) + mole_pkt_t (234), type 2 = ascii status
(function () {
  const SYNC0 = 0xAA, SYNC1 = 0x55;
  const TYPE_PKT = 0x01, TYPE_STATUS = 0x02;
  const MAGIC = 0x4D;
  const TAPS = 56;
  const PKT_LEN = 10 + 4 * TAPS;
  const PAYLOAD_PKT = 6 + PKT_LEN;
  const MAX_PAYLOAD = 1024;
  const FP_FAILED = 0xFFFF;

  const LINK_PODS = { 1: { tx: 0, rx: 1 }, 2: { tx: 0, rx: 2 }, 3: { tx: 1, rx: 2 } };

  const hex = (b, i, n) => Array.from(b.subarray(i, i + n), x => x.toString(16).padStart(2, '0')).join(':');

  function toRow(dv, t) {
    const linkId = dv.getUint8(1);
    const pods = LINK_PODS[linkId];
    if (dv.getUint8(0) !== MAGIC || !pods) return null;
    const range = dv.getFloat32(4, true), fp = dv.getUint16(8, true);
    const re = new Float32Array(TAPS), im = new Float32Array(TAPS), mag = new Float32Array(TAPS);
    for (let k = 0; k < TAPS; k++) {
      re[k] = dv.getInt16(10 + 2 * k, true);
      im[k] = dv.getInt16(10 + 2 * TAPS + 2 * k, true);
      mag[k] = Math.hypot(re[k], im[k]);
    }
    return {
      kind: 'frame', t, tx: pods.tx, rx: pods.rx, link_id: linkId,
      seq: dv.getUint16(2, true), fp,
      status: fp === FP_FAILED ? 2 : (Number.isNaN(range) ? 1 : 0),
      range: Number.isFinite(range) ? range : null,
      re, im, mag
    };
  }

  function createDecoder(handlers) {
    const h = handlers || {};
    const now = h.now || (() => performance.now());
    let buf = new Uint8Array(0);
    const stats = { frames: 0, packets: 0, status: 0, badChecksum: 0, badPacket: 0, skippedBytes: 0 };

    function push(chunk) {
      const merged = new Uint8Array(buf.length + chunk.length);
      merged.set(buf); merged.set(chunk, buf.length);
      buf = merged;
      let i = 0;
      for (;;) {
        while (i + 1 < buf.length && !(buf[i] === SYNC0 && buf[i + 1] === SYNC1)) { i++; stats.skippedBytes++; }
        if (i + 5 > buf.length) break;
        const type = buf[i + 2], len = buf[i + 3] | (buf[i + 4] << 8);
        if (len > MAX_PAYLOAD) { i += 2; stats.skippedBytes += 2; continue; }
        const end = i + 5 + len + 1;
        if (end > buf.length) break;
        let x = type ^ buf[i + 3] ^ buf[i + 4];
        for (let k = 0; k < len; k++) x ^= buf[i + 5 + k];
        if (x !== buf[end - 1]) { stats.badChecksum++; i += 2; continue; }

        stats.frames++;
        if (type === TYPE_PKT) {
          if (len !== PAYLOAD_PKT) stats.badPacket++;
          else {
            const dv = new DataView(buf.buffer, buf.byteOffset + i + 5 + 6, PKT_LEN);
            const row = toRow(dv, now());
            if (!row) stats.badPacket++;
            else { row.mac = hex(buf, i + 5, 6); stats.packets++; if (h.onRow) h.onRow(row); }
          }
        } else if (type === TYPE_STATUS) {
          stats.status++;
          if (h.onStatus) h.onStatus(String.fromCharCode.apply(null, buf.subarray(i + 5, i + 5 + len)).trim());
        }
        i = end;
      }
      buf = buf.slice(i);
    }

    return { push, stats, reset() { buf = new Uint8Array(0); } };
  }

  function decodeAll(bytes) {
    const rows = [], statuses = [];
    const d = createDecoder({ onRow: r => rows.push(r), onStatus: s => statuses.push(s), now: () => 0 });
    d.push(bytes);
    return { rows, statuses, stats: d.stats };
  }

  window.MolesFrames = { createDecoder, decodeAll, toRow, LINK_PODS, TAPS, PKT_LEN };
})();

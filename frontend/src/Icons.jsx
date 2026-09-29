// Icon defs referenced elsewhere via <svg class="ic"><use href="#i-x"/></svg>.
// Kept as a single sprite sheet, same approach as the old index.html.
export default function Icons() {
  return (
    <svg width="0" height="0" style={{ position: "absolute" }} aria-hidden="true" focusable="false">
      <symbol id="i-activity" viewBox="0 0 24 24">
        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
      </symbol>
      <symbol id="i-zap" viewBox="0 0 24 24">
        <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
      </symbol>
      <symbol id="i-gauge" viewBox="0 0 24 24">
        <path d="m12 14 4-4" />
        <path d="M3.34 19a10 10 0 1 1 17.32 0" />
      </symbol>
      <symbol id="i-radio" viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="2" />
        <path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9" />
        <path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5" />
        <path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5" />
        <path d="M19.1 4.9C23 8.8 23 15.1 19.1 19" />
      </symbol>
      <symbol id="i-up" viewBox="0 0 24 24">
        <polyline points="22 7 13.5 15.5 8.5 10.5 2 17" />
        <polyline points="16 7 22 7 22 13" />
      </symbol>
      <symbol id="i-down" viewBox="0 0 24 24">
        <polyline points="22 17 13.5 8.5 8.5 13.5 2 7" />
        <polyline points="16 17 22 17 22 11" />
      </symbol>
      <symbol id="i-pause" viewBox="0 0 24 24">
        <rect x="14" y="4" width="4" height="16" rx="1" />
        <rect x="6" y="4" width="4" height="16" rx="1" />
      </symbol>
      <symbol id="i-play" viewBox="0 0 24 24">
        <polygon points="6 3 20 12 6 21 6 3" />
      </symbol>
    </svg>
  );
}

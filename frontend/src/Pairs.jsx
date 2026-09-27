import { reverseLink } from "./useMolesData.js";

// Groups directed links into undirected pairs when both directions exist
// (e.g. A->B and B->A), otherwise treats each one-way link as its own group.
function groupLinks(linkIds) {
  const set = new Set(linkIds);
  const seen = new Set();
  const groups = [];
  linkIds.forEach((id) => {
    if (seen.has(id)) return;
    const rev = reverseLink(id);
    if (set.has(rev)) {
      seen.add(id);
      seen.add(rev);
      groups.push({ key: id, label: id.split("->").join("–"), links: [id, rev], twoWay: true });
    } else {
      seen.add(id);
      groups.push({ key: id, label: id.replace("->", " → "), links: [id], twoWay: false });
    }
  });
  return groups;
}

export default function Pairs({ linkIds, current }) {
  const groups = groupLinks(linkIds);

  return (
    <ul className="rowlist pairs" aria-label="Pair checks">
      {groups.map((g) => {
        const states = g.links.map((l) => !!(current[l] && current[l].breathing));
        const allHit = states.every(Boolean);
        const anyHit = states.some(Boolean);
        const cls = allHit ? "high" : anyHit ? "med" : "fill";
        const dot = allHit ? "ok" : anyHit ? "hot" : "";
        const verdict = allHit ? "both directions" : anyHit ? "one direction" : "quiet";
        return (
          <li key={g.key}>
            <span className={"dotc " + dot} />
            <span className="nm">{g.label}</span>
            <span className={"end badge " + cls}>{verdict}</span>
          </li>
        );
      })}
    </ul>
  );
}

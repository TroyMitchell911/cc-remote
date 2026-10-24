import type { DshItem } from "./protocol";

/** Page ranges are native exclusive sequence windows. Preserve newer outcomes. */
export function mergeDshPages(older: DshItem[], newer: DshItem[], kind: string): DshItem[] {
  const rows = new Map(older.map(row => [row.id, row]));
  for (const row of newer) {
    const previous = rows.get(row.id);
    if (!previous || !["workflows", "trajectory"].includes(kind)) { rows.set(row.id, row); continue; }
    const before = previous.data ?? {}, after = row.data ?? {};
    if (kind === "workflows") {
      const members = new Map<string, Record<string, unknown>>();
      for (const item of [...(Array.isArray(before.members) ? before.members : []), ...(Array.isArray(after.members) ? after.members : [])]) {
        const member = item as Record<string, unknown>;
        const key = String(member.seq ?? member.sid);
        members.set(key, { ...members.get(key), ...member });
      }
      rows.set(row.id, { ...row, title: after.name_known ? row.title : previous.title,
        state: row.state === "unknown" ? previous.state : row.state,
        data: { ...before, ...after, name_known: !!(before.name_known || after.name_known), members: [...members.values()] } });
    } else {
      const events = new Map<number, unknown>();
      for (const event of [...(Array.isArray(before.events) ? before.events : []), ...(Array.isArray(after.events) ? after.events : [])]) {
        const entry = event as Record<string, unknown>;
        if (typeof entry.seq === "number") events.set(entry.seq, entry);
      }
      rows.set(row.id, { ...row, state: row.state === "unknown" ? previous.state : row.state,
        data: { ...before, ...after, started: before.started ?? after.started,
          events: [...events.values()], input: Number(before.input ?? 0) + Number(after.input ?? 0),
          output: Number(before.output ?? 0) + Number(after.output ?? 0) } });
    }
  }
  return [...rows.values()];
}

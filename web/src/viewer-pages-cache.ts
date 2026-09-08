import { viewerScopeKey, type ViewerPage, type ViewerScope } from "./remote-viewer.ts";

/** Paint-only metadata. Never cache grants, resource bytes or authorization.
 * Kept by one provider (not global/storage), bounded across focus switches. */
export class ViewerPagesCache {
  private readonly entries = new Map<string, { pages: ViewerPage[]; size: number }>();

  get(scope: ViewerScope): ViewerPage[] | undefined {
    return this.entries.get(viewerScopeKey(scope))?.pages;
  }

  set(scope: ViewerScope, pages: ViewerPage[]): void {
    const key = viewerScopeKey(scope);
    const size = JSON.stringify(pages).length * 2;
    this.entries.delete(key);
    if (pages.length > 32 || size > 96 * 1024) return;
    this.entries.set(key, { pages, size });
    let total = [...this.entries.values()].reduce((sum, entry) => sum + entry.size, 0);
    while (this.entries.size > 32 || total > 512 * 1024) {
      const first = this.entries.entries().next().value;
      if (!first) break;
      total -= first[1].size;
      this.entries.delete(first[0]);
    }
  }

  clear(): void { this.entries.clear(); }
}

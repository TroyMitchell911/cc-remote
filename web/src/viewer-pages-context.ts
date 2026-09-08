import { createContext, useContext } from "react";
import type { ViewerPage, ViewerScope } from "./remote-viewer";

export interface PagesContext {
  scope: ViewerScope; pages: ViewerPage[]; loading: boolean; error: string | null;
  /** False only for paint-only restoration before the lazy controller mounts. */
  commandsReady?: boolean;
  refresh: () => Promise<ViewerPage[]>;
  discover: (paths: string[], turnId: string, done?: boolean) => void;
  associate: (page: { machine_id: string; site_id: string; entry: string }) => Promise<ViewerPage | undefined>;
  remove: (id: string) => Promise<void>;
  open: (page: ViewerPage) => void;
  openLink?: (href: string) => boolean;
}
export const ViewerPagesContext = createContext<PagesContext | null>(null);
export const useViewerPages = () => useContext(ViewerPagesContext);

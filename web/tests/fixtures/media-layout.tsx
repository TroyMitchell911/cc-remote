import { useCallback, useMemo, useState } from "react";
import { MermaidBlock } from "../../src/components/MermaidBlock";
import { HistoryUserImage } from "../../src/components/HistoryUserImage";
import { MessageBlock } from "../../src/components/MessageBlock";
import { GeneratedImagePreview } from "../../src/components/ProcessTimeline";
import type { HistoryImageAsset } from "../../src/history-image-assets";
import type { InlineImageAsset } from "../../src/inline-image-assets";

const source = "flowchart TD\nA[Start] --> B[Prepare]\nB --> C[Finish]";

export function MediaLayoutFixture() {
  const [mounted, setMounted] = useState(true);
  const [copies, setCopies] = useState(1);
  const [loaded, setLoaded] = useState<Record<string, HistoryImageAsset>>({});
  const [ready, setReady] = useState(false);
  const onLoad = useCallback((_turn: string, id: string) => {
    setLoaded((previous) => ({ ...previous, [id]: { status: "loading", startedAt: Date.now() } }));
    return true;
  }, []);
  const geometry = new URLSearchParams(location.search).has("geometry");
  const data = useMemo(() => {
    const canvas = document.createElement("canvas");
    canvas.width = 400;
    canvas.height = 600;
    canvas.getContext("2d")!.fillRect(0, 0, 400, 600);
    return canvas.toDataURL("image/png").split(",")[1];
  }, []);
  const asset: InlineImageAsset = {
    status: ready ? "ready" : "loading", width: 400, height: 600,
    ...(ready ? { data, mediaType: "image/png" } : {}),
  };
  if (geometry) return <main style={{ padding: 20 }}>
    <button onClick={() => setReady(true)}>Resolve images</button>
    <MessageBlock text="![portrait](/tmp/portrait.png)" done
      imageAssets={{ "/tmp/portrait.png": asset }} onPreviewImage={() => {}} />
    <GeneratedImagePreview block={{ kind: "process", processKind: "mcp", tool: "image_generation",
      input: { path: "/tmp/portrait.png", preview_id: "preview",
        history_image: { image_id: "portrait", width: 400, height: 600, media_type: "image/png", byte_size: 10 } },
      item_id: "generated", title: "Generate image", phase: "end", status: "succeeded", done: true }} imageAssets={{ preview: asset }} />
    <p data-testid="below-images">The reading position below both images.</p>
  </main>;
  return <main style={{ padding: 20 }}>
    <button onClick={() => setMounted((value) => !value)}>Toggle diagram</button>
    <button onClick={() => setCopies(2)}>Duplicate diagram</button>
    <output data-testid="loaded-media">{Object.keys(loaded).join(",")}</output>
    <div style={{ height: 420, overflow: "hidden" }}>
      <div data-testid="media-scrollport" style={{ height: 400, overflowY: "auto" }}>
        <div style={{ height: 800 }} />
        <div className="prose" data-testid="near-diagram">
          {mounted && Array.from({ length: copies }, (_, index) => <MermaidBlock key={index} source={source} />)}
        </div>
        <HistoryUserImage turnId="turn" imageId="near" width={400} height={200}
          asset={loaded.near} onLoad={onLoad} onPreview={() => {}} />
        <div style={{ height: 3000 }} />
        <HistoryUserImage turnId="turn" imageId="far" width={400} height={200}
          asset={loaded.far} onLoad={onLoad} onPreview={() => {}} />
      </div>
    </div>
  </main>;
}

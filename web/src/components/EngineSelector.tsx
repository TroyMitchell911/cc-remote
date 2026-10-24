import { lazy, Suspense, useEffect, useRef, useState } from "react";
import type { Engine } from "../protocol";
import { EngineIcon } from "../icons";

const EngineMenu = lazy(() => import("./EngineMenu"));

export function EngineSelector({ engine, onChange }: {
  engine: Engine;
  onChange: (engine: Engine) => void;
}) {
  const button = useRef<HTMLButtonElement>(null);
  const [menu, setMenu] = useState<boolean | null>(null);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229 || event.repeat
          || !(event.metaKey || event.ctrlKey) || event.shiftKey || event.altKey
          || (event.key !== "1" && event.key !== "2" && event.key !== "3")) return;
      // Harness navigation belongs to the app, even inside a modal or editor.
      // Capture before their key handlers; never submit or dismiss their work.
      event.preventDefault();
      event.stopPropagation();
      setMenu(null);
      const next = event.key === "1" ? "claude" : event.key === "2" ? "codex" : "dsh";
      if (next !== engine) onChange(next);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [engine, onChange]);
  const close = (restoreFocus: boolean) => {
    setMenu(null);
    if (restoreFocus) button.current?.focus();
  };
  const open = (keyboard: boolean) => {
    if (!keyboard) button.current?.blur();
    setMenu(keyboard);
  };
  return <span className="engine-selector">
    <button ref={button} type="button" className="engine-toggle" aria-label="切换新会话引擎"
      aria-haspopup="menu" aria-expanded={menu !== null} title="切换会话引擎"
      onClick={event => menu !== null ? close(false) : open(event.detail === 0)}
      onKeyDown={event => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault(); open(true);
        }
      }}>
      <span className="engine-label"><EngineIcon engine={engine} />
        {engine === "dsh" ? "DSH" : engine === "codex" ? "Codex" : "Claude"}</span>
    </button>
    {menu !== null && <Suspense fallback={null}><EngineMenu engine={engine} anchor={button}
      keyboard={menu} onClose={close} onSelect={(next, keyboard) => {
        close(keyboard);
        if (next !== engine) onChange(next);
      }} /></Suspense>}
  </span>;
}

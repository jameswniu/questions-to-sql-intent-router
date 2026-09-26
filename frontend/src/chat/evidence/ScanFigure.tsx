import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { cellText, scanCaption } from "@/chat/evidence/model";
import { cn } from "@/lib/cn";

// Scans are rendered at about twice screen resolution, so a crop is drawn at half size to match the page text.
const PAD = 12;
const SCALE = 0.5;
const images = new Map<string, Promise<HTMLImageElement>>();

function loadScan(docId: string): Promise<HTMLImageElement> {
  let found = images.get(docId);
  if (!found) {
    found = new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => {
        resolve(image);
      };
      image.onerror = () => {
        reject(new Error(`scan ${docId} did not load`));
      };
      image.src = `/evidence/scan/${encodeURIComponent(docId)}`;
    });
    images.set(docId, found);
  }
  return found;
}

function Crop({ image, field, label }: { image: HTMLImageElement; field: Record<string, unknown>; label: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const box = Array.isArray(field.bbox) && field.bbox.length === 4 ? field.bbox.map(Number) : null;
  const [x, y, w, h] = box ?? [0, 0, image.naturalWidth, image.naturalHeight];
  const sx = Math.max(0, (x ?? 0) - PAD);
  const sy = Math.max(0, (y ?? 0) - PAD);
  const sw = Math.min(image.naturalWidth, (x ?? 0) + (w ?? 0) + PAD) - sx;
  const sh = Math.min(image.naturalHeight, (y ?? 0) + (h ?? 0) + PAD) - sy;
  useLayoutEffect(() => {
    canvas.current?.getContext("2d")?.drawImage(image, sx, sy, sw, sh, 0, 0, sw, sh);
  }, [image, sx, sy, sw, sh]);
  return (
    <canvas
      ref={canvas}
      width={sw}
      height={sh}
      // A canvas that only draws part of a scan is a picture, and its label says what it shows.
      // eslint-disable-next-line jsx-a11y-x/no-interactive-element-to-noninteractive-role
      role="img"
      aria-label={label}
      style={{ width: `${Math.round(sw * SCALE)}px` }}
      className="block h-auto max-w-full rounded-md border border-border-strong bg-white"
    />
  );
}

/** One field on a scanned form, cropped out of the scan with its caption. The image loads after the answer. */
export function ScanFigure({ field }: { field: Record<string, unknown> }) {
  const docId = cellText(field.doc_id);
  const [loaded, setLoaded] = useState<{ docId: string; image: HTMLImageElement | null } | null>(null);
  useEffect(() => {
    let live = true;
    loadScan(docId).then(
      (image) => {
        if (live) setLoaded({ docId, image });
      },
      () => {
        if (live) setLoaded({ docId, image: null });
      },
    );
    return () => {
      live = false;
    };
  }, [docId]);
  const caption = scanCaption(field);
  const state = loaded?.docId === docId ? loaded : null;
  return (
    <figure
      className={cn(
        "scan flex flex-col items-start gap-2.5 rounded-lg border border-border bg-surface p-3",
        Boolean(field.flagged) && "border-warning-border bg-warning-soft",
      )}
    >
      {state === null ? (
        <div aria-hidden="true" className="h-20 w-48 max-w-full animate-pulse rounded-md bg-surface-hover" />
      ) : state.image ? (
        <Crop image={state.image} field={field} label={`Part of scan ${docId}. ${caption}`} />
      ) : (
        <p className="legend">The scan image is not available.</p>
      )}
      <figcaption>{caption}</figcaption>
    </figure>
  );
}

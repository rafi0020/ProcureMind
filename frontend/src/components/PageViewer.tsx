"use client";

import { useState } from "react";
import { API, useApi } from "@/lib/api";

interface PageInfo { page: number; width: number; height: number; source: string }
export interface Box { page: number; bbox: number[]; label: string; active?: boolean }

/** A document page as an image on a raised sheet, with boxes drawn where extracted values were found. Boxes are in the
 * page's own units (PDF points, or pixels for images), so they are placed as fractions of the page's width and height. */
export function PageViewer({ documentId, boxes, page, onPage }: { documentId: string; boxes: Box[]; page: number; onPage: (n: number) => void }) {
  const info = useApi<{ pages: PageInfo[] }>(`/api/documents/${documentId}/pages`);
  const [failed, setFailed] = useState(false);
  const pages = info.data?.pages ?? [];
  const current = pages.find((p) => p.page === page);
  if (info.error) return <p className="hint">The document page could not be loaded.</p>;
  if (info.data && !pages.length) return <p className="muted">This document has no page image.</p>;
  if (!info.data) return <div className="sheet skeleton" style={{ aspectRatio: "1 / 1.35" }} role="status" aria-label="Loading the document" />;
  return (
    <div className="viewer">
      {pages.length > 1 && (
        <div className="viewer__pages" role="tablist" aria-label="Pages">
          {pages.map((p) => (
            <button key={p.page} role="tab" aria-selected={p.page === page} className="tab" onClick={() => onPage(p.page)}>Page {p.page}</button>
          ))}
        </div>
      )}
      <div className="viewer__frame">
        {failed ? <p className="muted p-6">The page image could not be shown.</p> : (
          // eslint-disable-next-line @next/next/no-img-element -- a same-origin image from the API, not a static asset
          <img src={`${API}/api/documents/${documentId}/pages/${page}.png`} alt={`Page ${page} of the document`} onError={() => setFailed(true)} />
        )}
        {current && boxes.filter((b) => b.page === page).map((b) => (
          <span key={b.label} className="viewer__box" data-active={b.active ? "true" : undefined} title={b.label}
            style={{ left: `${(b.bbox[0] / current.width) * 100}%`, top: `${(b.bbox[1] / current.height) * 100}%`,
                     width: `${((b.bbox[2] - b.bbox[0]) / current.width) * 100}%`, height: `${((b.bbox[3] - b.bbox[1]) / current.height) * 100}%` }} />
        ))}
      </div>
      {current && <p className="viewer__note">{current.source === "ocr" ? "Read by OCR from a scan." : current.source === "sheet" ? "A spreadsheet." : "Read from the PDF's text."}</p>}
    </div>
  );
}

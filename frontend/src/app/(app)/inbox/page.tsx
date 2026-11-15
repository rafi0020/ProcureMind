"use client";

import { Upload } from "lucide-react";
import Link from "next/link";
import { useRef, useState, type DragEvent } from "react";
import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, StatusChip, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { when } from "@/lib/format";

interface Doc { id: string; filename: string; kind: string | null; status: string; error: string | null; source: string; received_at: string; pages: number | null; duplicate_of: string | null; inbox_from: string | null; inbox_subject: string | null }
interface InvoiceRef { id: string; document_id: string | null }

const KIND: Record<string, string> = { invoice: "Invoice", credit_note: "Credit note", purchase_order: "Purchase order", goods_receipt: "Goods receipt", statement: "Statement", other: "Other" };
const STATUS_WORDS = (s: string) => (s === "needs_review" ? "needs review" : s.replace(/_/g, " "));

export default function InboxPage() {
  const { user } = useMe();
  const docs = useApi<{ documents: Doc[] }>("/api/documents?limit=200", { refreshInterval: (d) => (d?.documents.some((x) => x.status === "queued" || x.status === "processing") ? 3000 : 30_000) });
  const invoices = useApi<{ invoices: InvoiceRef[] }>("/api/invoices?limit=500");
  const byDocument = new Map((invoices.data?.invoices ?? []).filter((i) => i.document_id).map((i) => [i.document_id!, i.id]));
  const [busy, setBusy] = useState(false);
  const [over, setOver] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const toast = useToast();
  const canUpload = user && user.role !== "viewer";
  const list = docs.data?.documents ?? [];
  const current = list.find((d) => d.id === picked) ?? list[0] ?? null;

  const send = async (files: FileList | null) => {
    if (!files?.length) return;
    const form = new FormData();
    Array.from(files).forEach((f) => form.append("files", f));
    setBusy(true);
    try {
      const r = await api<{ documents: Doc[] }>("/api/documents", { method: "POST", body: form });
      const dups = r.documents.filter((d) => d.duplicate_of).length;
      toast(`${r.documents.length - dups} document(s) queued${dups ? `, ${dups} already received` : ""}.`, "ok");
      docs.mutate();
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Upload failed.", "bad");
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  };
  const drop = (e: DragEvent) => { e.preventDefault(); setOver(false); send(e.dataTransfer.files); };

  return (
    <Page title="Inbox" lede="Invoices, credit notes, orders and receipts as they arrive: by upload, or by the inbox address. Each is read, matched and checked.">
      <div className="intake">
        {canUpload && (
          <label className="dropzone" data-over={over} onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)} onDrop={drop}>
            <Upload aria-hidden />
            <span><strong>{busy ? "Uploading" : "Drop files here or choose them"}</strong><span className="label block">PDF, PNG, JPEG, TIFF, XLSX or CSV, up to 20 MB each, ten at a time</span></span>
            <input ref={input} type="file" multiple accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.xlsx,.csv" className="sr-only" onChange={(e) => send(e.target.files)} disabled={busy} />
          </label>
        )}
      </div>
      {docs.error && <ErrorState error={docs.error} retry={() => docs.mutate()} />}
      {docs.isLoading && <div className="panel"><Loading rows={6} /></div>}
      {docs.data && list.length === 0 && <div className="panel"><Empty title="Nothing has arrived yet">Upload an invoice, or send one to the inbox address an administrator sets up.</Empty></div>}
      {!!list.length && current && (
        <div className="split">
          <section className="panel" aria-label="Received documents">
            <ul className="queue">
              {list.map((d) => {
                const from = d.source === "inbox" ? d.inbox_from ?? "Inbox" : "Upload";
                return (
                  <li key={d.id}>
                    <button type="button" className="queue__item queue__button" aria-current={d.id === current.id ? "true" : undefined} onClick={() => setPicked(d.id)}>
                      <span className="queue__top"><span className="queue__ref">{d.filename}</span><StatusChip status={d.status} label={STATUS_WORDS(d.status)} /></span>
                      <span className="queue__sub"><span>{d.inbox_subject ?? from}{d.inbox_subject && <span className="block label">{from}</span>}</span><span className="queue__age">{when(d.received_at)}</span></span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>

          <aside className="split__side split__side--sticky" aria-label="Selected document">
            <section className="panel" aria-live="polite">
              <div className="panel__head"><h2>What arrived</h2><span className="label">{current.source === "inbox" ? "By the inbox address" : "By upload"}</span></div>
              <div className="panel__body grid gap-4">
                <dl className="facts">
                  <dt>File</dt><dd>{current.filename}</dd>
                  <dt>Kind</dt><dd>{current.kind ? KIND[current.kind] : "not known yet"}</dd>
                  <dt>Pages</dt><dd>{current.pages ?? "not counted"}</dd>
                  <dt>Received</dt><dd>{when(current.received_at)}</dd>
                  {current.inbox_from && <><dt>From</dt><dd>{current.inbox_from}</dd></>}
                  {current.inbox_subject && <><dt>Subject</dt><dd>{current.inbox_subject}</dd></>}
                </dl>
                <div className="grid gap-2">
                  <span className="label">Status</span>
                  <div><StatusChip status={current.status} label={STATUS_WORDS(current.status)} /></div>
                </div>
                {current.duplicate_of && <div className="banner banner--open" role="status">This is a copy of a document already received.</div>}
                {current.error && <div className="banner banner--bad" role="alert">{current.error}</div>}
                <div className="grid gap-2">
                  <span className="label">Became</span>
                  {byDocument.get(current.id) ? (
                    <Link className="btn" href={`/invoices/?id=${byDocument.get(current.id)}`}>Open the invoice</Link>
                  ) : <p className="ink-2">{current.status === "needs_review" ? "No invoice yet: a person needs to read it." : "No invoice yet."}</p>}
                </div>
              </div>
            </section>
          </aside>
        </div>
      )}
    </Page>
  );
}

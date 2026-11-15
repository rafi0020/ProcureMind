# Sample documents to upload

Two copies of an invoice that Kestrel Foods already received (Harbor Metals Ltd, INV-P1001, against PO-P1001), sent
again under other spellings of its number. Sign in as `clerk@kestrel.demo`, open **Inbox**, upload either file, and
watch it become an exception: a duplicate of INV-P1001, and billing more than was received.

- `resent-invoice-digital.pdf`: a digital PDF numbered "INV P1001".
- `resent-invoice-scanned.pdf`: a worn scan numbered "inv-p1001", read by OCR.

Generated with `seed/documents.py`; the planted cases already in the demo are listed in `backend/seed/generate.py`.

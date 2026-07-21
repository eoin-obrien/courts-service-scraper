# Data dictionary

Generated from the export Table Schema (`EXPORT_FIELDS`). Do not edit by hand; regenerate with `courts-scraper dictionary`.

Primary key: `document_uuid`. Missing values are the empty string.

| Column | Type | Format | Description |
|--------|------|--------|-------------|
| `document_uuid` | string |  | Stable Alfresco id for this specific document/opinion. Primary key. |
| `collection_uuid` | string |  | Alfresco id grouping all documents of one judgment/case. |
| `neutral_citation` | string |  | e.g. '[2026] IESC 36'. Case-level: repeats across a case's opinions, so NOT a row identifier. |
| `title` | string |  | Case title as published. |
| `court` | string |  | Court name as published. |
| `authoring_judge` | string |  | Author of THIS opinion (not the whole bench). |
| `panel` | string |  | Full bench that heard the case, ';'-separated. See panel_count. |
| `panel_count` | integer |  | Number of judges on the panel. |
| `date_delivered` | date | %Y-%m-%d | Delivery date (ISO 8601). |
| `date_uploaded` | date | %Y-%m-%d | Upload date (ISO 8601). |
| `record_number` | string |  | Court record number. |
| `status` | string |  | Status label as served (e.g. 'Approved'). |
| `status_in_vocab` | boolean |  | False flags a status value outside the observed controlled vocabulary. |
| `result` | string |  | Result label as served (e.g. 'Allow Appeal'). |
| `result_in_vocab` | boolean |  | False flags a result value outside the observed controlled vocabulary. |
| `vocab_flags` | string |  | Human-readable drift warnings, empty when all values are in vocabulary. |
| `view_url` | string | uri | Judgment view page. |
| `pdf_url` | string | uri | Direct PDF download URL. |
| `filename` | string |  | Local PDF filename in the bundle. |
| `sha256` | string |  | Hex SHA-256 of the downloaded PDF. |
| `bytes` | integer |  | On-disk size of the downloaded PDF. |
| `http_content_type` | string |  | Content-Type header served with the PDF. |
| `http_content_length` | integer |  | Content-Length header served with the PDF (often absent). |
| `http_last_modified` | string |  | Last-Modified header served with the PDF (often absent). |
| `http_etag` | string |  | ETag header served with the PDF. |
| `listed_at` | datetime |  | When the search row was first seen (UTC ISO 8601). |
| `meta_retrieved_at` | datetime |  | When the view page was scraped (UTC ISO 8601). |
| `pdf_retrieved_at` | datetime |  | When the PDF was fetched and verified (UTC ISO 8601). |
| `meta_status` | string |  | pending \| ok \| error. |
| `download_status` | string |  | pending \| done \| error. |

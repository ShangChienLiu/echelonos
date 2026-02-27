"""Azure Document Intelligence client for OCR with table preservation."""

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential

from echelonos.config import settings


def get_azure_client() -> DocumentIntelligenceClient:
    return DocumentIntelligenceClient(
        endpoint=settings.azure_doc_intelligence_endpoint,
        credential=AzureKeyCredential(settings.azure_doc_intelligence_key),
    )


def analyze_document(client: DocumentIntelligenceClient, file_path: str) -> dict:
    """Analyze a document using Azure Document Intelligence.

    Returns per-page text with table structures preserved as markdown.
    """
    with open(file_path, "rb") as f:
        poller = client.begin_analyze_document("prebuilt-layout", body=f, content_type="application/octet-stream")

    result = poller.result()

    pages = []
    for page in result.pages:
        page_data = {
            "page_number": page.page_number,
            "text": "",
            "tables": [],
            "confidence": page.spans[0].confidence if page.spans else 0.0,
        }
        pages.append(page_data)

    # Extract tables as markdown
    if result.tables:
        for table in result.tables:
            rows = {}
            for cell in table.cells:
                row_idx = cell.row_index
                if row_idx not in rows:
                    rows[row_idx] = {}
                rows[row_idx][cell.column_index] = cell.content

            # Build markdown table
            md_rows = []
            for row_idx in sorted(rows.keys()):
                cols = rows[row_idx]
                md_row = " | ".join(cols.get(c, "") for c in range(table.column_count))
                md_rows.append(f"| {md_row} |")
                if row_idx == 0:
                    md_rows.append("|" + " --- |" * table.column_count)

            md_table = "\n".join(md_rows)

            # Assign table to the page it starts on
            if table.bounding_regions:
                page_num = table.bounding_regions[0].page_number
                for p in pages:
                    if p["page_number"] == page_num:
                        p["tables"].append(md_table)

    # Extract page text
    if result.content:
        for paragraph in result.paragraphs or []:
            if paragraph.bounding_regions:
                page_num = paragraph.bounding_regions[0].page_number
                for p in pages:
                    if p["page_number"] == page_num:
                        p["text"] += paragraph.content + "\n"

    return {"pages": pages, "total_pages": len(pages)}

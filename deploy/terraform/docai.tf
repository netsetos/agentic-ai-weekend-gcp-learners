# One processor, chosen by residency - the terraform half of parser.py's PROCESSORS map.
# Keep the two in step: a type added here without a row there is a processor nothing calls.
locals {
  docai = {
    india = { location = "asia-south1", type = "OCR_PROCESSOR" }
    us    = { location = "us", type = "LAYOUT_PARSER_PROCESSOR" }
  }
  docai_cfg = local.docai[var.residency]
}

resource "google_document_ai_processor" "documind" {
  project      = var.project_id
  location     = local.docai_cfg.location
  display_name = "documind-parser"
  type         = local.docai_cfg.type
}

# parser.py builds the processor path itself, so it needs the bare id and the
# location separately - not the full resource name.
output "docai_processor_id" {
  description = "DOCAI_PROCESSOR_ID for the ingest worker"
  value       = element(split("/", google_document_ai_processor.documind.id), 5)
}

output "docai_location" {
  description = "matches RESIDENCY in services/ingest/parser.py"
  value       = local.docai_cfg.location
}

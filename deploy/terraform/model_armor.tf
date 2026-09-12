# The template guard.py sanitises against, on both sides of the model.
resource "google_model_armor_template" "documind_guard" {
  provider    = google-beta
  location    = var.india_region        # regional: it inspects India-resident prompts
  template_id = "documind-guard"

  filter_config {
    # Prompt injection and jailbreak, at MEDIUM_AND_ABOVE. The polite, code-mixed
    # attempt this market actually sees scores MEDIUM; a HIGH floor reports clean
    # and passes it.
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "MEDIUM_AND_ABOVE"
    }

    # Sensitive Data Protection, basic config: the same India info types the
    # ingest worker scans with (shared/pii.py), applied to prompts and responses.
    sdp_settings {
      basic_config {
        filter_enforcement = "ENABLED"
      }
    }

    # Responsible-AI filters. Dangerous and harassment at MEDIUM_AND_ABOVE for the
    # same reason as above.
    rai_settings {
      rai_filters {
        filter_type      = "DANGEROUS"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "HARASSMENT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "HATE_SPEECH"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "SEXUALLY_EXPLICIT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
    }
  }

  template_metadata {
    # Log what was sanitised, never the text. A findings log that quotes the
    # injection has stored the payload it blocked.
    log_sanitize_operations = true
  }
}

output "armor_template" {
  description = "ARMOR_TEMPLATE for rag-api (guard.py)"
  value       = google_model_armor_template.documind_guard.template_id
}

output "armor_location" {
  description = "ARMOR_LOCATION for rag-api - regional, NOT the global endpoint"
  value       = var.india_region
}

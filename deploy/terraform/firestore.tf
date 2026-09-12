resource "google_firestore_database" "main" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.india_region   # DPDPA-aligned data residency
  type        = "FIRESTORE_NATIVE"
  concurrency_mode                = "OPTIMISTIC"
  app_engine_integration_mode     = "DISABLED"
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_ENABLED"
  delete_protection_state         = "DELETE_PROTECTION_ENABLED"

  # A database that already existed - Module 4's notebooks create (default) in whichever
  # region the learner chose - is ADOPTED by `make adopt-firestore`, region and all. The
  # residency above applies to a database this kit creates; a location change on an
  # adopted one would otherwise plan a destroy of the corpus.
  lifecycle {
    ignore_changes = [location_id, type, concurrency_mode, app_engine_integration_mode]
  }
}

resource "google_storage_bucket" "backend_uploads" {
  for_each = var.enable_gke ? local.gke_upload_buckets : {}

  project  = var.project_id
  name     = each.key
  location = each.value.location

  force_destroy = false

  lifecycle {
    prevent_destroy = true
    ignore_changes = [
      encryption,
      location,
      storage_class,
    ]
  }

  depends_on = [
    google_project_service.gke_required,
  ]
}

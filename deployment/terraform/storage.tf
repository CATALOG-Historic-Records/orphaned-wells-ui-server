resource "google_storage_bucket" "backend_uploads" {
  for_each = var.enable_gke ? local.gke_upload_buckets : {}

  project  = var.project_id
  name     = each.key
  location = each.value.location

  force_destroy = false

  cors {
    origin = lookup(var.upload_bucket_cors_origins, each.key, distinct(flatten([
      for name, backend in local.gke_backends :
      name == "staging" ? [
        "https://${var.backend_dns_domain}",
        "http://localhost:3000",
        "http://localhost:3001",
      ] : ["https://${name}.${var.backend_dns_domain}"]
      if backend.upload_bucket_name == each.key
    ])))
    method          = ["GET", "HEAD", "POST", "PUT"]
    response_header = ["Content-Type", "Content-Range", "Range", "Location", "ETag"]
    max_age_seconds = 3600
  }

  # Originals and batch output are temporary. Record display images and caller-
  # owned GCS batch source files are outside these two prefixes.
  lifecycle_rule {
    action { type = "Delete" }
    condition {
      age            = 14
      matches_prefix = ["directory_uploads/", "directory_upload_outputs/"]
    }
  }

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

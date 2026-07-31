terraform {
  backend "gcs" {
    bucket = "tidy-outlet-412020-ogrre-terraform-state"
    prefix = "orphaned-wells-ui-server"
  }
}

# if we use oslogin in the future, we need to add users like so:
# resource "google_project_iam_member" "mpesce_oslogin" {
#   project = var.project_id
#   role    = "roles/compute.osAdminLogin"
#   member  = "user:mpesce@lbl.gov"
# }
locals {
  gke_required_services = toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "dns.googleapis.com",
  ])

  gke_backends = {
    for name, _ in local.collaborators : name => {
      namespace            = "uow-${name}"
      hostname             = trimsuffix(lookup(var.gke_backend_hostnames, name, "${name}-k8s-server.uow-carbon.org"), ".")
      test_hostname        = "${name}-k8s-server.uow-carbon.org"
      static_ip_name       = "${name}-uow-gke-ip"
      replicas             = 1
      cpu_request          = "2"
      memory_request       = "6Gi"
      cpu_limit            = "2"
      memory_limit         = "6Gi"
      persistent_disk_size = name == "ca" ? "10Gi" : "20Gi"
    }
  }
}

resource "google_project_service" "gke_required" {
  for_each = var.enable_gke && var.manage_project_services ? local.gke_required_services : toset([])

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

resource "google_container_cluster" "backend" {
  count = var.enable_gke ? 1 : 0

  name     = var.gke_cluster_name
  location = var.gke_location

  enable_autopilot    = true
  deletion_protection = var.gke_deletion_protection

  network    = var.gke_network
  subnetwork = var.gke_subnetwork

  release_channel {
    channel = var.gke_release_channel
  }

  ip_allocation_policy {}

  depends_on = [
    google_project_service.gke_required,
  ]
}

resource "google_compute_global_address" "gke_backend" {
  for_each = var.enable_gke ? local.gke_backends : {}

  name = each.value.static_ip_name
}

resource "google_dns_record_set" "gke_backend_test" {
  for_each = var.enable_gke && var.create_gke_test_dns_records ? local.gke_backends : {}

  name         = "${each.value.test_hostname}."
  type         = "A"
  ttl          = var.gke_dns_ttl
  managed_zone = "uow-carbon-org"

  rrdatas = [
    google_compute_global_address.gke_backend[each.key].address,
  ]
}

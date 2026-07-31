locals {
  gke_required_services = toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "dns.googleapis.com",
  ])

  gke_backend_definitions = merge(var.gke_backends, var.gke_backend_overrides)

  gke_backends = {
    for name, backend in local.gke_backend_definitions : name => {
      namespace                 = coalesce(try(backend.namespace, null), "uow-${name}")
      hostname                  = trimsuffix(coalesce(try(backend.hostname, null), "${name}-server.${var.backend_dns_domain}"), ".")
      test_hostname             = trimsuffix(coalesce(try(backend.test_hostname, null), "${name}-k8s-server.${var.backend_dns_domain}"), ".")
      static_ip_name            = coalesce(try(backend.static_ip_name, null), "${name}-uow-gke-ip")
      replicas                  = coalesce(try(backend.replicas, null), 2)
      cpu_request               = coalesce(try(backend.cpu_request, null), "2")
      memory_request            = coalesce(try(backend.memory_request, null), "12Gi")
      cpu_limit                 = coalesce(try(backend.cpu_limit, null), "2")
      memory_limit              = coalesce(try(backend.memory_limit, null), "12Gi")
      persistent_disk_size      = coalesce(try(backend.persistent_disk_size, null), "20Gi")
      create_primary_dns_record = coalesce(try(backend.create_primary_dns_record, null), true)
      create_test_dns_record    = coalesce(try(backend.create_test_dns_record, null), true)
      dns_ttl                   = coalesce(try(backend.dns_ttl, null), var.gke_dns_ttl)
    }
  }

  gke_primary_dns_backends = {
    for name, backend in local.gke_backends : name => backend
    if backend.create_primary_dns_record
  }

  gke_test_dns_backends = {
    for name, backend in local.gke_backends : name => backend
    if backend.create_test_dns_record
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

resource "google_dns_record_set" "gke_backend_primary" {
  for_each = var.enable_gke ? local.gke_primary_dns_backends : {}

  name         = "${each.value.hostname}."
  type         = "A"
  ttl          = each.value.dns_ttl
  managed_zone = var.dns_managed_zone

  rrdatas = [
    google_compute_global_address.gke_backend[each.key].address,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_dns_record_set" "gke_backend_test" {
  for_each = var.enable_gke && var.create_gke_test_dns_records ? local.gke_test_dns_backends : {}

  name         = "${each.value.test_hostname}."
  type         = "A"
  ttl          = each.value.dns_ttl
  managed_zone = var.dns_managed_zone

  rrdatas = [
    google_compute_global_address.gke_backend[each.key].address,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

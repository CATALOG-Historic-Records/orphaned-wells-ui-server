terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0, < 7.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  collaborators = {
    isgs = {
      enable_startup_script  = false
      zone                   = "us-central1-a"
      machine_type           = "e2-standard-2"
      boot_image             = "https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-12-bookworm-v20240515"
      boot_disk_size         = 20
      boot_resource_policies = []
      boot_disk_device_name  = "isgs-uow-server"
    }

    osage = {
      enable_startup_script = false
      zone                  = "us-central1-f"
      machine_type          = "e2-standard-2"
      boot_image            = "https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-12-bookworm-v20250910"
      boot_disk_size        = 20
      boot_resource_policies = [
        "https://www.googleapis.com/compute/v1/projects/tidy-outlet-412020/regions/us-central1/resourcePolicies/default-schedule-1"
      ]
      boot_disk_device_name = "osage-uow-server"
    }

    ca = {
      enable_startup_script  = false
      zone                   = "us-central1-f"
      machine_type           = "e2-medium"
      boot_image             = "https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-12-bookworm-v20241009"
      boot_disk_size         = 10
      boot_resource_policies = []
      boot_disk_device_name  = "ca-uow-server"
    }

    newts = {
      enable_startup_script = false
      zone                  = "us-central1-b"
      machine_type          = "e2-standard-2"
      boot_image            = "https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-12-bookworm-v20260513"
      boot_disk_size        = 20
      boot_resource_policies = [
        "https://www.googleapis.com/compute/v1/projects/tidy-outlet-412020/regions/us-central1/resourcePolicies/default-schedule-1"
      ]
      boot_disk_device_name = "newts-ogrre-server"
    }

    staging = {
      enable_startup_script  = false
      zone                   = "us-central1-a"
      machine_type           = "e2-custom-medium-6400"
      boot_image             = "https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-11-bullseye-v20240110"
      boot_disk_size         = 20
      boot_resource_policies = []
      boot_disk_device_name  = "oprhaned-wells-ui-server-v0"
    }
  }
}

module "backend_vms" {
  for_each = local.collaborators

  source = "./modules/backend_vm"

  collaborator = each.key
  zone         = each.value.zone

  machine_type           = each.value.machine_type
  boot_image             = each.value.boot_image
  boot_disk_size         = each.value.boot_disk_size
  boot_disk_device_name  = each.value.boot_disk_device_name
  boot_resource_policies = each.value.boot_resource_policies

  enable_startup_script = each.value.enable_startup_script

  dns_rrdatas_override = (
    var.enable_gke && contains(var.primary_dns_to_gke_backends, each.key)
    ? [google_compute_global_address.gke_backend[each.key].address]
    : null
  )
}

resource "google_compute_firewall" "backend_http_https" {
  name    = "backend-http-https"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }

  # Open to everyone
  source_ranges = ["0.0.0.0/0"]

  target_tags = ["backend"]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_firewall" "backend_ssh" {
  name    = "backend-ssh"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  # Restricted LBL VPN SSH source range
  source_ranges = [
    "128.3.0.0/16",
    "131.243.0.0/16",
    "35.235.240.0/20",
  ]

  target_tags = ["backend"]

  lifecycle {
    prevent_destroy = true
  }
}

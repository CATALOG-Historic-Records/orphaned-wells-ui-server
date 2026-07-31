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

module "backend_vms" {
  for_each = var.legacy_backend_vms

  source = "./modules/backend_vm"

  collaborator = each.key
  zone         = each.value.zone

  machine_type           = each.value.machine_type
  boot_image             = each.value.boot_image
  boot_disk_size         = each.value.boot_disk_size
  boot_disk_device_name  = each.value.boot_disk_device_name
  boot_resource_policies = each.value.boot_resource_policies

  enable_startup_script = each.value.enable_startup_script

  backend_dns_domain = var.backend_dns_domain
  dns_managed_zone   = var.dns_managed_zone
  create_dns_record  = !(var.enable_gke && try(local.gke_backends[each.key].create_primary_dns_record, false))
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
